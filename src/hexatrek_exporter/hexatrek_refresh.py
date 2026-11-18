"""Read-only HexaTrek cloud refresh using an explicitly supplied existing session."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
from datetime import datetime, timezone
from urllib.parse import quote
import zlib

import requests

from .hexatrek_session import FirebaseSession, SessionError
from .photo_quality import can_reuse_photo, compress_photo, normalize_quality

_QUERY_URL = "https://firestore.googleapis.com/v1/projects/hexatrek-44801/databases/(default)/documents:runQuery"
_DOCUMENT_PREFIX = "projects/hexatrek-44801/databases/(default)/documents/Comment/"
_STORAGE_PREFIX = "https://firebasestorage.googleapis.com/v0/b/hexatrek-44801.appspot.com/o/"
_PAGE_SIZE = 200
_MAX_PAGES = 10000
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


class RefreshError(ValueError):
    """Refresh failed without publishing a new export or disclosing credentials."""


def _read_token(path: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise RefreshError("Token must be a private user-owned regular file (mode 0600)")
            data = source.read(16385)
        if len(data) > 16384:
            raise RefreshError("Token file exceeds 16 KiB")
        token = data.decode("ascii").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise RefreshError("Token file must contain only an existing Firebase ID token")
        return token
    except (OSError, UnicodeError):
        raise RefreshError("Cannot read a private existing-session token file") from None


def _request(session, method: str, url: str, token: str | FirebaseSession, *, payload=None, photo=False) -> bytes:
    # URLs are constructed here, never taken from a manifest, comment or response.
    if (photo and not url.startswith(_STORAGE_PREFIX)) or (not photo and url != _QUERY_URL):
        raise RefreshError("Unapproved cloud endpoint")
    try:
        current_token = token if isinstance(token, str) else token.token()
        with session.request(
            method, url, json=payload,
            headers={"Authorization": ("Firebase " if photo else "Bearer ") + current_token},
            allow_redirects=False, timeout=(15, 60), stream=True,
        ) as response:
            if response.status_code != 200:
                raise RefreshError(f"Cloud {'photo' if photo else 'comments'} request failed (HTTP {response.status_code})")
            limit = _MAX_IMAGE_BYTES if photo else _MAX_JSON_BYTES
            chunks = bytearray()
            for chunk in response.iter_content(65536):
                if len(chunks) + len(chunk) > limit:
                    raise RefreshError("Cloud response exceeds the permitted size")
                chunks.extend(chunk)
            return bytes(chunks)
    except SessionError:
        raise RefreshError("Cannot renew the existing Firebase session; no new export was published") from None
    except requests.RequestException:
        raise RefreshError("Cloud request failed; no new export was published") from None


def _photo_metadata(session, token: str | FirebaseSession, internal_id: int, image: str) -> dict:
    """Read metadata bound to the exact requested cloud object."""
    object_name = f"comment/{internal_id}/{image}"
    endpoint = _STORAGE_PREFIX + quote(object_name, safe="")
    try:
        metadata = json.loads(_request(session, "GET", endpoint, token, photo=True))
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, RefreshError):
            raise
        raise RefreshError("Invalid cloud photo metadata") from None
    if not isinstance(metadata, dict) or metadata.get("bucket") != "hexatrek-44801.appspot.com" or metadata.get("name") != object_name:
        raise RefreshError("Cloud photo metadata does not match the exact requested object")
    return metadata


def _photo_url(session, token: str | FirebaseSession, internal_id: int, image: str) -> str:
    """Resolve an exact cloud object to its existing public download-token URL."""
    metadata = _photo_metadata(session, token, internal_id, image)
    endpoint = _STORAGE_PREFIX + quote(f"comment/{internal_id}/{image}", safe="")
    tokens = metadata.get("downloadTokens")
    first_token = tokens.split(",", 1)[0] if isinstance(tokens, str) else ""
    if not first_token.strip():
        raise RefreshError("Cloud photo metadata has no download token")
    return endpoint + "?alt=media&token=" + quote(first_token, safe="")


def _value(value):
    if not isinstance(value, dict) or len(value) != 1:
        raise RefreshError("Malformed Firestore field")
    kind, item = next(iter(value.items()))
    if kind in {"stringValue", "timestampValue", "referenceValue", "bytesValue"} and isinstance(item, str):
        return item
    if kind == "integerValue" and isinstance(item, (str, int)) and not isinstance(item, bool):
        try:
            return int(item)
        except ValueError:
            pass
    if kind == "doubleValue" and isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item):
        return item
    if kind == "booleanValue" and isinstance(item, bool):
        return item
    if kind == "nullValue" and item is None:
        return None
    if kind == "mapValue" and isinstance(item, dict):
        fields = item.get("fields", {})
        if isinstance(fields, dict):
            return {key: _value(field) for key, field in fields.items()}
    if kind == "arrayValue" and isinstance(item, dict):
        values = item.get("values", [])
        if isinstance(values, list):
            return [_value(field) for field in values]
    if kind == "geoPointValue" and isinstance(item, dict) and set(item) == {"latitude", "longitude"}:
        return item
    raise RefreshError("Unsupported or malformed Firestore field")


def _timestamp(value):
    if not isinstance(value, str):
        raise RefreshError("Comment lacks a valid createdAt timestamp")
    # Firestore timestamps can have nanoseconds; retain those digits for ordering.
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z", value)
    if not match:
        raise RefreshError("Comment lacks a valid UTC createdAt timestamp")
    try:
        datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise RefreshError("Comment has an invalid createdAt timestamp") from None
    return match[1], (match[2] or "").ljust(9, "0")


def _identity(poi):
    try:
        identity = (poi["stage_id"], poi["group_id"], poi["internal_id"])
    except (KeyError, TypeError):
        raise RefreshError("POI is missing its complete source identity") from None
    if any(not isinstance(value, int) or isinstance(value, bool) for value in identity):
        raise RefreshError("POI source identities must be integers")
    if poi.get("id") != ":".join(map(str, identity)):
        raise RefreshError("POI ID does not match its source identity")
    return identity


def _comments(session, token, poi):
    identity = _identity(poi)
    internal_id = identity[2]
    identity_field = "trekStageInterestPointInternalId"
    query = {
        "from": [{"collectionId": "Comment"}],
        "where": {"fieldFilter": {
            "field": {"fieldPath": identity_field}, "op": "EQUAL", "value": {"integerValue": str(internal_id)},
        }},
        "orderBy": [{"field": {"fieldPath": key}, "direction": "DESCENDING"} for key in ("createdAt", "__name__")],
        "limit": _PAGE_SIZE,
    }
    comments, seen = [], set()
    previous = None
    read_time = None
    for _ in range(_MAX_PAGES):
        try:
            payload = {"structuredQuery": query}
            if read_time is not None:
                payload["readTime"] = read_time
            rows = json.loads(_request(session, "POST", _QUERY_URL, token, payload=payload))
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, RefreshError):
                raise
            raise RefreshError("Invalid cloud comments response") from None
        if not isinstance(rows, list):
            raise RefreshError("Invalid cloud comments response")
        documents = []
        for row in rows:
            if not isinstance(row, dict) or "error" in row or "skippedResults" in row:
                raise RefreshError("Incomplete or invalid cloud comments response")
            if "document" in row:
                documents.append(row["document"])
            elif set(row) - {"readTime", "transaction", "done"}:
                raise RefreshError("Unrecognized cloud comments response")
        if rows:
            page_time = rows[-1].get("readTime")
            _timestamp(page_time)
            if read_time is None:
                read_time = page_time
            elif page_time != read_time:
                raise RefreshError("Cloud comments snapshot changed during pagination")
        if len(documents) > _PAGE_SIZE:
            raise RefreshError("Cloud comments page exceeds its query limit")
        for doc in documents:
            if not isinstance(doc, dict):
                raise RefreshError("Invalid cloud comment document")
            name = doc.get("name", "")
            if not isinstance(name, str) or not name.startswith(_DOCUMENT_PREFIX) or not name[len(_DOCUMENT_PREFIX):] or "/" in name[len(_DOCUMENT_PREFIX):]:
                raise RefreshError("Cloud comment belongs to an unexpected collection")
            fields = doc.get("fields")
            if not isinstance(fields, dict):
                raise RefreshError("Cloud comment fields are missing")
            raw = {key: _value(value) for key, value in fields.items()}
            if isinstance(raw.get(identity_field), bool) or raw.get(identity_field) != internal_id:
                raise RefreshError("Cloud comment does not match the exact POI internal ID")
            created = raw.get("createdAt")
            order = (*_timestamp(created), name)
            if name in seen or (previous is not None and order >= previous):
                raise RefreshError("Cloud comment pagination is duplicated or out of order")
            seen.add(name)
            previous = order
            user = raw.get("user", {})
            images = raw.get("images", [])
            text = raw.get("text", "")
            rating = raw.get("rating")
            if not isinstance(user, dict) or not isinstance(user.get("name", ""), str) or not isinstance(text, str):
                raise RefreshError("Malformed cloud comment author or text")
            if not isinstance(images, list) or any(not isinstance(image, str) or not image or image in {".", ".."} or "/" in image or "\\" in image or any(ord(char) < 32 for char in image) for image in images):
                raise RefreshError("Invalid cloud comment image basename")
            if rating is not None and (not isinstance(rating, (int, float)) or isinstance(rating, bool) or not math.isfinite(rating)):
                raise RefreshError("Invalid cloud comment rating")
            comments.append({"id": name[len(_DOCUMENT_PREFIX):], "created_at": created, "author": user.get("name", ""), "rating": f"{rating:g}/5" if rating is not None else "", "text": text, "images": images, "raw": raw})
        if len(documents) < _PAGE_SIZE:
            return comments
        last = documents[-1]
        query["startAt"] = {"values": [last["fields"]["createdAt"], {"referenceValue": last["name"]}], "before": False}
    raise RefreshError("Cloud comments pagination limit reached; coverage is incomplete")


def _image_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        offset, kinds = 8, []
        while offset + 12 <= len(data):
            size = struct.unpack_from(">I", data, offset)[0]
            end = offset + 12 + size
            if end > len(data):
                break
            kind = data[offset + 4:offset + 8]
            body = data[offset + 8:offset + 8 + size]
            if zlib.crc32(kind + body) & 0xffffffff != struct.unpack_from(">I", data, end - 4)[0]:
                break
            if not kinds:
                if kind != b"IHDR" or size != 13:
                    break
                width, height = struct.unpack_from(">II", body)
                if not _dimensions(width, height):
                    break
            kinds.append(kind)
            offset = end
            if kind == b"IEND":
                if size == 0 and offset == len(data) and b"IDAT" in kinds:
                    return ".png"
                break
    elif data.startswith(b"\xff\xd8"):
        offset, frame, scan, multipicture = 2, False, False, False
        while offset < len(data):
            if data[offset] != 255:
                if scan:
                    offset += 1
                    continue
                break
            while offset < len(data) and data[offset] == 255:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if scan and (marker == 0 or 0xd0 <= marker <= 0xd7):
                continue
            if marker == 0xd9:
                if frame and scan:
                    if offset == len(data):
                        return ".jpg"
                    # MPF JPEGs can append another complete image (e.g. an HDR gain map).
                    if multipicture and data[offset:offset + 2] == b"\xff\xd8":
                        offset += 2
                        frame = scan = False
                        continue
                break
            if offset + 2 > len(data):
                break
            size = struct.unpack_from(">H", data, offset)[0]
            if size < 2 or offset + size > len(data):
                break
            if marker == 0xe2 and data[offset + 2:offset + 6] == b"MPF\0":
                multipicture = True
            if marker in {0xc0, 0xc1, 0xc2}:
                if size < 8:
                    break
                height, width = struct.unpack_from(">HH", data, offset + 3)
                if not _dimensions(width, height):
                    break
                frame = True
            if marker == 0xda:
                scan = True
            offset += size
    raise RefreshError("Cloud photo is not a complete supported JPEG or PNG")


def _dimensions(width, height):
    return 0 < width <= 20000 and 0 < height <= 20000 and width * height <= 40000000


def _local_photos(manifest):
    photos = manifest.get("photos", [])
    if not isinstance(photos, list):
        raise RefreshError("Invalid source photo metadata")
    indexed = {}
    for photo in photos:
        if not isinstance(photo, dict):
            raise RefreshError("Invalid source photo metadata")
        if "path" not in photo:
            continue
        identity = tuple(photo.get(key) for key in ("poi_id", "comment_id", "source_image"))
        if not all(isinstance(value, str) and value for value in identity) or identity in indexed:
            raise RefreshError("Invalid or duplicate source photo identity")
        indexed[identity] = photo
    return indexed


def _read_local_photo(root: Path, photo) -> bytes:
    relative = photo.get("path")
    digest = photo.get("sha256")
    if (not isinstance(relative, str) or "\\" in relative
            or any(part in ("", ".", "..") for part in relative.split("/"))
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
        raise RefreshError("Invalid source photo path or digest")
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = relative.split("/")
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_IMAGE_BYTES:
                raise RefreshError("Invalid source photo file")
            data = stream.read(_MAX_IMAGE_BYTES + 1)
    except (OSError, ValueError):
        raise RefreshError("Cannot safely read the source photo") from None
    if len(data) > _MAX_IMAGE_BYTES or hashlib.sha256(data).hexdigest() != digest:
        raise RefreshError("Source photo digest does not match its bytes")
    return data


def refresh_pois(manifest_path: str, output_directory: str, token_file: str | None = None, include_photos: bool = True, *, session_file: str | None = None, photo_mode: str = "links", photo_quality: str = "med", photo_cache_path: str | None = None) -> dict:
    """Fetch every POI's comments, optionally exact-bound photos, then publish a new export.

    Supply exactly one private raw ID-token file or persisted Firebase session.
    Persisted sessions renew their ID token before requests when needed.
    Photos default to hosted links from exact-bound metadata; files mode reuses
    verified eligible photos and caches privacy-safe JPEGs at the selected quality.
    Any cloud, parsing or rendering failure leaves the source and destination alone.
    """
    if (token_file is None) == (session_file is None):
        raise RefreshError("Supply exactly one of token_file or session_file")
    if not isinstance(include_photos, bool):
        raise RefreshError("include_photos must be a boolean")
    if photo_mode not in ("links", "files"):
        raise RefreshError("photo_mode must be 'links' or 'files'")
    if include_photos and photo_mode == "files":
        try:
            photo_quality = normalize_quality(photo_quality)
        except ValueError as exc:
            raise RefreshError(str(exc)) from None
    output = Path(output_directory).absolute()
    if output.exists() or output.is_symlink():
        raise RefreshError("Output directory must not already exist")
    if not output.parent.is_dir():
        raise RefreshError("Output parent directory must already exist")
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise RefreshError("Cannot read the source manifest") from None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("pois"), list) or not isinstance(manifest.get("source"), dict):
        raise RefreshError("Unsupported source manifest")
    manifest = copy.deepcopy(manifest)
    identities = [_identity(poi) for poi in manifest["pois"]]
    if len(set(identities)) != len(identities):
        raise RefreshError("Duplicate POI identities in source manifest")
    local_photos = _local_photos(manifest) if include_photos and photo_mode == "files" else {}
    source_root = Path(manifest_path).absolute().parent
    cached_photos = {}
    cache_root = source_root
    if include_photos and photo_mode == "files" and photo_cache_path is not None:
        from .workflow import _verify

        cache_path = Path(photo_cache_path).absolute()
        cache_root = cache_path.parent
        try:
            _verify(cache_root)
            cached_manifest = json.loads(cache_path.read_text(encoding="utf-8"))
            if (not isinstance(cached_manifest, dict) or cached_manifest.get("schema_version") != 1
                    or cached_manifest.get("stage_id") != manifest.get("stage_id")
                    or not isinstance(cached_manifest.get("source"), dict)
                    or cached_manifest["source"].get("photo_mode") != "files"):
                raise ValueError("Invalid source photo cache manifest")
            cached_photos = _local_photos(cached_manifest)
            for cached in cached_photos.values():
                _image_extension(_read_local_photo(cache_root, cached))
                can_reuse_photo(cached, photo_quality)
        except (OSError, ValueError, UnicodeError):
            raise RefreshError("Cannot safely reuse the source photo cache") from None
    token = _read_token(token_file) if token_file is not None else None
    # Import at call time: exporter and refresher share the sole GPX renderer.
    from .hexatrek_export import write_manifest_export
    staging = Path(tempfile.mkdtemp(prefix=".hexatrek-refresh-", dir=output.parent))
    try:
        media = staging / "photos"
        if include_photos and photo_mode == "files":
            media.mkdir()
        photos = []
        with requests.Session() as session:
            session.trust_env = False  # no proxies, netrc or ambient credentials
            session.auth = None
            credentials = FirebaseSession(session_file, session) if session_file is not None else token
            assert credentials is not None
            for poi in manifest["pois"]:
                poi["comments"] = _comments(session, credentials, poi)
                if include_photos:
                    for comment in poi["comments"]:
                        for image in comment["images"]:
                            if photo_mode == "links":
                                url = _photo_url(session, credentials, poi["internal_id"], image)
                                photos.append({"poi_id": poi["id"], "comment_id": comment["id"], "source_image": image, "url": url})
                                continue
                            identity = (poi["id"], comment["id"], image)
                            choices = [(cached_photos.get(identity), cache_root),
                                       (local_photos.get(identity), source_root)]
                            choices = [(photo, root) for photo, root in choices if photo is not None]
                            choices.sort(key=lambda item: item[0].get("quality") != photo_quality)
                            data = None
                            reused = None
                            verified = []
                            for existing, existing_root in choices:
                                local_data = _read_local_photo(existing_root, existing)
                                _image_extension(local_data)
                                md5 = existing.get("source_md5")
                                if md5 is None:
                                    md5 = base64.b64encode(hashlib.md5(local_data, usedforsecurity=False).digest()).decode("ascii")
                                try:
                                    eligible = can_reuse_photo(existing, photo_quality)
                                except ValueError as exc:
                                    raise RefreshError(str(exc)) from None
                                verified.append((local_data, existing, md5, eligible))
                            if verified:
                                metadata = _photo_metadata(session, credentials, poi["internal_id"], image)
                                for local_data, existing, md5, eligible in verified:
                                    if metadata.get("md5Hash") == md5 and eligible:
                                        data = local_data
                                        reused = existing
                                        break
                            if data is None:
                                object_name = f"comment/{poi['internal_id']}/{image}"
                                url = _STORAGE_PREFIX + quote(object_name, safe="") + "?alt=media"
                                data = _request(session, "GET", url, credentials, photo=True)
                            _image_extension(data)
                            try:
                                data, encoding = compress_photo(data, photo_quality, existing=reused)
                            except ValueError as exc:
                                raise RefreshError(str(exc)) from None
                            digest = hashlib.sha256(data).hexdigest()
                            filename = digest + ".jpg"
                            target = media / filename
                            if not target.exists():
                                target.write_bytes(data)
                            photos.append({"poi_id": poi["id"], "comment_id": comment["id"], "source_image": image, "path": "photos/" + filename, "sha256": digest, **encoding})
        manifest["photos"] = photos
        retrieved = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        manifest["source"]["comments_coverage"] = "cloud-complete"
        manifest["source"]["comments_retrieved_at"] = retrieved
        manifest["source"]["photos_coverage"] = "cloud-complete" if include_photos else "not-requested"
        manifest["source"]["photo_mode"] = photo_mode
        manifest["source"].pop("photo_quality", None)
        if include_photos and photo_mode == "files":
            manifest["source"]["photo_quality"] = photo_quality
        manifest["exported_at"] = retrieved
        rendered = staging / "export"
        result = write_manifest_export(manifest, str(rendered))
        if photos and photo_mode == "files":
            media.rename(rendered / "photos")
        if output.exists() or output.is_symlink():
            raise RefreshError("Output directory appeared during refresh")
        rendered.rename(output)
        return {**result, "manifest_path": str(output / "manifest.json"), "poi_count": len(manifest["pois"]), "comment_count": sum(len(poi["comments"]) for poi in manifest["pois"]), "photo_count": len(photos)}
    finally:
        shutil.rmtree(staging)
