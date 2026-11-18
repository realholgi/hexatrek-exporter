"""Private candidate updates behind Workflow.update's stage lock."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .preparation import prepare_export
from .summaries import _unique_object, load_summaries, normalize_language, summary_document, text_issues
from .summary_generation import generate_summaries
from .update_content import compare_manifests
from .workflow import RECIPE_VERSION, _identity, _prepared_summaries, _read, _verify, _write


def _photo_mode(manifest):
    photos = manifest.get("photos", [])
    if manifest["source"].get("photos_coverage") == "not-requested":
        return "none"
    if manifest["source"].get("photo_mode") in ("files", "links"):
        return manifest["source"]["photo_mode"]
    return "files" if any("path" in photo for photo in photos) else "links" if photos else "none"


def _check_public(workflow, stage, state, targets):
    for target in targets:
        destination = workflow._public_destination(stage, target)
        owner = state.get("visible_exports", {}).get(target)
        if owner is None:
            if destination.exists() or destination.is_symlink():
                raise ValueError(f"Refusing to overwrite unmanaged public export: {destination}")
            continue
        record = _read(workflow._artifact(stage, "exports", owner) / "run.json")
        if record.get("pending_publication"):
            raise ValueError("Finish the interrupted export before updating this stage")
        _verify(destination, record["inventories"][target])


def _check_geometry(before, after):
    routes = lambda manifest: [{key: value for key, value in route.items() if key != "gpx"}
                               for route in manifest["routes"]]
    points = lambda manifest: {point["id"]: tuple(point.get(key) for key in (
        "stage_id", "group_id", "internal_id", "latitude", "longitude")) for point in manifest["pois"]}
    if routes(before) != routes(after) or points(before) != points(after):
        raise ValueError("Refresh changed route geometry or POI identities")


def _result(stage, identity, directory, record):
    result = {"stage": stage, "update_id": identity, "status": record["status"],
              "record_path": str(directory / "run.json"), "changes": record["changes"],
              "summary_language": record["language"], "photo_mode": record["photo_mode"],
              "targets": record["targets"]}
    result["photo_quality"] = record.get("photo_quality", "med")
    if record.get("summary_issues"):
        result["summary_issues"] = record["summary_issues"]
    if (directory / "summaries.json").exists():
        result["summaries_path"] = str(directory / "summaries.json")
    if record.get("outputs"):
        result["outputs"] = record["outputs"]
    return result


def _new_update(workflow, stage, session_file, photo_mode, targets, language, wants_summaries, photo_quality):
    from .hexatrek_refresh import refresh_pois
    from .photo_quality import normalize_quality

    baseline = workflow._state(stage)
    if not baseline.get("visible_exports"):
        raise ValueError("Update requires an already exported stage")
    if not session_file:
        raise ValueError("A new update requires --session-file; --resume needs no session")
    for identity in workflow._storage(stage).get("updates", {}):
        pending = _read(workflow._artifact(stage, "updates", identity) / "run.json")
        if pending["status"] == "publishing" or (pending["status"] not in ("completed", "unchanged")
                                                  and pending["baseline_state"] == baseline):
            raise ValueError(f"Stage {stage} has a pending update; use --resume {identity}")
    targets = sorted(set(baseline["visible_exports"] if targets is None else targets))
    if not targets or set(targets) - {"osmand", "komoot"}:
        raise ValueError("Update targets must be osmand and/or komoot")
    _check_public(workflow, stage, baseline, targets)
    snapshot = workflow._snapshot(stage, baseline)
    _verify(snapshot)
    before = _read(snapshot / "manifest.json")
    prepared = workflow._prepared(stage, baseline) if baseline.get("preparation") else None
    index = _read(prepared / "prepared.json") if prepared else {}
    previous_language = normalize_language(index.get("summary_language", before.get("summary_language", "de")))
    selected_language = normalize_language(language if language is not None else previous_language)
    previous_mode = _photo_mode(before)
    selected_mode = previous_mode if photo_mode is None else photo_mode
    if selected_mode not in ("none", "links", "files"):
        raise ValueError("Photo mode must be none, links or files")
    selected_quality = normalize_quality(
        photo_quality if photo_quality is not None else before["source"].get("photo_quality", "med"))
    needs_preparation = bool(prepared) or "komoot" in targets or language is not None or wants_summaries
    include_photos = selected_mode == "files" and (photo_mode == "files" or index.get("photo_mode") == "local-files")
    with workflow._temporary() as temporary:
        fetched = refresh_pois(str(snapshot / "manifest.json"), str(temporary / "source"),
                               session_file=session_file, include_photos=selected_mode != "none",
                               photo_mode="files" if selected_mode == "files" else "links",
                               photo_quality=selected_quality,
                               photo_cache_path=workflow._photo_cache(stage, selected_mode))
        path = Path(fetched["manifest_path"])
        after = _read(path)
        _check_geometry(before, after)
        changes = compare_manifests(before, after)
        stale = set(changes["stale_summaries"])
        old_points = {point["id"]: point for point in before["pois"]}
        cached = _prepared_summaries(prepared) if prepared else {
            point["id"]: point.get("summary") for point in before["pois"]}
        for point in after["pois"]:
            point.pop("summary", None)
            text = cached.get(point["id"])
            if (not needs_preparation and point["id"] not in stale
                    and previous_language == selected_language and not text_issues(point["id"], text)):
                point["summary"] = text
        _write(path, after)
        display_photos = selected_mode == "links" and any(photo.get("url") for photo in after.get("photos", []))
        unchanged = (not changes["changed"] and selected_language == previous_language
                     and selected_mode == previous_mode and set(targets) <= baseline["visible_exports"].keys()
                     and not (needs_preparation and not prepared)
                     and include_photos == (index.get("photo_mode") == "local-files")
                     and ("osmand" not in targets or display_photos == bool(baseline.get("osmand_photos"))))
        candidate = deepcopy(baseline)
        if display_photos or "osmand_photos" in baseline:
            candidate["osmand_photos"] = display_photos
        if not unchanged:
            stored = workflow._store_snapshot(path)
            candidate["snapshot"] = stored["snapshot"]
            candidate.pop("preparation", None)
        created = datetime.now(timezone.utc).isoformat()
        identity = _identity({"baseline": baseline, "candidate": candidate["snapshot"],
                              "targets": targets, "language": selected_language,
                              "photo_mode": selected_mode, "created_at": created})
        label = f"{created[:10]}-stage-{stage:02d}-update"
        directory = workflow._destination(stage, "updates", identity, label)
        record = {"schema_version": 1, "stage": stage, "created_at": created,
                  "status": "unchanged" if unchanged else "pending", "changes": changes,
                  "baseline_state": baseline, "candidate_state": candidate,
                  "targets": targets, "language": selected_language, "photo_mode": selected_mode,
                  "photo_quality": selected_quality,
                  "needs_preparation": needs_preparation, "include_photos": include_photos}
        _write(directory / "run.json", record)
        workflow._register(stage, "updates", identity, directory)
        if not unchanged and needs_preparation:
            manifest_path = workflow._snapshot(stage, candidate) / "manifest.json"
            document = summary_document(manifest_path, language=selected_language)
            for poi_id in document["summaries"]:
                text = cached.get(poi_id)
                if (poi_id in old_points and poi_id not in stale and previous_language == selected_language
                        and not text_issues(poi_id, text)):
                    document["summaries"][poi_id] = text
            _write(directory / "summaries.json", document)
    return identity, directory, record


def _check_resume(workflow, stage, record):
    current = workflow._state(stage)
    baseline = record["baseline_state"]
    if current == baseline:
        return
    if record["status"] != "publishing":
        raise ValueError("Stage changed since this update began; the candidate cannot be published")
    candidate = record["candidate_state"]
    if any(current.get(key) != candidate.get(key) for key in ("snapshot", "preparation", "export", "osmand_photos")):
        raise ValueError("Stage changed outside the interrupted update")
    ignored = {"snapshot", "preparation", "export", "visible_exports", "osmand_photos"}
    if {key: value for key, value in current.items() if key not in ignored} != {
            key: value for key, value in baseline.items() if key not in ignored}:
        raise ValueError("Stage changed outside the interrupted update")
    old_owners = baseline.get("visible_exports", {})
    owners = current.get("visible_exports", {})
    if set(owners) - set(old_owners) - set(record["targets"]):
        raise ValueError("Public export ownership changed outside the interrupted update")
    for target in old_owners.keys() | owners.keys():
        allowed = {old_owners.get(target)}
        if target in record["targets"]:
            allowed.add(candidate["export"])
        if owners.get(target) not in allowed:
            raise ValueError("Public export ownership changed outside the interrupted update")


def _prepare(workflow, stage, directory, record, summaries_file, generate, model, api_base):
    candidate = record["candidate_state"]
    manifest_path = workflow._snapshot(stage, candidate) / "manifest.json"
    draft = directory / "summaries.json"
    if summaries_file is not None:
        _, texts, _ = load_summaries(manifest_path, summaries_file, language=record["language"])
        document = summary_document(manifest_path, language=record["language"])
        document["summaries"] = {identity: texts[identity] for identity in document["summaries"]}
        _write(draft, document)
    document = json.loads(draft.read_bytes(), object_pairs_hook=_unique_object)
    expected = summary_document(manifest_path, language=record["language"])
    if (not isinstance(document, dict) or type(document.get("schema_version")) is not int
            or document["schema_version"] != 1
            or document.get("source_manifest_sha256") != expected["source_manifest_sha256"]):
        raise ValueError("Update summaries must match the candidate source manifest")
    if normalize_language(document.get("summary_language", "de")) != record["language"]:
        raise ValueError("Update summary language does not match the candidate")
    if not isinstance(document.get("summaries"), dict) or set(document["summaries"]) != set(expected["summaries"]):
        raise ValueError("Update summaries must contain exactly the candidate route POI IDs")
    if generate:
        document = generate_summaries(manifest_path, document, api_base=api_base, model=model,
                                      api_key=os.environ.get("HEXATREK_API_KEY"))
        _write(draft, document)
    document = json.loads(draft.read_bytes(), object_pairs_hook=_unique_object)
    issues = [issue for identity, text in document["summaries"].items() for issue in text_issues(identity, text)]
    if issues:
        record["status"] = "needs-review"
        record["summary_issues"] = issues
        _write(directory / "run.json", record)
        return False
    record.pop("summary_issues", None)
    _, _, digest = load_summaries(manifest_path, draft, language=record["language"])
    if record.get("summaries_sha256") == digest and candidate.get("preparation"):
        workflow._prepared(stage, candidate)
        return True
    with workflow._temporary() as temporary:
        prepare_export(manifest_path, temporary / "prepared", draft,
                       include_photos=record["include_photos"], language=record["language"])
        identity, _ = workflow._store_preparation(stage, temporary / "prepared/prepared.json")
    candidate["preparation"] = identity
    record["summaries_sha256"] = digest
    recipe = {"snapshot": candidate["snapshot"], "version": RECIPE_VERSION,
              "include_photos": record["include_photos"], "summary_language": record["language"],
              "summaries_file_sha256": digest, "preparation": identity}
    _write(workflow._recipe_path(stage, manifest_path.parent, record["include_photos"], record["language"]), recipe)
    _write(directory / "run.json", record)
    return True


def update_stage(workflow, stage, *, session_file=None, photo_mode=None, targets=None,
                 language=None, generate=False, model=None, api_base=None,
                 summaries_file=None, resume=None, photo_quality=None):
    if resume is None:
        identity, directory, record = _new_update(
            workflow, stage, session_file, photo_mode, targets, language,
            wants_summaries=generate or summaries_file is not None, photo_quality=photo_quality)
    else:
        identity = resume
        directory = workflow._artifact(stage, "updates", identity)
        record = _read(directory / "run.json")
        if photo_mode is not None or targets is not None or language is not None or photo_quality is not None:
            raise ValueError("Resume uses the update's stored photo mode, quality, targets and language")
    if record["status"] in ("completed", "unchanged"):
        return _result(stage, identity, directory, record)
    _check_resume(workflow, stage, record)
    snapshot = workflow._snapshot(stage, record["candidate_state"])
    _verify(snapshot)
    if record["status"] != "publishing":
        _check_public(workflow, stage, record["baseline_state"], record["targets"])
        if record["needs_preparation"]:
            if not _prepare(workflow, stage, directory, record, summaries_file, generate, model, api_base):
                return _result(stage, identity, directory, record)
        elif summaries_file is not None or generate:
            raise ValueError("This update retains an unprepared OsmAnd export; summaries require --language on a new update")
        exported = workflow._export(stage, record["targets"], deepcopy(record["candidate_state"]), publish=False)
        _check_public(workflow, stage, record["baseline_state"], record["targets"])
        record["candidate_state"]["export"] = exported["run_id"]
        record["status"] = "publishing"
        _write(directory / "run.json", record)
    elif summaries_file is not None:
        raise ValueError("Publication already started; resume without replacement summaries")
    candidate = deepcopy(record["candidate_state"])
    candidate["visible_exports"] = deepcopy(workflow._state(stage).get("visible_exports", {}))
    exported = workflow._export(stage, record["targets"], candidate)
    record["candidate_state"] = candidate
    record["outputs"] = exported["outputs"]
    record["status"] = "completed"
    _write(directory / "run.json", record)
    return _result(stage, identity, directory, record)
