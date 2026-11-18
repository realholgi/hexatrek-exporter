"""Photo quality validation and real offline library compression through the CLI."""

import hashlib
import json

from PIL import Image
import pytest
import requests

from hexatrek_exporter import cli, hexatrek_session
from hexatrek_exporter.workflow import _inventory


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Photo CLI tests must stay offline")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(hexatrek_session, "sign_in_anonymously", forbidden)
    yield
    assert calls == []


def command_arguments(command):
    arguments = [command, "--stage", "1", "2"]
    if command == "refresh":
        arguments += ["--session-file", "private-session.json"]
    return arguments


@pytest.mark.parametrize("command, default", [("refresh", "med"), ("photos", "med"), ("update", None)])
def test_photo_quality_defaults_and_stage_selection(command, default):
    args = cli._parser().parse_args(command_arguments(command))
    assert args.photo_quality == default
    assert args.stage == [1, 2]
    if command == "photos":
        assert args.session_file is None


@pytest.mark.parametrize("command", ["refresh", "photos", "update"])
@pytest.mark.parametrize("quality", ["hi", "med", "lo"])
def test_photo_quality_choices(command, quality):
    args = cli._parser().parse_args(command_arguments(command) + ["--photo-quality", quality])
    assert args.photo_quality == quality


def test_photos_accepts_existing_session():
    args = cli._parser().parse_args([
        "photos", "--stage", "1", "--session-file", "private-session.json",
    ])
    assert args.session_file == "private-session.json"


@pytest.mark.parametrize("command", ["refresh", "photos", "update"])
@pytest.mark.parametrize("options, diagnostic", [
    (["--photo-quality", "medium"], "invalid choice"),
    (["--quality", "hi"], "unrecognized arguments"),
])
def test_invalid_photo_quality_cannot_touch_storage(tmp_path, monkeypatch, capsys, command, options, diagnostic):
    root = tmp_path / "data"
    root.mkdir()
    sentinel = root / "original.json"
    sentinel.write_bytes(b'{"immutable": true}\n')
    before = {path.relative_to(tmp_path): path.read_bytes()
              for path in tmp_path.rglob("*") if path.is_file()}
    before_paths = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid arguments must not construct Workflow")

    monkeypatch.setattr(cli, "Workflow", forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(["--data-root", str(root), *command_arguments(command), *options])

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert diagnostic in captured.err
    assert {path.relative_to(tmp_path): path.read_bytes()
            for path in tmp_path.rglob("*") if path.is_file()} == before
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before_paths


@pytest.mark.parametrize("stage_arguments", [[], ["--stage", "0"], ["--stage", "no"]])
def test_photos_requires_positive_stage_without_creating_storage(tmp_path, capsys, stage_arguments):
    root = tmp_path / "data"
    with pytest.raises(SystemExit) as error:
        cli.main(["--data-root", str(root), "photos", *stage_arguments])
    assert error.value.code == 2
    diagnostic = "--stage" if not stage_arguments else "stage must be a positive integer"
    assert diagnostic in capsys.readouterr().err
    assert not root.exists()


@pytest.mark.parametrize("command", ["refresh", "photos", "update"])
def test_photo_quality_help_lists_presets_without_writing(tmp_path, capsys, command):
    with pytest.raises(SystemExit) as error:
        cli.main(["--data-root", str(tmp_path / "data"), command, "--help"])
    assert error.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    help_text = " ".join(captured.out.split())
    assert "--photo-quality {hi,med,lo}" in help_text
    for preset in ("hi 1000px/JPEG55", "med 800px/JPEG50", "lo 640px/JPEG45", "longest side"):
        assert preset in help_text
    assert ("inherit selected quality, otherwise med" if command == "update" else "default: med") in help_text
    assert list(tmp_path.iterdir()) == []


def create_legacy_library(root, stage):
    library = root / "photos" / f"stage-{stage:02d}"
    photos = library / "photos"
    photos.mkdir(parents=True)
    temporary = photos / "original.jpg"
    Image.new("RGB", (1600, 1000), color=(17, 81, 143)).save(temporary, format="JPEG", quality=95)
    original = temporary.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    temporary.rename(photos / f"{digest}.jpg")
    manifest = {
        "schema_version": 1,
        "stage_id": stage,
        "source": {"photo_mode": "files"},
        "routes": [],
        "pois": [],
        "photos": [{"poi_id": f"{stage}:0:1", "comment_id": "comment",
                    "source_image": "image.jpg", "path": f"photos/{digest}.jpg", "sha256": digest}],
    }
    source = json.dumps(manifest).encode()
    (library / "manifest.json").write_bytes(source)
    (library / "source-manifest.json").write_bytes(source)
    (library / "run.json").write_text("{}")
    (library / "inventory.json").write_text(json.dumps(_inventory(library)))
    return library, source, original


@pytest.mark.parametrize("quality, expected, longest_side", [(None, "med", 800), ("hi", "hi", 1000),
                                                           ("med", "med", 800), ("lo", "lo", 640)])
def test_photos_compresses_real_offline_libraries_for_unique_stages(tmp_path, capsys, quality, expected, longest_side):
    root = tmp_path / "data"
    libraries = [create_legacy_library(root, stage) for stage in (1, 2)]
    public = tmp_path / "exports" / "route.gpx"
    public.parent.mkdir()
    public.write_bytes(b'<gpx version="1.1"/>\n')
    public_before = public.read_bytes()
    arguments = ["--data-root", str(root), "photos", "--stage", "2", "1", "2"]
    if quality is not None:
        arguments += ["--photo-quality", quality]

    assert cli.main(arguments) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    results = json.loads(captured.out)["stages"]
    assert [row["stage"] for row in results] == [2, 1]
    for row in results:
        assert row["result"]["photo_quality"] == expected
        assert row["result"]["downloaded"] == 0
        assert row["result"]["references"] == 1
        assert row["result"]["unique_files"] == 1
    for library, source, original in libraries:
        manifest = json.loads((library / "manifest.json").read_text())
        cached = library / manifest["photos"][0]["path"]
        compressed = cached.read_bytes()
        assert len(compressed) < len(original)
        assert manifest["photos"][0]["sha256"] == hashlib.sha256(compressed).hexdigest()
        with Image.open(cached) as image:
            assert image.format == "JPEG"
            assert image.size == (longest_side, longest_side * 5 // 8)
        assert (library / "source-manifest.json").read_bytes() == source
        assert json.loads((library / "inventory.json").read_text()) == _inventory(library)
    assert public.read_bytes() == public_before
