"""Local immutable stage snapshots and resumable, readably named exports."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import xml.etree.ElementTree as ET

from .hexatrek_export import export_stage
from .hexatrek_refresh import refresh_pois
from .osmand_export import export_osmand
from .naming import export_filenames, export_name, preparation_name, prepared_filenames, snapshot_name

RECIPE_VERSION = 1


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def _inventory(root):
    files = {}
    for path in sorted(Path(root).rglob("*")):
        if path.name == ".DS_Store":
            continue
        if path.is_symlink():
            raise ValueError("Data bundles must not contain symlinks")
        if path.is_file() and path.relative_to(root).as_posix() != "inventory.json":
            files[path.relative_to(root).as_posix()] = _digest(path)
    return files


def _verify(root, expected=None):
    if expected is None:
        expected = {name: digest for name, digest in _read(Path(root) / "inventory.json").items()
                    if Path(name).name != ".DS_Store"}
        actual = _inventory(root)
    else:
        actual = _public_inventory(root)
    if actual != expected:
        raise ValueError(f"Stored data changed: {root}; restore it before reuse")


def _public_inventory(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Stored data changed: {root}; expected a public GPX directory")
    files = {}
    for path in sorted(root.iterdir()):
        if path.name == ".DS_Store" and path.is_file() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file() or path.suffix != ".gpx":
            raise ValueError(f"Stored data changed: {root}; only flat GPX files are permitted")
        files[path.name] = _digest(path)
    return files


def _file(root, name):
    base = Path(root).resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise ValueError("Referenced file must exist within its data bundle")
    return path


def _stage_id(stage):
    if type(stage) is not int or stage < 1:
        raise ValueError("Stage must be a positive integer")
    return f"{stage:02d}"


def _prepared_summaries(directory):
    from .poi_details import GPX
    import xml.etree.ElementTree as ET

    summaries = {}
    for route in _read(Path(directory) / "prepared.json")["routes"]:
        texts = {point.findtext(f"{{{GPX}}}name"): point.findtext(f"{{{GPX}}}desc", default="")
                 for point in ET.parse(_file(directory, route["gpx"])).findall(f"{{{GPX}}}wpt")}
        summaries.update({identity: texts[alias] for identity, alias in route["aliases"].items()})
    return summaries


class Workflow:
    """Own paths, provenance and local retries for exporter artifacts.

    Construction and status are read-only. Mutations serialize per stage using
    an exclusive lock; an interrupted process leaves that lock for inspection.
    """

    def __init__(self, data_root):
        self.root = Path(data_root).expanduser().resolve()
        self.output_root = self.root.parent / "exports"

    def _directory(self, stage):
        return self.root / "stages" / _stage_id(stage)

    def _state(self, stage):
        path = self._directory(stage) / "stage.json"
        return _read(path) if path.exists() else {"schema_version": 1, "stage": stage}

    def _storage(self, stage):
        path = self._directory(stage) / "storage.json"
        return _read(path) if path.exists() else {
            "snapshots": {}, "preparations": {}, "exports": {}}

    def _artifact(self, stage, kind, identity):
        """Resolve a technical identity exclusively through the storage index."""
        name = self._storage(stage).get(kind, {}).get(identity)
        if name is None:
            raise ValueError(f"No stored {kind} mapping for {identity}; migrate the stage storage first")
        if not name or Path(name).name != name or name in (".", ".."):
            raise ValueError("Stored artifact directory must be a single descriptive name")
        return self._directory(stage) / kind / name

    @staticmethod
    def _available(path, reserved=()):
        candidate = path
        number = 2
        while candidate.exists() or candidate.name in reserved:
            candidate = path.with_name(f"{path.stem}-{number:02d}{path.suffix}")
            number += 1
        return candidate

    def _destination(self, stage, kind, identity, label):
        """Reuse an indexed identity or reserve a collision-free readable name."""
        storage = self._storage(stage)
        if identity in storage.get(kind, {}):
            return self._artifact(stage, kind, identity)
        if not label or Path(label).name != label or label in (".", ".."):
            raise ValueError("Artifact label must be a single descriptive name")
        return self._available(self._directory(stage) / kind / label, storage.get(kind, {}).values())

    def _register(self, stage, kind, identity, path):
        """Atomically index an artifact after its successful final rename."""
        path = Path(path)
        if path.parent != self._directory(stage) / kind or not path.is_dir():
            raise ValueError("Artifact must exist in its stage storage directory")
        storage = self._storage(stage)
        storage.setdefault(kind, {})
        previous = storage[kind].get(identity)
        if previous is not None and previous != path.name:
            raise ValueError("Artifact identity already has a different directory")
        if any(key != identity and name == path.name for key, name in storage[kind].items()):
            raise ValueError("Artifact directory already belongs to another identity")
        storage[kind][identity] = path.name
        _write(self._directory(stage) / "storage.json", storage)

    def _save(self, stage, state):
        _write(self._directory(stage) / "stage.json", state)

    @contextmanager
    def _lock(self, stage):
        directory = self._directory(stage)
        directory.mkdir(parents=True, exist_ok=True)
        lock = directory / ".lock"
        try:
            with lock.open("x") as stream:
                stream.write(str(os.getpid()))
        except FileExistsError:
            raise ValueError(f"Stage {stage} is locked; inspect {lock} before removing it") from None
        try:
            yield
        finally:
            lock.unlink()

    @contextmanager
    def _temporary(self):
        directory = self.root / "cache"
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="workflow-", dir=directory) as name:
            yield Path(name)

    def status(self, stages=None):
        if stages is None:
            directory = self.root / "stages"
            stages = sorted(int(p.name) for p in directory.iterdir()
                            if p.is_dir() and p.name.isdigit()) if directory.exists() else []
        rows = []
        for stage in stages:
            state = self._state(stage)
            row = dict(state)
            if state.get("snapshot"):
                manifest = _read(self._snapshot(stage, state) / "manifest.json")
                row.update(comments_coverage=manifest["source"].get("comments_coverage"),
                           comments_retrieved_at=manifest["source"].get("comments_retrieved_at"),
                           pois=len(manifest["pois"]), routes=len(manifest["routes"]))
            for key, kind in (("snapshot", "snapshots"), ("preparation", "preparations"), ("export", "exports")):
                if state.get(key):
                    row[key + "_path"] = str(self._artifact(stage, kind, state[key]))
            if state.get("preparation"):
                from .summaries import normalize_language

                prepared = _read(Path(row["preparation_path"]) / "prepared.json")
                row["summary_language"] = normalize_language(prepared.get("summary_language", "de"))
            if state.get("export"):
                record_path = self._artifact(stage, "exports", state["export"]) / "run.json"
                row["export_record_path"] = str(record_path)
                row["export_path"] = str(self.output_root)
                record = _read(record_path)
                row["outputs"] = {
                    target: path for target, path in record["completed"].items()
                    if state.get("visible_exports", {}).get(target) == state["export"]}
            pending_updates = []
            for identity in self._storage(stage).get("updates", {}):
                directory = self._artifact(stage, "updates", identity)
                update = _read(directory / "run.json")
                if update["status"] not in ("completed", "unchanged"):
                    pending_updates.append({"update_id": identity, "status": update["status"],
                                            "record_path": str(directory / "run.json")})
            if pending_updates:
                row["pending_updates"] = pending_updates
            rows.append(row)
        return {"data_root": str(self.root), "exports_root": str(self.output_root), "stages": rows}

    def write_summaries(self, stage, output_path, *, language="de", generate=False,
                        model=None, api_base=None):
        """Write source-bound drafts or explicitly generated, language-bound text."""
        from .summaries import normalize_language, summary_document, text_issues

        language = normalize_language(language)
        self._snapshot(stage, self._state(stage))
        destination = Path(output_path).expanduser().absolute()
        resolved = destination.resolve()
        if resolved.is_relative_to(self.root / "stages") or resolved.is_relative_to(self.output_root):
            raise ValueError("Summary input must stay outside managed stages and public exports")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"Summary output already exists: {destination}")
        with self._lock(stage):
            state = self._state(stage)
            snapshot = self._snapshot(stage, state)
            _verify(snapshot)
            document = summary_document(snapshot / "manifest.json", language=language)
            if generate:
                cached = None
                if state.get("preparation"):
                    active = self._prepared(stage, state)
                    active_language = normalize_language(_read(active / "prepared.json").get("summary_language", "de"))
                    if active_language == language:
                        cached = active
                if cached is None:
                    for include_photos in (False, True):
                        recipe_path = self._recipe_path(stage, snapshot, include_photos, language)
                        if not recipe_path.exists():
                            continue
                        recipe = _read(recipe_path)
                        if (recipe.get("snapshot") == state["snapshot"] and
                                recipe.get("version") == RECIPE_VERSION and
                                normalize_language(recipe.get("summary_language", "de")) == language):
                            candidate = self._prepared(stage, {**state, "preparation": recipe["preparation"]})
                            if normalize_language(_read(candidate / "prepared.json").get("summary_language", "de")) != language:
                                raise ValueError("Cached preparation summary language does not match")
                            cached = candidate
                            break
                if cached is not None:
                    for identity, text in _prepared_summaries(cached).items():
                        if identity in document["summaries"] and not text_issues(identity, text):
                            document["summaries"][identity] = text
                from .summary_generation import generate_summaries

                document = generate_summaries(
                    snapshot / "manifest.json", document, api_base=api_base, model=model,
                    api_key=os.environ.get("HEXATREK_API_KEY"))
            payload = (json.dumps(document, indent=2, allow_nan=False) + "\n").encode("utf-8")
            _verify(snapshot)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(payload)
            texts = document["summaries"]
            return {"stage": stage, "summaries_path": str(destination),
                    "pois": len(texts), "missing": sum(text is None for text in texts.values()),
                    "summary_language": language}

    def _snapshot(self, stage, state):
        if not state.get("snapshot"):
            raise ValueError(f"Stage {stage} has no snapshot; collect or adopt one first")
        return self._artifact(stage, "snapshots", state["snapshot"])

    def _store_snapshot(self, manifest_path):
        path = Path(manifest_path).expanduser().resolve()
        manifest = _read(path)
        if manifest.get("schema_version") != 1:
            raise ValueError("Expected a HexaTrek manifest version 1")
        stage = manifest["stage_id"]
        _stage_id(stage)
        for route in manifest["routes"]:
            _file(path.parent, route["gpx"])
        for photo in manifest.get("photos", []):
            if "path" in photo:
                _file(path.parent, photo["path"])
        if path.name != "manifest.json":
            raise ValueError("Snapshot source must be named manifest.json")
        inventory = _inventory(path.parent)
        snapshot_id = _identity(inventory)
        destination = self._destination(stage, "snapshots", snapshot_id, snapshot_name(manifest))
        if destination.is_relative_to(path.parent) and destination != path.parent:
            raise ValueError("Snapshot output cannot be inside its source bundle")
        if destination.exists():
            _verify(destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with self._temporary() as temporary:
                copied = temporary / "snapshot"
                shutil.copytree(path.parent, copied)
                if _inventory(copied) != inventory or _inventory(path.parent) != inventory:
                    raise ValueError("Source bundle changed while copying")
                _write(copied / "inventory.json", inventory)
                copied.rename(destination)
                self._register(stage, "snapshots", snapshot_id, destination)
        return {"stage": stage, "snapshot": snapshot_id, "manifest_path": str(destination / "manifest.json")}

    def _adopt(self, manifest_path):
        result = self._store_snapshot(manifest_path)
        stage, snapshot_id = result["stage"], result["snapshot"]
        state = self._state(stage)
        if state.get("snapshot") != snapshot_id:
            state.pop("preparation", None)
            state.pop("export", None)
        state["snapshot"] = snapshot_id
        self._save(stage, state)
        return result

    def adopt_snapshot(self, manifest_path):
        stage = _read(Path(manifest_path).expanduser())["stage_id"]
        with self._lock(stage):
            return self._adopt(manifest_path)

    def collect(self, stage, cache_path):
        with self._lock(stage), self._temporary() as temporary:
            result = export_stage(cache_path, stage, str(temporary / "source"))
            return self._adopt(result["manifest_path"])

    def refresh(self, stage, session_file, photo_mode="none", *, photo_quality="med"):
        if photo_mode not in ("none", "files", "links"):
            raise ValueError("Photo mode must be none, files or links")
        with self._lock(stage), self._temporary() as temporary:
            snapshot = self._snapshot(stage, self._state(stage))
            _verify(snapshot)
            result = refresh_pois(str(snapshot / "manifest.json"), str(temporary / "source"),
                                  session_file=session_file, include_photos=photo_mode != "none",
                                  photo_mode="files" if photo_mode == "files" else "links",
                                  photo_quality=photo_quality,
                                  photo_cache_path=self._photo_cache(stage, photo_mode))
            refreshed = _read(result["manifest_path"])
            for poi in refreshed["pois"]:
                poi.pop("summary", None)
            _write(result["manifest_path"], refreshed)
            return self._adopt(result["manifest_path"])

    def _photo_cache(self, stage, photo_mode):
        parent = self.root / "photos"
        directory = parent / f"stage-{stage:02d}"
        transaction = parent / f".stage-{stage:02d}-photo-transaction"
        if photo_mode != "files":
            return None
        if transaction.exists() or transaction.is_symlink():
            raise ValueError(f"Photo library update is interrupted; run photos --stage {stage} first")
        path = directory / "manifest.json"
        if parent.is_symlink() or directory.is_symlink() or path.is_symlink():
            raise ValueError("Photo library paths must not be symlinks")
        if directory.exists() and not path.is_file():
            raise ValueError("Existing photo library has no manifest; restore it before reuse")
        return str(path) if path.is_file() else None

    def photos(self, stage, session_file=None, *, photo_quality="med"):
        """Download or compact the separate private photo library."""
        from .photo_library import manage_library

        with self._lock(stage):
            return manage_library(self, stage, session_file, quality=photo_quality)

    def update(self, stage, session_file=None, photo_mode=None, targets=None, *,
               language=None, generate=False, model=None, api_base=None,
               summaries_file=None, resume=None, photo_quality=None):
        """Refresh an exported stage, then review or publish a recoverable candidate."""
        from .updates import update_stage

        with self._lock(stage):
            return update_stage(
                self, stage, session_file=session_file, photo_mode=photo_mode, targets=targets,
                language=language, generate=generate, model=model, api_base=api_base,
                summaries_file=summaries_file, resume=resume, photo_quality=photo_quality)

    def _store_preparation(self, stage, prepared_path):
        path = Path(prepared_path).expanduser().resolve()
        prepared = _read(path)
        if prepared.get("schema_version") != 1:
            raise ValueError("Expected a prepared manifest version 1")
        from .summaries import normalize_language

        normalize_language(prepared.get("summary_language", "de"))
        for route in prepared["routes"]:
            _file(path.parent, route["gpx"])
            _file(path.parent, route["details"])
        inventory = _inventory(path.parent)
        preparation_id = _identity(inventory)
        naming_routes = [dict(route) for route in prepared["routes"]]
        destination = self._destination(
            stage, "preparations", preparation_id,
            preparation_name(stage, prepared, datetime.now(timezone.utc).isoformat()))
        if destination.is_relative_to(path.parent):
            raise ValueError("Preparation output cannot be inside its source")
        if destination.exists():
            _verify(destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with self._temporary() as temporary:
                copied = temporary / "prepared"
                if any("kind" not in route for route in naming_routes):
                    source_path = Path(prepared.get("source_manifest", "")).expanduser()
                    if not source_path.is_file():
                        source_path = self._snapshot(stage, self._state(stage)) / "manifest.json"
                    if _digest(source_path) != prepared.get("source_manifest_sha256"):
                        raise ValueError("Preparation naming requires its matching source manifest")
                    source_routes = {str(route["id"]): route for route in _read(source_path)["routes"]}
                    for route in naming_routes:
                        source_id = str(route.get("source_route_id") or str(route["id"]).split("-part-")[0])
                        if "kind" not in route:
                            route["kind"] = source_routes[source_id]["kind"]
                shutil.copytree(path.parent, copied)
                if _inventory(copied) != inventory or _inventory(path.parent) != inventory:
                    raise ValueError("Preparation changed while copying")
                names = prepared_filenames(stage, naming_routes)
                moves = []
                moved_sources = {}
                updated = {**prepared, "routes": [dict(route) for route in prepared["routes"]]}
                for route in updated["routes"]:
                    for key in ("gpx", "details"):
                        old, new = route[key], names[route["id"]][key]
                        if old != new:
                            if old in moved_sources:
                                if moved_sources[old] != new:
                                    raise ValueError("Shared preparation asset has conflicting readable names")
                            else:
                                staged = temporary / f"asset-{len(moves)}"
                                _file(copied, old).rename(staged)
                                moves.append((staged, copied / new))
                                moved_sources[old] = new
                            route[key] = new
                for staged, target in moves:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        raise ValueError("Readable preparation filename collides with another source file")
                    staged.rename(target)
                for directory in sorted((item for item in copied.rglob("*") if item.is_dir()),
                                        key=lambda item: len(item.parts), reverse=True):
                    if not any(directory.iterdir()):
                        directory.rmdir()
                if moves:
                    _write(copied / "prepared.json", updated)
                _write(copied / "inventory.json", _inventory(copied))
                copied.rename(destination)
                self._register(stage, "preparations", preparation_id, destination)
        return preparation_id, destination

    def adopt_preparation(self, stage, prepared_path):
        with self._lock(stage):
            state = self._state(stage)
            snapshot = self._snapshot(stage, state)
            _verify(snapshot)
            prepared = _read(Path(prepared_path).expanduser())
            if prepared.get("source_manifest_sha256") != _digest(snapshot / "manifest.json"):
                raise ValueError("Preparation does not match the active source snapshot")
            identity, destination = self._store_preparation(stage, prepared_path)
            state["preparation"] = identity
            self._save(stage, state)
            return {"stage": stage, "preparation": identity, "prepared_path": str(destination / "prepared.json")}

    def _recipe_path(self, stage, snapshot, include_photos, language):
        photo_label = "mit" if include_photos else "ohne"
        language_suffix = f"-{language}" if language != "de" else ""
        return self._directory(stage) / "preparations" / (
            f"rezept-{snapshot.name}-v{RECIPE_VERSION}-{photo_label}-fotos{language_suffix}.json")

    def prepare(self, stage, summaries_file=None, include_photos=False, *, language="de"):
        from .preparation import prepare_export
        from .summaries import load_summaries, normalize_language

        language = normalize_language(language)
        with self._lock(stage):
            state = self._state(stage)
            snapshot = self._snapshot(stage, state)
            _verify(snapshot)
            summaries_sha256 = None
            if summaries_file is not None:
                _, _, summaries_sha256 = load_summaries(snapshot / "manifest.json", summaries_file, language=language)
            recipe = {"snapshot": state["snapshot"], "version": RECIPE_VERSION,
                      "include_photos": include_photos, "summary_language": language}
            if summaries_sha256 is not None:
                recipe["summaries_file_sha256"] = summaries_sha256
            recipe_path = self._recipe_path(stage, snapshot, include_photos, language)
            stored_recipe = _read(recipe_path) if recipe_path.exists() else None
            stored_inputs = {
                key: value for key, value in stored_recipe.items() if key != "preparation"
            } if stored_recipe is not None else None
            if stored_inputs is not None:
                stored_inputs["summary_language"] = normalize_language(stored_inputs.get("summary_language", "de"))
            if stored_inputs is not None and summaries_file is None:
                # No replacement text was supplied: reuse the last validated preparation.
                stored_inputs.pop("summaries_file_sha256", None)
            if stored_inputs == recipe:
                identity = stored_recipe["preparation"]
                destination = self._artifact(stage, "preparations", identity)
                _verify(destination)
                if normalize_language(_read(destination / "prepared.json").get("summary_language", "de")) != language:
                    raise ValueError("Cached preparation summary language does not match")
            else:
                with self._temporary() as temporary:
                    prepare_export(str(snapshot / "manifest.json"), str(temporary / "prepared"), summaries_file,
                                   include_photos=include_photos, language=language)
                    identity, destination = self._store_preparation(stage, temporary / "prepared/prepared.json")
                _write(recipe_path, {**recipe, "preparation": identity})
            state["preparation"] = identity
            self._save(stage, state)
            return {"stage": stage, "preparation": identity, "prepared_path": str(destination / "prepared.json")}

    def _prepared(self, stage, state):
        if not state.get("preparation"):
            raise ValueError(f"Stage {stage} needs preparation first; supply local summaries or adopt matching prepared data")
        directory = self._artifact(stage, "preparations", state["preparation"])
        _verify(directory)
        if _read(directory / "prepared.json").get("source_manifest_sha256") != _digest(self._snapshot(stage, state) / "manifest.json"):
            raise ValueError("Preparation does not match the active snapshot")
        return directory

    def _public_destination(self, stage, target):
        destination = self.output_root / target / f"stage-{stage:02d}"
        if self.output_root.is_symlink() or destination.parent.is_symlink():
            raise ValueError("Public export directories must not be symlinks")
        return destination

    def _publish_output(self, stage, target, run_id, directory, record, state, output):
        """Journal ownership and archive verified predecessors before atomic rename."""
        destination = self._public_destination(stage, target)
        expected = record["inventories"][target]
        pending = record.setdefault("pending_publication", {})
        intent = pending.get(target)
        owners = state.setdefault("visible_exports", {})
        if intent and destination.exists() and (
                not intent["archive_path"] or Path(intent["archive_path"]).is_dir()):
            # A rename may have succeeded before the run/state journals were saved.
            if _public_inventory(destination) == expected:
                record["completed"][target] = str(destination)
                owners[target] = run_id
                self._save(stage, state)
                pending.pop(target)
                _write(directory / "run.json", record)
                return
        if intent is None:
            previous = owners.get(target)
            archive = None
            if destination.exists() or destination.is_symlink():
                if previous is None or previous == run_id:
                    raise ValueError(f"Refusing to overwrite unmanaged public export: {destination}")
                old_directory = self._artifact(stage, "exports", previous)
                old_record = _read(old_directory / "run.json")
                _verify(destination, old_record["inventories"][target])
                archive = self._available(old_directory / "archives" / target)
            elif previous:
                raise ValueError(f"Stored data changed: {destination}; managed public export is missing")
            intent = {"previous_run": previous, "archive_path": str(archive) if archive else None}
            pending[target] = intent
            _write(directory / "run.json", record)
        if intent["archive_path"]:
            archive = Path(intent["archive_path"])
            previous = intent["previous_run"]
            old_directory = self._artifact(stage, "exports", previous)
            old_record = _read(old_directory / "run.json")
            old_inventory = old_record["inventories"][target]
            if not archive.exists():
                _verify(destination, old_inventory)
                archive.parent.mkdir(parents=True, exist_ok=True)
                destination.rename(archive)
            else:
                _verify(archive, old_inventory)
                if destination.exists():
                    raise ValueError(f"Refusing to overwrite unexpected public export: {destination}")
            archives = old_record.setdefault("archives", {}).setdefault(target, [])
            if str(archive) not in archives:
                archives.append(str(archive))
                _write(old_directory / "run.json", old_record)
        elif destination.exists() or destination.is_symlink():
            raise ValueError(f"Refusing to overwrite unmanaged public export: {destination}")
        _verify(output, expected)
        output.rename(destination)
        record["completed"][target] = str(destination)
        owners[target] = run_id
        self._save(stage, state)
        pending.pop(target)
        _write(directory / "run.json", record)

    def export(self, stage, targets=("osmand", "komoot")):
        with self._lock(stage):
            return self._export(stage, targets, self._state(stage))

    def _export(self, stage, targets, state, *, publish=True, run_id=None):
        """Build all target payloads before entering recoverable publication."""
        targets = tuple(sorted(set(targets)))
        if not targets or set(targets) - {"osmand", "komoot"}:
            raise ValueError("Export targets must be osmand and/or komoot")
        snapshot = self._snapshot(stage, state)
        _verify(snapshot)
        preparation = self._prepared(stage, state) if "komoot" in targets or state.get("preparation") else None
        recipe = {"snapshot": state["snapshot"], "preparation": state.get("preparation"),
                  "targets": list(targets), "version": RECIPE_VERSION}
        include_photos = bool(state.get("osmand_photos")) and "osmand" in targets
        if include_photos:
            recipe["osmand_photos"] = True
        if "komoot" in targets:
            recipe["komoot_gpx_format"] = 1
        if run_id is None:
            run_id = _identity(recipe)
            directory = self._destination(
                stage, "exports", run_id, export_name(stage, targets, datetime.now(timezone.utc).isoformat()))
        else:
            directory = self._artifact(stage, "exports", run_id)
        record_path = directory / "run.json"
        if directory.exists():
            record = _read(record_path)
        else:
            record = {"schema_version": 1, **recipe,
                      "created_at": datetime.now(timezone.utc).isoformat(),
                      "completed": {}, "inventories": {}, "receipts": {}}
            directory.parent.mkdir(parents=True, exist_ok=True)
            with self._temporary() as temporary:
                staged = temporary / "export"
                staged.mkdir()
                _write(staged / "run.json", record)
                staged.rename(directory)
            self._register(stage, "exports", run_id, directory)
        for target in targets:
            destination = self._public_destination(stage, target)
            owner = state.get("visible_exports", {}).get(target)
            if target not in record.get("pending_publication", {}) and (
                    destination.exists() or destination.is_symlink()) and owner is None:
                raise ValueError(f"Refusing to overwrite unmanaged public export: {destination}")
            if (target in record["completed"] and owner == run_id
                    and target not in record.get("pending_publication", {})):
                _verify(destination, record["inventories"][target])
                continue
            intent = record.get("pending_publication", {}).get(target)
            if intent and destination.exists() and (
                    not intent["archive_path"] or Path(intent["archive_path"]).is_dir()):
                if _public_inventory(destination) == record["inventories"][target]:
                    continue
            staged = directory / "staging" / target
            if staged.exists():
                _verify(staged, record["inventories"][target])
                continue
            with self._temporary() as temporary:
                output = temporary / "payload"
                output.mkdir()
                archives = record.get("archives", {}).get(target, [])
                if target in record["completed"]:
                    if not archives:
                        raise ValueError("Stored export has no managed payload; migrate the stage exports first")
                    archived = Path(archives[-1])
                    _verify(archived, record["inventories"][target])
                    for filename in record["inventories"][target]:
                        shutil.copyfile(archived / filename, output / filename)
                elif target == "komoot":
                    from .preparation import _komoot_gpx

                    prepared = _read(preparation / "prepared.json")
                    source_routes = {str(route["id"]): route for route in _read(snapshot / "manifest.json")["routes"]}
                    for route in prepared["routes"]:
                        if "kind" not in route:
                            source_id = str(route.get("source_route_id") or str(route["id"]).split("-part-")[0])
                            route["kind"] = source_routes[source_id]["kind"]
                    names = export_filenames(stage, prepared["routes"], target)
                    for route in prepared["routes"]:
                        source = _file(preparation, route["gpx"])
                        destination = output / names[route["id"]]["gpx"]
                        if "komoot_gpx_format" in record:
                            destination.write_text(_komoot_gpx(ET.parse(source).getroot()), encoding="utf-8")
                        else:
                            shutil.copyfile(source, destination)
                else:
                    manifest_path = snapshot / "manifest.json"
                    if preparation:
                        manifest = _read(manifest_path)
                        summaries = _prepared_summaries(preparation)
                        for poi in manifest["pois"]:
                            if poi["id"] in summaries:
                                poi["summary"] = summaries[poi["id"]]
                        manifest_path = temporary / "osmand-source.json"
                        _write(manifest_path, manifest)
                    manifest = _read(manifest_path)
                    names = export_filenames(stage, manifest["routes"], target)
                    receipts = record.setdefault("receipts", {}).setdefault(target, {})
                    for number, route in enumerate(manifest["routes"]):
                        naming = names[route["id"]]
                        result = export_osmand(str(manifest_path), str(temporary / f"route-{number}"),
                                               str(route["id"]), include_photos=include_photos)
                        receipt = Path(result["path"]) / "export.json"
                        data = _read(receipt)
                        renamed = []
                        for filename in data["files"]:
                            if filename in naming:
                                shutil.copyfile(receipt.parent / filename, output / naming[filename])
                                renamed.append(naming[filename])
                        data["files"] = renamed
                        data["source_manifest"] = str(snapshot / "manifest.json")
                        data["source_manifest_sha256"] = _digest(snapshot / "manifest.json")
                        data["preparation"] = state.get("preparation")
                        receipts[str(route["id"])] = data
                inventory = _public_inventory(output)
                previous_inventory = record.setdefault("inventories", {}).get(target)
                if previous_inventory is not None and inventory != previous_inventory:
                    raise ValueError("Stored export inventory differs from regenerated payload")
                record["inventories"][target] = inventory
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(output, staged)
                _write(record_path, record)
        if publish:
            for target in targets:
                destination = self._public_destination(stage, target)
                if (target in record["completed"] and state.get("visible_exports", {}).get(target) == run_id
                        and target not in record.get("pending_publication", {})):
                    _verify(destination, record["inventories"][target])
                    continue
                staged = directory / "staging" / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=f".stage-{stage:02d}-", dir=destination.parent) as name:
                    output = None
                    if staged.exists():
                        _verify(staged, record["inventories"][target])
                        output = Path(name) / "payload"
                        shutil.copytree(staged, output)
                    self._publish_output(stage, target, run_id, directory, record, state, output)
                if staged.exists():
                    shutil.rmtree(staged)
            state["export"] = run_id
            self._save(stage, state)
        return {"stage": stage, "run_id": run_id, "path": str(self.output_root),
                "record_path": str(record_path), "outputs": record["completed"]}

