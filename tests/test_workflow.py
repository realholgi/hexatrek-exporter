"""Durable source isolation, local retry and account-mutation safety."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from hexatrek_exporter.hexatrek_export import write_manifest_export
from hexatrek_exporter.workflow import Workflow
from hexatrek_exporter.poi_details import GPX


@pytest.fixture
def source(tmp_path):
    manifest = {
        "schema_version": 1, "stage_id": 1, "exported_at": "2026-11-17T12:00:00Z",
        "source": {"kind": "hexatrek-firestore-cache", "comments_coverage": "cache-only"},
        "pois": [{"id": "1:1:1", "stage_id": 1, "name": "Water", "type": "Fontaine",
                  "latitude": 48.0, "longitude": 7.0, "description": "Original background",
                  "summary": "Wasser unbestätigt.", "comments": [], "raw": {}}],
        "routes": [{"id": "1", "stage_id": 1, "name": "Stage 1", "kind": "main",
                    "gpx": "stage-1.gpx", "poi_ids": ["1:1:1"], "points": [
                        {"internalId": i, "stageId": 1, "latitude": 48.0 + i / 100,
                         "longitude": 7.0, "elevation": 100 + i, "category": 101, "distance": i * 100}
                        for i in range(3)]}], "point_types": [],
    }
    result = write_manifest_export(manifest, str(tmp_path / "source"))
    return Path(result["manifest_path"])


def test_snapshot_survives_source_changes_and_detects_tampering(tmp_path, source):
    workflow = Workflow(tmp_path / "data")
    adopted = workflow.adopt_snapshot(str(source))
    original = source.read_bytes()
    source.write_text("changed")
    stored = Path(adopted["manifest_path"])
    assert stored.read_bytes() == original
    exported = workflow.export(1, ("osmand",))
    navigation = Path(exported["outputs"]["osmand"]) / "stage-01.gpx"
    tree = ET.parse(navigation)
    receipt = json.loads(Path(exported["record_path"]).read_text())["receipts"]["osmand"]["1"]
    assert navigation.name in receipt["files"]
    assert all((navigation.parent / filename).is_file() for filename in receipt["files"])
    assert tree.findtext(f"{{{GPX}}}wpt/{{{GPX}}}desc") == "Wasser unbestätigt."
    stored.write_text("tampered")
    with pytest.raises(ValueError, match="Stored data changed"):
        workflow.export(1, ("osmand",))


def test_completed_local_export_reuses_files_and_recovers_journal_gap(tmp_path, source, monkeypatch):
    workflow = Workflow(tmp_path / "data")
    workflow.adopt_snapshot(str(source))
    first = workflow.export(1, ("osmand",))
    navigation = Path(first["outputs"]["osmand"]) / "stage-01.gpx"
    before = navigation.stat().st_mtime_ns
    journal = Path(first["record_path"])
    record = json.loads(journal.read_text())
    record["completed"] = {}
    record["pending_publication"] = {"osmand": {"previous_run": None, "archive_path": None}}
    journal.write_text(json.dumps(record))

    def forbidden(*args, **kwargs):
        pytest.fail("verified public payload must not be regenerated")

    monkeypatch.setattr("hexatrek_exporter.workflow.export_osmand", forbidden)
    again = workflow.export(1, ("osmand",))
    assert again["run_id"] == first["run_id"]
    assert navigation.stat().st_mtime_ns == before
    assert json.loads(journal.read_text())["completed"]["osmand"] == first["outputs"]["osmand"]


def test_preparation_from_another_source_is_rejected(tmp_path, source):
    workflow = Workflow(tmp_path / "data")
    workflow.adopt_snapshot(str(source))
    path = tmp_path / "prepared.json"
    path.write_text(json.dumps({"schema_version": 1, "source_manifest_sha256": "unrelated", "routes": []}))
    with pytest.raises(ValueError, match="does not match"):
        workflow.adopt_preparation(1, str(path))


def test_snapshot_rejects_symlinked_source_assets(tmp_path, source):
    (source.parent / "secret").symlink_to(tmp_path / "external")
    with pytest.raises(ValueError, match="symlinks"):
        Workflow(tmp_path / "data").adopt_snapshot(str(source))


def test_refresh_invalidates_previously_prepared_summaries(tmp_path, source, monkeypatch):
    import hexatrek_exporter.workflow as module

    workflow = Workflow(tmp_path / "data")
    original = workflow.adopt_snapshot(str(source))

    def refreshed(manifest_path, output_directory, **kwargs):
        manifest = json.loads(Path(manifest_path).read_text())
        manifest["pois"][0]["description"] = "New report: water unavailable"
        manifest["source"]["comments_coverage"] = "cloud-complete"
        return write_manifest_export(manifest, output_directory)

    monkeypatch.setattr(module, "refresh_pois", refreshed)
    updated = workflow.refresh(1, "existing-private-session")
    new = json.loads(Path(updated["manifest_path"]).read_text())
    assert "summary" not in new["pois"][0]
    assert json.loads(Path(original["manifest_path"]).read_text())["pois"][0]["summary"] == "Wasser unbestätigt."
    with pytest.raises(ValueError, match="Missing prepared summary"):
        workflow.prepare(1)


def test_snapshot_storage_names_collide_without_losing_identity_reuse(tmp_path, source):
    workflow = Workflow(tmp_path / "data")
    first = workflow.adopt_snapshot(source)
    first_path = Path(first["manifest_path"]).parent
    assert first_path.name == "2026-11-17-app-cache"
    original = source.read_bytes()
    manifest = json.loads(original)
    manifest["pois"][0]["description"] = "Another local report"
    source.write_text(json.dumps(manifest))
    second = workflow.adopt_snapshot(source)
    assert Path(second["manifest_path"]).parent.name == "2026-11-17-app-cache-02"
    source.write_bytes(original)
    again = workflow.adopt_snapshot(source)
    assert again == first
    storage = json.loads((tmp_path / "data/stages/01/storage.json").read_text())
    assert storage == {
        "snapshots": {first["snapshot"]: first_path.name,
                      second["snapshot"]: "2026-11-17-app-cache-02"},
        "preparations": {}, "exports": {}}
    assert workflow.status()["stages"][0]["snapshot_path"] == str(first_path)


@pytest.mark.parametrize("kind", ["snapshots", "preparations", "exports"])
def test_storage_helpers_reuse_mapping_and_skip_unindexed_collisions(tmp_path, kind):
    workflow = Workflow(tmp_path / "data")
    label = "2026-11-17-stage-01"
    first = workflow._destination(1, kind, "first-id", label)
    first.mkdir(parents=True)
    workflow._register(1, kind, "first-id", first)
    assert workflow._artifact(1, kind, "first-id") == first
    assert workflow._destination(1, kind, "first-id", "different-label") == first
    second = workflow._destination(1, kind, "second-id", label)
    assert second.name == label + "-02"
    second.mkdir()
    assert workflow._destination(1, kind, "third-id", label).name == label + "-03"
    hash_path = first.parent / "old-hash"
    hash_path.mkdir()
    with pytest.raises(ValueError, match="migrate"):
        workflow._artifact(1, kind, "old-hash")


def test_finder_metadata_does_not_change_identity_or_mask_tampering(tmp_path, source):
    workflow = Workflow(tmp_path / "data")
    first = workflow.adopt_snapshot(source)
    (source.parent / ".DS_Store").write_bytes(b"Finder changed source metadata")
    assert workflow.adopt_snapshot(source) == first
    snapshot = Path(first["manifest_path"]).parent
    (snapshot / ".DS_Store").write_bytes(b"new metadata")
    inventory_path = snapshot / "inventory.json"
    inventory = json.loads(inventory_path.read_text())
    inventory[".DS_Store"] = "old Finder checksum"
    inventory_path.write_text(json.dumps(inventory))
    exported = workflow.export(1, ("osmand",))
    navigation = Path(exported["outputs"]["osmand"]) / "stage-01.gpx"
    (navigation.parent / ".DS_Store").write_bytes(b"another Finder update")
    assert workflow.export(1, ("osmand",))["run_id"] == exported["run_id"]
    navigation.write_text("real content change")
    with pytest.raises(ValueError, match="Stored data changed"):
        workflow.export(1, ("osmand",))


def test_preparation_recipe_and_export_discovery_reuse_readable_paths(tmp_path, source):
    workflow = Workflow(tmp_path / "data")
    snapshot = workflow.adopt_snapshot(source)
    prepared = workflow.prepare(1)
    assert "-stage-01-komoot-vorbereitet" in Path(prepared["prepared_path"]).parent.name
    recipes = list((tmp_path / "data/stages/01/preparations").glob("rezept-*.json"))
    assert [path.name for path in recipes] == ["rezept-2026-11-17-app-cache-v1-ohne-fotos.json"]
    assert json.loads(recipes[0].read_text())["snapshot"] == snapshot["snapshot"]
    assert workflow.prepare(1) == prepared
    exported = workflow.export(1)
    manifest = json.loads(Path(prepared["prepared_path"]).read_text())
    from hexatrek_exporter.naming import export_filenames
    names = export_filenames(1, manifest["routes"], "komoot")
    for route in manifest["routes"]:
        assert (Path(exported["outputs"]["komoot"]) / names[route["id"]]["gpx"]).read_bytes() == (
            Path(prepared["prepared_path"]).parent / route["gpx"]).read_bytes()
        assert (Path(prepared["prepared_path"]).parent / route["details"]).is_file()
    for target, directory in exported["outputs"].items():
        assert Path(directory) == tmp_path / "exports" / target / "stage-01"
        assert all(path.is_file() and path.suffix == ".gpx" for path in Path(directory).iterdir())
    assert Path(exported["record_path"]).is_relative_to(tmp_path / "data/stages/01/exports")
    status = workflow.status()["stages"][0]
    assert status["preparation_path"] == str(Path(prepared["prepared_path"]).parent)
    assert status["export_path"] == exported["path"]
    assert status["export_record_path"] == exported["record_path"]
    assert status["outputs"] == exported["outputs"]


def test_adopted_preparation_preserves_payloads_and_shared_archive(tmp_path, source):
    workflow = Workflow(tmp_path / "data")
    snapshot = workflow.adopt_snapshot(source)
    source_path = Path(snapshot["manifest_path"])
    legacy = tmp_path / "legacy-prepared"
    legacy.mkdir()
    payloads = {"stage-1-part-1.gpx": b"first GPX payload",
                "stage-1-part-2.gpx": b"second GPX payload",
                "stage-1-details.zip": b"original archive including immutable source members"}
    for name, payload in payloads.items():
        (legacy / name).write_bytes(payload)
    prepared = {
        "schema_version": 1, "source_manifest": str(source_path),
        "source_manifest_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "routes": [
            {"id": f"1-part-{number}", "name": f"Stage 1 · {number}/2",
             "gpx": f"stage-1-part-{number}.gpx", "details": "stage-1-details.zip",
             "aliases": {}, "section": {"number": number, "count": 2}}
            for number in (1, 2)]}
    original = json.dumps(prepared)
    prepared_path = legacy / "prepared.json"
    prepared_path.write_text(original)
    first = workflow.adopt_preparation(1, prepared_path)
    stored = Path(first["prepared_path"])
    normalized = json.loads(stored.read_text())
    assert normalized["routes"][0]["details"] == normalized["routes"][1]["details"]
    assert {route["gpx"] for route in normalized["routes"]} == {"stage-01a.gpx", "stage-01b.gpx"}
    for route in normalized["routes"]:
        assert (stored.parent / route["gpx"]).read_bytes() == payloads[f"stage-1-part-{route['section']['number']}.gpx"]
        assert (stored.parent / route["details"]).read_bytes() == payloads["stage-1-details.zip"]
    assert len(list(stored.parent.rglob("*.zip"))) == 1
    assert prepared_path.read_text() == original
    assert workflow.adopt_preparation(1, prepared_path) == first
    exported = workflow.export(1, ("komoot",))
    public = Path(exported["outputs"]["komoot"])
    assert {path.name: path.read_bytes() for path in public.iterdir()} == {
        "stage-01a.gpx": payloads["stage-1-part-1.gpx"],
        "stage-01b.gpx": payloads["stage-1-part-2.gpx"]}


@pytest.mark.parametrize("extra", ["unexpected.gpx", "run.json", "details.zip", "directory"])
def test_public_exports_reject_unrecorded_entries(tmp_path, source, extra):
    workflow = Workflow(tmp_path / "data")
    workflow.adopt_snapshot(source)
    exported = workflow.export(1, ("osmand",))
    path = Path(exported["outputs"]["osmand"]) / extra
    if extra == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"user content")
    with pytest.raises(ValueError, match="Stored data changed"):
        workflow.export(1, ("osmand",))
    assert path.exists()


def test_public_export_never_adopts_unmanaged_matching_payload(tmp_path, source):
    workflow = Workflow(tmp_path / "data")
    workflow.adopt_snapshot(source)
    exported = workflow.export(1, ("osmand",))
    journal = Path(exported["record_path"])
    record = json.loads(journal.read_text())
    record["completed"] = {}
    journal.write_text(json.dumps(record))
    state_path = tmp_path / "data/stages/01/stage.json"
    state = json.loads(state_path.read_text())
    state["visible_exports"] = {}
    state_path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="unmanaged"):
        workflow.export(1, ("osmand",))


def test_replacement_archives_prior_verified_public_version(tmp_path, source):
    workflow = Workflow(tmp_path / "data")
    original_source = source.read_bytes()
    workflow.adopt_snapshot(source)
    first = workflow.export(1, ("osmand",))
    public = Path(first["outputs"]["osmand"])
    original = {path.name: path.read_bytes() for path in public.iterdir()}
    manifest = json.loads(original_source)
    manifest["pois"][0]["summary"] = "New verified water report."
    source.write_text(json.dumps(manifest))
    workflow.adopt_snapshot(source)
    second = workflow.export(1, ("osmand",))
    assert second["run_id"] != first["run_id"]
    assert second["outputs"] == first["outputs"]
    old_record = json.loads(Path(first["record_path"]).read_text())
    archived = Path(old_record["archives"]["osmand"][0])
    assert archived.is_relative_to(Path(first["record_path"]).parent)
    assert {path.name: path.read_bytes() for path in archived.iterdir()} == original
    assert public.joinpath("stage-01.gpx").read_bytes() != original["stage-01.gpx"]
    assert workflow.status()["stages"][0]["visible_exports"] == {"osmand": second["run_id"]}
    source.write_bytes(original_source)
    workflow.adopt_snapshot(source)
    assert workflow.export(1, ("osmand",))["run_id"] == first["run_id"]
    assert {path.name: path.read_bytes() for path in public.iterdir()} == original


def test_replacement_refuses_user_modified_previous_version(tmp_path, source):
    workflow = Workflow(tmp_path / "data")
    workflow.adopt_snapshot(source)
    first = workflow.export(1, ("osmand",))
    public = Path(first["outputs"]["osmand"]) / "stage-01.gpx"
    public.write_bytes(b"user-edited navigation")
    manifest = json.loads(source.read_text())
    manifest["pois"][0]["summary"] = "New water report."
    source.write_text(json.dumps(manifest))
    workflow.adopt_snapshot(source)
    with pytest.raises(ValueError, match="Stored data changed"):
        workflow.export(1, ("osmand",))
    assert public.read_bytes() == b"user-edited navigation"


def test_unknown_bivouac_overlay_stays_out_of_public_exports(tmp_path, source):
    manifest = json.loads(source.read_text())
    manifest["routes"][0]["points"][0]["category"] = 999
    source.write_text(json.dumps(manifest))
    workflow = Workflow(tmp_path / "data")
    workflow.adopt_snapshot(source)
    exported = workflow.export(1, ("osmand",))
    public = Path(exported["outputs"]["osmand"])
    assert {path.name for path in public.iterdir()} == {"stage-01.gpx", "stage-01-biwak.gpx"}
    assert len(ET.parse(public / "stage-01.gpx").findall(f".//{{{GPX}}}trkpt")) == 3
    record = json.loads(Path(exported["record_path"]).read_text())
    receipt = record["receipts"]["osmand"]["1"]
    assert receipt["segments"]["unknown"] == 1
    assert receipt["files"] == ["stage-01.gpx", "stage-01-biwak.gpx"]
    assert set(record["inventories"]["osmand"]) == set(receipt["files"])


def test_interrupted_replacement_resumes_without_regenerating_payload(tmp_path, source, monkeypatch):
    workflow = Workflow(tmp_path / "data")
    workflow.adopt_snapshot(source)
    first = workflow.export(1, ("osmand",))
    original = Path(first["outputs"]["osmand"]).joinpath("stage-01.gpx").read_bytes()
    manifest = json.loads(source.read_text())
    manifest["pois"][0]["summary"] = "Updated water report."
    source.write_text(json.dumps(manifest))
    workflow.adopt_snapshot(source)
    rename = Path.rename

    def interrupted(path, destination):
        if path.name == "payload":
            raise OSError("interrupted public rename")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", interrupted)
    with pytest.raises(OSError, match="interrupted public rename"):
        workflow.export(1, ("osmand",))
    old_record = json.loads(Path(first["record_path"]).read_text())
    archived = Path(old_record["archives"]["osmand"][0])
    assert (archived / "stage-01.gpx").read_bytes() == original
    assert not Path(first["outputs"]["osmand"]).exists()
    monkeypatch.setattr(Path, "rename", rename)

    def forbidden(*args, **kwargs):
        pytest.fail("interrupted payload must be retained for an exact retry")

    monkeypatch.setattr("hexatrek_exporter.workflow.export_osmand", forbidden)
    completed = workflow.export(1, ("osmand",))
    record = json.loads(Path(completed["record_path"]).read_text())
    assert not record["pending_publication"]
    assert not (Path(completed["record_path"]).parent / "staging/osmand").exists()
    public = Path(completed["outputs"]["osmand"])
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in public.iterdir()} == record["inventories"]["osmand"]


def test_summary_input_changes_rebuild_recipe_and_identical_input_reuses(tmp_path, source, monkeypatch):
    workflow = Workflow(tmp_path / "data")
    snapshot = workflow.adopt_snapshot(source)
    manifest_path = Path(snapshot["manifest_path"])
    original = manifest_path.read_bytes()
    summaries = tmp_path / "summaries.json"

    def supply(text):
        summaries.write_text(json.dumps({
            "schema_version": 1,
            "source_manifest_sha256": hashlib.sha256(original).hexdigest(),
            "summaries": {"1:1:1": text},
        }))

    supply("First supplied text")
    first = workflow.prepare(1, summaries)
    first_path = Path(first["prepared_path"])
    supply("Second supplied text")
    second = workflow.prepare(1, summaries)
    assert first["preparation"] != second["preparation"]
    assert first_path.is_file()
    second_path = Path(second["prepared_path"])
    index = json.loads(second_path.read_text())
    assert ET.parse(second_path.parent / index["routes"][0]["gpx"]).findtext(
        f"{{{GPX}}}wpt/{{{GPX}}}desc") == "Second supplied text"
    recipes = list((tmp_path / "data/stages/01/preparations").glob("rezept-*.json"))
    assert [path.name for path in recipes] == ["rezept-2026-11-17-app-cache-v1-ohne-fotos.json"]
    recipe = json.loads(recipes[0].read_text())
    assert recipe["summaries_file_sha256"] == hashlib.sha256(summaries.read_bytes()).hexdigest()
    assert recipe["preparation"] == second["preparation"]
    storage = json.loads((tmp_path / "data/stages/01/storage.json").read_text())
    assert {first["preparation"], second["preparation"]} <= set(storage["preparations"])

    def forbidden(*args, **kwargs):
        pytest.fail("matching input must reuse its verified preparation")

    monkeypatch.setattr("hexatrek_exporter.preparation.prepare_export", forbidden)
    assert workflow.prepare(1, summaries) == second
    assert manifest_path.read_bytes() == original


def test_invalid_supplied_summaries_are_validated_before_cache_lookup(tmp_path, source):
    workflow = Workflow(tmp_path / "data")
    snapshot = workflow.adopt_snapshot(source)
    summaries = tmp_path / "summaries.json"
    value = {
        "schema_version": 1,
        "source_manifest_sha256": hashlib.sha256(Path(snapshot["manifest_path"]).read_bytes()).hexdigest(),
        "summaries": {"1:1:1": "Supplied text"},
    }
    summaries.write_text(json.dumps(value))
    prepared = workflow.prepare(1, summaries)
    recipe_path = next((tmp_path / "data/stages/01/preparations").glob("rezept-*.json"))
    recipe = recipe_path.read_bytes()
    value["source_manifest_sha256"] = "0" * 64
    summaries.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="does not match"):
        workflow.prepare(1, summaries)
    assert recipe_path.read_bytes() == recipe
    assert Path(prepared["prepared_path"]).is_file()


def test_summary_file_bytes_not_only_effective_text_bind_recipe(tmp_path, source, monkeypatch):
    workflow = Workflow(tmp_path / "data")
    snapshot = workflow.adopt_snapshot(source)
    summaries = tmp_path / "summaries.json"
    value = {
        "schema_version": 1,
        "source_manifest_sha256": hashlib.sha256(Path(snapshot["manifest_path"]).read_bytes()).hexdigest(),
        "summaries": {"1:1:1": "Supplied text"},
    }
    summaries.write_text(json.dumps(value))
    workflow.prepare(1, summaries)
    from hexatrek_exporter import preparation
    original_prepare = preparation.prepare_export
    calls = []

    def recording(*args, **kwargs):
        calls.append(args)
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(preparation, "prepare_export", recording)
    summaries.write_text(json.dumps(value, indent=2))
    workflow.prepare(1, summaries)
    assert len(calls) == 1
    recipe_path = next((tmp_path / "data/stages/01/preparations").glob("rezept-*.json"))
    assert json.loads(recipe_path.read_text())["summaries_file_sha256"] == hashlib.sha256(summaries.read_bytes()).hexdigest()


def test_no_file_reuses_old_recipe_even_without_cached_manifest_summaries(tmp_path, source, monkeypatch):
    manifest = json.loads(source.read_text())
    del manifest["pois"][0]["summary"]
    source.write_text(json.dumps(manifest))
    workflow = Workflow(tmp_path / "data")
    snapshot = workflow.adopt_snapshot(source)
    summaries = tmp_path / "summaries.json"
    summaries.write_text(json.dumps({
        "schema_version": 1,
        "source_manifest_sha256": hashlib.sha256(Path(snapshot["manifest_path"]).read_bytes()).hexdigest(),
        "summaries": {"1:1:1": "Previously prepared text"},
    }))
    prepared = workflow.prepare(1, summaries)
    recipe_path = next((tmp_path / "data/stages/01/preparations").glob("rezept-*.json"))
    recipe = json.loads(recipe_path.read_text())
    del recipe["summaries_file_sha256"]
    recipe_path.write_text(json.dumps(recipe))

    def forbidden(*args, **kwargs):
        pytest.fail("valid legacy recipe must not need a new summary input")

    monkeypatch.setattr("hexatrek_exporter.preparation.prepare_export", forbidden)
    assert workflow.prepare(1) == prepared


def test_summary_input_requires_a_snapshot_without_creating_storage(tmp_path):
    workflow = Workflow(tmp_path / "data")
    output = tmp_path / "draft.json"
    with pytest.raises(ValueError, match="no snapshot"):
        workflow.write_summaries(1, output)
    assert not workflow.root.exists()
    assert not output.exists()


@pytest.mark.parametrize("location", ["snapshot", "stages", "exports"])
def test_summary_input_cannot_change_managed_artifacts(tmp_path, source, location):
    workflow = Workflow(tmp_path / "data")
    adopted = workflow.adopt_snapshot(source)
    stored = Path(adopted["manifest_path"])
    destinations = {"snapshot": stored.parent, "stages": workflow.root / "stages",
                    "exports": workflow.output_root}
    output = destinations[location] / "draft.json"
    before = stored.read_bytes()
    with pytest.raises(ValueError, match="outside managed stages and public exports"):
        workflow.write_summaries(1, output)
    assert not output.exists()
    assert stored.read_bytes() == before


@pytest.mark.parametrize("symlink", [False, True])
def test_summary_input_never_overwrites_existing_files_or_links(tmp_path, source, symlink):
    workflow = Workflow(tmp_path / "data")
    workflow.adopt_snapshot(source)
    target = tmp_path / "notes.json"
    target.write_text("Keep my edits")
    output = tmp_path / "draft.json"
    if symlink:
        output.symlink_to(target)
    else:
        output.write_text("Keep my draft")
    with pytest.raises(FileExistsError):
        workflow.write_summaries(1, output)
    assert target.read_text() == "Keep my edits"
    assert output.read_text() == ("Keep my edits" if symlink else "Keep my draft")


def test_summary_input_rejects_a_changed_stored_snapshot(tmp_path, source):
    workflow = Workflow(tmp_path / "data")
    adopted = workflow.adopt_snapshot(source)
    stored = Path(adopted["manifest_path"])
    stored.write_text(stored.read_text() + "\n")
    output = tmp_path / "draft.json"
    with pytest.raises(ValueError, match="Stored data changed"):
        workflow.write_summaries(1, output)
    assert not output.exists()
