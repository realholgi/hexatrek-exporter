"""Verified, independently managed photo libraries with recoverable replacement."""

import base64
import copy
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import shutil
from urllib.parse import quote

import requests

from .hexatrek_refresh import (
    FirebaseSession, _identity, _photo_metadata, _read_local_photo, _request,
    _STORAGE_PREFIX,
)
from .photo_quality import can_reuse_photo, compress_photo, normalize_quality
from .workflow import _digest, _file, _inventory, _read, _stage_id, _verify, _write

_OWNER = "hexatrek-photo-library-v1"
_IDENTITY = ("poi_id", "comment_id", "source_image")


def _safe_tree(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Photo library must be a regular directory")
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Photo library must not contain symlinks or special files")


def _manifest(root, stage):
    _safe_tree(root)
    _verify(root)
    manifest = _read(root / "manifest.json")
    if manifest.get("schema_version") != 1 or manifest.get("stage_id") != stage:
        raise ValueError("Photo library stage or schema does not match")
    if manifest.get("source", {}).get("photo_mode") != "files":
        raise ValueError("Photo library must contain files-mode photo metadata")
    if not isinstance(manifest.get("photos"), list):
        raise ValueError("Photo library lacks a photo mapping")
    allowed = {"manifest.json", "source-manifest.json", "run.json"}
    indexed = {}
    for route in manifest.get("routes", []):
        name = route["gpx"]
        if not isinstance(name, str) or not name.startswith("source/"):
            raise ValueError("Library route sources must live in source/")
        _file(root, name)
        allowed.add(name)
    for entry in manifest["photos"]:
        key = tuple(entry.get(field) for field in _IDENTITY)
        if not all(isinstance(value, str) and value for value in key) or key in indexed:
            raise ValueError("Invalid or duplicate photo identity")
        name = entry.get("path", "")
        if not re.fullmatch(r"photos/[0-9a-f]{64}\.(?:jpg|jpeg|png)", name):
            raise ValueError("Photo library requires content-addressed photo files")
        if Path(name).stem != entry.get("sha256"):
            raise ValueError("Photo filename does not match its digest")
        if _digest(_file(root, name)) != entry["sha256"]:
            raise ValueError("Photo digest does not match its bytes")
        allowed.add(name)
        indexed[key] = entry
    if set(_inventory(root)) != allowed:
        raise ValueError("Photo library contains missing or unmanaged files")
    return manifest, indexed


def _sync_directory(path):
    import os

    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_transaction(transaction):
    _safe_tree(transaction)
    shutil.rmtree(transaction)
    _sync_directory(transaction.parent)


def _recover(destination, transaction, stage):
    if not transaction.exists() and not transaction.is_symlink():
        return
    _safe_tree(transaction)
    journal = _read(transaction / "run.json")
    if journal.get("owner") != _OWNER or journal.get("stage") != stage:
        raise ValueError("Refusing an unmanaged photo transaction directory")
    if any(path.name not in {"run.json", "run.json.tmp", "candidate", "previous"}
           for path in transaction.iterdir()):
        raise ValueError("Photo transaction contains unmanaged data")
    previous, candidate = transaction / "previous", transaction / "candidate"
    if previous.exists():
        _manifest(previous, stage)
        if destination.exists() or destination.is_symlink():
            _manifest(destination, stage)
            if _read(destination / "inventory.json") != journal.get("candidate_inventory"):
                raise ValueError("Published photo library does not match its transaction")
        elif candidate.exists():
            try:
                _manifest(candidate, stage)
                if _read(candidate / "inventory.json") != journal.get("candidate_inventory"):
                    raise ValueError("Photo candidate differs from its journal")
            except (ValueError, OSError, KeyError):
                previous.rename(destination)
                _sync_directory(destination.parent)
                _remove_transaction(transaction)
                return
            candidate.rename(destination)
            _sync_directory(destination.parent)
            _manifest(destination, stage)
        else:
            previous.rename(destination)
            _sync_directory(destination.parent)
    elif not destination.exists() and journal.get("candidate_inventory") is not None:
        _manifest(candidate, stage)
        if _read(candidate / "inventory.json") != journal["candidate_inventory"]:
            raise ValueError("Photo candidate differs from its journal")
        candidate.rename(destination)
        _sync_directory(destination.parent)
    _remove_transaction(transaction)


def _references(manifest):
    references = []
    seen = set()
    for poi in manifest["pois"]:
        _identity(poi)
        for comment in poi.get("comments", []):
            comment_id = comment.get("id")
            if not isinstance(comment_id, str) or not comment_id:
                raise ValueError("Photo reference lacks a comment identity")
            for image in comment.get("images", []):
                if (not isinstance(image, str) or not image or image in {".", ".."}
                        or "/" in image or "\\" in image):
                    raise ValueError("Photo reference lacks a safe source image identifier")
                key = (poi["id"], comment_id, image)
                if key in seen:
                    raise ValueError("Duplicate photo reference")
                seen.add(key)
                references.append((key, poi["internal_id"]))
    if not references:
        raise ValueError("The active snapshot has no photo references")
    return references


def _cloud_md5(metadata):
    value = metadata.get("md5Hash")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise ValueError("Cloud photo metadata lacks a valid original MD5") from None
    if len(decoded) != 16 or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("Cloud photo metadata lacks a valid original MD5")
    return value


def _md5(data):
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii")


def _result(destination, manifest, quality, before_bytes, downloaded):
    files = {entry["path"] for entry in manifest["photos"]}
    return {"stage": manifest["stage_id"], "path": str(destination),
            "photo_quality": quality, "references": len(manifest["photos"]),
            "unique_files": len(files), "bytes": sum((destination / name).stat().st_size for name in files),
            "before_bytes": before_bytes, "downloaded": downloaded}


def manage_library(workflow, stage, session_file=None, *, quality="med"):
    """Compact offline or refresh exact snapshot objects, never publishing exports.

    The caller owns the workflow's stage lock. A sibling transaction journal owns
    only candidate/previous directories, allowing interrupted renames to recover.
    """
    quality = normalize_quality(quality)
    stage_id = _stage_id(stage)
    parent = workflow.root / "photos"
    if parent.is_symlink():
        raise ValueError("Photo library parent must not be a symlink")
    destination = parent / f"stage-{stage_id}"
    transaction = parent / f".stage-{stage_id}-photo-transaction"
    if parent.exists():
        _recover(destination, transaction, stage)
    old_manifest, existing = (None, {})
    if destination.exists() or destination.is_symlink():
        old_manifest, existing = _manifest(destination, stage)
    if session_file is None and old_manifest is None:
        raise ValueError("No photo library exists; supply an existing Firebase session for initial download")
    before_bytes = sum((destination / name).stat().st_size
                       for name in {entry["path"] for entry in existing.values()})
    if session_file is None:
        if not existing:
            raise ValueError("Photo library has no photo references")
        if (old_manifest["source"].get("photo_quality") == quality
                and all(entry.get("quality") == quality and can_reuse_photo(entry, quality)
                        for entry in existing.values())):
            return _result(destination, old_manifest, quality, before_bytes, 0)
        manifest = copy.deepcopy(old_manifest)
        source = destination
        references = [(key, None) for key in existing]
    else:
        source = workflow._snapshot(stage, workflow._state(stage))
        _safe_tree(source)
        _verify(source)
        manifest = copy.deepcopy(_read(source / "manifest.json"))
        if manifest.get("schema_version") != 1 or manifest.get("stage_id") != stage:
            raise ValueError("Active snapshot stage or schema does not match")
        references = _references(manifest)
    parent.mkdir(parents=True, exist_ok=True)
    transaction.mkdir()
    journal = {"owner": _OWNER, "stage": stage}
    _write(transaction / "run.json", journal)
    _sync_directory(parent)
    candidate = transaction / "candidate"
    downloaded = 0
    try:
        candidate.mkdir(mode=0o700)
        (candidate / "photos").mkdir(mode=0o700)
        if session_file is None:
            shutil.copyfile(source / "source-manifest.json", candidate / "source-manifest.json")
            for route in manifest.get("routes", []):
                target = candidate / route["gpx"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_file(source, route["gpx"]), target)
        else:
            shutil.copyfile(source / "manifest.json", candidate / "source-manifest.json")
            for route in manifest.get("routes", []):
                original = _file(source, route["gpx"])
                name = "source/" + route["gpx"]
                target = candidate / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, target)
                route["gpx"] = name
        photos = []
        converted = {}
        cloud_objects = {}
        context = requests.Session() if session_file is not None else nullcontext(None)
        with context as http:
            if http is not None:
                http.trust_env = False
                http.auth = None
                credentials = FirebaseSession(session_file, http)
            for key, internal_id in references:
                object_key = (internal_id, key[2])
                if session_file is not None and object_key in cloud_objects:
                    photos.append({**dict(zip(_IDENTITY, key)), **cloud_objects[object_key]})
                    continue
                entry = existing.get(key)
                data = None
                if session_file is None:
                    data = _read_local_photo(destination, entry)
                else:
                    cloud_md5 = _cloud_md5(_photo_metadata(http, credentials, internal_id, key[2]))
                    if entry is not None and can_reuse_photo(entry, quality):
                        local = _read_local_photo(destination, entry)
                        original_md5 = entry.get("source_md5")
                        if original_md5 is None and entry.get("quality") is None:
                            original_md5 = _md5(local)
                        if cloud_md5 == original_md5:
                            data = local
                    if data is None:
                        endpoint = _STORAGE_PREFIX + quote(f"comment/{internal_id}/{key[2]}", safe="")
                        data = _request(http, "GET", endpoint + "?alt=media", credentials, photo=True)
                        if _md5(data) != cloud_md5:
                            raise ValueError("Cloud photo body does not match its original MD5")
                        downloaded += 1
                        entry = None
                conversion_key = (hashlib.sha256(data).hexdigest(), quality,
                                  entry.get("source_sha256") if entry else None)
                if conversion_key not in converted:
                    converted[conversion_key] = compress_photo(data, quality, existing=entry)
                encoded, metadata = converted[conversion_key]
                digest = hashlib.sha256(encoded).hexdigest()
                name = f"photos/{digest}.jpg"
                target = candidate / name
                if not target.exists():
                    target.write_bytes(encoded)
                payload = {**metadata, "path": name, "sha256": digest}
                photos.append({**dict(zip(_IDENTITY, key)), **payload})
                if session_file is not None:
                    cloud_objects[object_key] = payload
        manifest["photos"] = photos
        manifest.setdefault("source", {}).update(photo_mode="files", photo_quality=quality)
        if (session_file is not None and old_manifest == manifest
                and (source / "manifest.json").read_bytes()
                == (destination / "source-manifest.json").read_bytes()):
            _remove_transaction(transaction)
            return _result(destination, old_manifest, quality, before_bytes, downloaded)
        _write(candidate / "manifest.json", manifest)
        record = _read(source / "run.json") if session_file is None else {}
        record.update(schema_version=1, stage=stage, photo_quality=quality,
                      references=len(photos), downloaded=downloaded,
                      mode="cloud" if session_file is not None else "offline")
        files = {photo["path"] for photo in photos}
        record.update(unique_files=len(files), bytes=sum((candidate / name).stat().st_size for name in files),
                      source_manifest_sha256=_digest(candidate / "source-manifest.json"))
        record["acquired_at" if session_file is not None else "compacted_at"] = datetime.now(timezone.utc).isoformat()
        _write(candidate / "run.json", record)
        _write(candidate / "inventory.json", _inventory(candidate))
        _manifest(candidate, stage)
        # Flush payloads and their directory entries before journaling a candidate
        # that recovery is allowed to publish.
        import os

        for path in candidate.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        for path in sorted((path for path in candidate.rglob("*") if path.is_dir()),
                           key=lambda path: len(path.parts), reverse=True):
            _sync_directory(path)
        _sync_directory(candidate)
        journal["candidate_inventory"] = _read(candidate / "inventory.json")
        _write(transaction / "run.json", journal)
        _sync_directory(transaction)
        if destination.exists():
            destination.rename(transaction / "previous")
            _sync_directory(parent)
            _sync_directory(transaction)
        candidate.rename(destination)
        _sync_directory(parent)
        _manifest(destination, stage)
        _remove_transaction(transaction)
    except BaseException:
        previous = transaction / "previous"
        if previous.exists() and not destination.exists():
            previous.rename(destination)
            _sync_directory(parent)
        if transaction.exists() and not previous.exists():
            _remove_transaction(transaction)
        raise
    return _result(destination, manifest, quality, before_bytes, downloaded)
