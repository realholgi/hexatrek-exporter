"""Update CLI validation remains local and preserves inherited option defaults."""

from pathlib import Path

import pytest
import requests

from hexatrek_exporter import cli, hexatrek_session


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("CLI option handling must not make account or network requests")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(hexatrek_session, "sign_in_anonymously", forbidden)
    yield
    assert calls == []


@pytest.mark.parametrize("arguments, expected", [
    ([], "--stage"),
    (["--stage"], "expected at least one argument"),
    (["--stage", "0"], "positive integer"),
    (["--stage", "-1"], "positive integer"),
    (["--stage", "abc"], "positive integer"),
    (["--stage", "1", "--photo-mode", "download"], "invalid choice"),
    (["--stage", "1", "--target", "other"], "target must be"),
    (["--stage", "1", "--target", "osmand,osmand"], "must not be repeated"),
    (["--stage", "1", "--language", "../de"], "language"),
    (["--stage", "1", "2", "--resume", "update-id"], "--resume requires exactly one stage"),
    (["--stage", "1", "2", "--summaries-file", "review.json"],
     "--summaries-file requires exactly one stage"),
    (["--stage", "1", "--api-key", "secret"], "unrecognized arguments"),
])
def test_invalid_update_options_preserve_storage(tmp_path, capsys, arguments, expected):
    root = tmp_path / "data"
    root.mkdir()
    original = root / "original.json"
    original.write_bytes(b'{"immutable": true}\n')
    exports = tmp_path / "exports"
    exports.mkdir()
    track = exports / "route.gpx"
    track.write_bytes(b"<gpx/>\n")
    before = {path.relative_to(tmp_path): path.read_bytes()
              for path in tmp_path.rglob("*") if path.is_file()}

    with pytest.raises(SystemExit) as error:
        cli.main(["--data-root", str(root), "update", *arguments])

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err
    assert "usage:" in captured.err
    assert {path.relative_to(tmp_path): path.read_bytes()
            for path in tmp_path.rglob("*") if path.is_file()} == before
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == [
        "data", "data/original.json", "exports", "exports/route.gpx",
    ]


@pytest.mark.parametrize("configured", [False, True])
def test_update_defaults_inherit_without_enabling_generation(monkeypatch, configured):
    for name, value in [("HEXATREK_MODEL", "configured-model"),
                        ("HEXATREK_API_BASE", "https://model.invalid/v1")]:
        if configured:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)

    args = cli._parser().parse_args(["update", "--stage", "1", "2"])

    assert args.stage == [1, 2]
    assert args.session_file is None
    assert args.photo_mode is None
    assert args.target is None
    assert args.language is None
    assert args.generate is False
    assert args.summaries_file is None
    assert args.resume is None
    assert args.model == ("configured-model" if configured else None)
    assert args.api_base == ("https://model.invalid/v1" if configured else None)


@pytest.mark.parametrize("photo_mode", ["none", "files", "links"])
def test_update_accepts_explicit_options_and_normalizes_language(photo_mode, monkeypatch):
    monkeypatch.setenv("HEXATREK_MODEL", "unused-environment-model")
    monkeypatch.setenv("HEXATREK_API_BASE", "https://unused.invalid/v1")

    args = cli._parser().parse_args([
        "update", "--stage", "3", "--session-file", "private-session.json",
        "--photo-mode", photo_mode, "--target", "komoot,osmand", "--language", "EN-us",
        "--generate", "--model", "chosen-model", "--api-base", "https://chosen.invalid/v1",
        "--summaries-file", "review.json", "--resume", "technical-update-id",
    ])

    assert args.stage == [3]
    assert args.session_file == "private-session.json"
    assert args.photo_mode == photo_mode
    assert args.target == ("komoot", "osmand")
    assert args.language == "en-us"
    assert args.generate is True
    assert args.model == "chosen-model"
    assert args.api_base == "https://chosen.invalid/v1"
    assert args.summaries_file == Path("review.json")
    assert args.resume == "technical-update-id"


def test_update_help_describes_actual_usage_without_creating_storage(tmp_path, capsys):
    root = tmp_path / "data"

    with pytest.raises(SystemExit) as error:
        cli.main(["--data-root", str(root), "update", "--help"])

    assert error.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "update" in captured.out
    assert "--stage STAGE [STAGE ...]" in captured.out
    for option in ("--session-file", "--photo-mode", "--target", "--language", "--generate",
                   "--model", "--api-base", "--summaries-file", "--resume"):
        assert option in captured.out
    assert "inherit" in captured.out
    assert "technical update id" in captured.out
    assert not root.exists()
    assert list(tmp_path.iterdir()) == []
