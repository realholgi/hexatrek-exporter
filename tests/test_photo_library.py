"""Photo library tests use only generated images and temporary local bundles."""

import base64
import copy
import hashlib
from io import BytesIO
from pathlib import Path
import shutil

from PIL import Image
import pytest

from hexatrek_exporter import photo_library as library
from hexatrek_exporter.workflow import Workflow, _inventory, _read, _verify, _write


def image_bytes(color="red"):
    output = BytesIO()
    Image.new("RGB", (1600, 1200), color).save(output, format="PNG")
    return output.getvalue()


def manifest():
    return {"schema_version": 1, "stage_id": 1, "source": {"photo_mode": "links"},
            "routes": [{"id": "main", "gpx": "route.gpx"}],
            "pois": [{"id": "1:0:7", "stage_id": 1, "group_id": 0, "internal_id": 7,
                      "comments": [{"id": "comment-a", "images": ["image.png"]}]}],
            "photos": []}


@pytest.fixture
def workflow(tmp_path):
    workflow = Workflow(tmp_path / "data")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _write(snapshot / "manifest.json", manifest())
    (snapshot / "route.gpx").write_bytes(b'<?xml version="1.0"?><gpx> original route bytes </gpx>\n')
    _write(snapshot / "inventory.json", _inventory(snapshot))
    workflow._snapshot = lambda stage, state: snapshot
    return workflow


def legacy_library(workflow, data=None, *, duplicates=1):
    data = image_bytes() if data is None else data
    root = workflow.root / "photos" / "stage-01"
    (root / "photos").mkdir(parents=True)
    (root / "source").mkdir()
    source = workflow._snapshot(1, {})
    shutil.copyfile(source / "manifest.json", root / "source-manifest.json")
    shutil.copyfile(source / "route.gpx", root / "source" / "route.gpx")
    digest = hashlib.sha256(data).hexdigest()
    name = f"photos/{digest}.png"
    (root / name).write_bytes(data)
    value = manifest()
    value["routes"][0]["gpx"] = "source/route.gpx"
    value["source"]["photo_mode"] = "files"
    value["photos"] = [{"poi_id": "1:0:7", "comment_id": f"comment-{index}",
                        "source_image": "image.png", "path": name, "sha256": digest}
                       for index in range(duplicates)]
    _write(root / "manifest.json", value)
    _write(root / "run.json", {"acquired_at": "original-acquisition-time",
                              "bytes": len(data), "unique_files": 1})
    _write(root / "inventory.json", _inventory(root))
    return root


def fake_cloud(monkeypatch, data):
    calls = {"metadata": [], "media": []}
    monkeypatch.setattr(library, "FirebaseSession", lambda path, http: object())

    def metadata(http, credentials, internal_id, name):
        calls["metadata"].append((internal_id, name))
        return {"md5Hash": base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()}

    def media(http, method, url, credentials, *, photo):
        assert method == "GET" and photo
        assert url == library._STORAGE_PREFIX + "comment%2F7%2Fimage.png?alt=media"
        calls["media"].append(url)
        return data

    monkeypatch.setattr(library, "_photo_metadata", metadata)
    monkeypatch.setattr(library, "_request", media)
    return calls


def test_offline_compaction_preserves_sources_and_original_fingerprints(workflow):
    original = image_bytes()
    root = legacy_library(workflow, original)
    source_bytes = (root / "source-manifest.json").read_bytes()
    route_bytes = (root / "source/route.gpx").read_bytes()
    result = library.manage_library(workflow, 1)
    assert result["photo_quality"] == "med"
    assert result["downloaded"] == 0
    assert result["references"] == result["unique_files"] == 1
    assert result["before_bytes"] == len(original)
    _verify(root)
    entry = _read(root / "manifest.json")["photos"][0]
    local = (root / entry["path"]).read_bytes()
    assert entry["sha256"] == hashlib.sha256(local).hexdigest()
    assert entry["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert entry["source_md5"] == base64.b64encode(hashlib.md5(original, usedforsecurity=False).digest()).decode()
    assert entry["source_size_bytes"] == len(original)
    assert entry["quality"] == "med"
    with Image.open(BytesIO(local)) as photo:
        assert photo.format == "JPEG" and photo.size == (800, 600)
    assert (root / "source-manifest.json").read_bytes() == source_bytes
    assert (root / "source/route.gpx").read_bytes() == route_bytes
    assert _read(root / "run.json")["acquired_at"] == "original-acquisition-time"
    assert _read(root / "run.json")["bytes"] == len(local)
    assert _read(root / "run.json")["unique_files"] == result["unique_files"]
    assert not list(root.parent.glob(".*photo-transaction"))
    assert not list((root / "photos").glob("*.png"))


def test_same_quality_is_exact_noop(workflow, monkeypatch):
    root = legacy_library(workflow)
    library.manage_library(workflow, 1)
    before = {path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
              for path in root.rglob("*") if path.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("same-quality run must not encode or write inventory")

    monkeypatch.setattr(library, "compress_photo", forbidden)
    monkeypatch.setattr(library, "_write", forbidden)
    library.manage_library(workflow, 1)
    assert before == {path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
                      for path in root.rglob("*") if path.is_file()}


def test_offline_upgrade_refused_without_changing_library(workflow):
    root = legacy_library(workflow)
    library.manage_library(workflow, 1, quality="lo")
    before = _inventory(root)
    with pytest.raises(ValueError):
        library.manage_library(workflow, 1, quality="hi")
    assert _inventory(root) == before
    _verify(root)
    assert not list(root.parent.glob(".*photo-transaction"))


def test_duplicate_references_share_one_local_payload(workflow):
    root = legacy_library(workflow, duplicates=20)
    result = library.manage_library(workflow, 1)
    assert result["references"] == 20 and result["unique_files"] == 1
    assert len(list((root / "photos").iterdir())) == 1
    assert result["bytes"] == next((root / "photos").iterdir()).stat().st_size


def test_initial_download_then_original_md5_reuses_compressed_photo(workflow, monkeypatch):
    original = image_bytes()
    calls = fake_cloud(monkeypatch, original)
    result = library.manage_library(workflow, 1, "synthetic-session")
    assert result["downloaded"] == 1
    root = Path(result["path"])
    before = _inventory(root)
    result = library.manage_library(workflow, 1, "synthetic-session")
    assert result["downloaded"] == 0
    assert len(calls["metadata"]) == 2 and len(calls["media"]) == 1
    assert _inventory(root) == before
    _verify(root)


def test_changed_cloud_body_replaces_original_fingerprint(workflow, monkeypatch):
    fake_cloud(monkeypatch, image_bytes("red"))
    result = library.manage_library(workflow, 1, "synthetic-session")
    root = Path(result["path"])
    old_entry = _read(root / "manifest.json")["photos"][0]
    replacement = image_bytes("blue")
    calls = fake_cloud(monkeypatch, replacement)
    result = library.manage_library(workflow, 1, "synthetic-session")
    entry = _read(root / "manifest.json")["photos"][0]
    assert result["downloaded"] == len(calls["media"]) == 1
    assert entry["source_sha256"] == hashlib.sha256(replacement).hexdigest()
    assert entry["source_md5"] != old_entry["source_md5"]
    assert len(list((root / "photos").iterdir())) == 1


def test_cloud_upgrade_redownloads_original(workflow, monkeypatch):
    original = image_bytes()
    calls = fake_cloud(monkeypatch, original)
    library.manage_library(workflow, 1, "synthetic-session", quality="lo")
    result = library.manage_library(workflow, 1, "synthetic-session", quality="hi")
    assert result["downloaded"] == 1 and len(calls["media"]) == 2
    entry = _read(Path(result["path"]) / "manifest.json")["photos"][0]
    assert entry["quality"] == "hi" and max(entry["width"], entry["height"]) == 1000


def test_bad_cloud_body_leaves_existing_library_usable(workflow, monkeypatch):
    root = legacy_library(workflow)
    before = _inventory(root)
    fake_cloud(monkeypatch, image_bytes("blue"))
    monkeypatch.setattr(library, "_request", lambda *args, **kwargs: image_bytes("green"))
    with pytest.raises(ValueError, match="original MD5"):
        library.manage_library(workflow, 1, "synthetic-session")
    assert _inventory(root) == before
    _verify(root)


def test_failed_candidate_rename_restores_previous_library(workflow, monkeypatch):
    root = legacy_library(workflow)
    before = _inventory(root)
    original_rename = Path.rename

    def fail_candidate(path, target):
        if path.name == "candidate":
            raise OSError("simulated rename failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_candidate)
    with pytest.raises(OSError, match="simulated"):
        library.manage_library(workflow, 1)
    assert _inventory(root) == before
    _verify(root)


def test_recovery_finishes_valid_interrupted_cutover(workflow):
    root = legacy_library(workflow)
    transaction = root.parent / ".stage-01-photo-transaction"
    transaction.mkdir()
    candidate = transaction / "candidate"
    shutil.copytree(root, candidate)
    inventory = _read(candidate / "inventory.json")
    _write(transaction / "run.json", {"owner": library._OWNER, "stage": 1,
                                     "candidate_inventory": inventory})
    root.rename(transaction / "previous")
    library._recover(root, transaction, 1)
    _verify(root)
    assert not transaction.exists()


def test_recovery_restores_old_library_when_candidate_is_incomplete(workflow):
    root = legacy_library(workflow)
    before = _inventory(root)
    transaction = root.parent / ".stage-01-photo-transaction"
    transaction.mkdir()
    _write(transaction / "run.json", {"owner": library._OWNER, "stage": 1,
                                     "candidate_inventory": {"incomplete": "candidate"}})
    (transaction / "candidate").mkdir()
    root.rename(transaction / "previous")
    library._recover(root, transaction, 1)
    assert _inventory(root) == before
    _verify(root)
    assert not transaction.exists()


@pytest.mark.parametrize("kind", ["untracked", "symlink", "tracked-unmanaged"])
def test_refuses_unmanaged_or_symlink_library_data(workflow, kind):
    root = legacy_library(workflow)
    if kind == "symlink":
        (root / "unexpected").symlink_to(root / "manifest.json")
    else:
        (root / "unexpected").write_text("do not delete")
        if kind == "tracked-unmanaged":
            _write(root / "inventory.json", _inventory(root))
    with pytest.raises(ValueError):
        library.manage_library(workflow, 1)
    assert (root / "unexpected").exists()


def test_missing_library_and_empty_references_fail_clearly(workflow, monkeypatch):
    with pytest.raises(ValueError, match="session"):
        library.manage_library(workflow, 1)
    source = workflow._snapshot(1, {})
    value = copy.deepcopy(manifest())
    value["pois"][0]["comments"] = []
    _write(source / "manifest.json", value)
    _write(source / "inventory.json", _inventory(source))
    monkeypatch.setattr(library, "FirebaseSession", lambda *args: pytest.fail("No network for empty references"))
    with pytest.raises(ValueError, match="no photo references"):
        library.manage_library(workflow, 1, "synthetic-session")


def test_cloud_duplicate_objects_download_once(workflow, monkeypatch):
    source = workflow._snapshot(1, {})
    value = manifest()
    value["pois"][0]["comments"].append({"id": "comment-b", "images": ["image.png"]})
    _write(source / "manifest.json", value)
    _write(source / "inventory.json", _inventory(source))
    calls = fake_cloud(monkeypatch, image_bytes())
    result = library.manage_library(workflow, 1, "synthetic-session")
    assert result["references"] == 2
    assert result["unique_files"] == result["downloaded"] == 1
    assert len(calls["metadata"]) == len(calls["media"]) == 1


def test_recovery_removes_previous_only_after_valid_publication(workflow):
    root = legacy_library(workflow)
    transaction = root.parent / ".stage-01-photo-transaction"
    transaction.mkdir()
    shutil.copytree(root, transaction / "previous")
    _write(transaction / "run.json", {"owner": library._OWNER, "stage": 1,
                                     "candidate_inventory": _read(root / "inventory.json")})
    library._recover(root, transaction, 1)
    _verify(root)
    assert not transaction.exists()


def test_recovery_refuses_unknown_transaction_contents(workflow):
    root = legacy_library(workflow)
    transaction = root.parent / ".stage-01-photo-transaction"
    transaction.mkdir()
    _write(transaction / "run.json", {"owner": library._OWNER, "stage": 1})
    (transaction / "personal-file").write_text("not managed")
    with pytest.raises(ValueError, match="unmanaged"):
        library.manage_library(workflow, 1)
    assert (transaction / "personal-file").read_text() == "not managed"
    _verify(root)
