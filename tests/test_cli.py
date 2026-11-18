"""CLI consumers use explicit local summaries and opt in to anonymous login."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock, Mock

import pytest

from hexatrek_exporter import cli, hexatrek_session
from hexatrek_exporter.hexatrek_export import export_stage
from test_hexatrek_export import cache_at, stage_entries


@pytest.fixture
def source_manifest(tmp_path):
    cache = cache_at(tmp_path, stage_entries())
    return Path(export_stage(str(cache), 1, str(tmp_path / "source"))["manifest_path"])


def test_cli_adopts_real_snapshot_and_reports_missing_summaries(tmp_path, source_manifest, capsys):
    root = tmp_path / "data"
    source_before = source_manifest.read_bytes()
    assert cli.main(["--data-root", str(root), "adopt", "--manifest", str(source_manifest)]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), dict)
    assert source_manifest.read_bytes() == source_before
    assert list(root.rglob("manifest.json"))

    assert cli.main(["--data-root", str(root), "status", "--stage", "1"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert isinstance(json.loads(captured.out), dict)

    assert cli.main(["--data-root", str(root), "prepare", "--stage", "1"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "summar" in captured.err.lower()
    assert "MCP" not in captured.err and "sampling" not in captured.err
    assert not list(root.rglob("prepared.json"))
    assert source_manifest.read_bytes() == source_before


def test_cli_missing_manifest_is_stderr_failure(tmp_path, capsys):
    missing = tmp_path / "missing-manifest.json"
    assert cli.main(["--data-root", str(tmp_path / "data"), "adopt", "--manifest", str(missing)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hexatrek:" in captured.err
    assert "manifest" in captured.err.lower() or "snapshot" in captured.err.lower()
    assert not list(tmp_path.rglob("manifest.json"))


@pytest.mark.parametrize("arguments, expected", [
    (["collect", "--stage", "0", "--cache-path", "/not/read"], "positive integer"),
    (["collect", "--stage", "abc", "--cache-path", "/not/read"], "positive integer"),
    (["export", "--stage", "1", "--target", "other"], "target"),
    (["prepare", "--stage", "1", "2", "--summaries-file", "/not/read"], "exactly one stage"),
    (["summaries", "--stage", "0", "--output", "/not/written"], "positive integer"),
    (["summaries", "--stage", "1"], "--output"),
    (["summaries", "--stage", "1", "2", "--output", "/not/written"], "unrecognized arguments"),
    (["status", "--format", "yaml"], "invalid choice"),
    (["summaries", "--stage", "1", "--output", "/not/written", "--format", "text"],
     "unrecognized arguments"),
])
def test_cli_invalid_requests_never_construct_workflow(monkeypatch, capsys, arguments, expected):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid CLI requests must not inspect workflow storage")
    monkeypatch.setattr(cli, "Workflow", forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err


def test_cli_summaries_for_uncollected_stage_fails_without_writing(tmp_path, capsys):
    root = tmp_path / "data"
    output = tmp_path / "summaries.json"
    assert cli.main([
        "--data-root", str(root), "summaries", "--stage", "7", "--output", str(output),
    ]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Stage 7 has no snapshot" in captured.err
    assert not output.exists()
    assert not root.exists()


def test_cli_generates_editable_summaries_without_overwriting_sources(tmp_path, source_manifest, capsys):
    root = tmp_path / "data"
    source_before = source_manifest.read_bytes()
    assert cli.main(["--data-root", str(root), "adopt", "--manifest", str(source_manifest)]) == 0
    adopted = json.loads(capsys.readouterr().out)
    stored = Path(adopted["manifest_path"])
    stored_before = stored.read_bytes()
    output = tmp_path / "summaries.json"
    arguments = ["--data-root", str(root), "summaries", "--stage", "1", "--output", str(output)]
    assert cli.main(arguments) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    manifest = json.loads(stored_before)
    assert json.loads(captured.out) == {
        "stage": 1, "summaries_path": str(output.resolve()),
        "pois": len(manifest["pois"]), "missing": len(manifest["pois"]),
    }
    template = json.loads(output.read_text())
    assert template["source_manifest_sha256"] == hashlib.sha256(stored_before).hexdigest()
    assert template["summaries"] == {poi["id"]: None for poi in manifest["pois"]}
    template["summaries"] = {poi["id"]: "" for poi in manifest["pois"]}
    output.write_text(json.dumps(template))
    edited_before = output.read_bytes()
    assert cli.main(arguments) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hexatrek:" in captured.err
    assert output.read_bytes() == edited_before
    assert cli.main([
        "--data-root", str(root), "prepare", "--stage", "1", "--summaries-file", str(output),
    ]) == 0
    assert len(json.loads(capsys.readouterr().out)["stages"]) == 1
    assert source_manifest.read_bytes() == source_before
    assert stored.read_bytes() == stored_before


def test_cli_text_status_for_empty_and_uncollected_stages_has_no_effects(tmp_path, capsys):
    root = tmp_path / "data"
    arguments = ["--data-root", str(root), "status", "--format", "text"]
    assert cli.main(arguments) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"Data root: {root}" in captured.out
    assert f"Exports root: {tmp_path / 'exports'}" in captured.out
    assert "No stages collected." in captured.out
    assert not root.exists()
    assert cli.main([*arguments, "--stage", "7"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Stage 07\n" in captured.out
    assert "Snapshot: missing" in captured.out
    assert "Preparation: missing" in captured.out
    assert "Visible exports: none" in captured.out
    assert not root.exists()
    assert not (tmp_path / "exports").exists()


def test_cli_status_reports_snapshot_preparation_and_only_visible_exports(
    tmp_path, source_manifest, capsys,
):
    manifest = json.loads(source_manifest.read_text())
    manifest["source"]["comments_retrieved_at"] = "2026-11-18T09:00:00Z"
    source_manifest.write_text(json.dumps(manifest))
    root = tmp_path / "data"
    prefix = ["--data-root", str(root)]
    assert cli.main([*prefix, "adopt", "--manifest", str(source_manifest)]) == 0
    capsys.readouterr()
    status_arguments = [*prefix, "status"]
    assert cli.main(status_arguments) == 0
    default_json = capsys.readouterr().out
    assert cli.main([*status_arguments, "--format", "json"]) == 0
    assert capsys.readouterr().out == default_json
    snapshot_status = json.loads(default_json)
    assert snapshot_status == cli.Workflow(root).status()
    assert cli.main([*status_arguments, "--format", "text"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Stage 01\n" in captured.out
    assert "Snapshot: 2 routes, 4 POIs" in captured.out
    assert f"Comments coverage: {manifest['source']['comments_coverage']}" in captured.out
    assert "Comments retrieved: 2026-11-18T09:00:00Z" in captured.out
    assert "Preparation: missing" in captured.out
    assert "Visible exports: none" in captured.out

    summaries = tmp_path / "summaries.json"
    assert cli.main([*prefix, "summaries", "--stage", "1", "--output", str(summaries)]) == 0
    capsys.readouterr()
    template = json.loads(summaries.read_text())
    template["summaries"] = dict.fromkeys(template["summaries"], "")
    summaries.write_text(json.dumps(template))
    assert cli.main([
        *prefix, "prepare", "--stage", "1", "--summaries-file", str(summaries),
    ]) == 0
    capsys.readouterr()
    assert cli.main([*status_arguments, "--format", "text"]) == 0
    captured = capsys.readouterr()
    assert "Preparation: available (" in captured.out
    assert "Visible exports: none" in captured.out

    assert cli.main([*prefix, "export", "--stage", "1", "--target", "osmand"]) == 0
    exported = json.loads(capsys.readouterr().out)["stages"][0]["result"]
    public_path = exported["outputs"]["osmand"]
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert cli.main([*status_arguments, "--format", "text"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"    osmand: {public_path}" in captured.out
    assert "komoot:" not in captured.out
    assert "\x1b" not in captured.out
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before

    stage_path = root / "stages" / "01" / "stage.json"
    state = json.loads(stage_path.read_text())
    state["visible_exports"] = {}
    stage_path.write_text(json.dumps(state))
    assert cli.main([*status_arguments, "--format", "text"]) == 0
    captured = capsys.readouterr()
    assert "Visible exports: none" in captured.out
    assert public_path not in captured.out


def test_cli_prepare_forwards_explicit_summary_file_synchronously(tmp_path, monkeypatch, capsys):
    workflow = Mock()
    workflow.prepare.return_value = {"prepared": "local"}
    create = Mock(return_value=workflow)
    monkeypatch.setattr(cli, "Workflow", create)
    summaries = tmp_path / "texts.json"
    root = tmp_path / "data"
    assert cli.main([
        "--data-root", str(root), "prepare", "--stage", "3",
        "--summaries-file", str(summaries), "--include-photos",
    ]) == 0
    create.assert_called_once_with(root)
    workflow.prepare.assert_called_once_with(3, summaries_file=summaries, include_photos=True)
    assert json.loads(capsys.readouterr().out) == {
        "stages": [{"stage": 3, "result": {"prepared": "local"}}],
    }


def test_cli_cached_prepare_keeps_multi_stage_support(tmp_path, monkeypatch, capsys):
    workflow = Mock()
    workflow.prepare.side_effect = [{"prepared": "first"}, {"prepared": "second"}]
    monkeypatch.setattr(cli, "Workflow", Mock(return_value=workflow))
    assert cli.main([
        "--data-root", str(tmp_path), "prepare", "--stage", "1", "2", "1",
    ]) == 0
    assert [call.args[0] for call in workflow.prepare.call_args_list] == [1, 2]
    assert all(call.kwargs == {"summaries_file": None, "include_photos": False}
               for call in workflow.prepare.call_args_list)
    assert len(json.loads(capsys.readouterr().out)["stages"]) == 2


def test_session_login_requires_confirmation_before_any_path_or_request(monkeypatch, capsys):
    forbidden = Mock(side_effect=AssertionError("unconfirmed login performed an action"))
    with monkeypatch.context() as patch:
        patch.setattr(cli, "Workflow", forbidden)
        patch.setattr(cli, "Path", forbidden)
        patch.setattr(hexatrek_session.os.path, "abspath", forbidden)
        patch.setattr(hexatrek_session.os.path, "expanduser", forbidden)
        patch.setattr(hexatrek_session.requests, "Session", forbidden)
        patch.setattr(hexatrek_session, "_request", forbidden)
        with pytest.raises(SystemExit) as error:
            cli.main(["session-login", "--api-key", "test-key", "--session-file", "/not/touched"])
    assert error.value.code == 2
    forbidden.assert_not_called()
    assert "--confirm" in capsys.readouterr().err


def test_session_login_forwards_options_without_constructing_workflow(monkeypatch, capsys):
    login = Mock(return_value={"session_file": "/private/session.json", "created": False})
    monkeypatch.setattr(hexatrek_session, "sign_in_anonymously", login)
    monkeypatch.setattr(cli, "Workflow", Mock(side_effect=AssertionError("unexpected workflow")))
    assert cli.main([
        "session-login", "--api-key", "explicit-key",
        "--session-file", "~/private/session.json", "--confirm",
    ]) == 0
    login.assert_called_once_with("explicit-key", "~/private/session.json")
    assert json.loads(capsys.readouterr().out)["created"] is False


def test_session_login_reuses_private_session_without_second_request(tmp_path, monkeypatch, capsys):
    request = Mock(return_value={
        "idToken": "test-id-token", "refreshToken": "test-refresh-token",
        "localId": "test-user", "expiresIn": "3600",
    })
    monkeypatch.setattr(hexatrek_session, "_request", request)
    monkeypatch.setattr(hexatrek_session.requests, "Session", MagicMock())
    session = tmp_path / "session.json"
    arguments = ["session-login", "--api-key", "test-key", "--session-file", str(session), "--confirm"]
    assert cli.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["created"] is True
    original = session.read_bytes()
    assert session.stat().st_mode & 0o777 == 0o600
    assert cli.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["created"] is False
    assert session.read_bytes() == original
    request.assert_called_once()


def test_fresh_cli_and_preparation_never_load_model_or_komoot_account_modules(tmp_path, source_manifest):
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root / "src"), env.get("PYTHONPATH")]))
    manifest = json.loads(source_manifest.read_text())
    summaries = tmp_path / "summaries.json"
    summaries.write_text(json.dumps({
        "schema_version": 1,
        "source_manifest_sha256": hashlib.sha256(source_manifest.read_bytes()).hexdigest(),
        "summaries": {poi["id"]: "Caller-provided complete text." for poi in manifest["pois"]},
    }))
    script = """
import importlib.abc
import sys
from pathlib import Path

blocked = ("mcp", "kompy", "gpxpy", "openrouteservice", "komoot_mcp")
class NoAccountOrModelImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Forbidden export dependency: " + fullname)
sys.meta_path.insert(0, NoAccountOrModelImports())

from hexatrek_exporter.cli import main
from hexatrek_exporter.preparation import prepare_export
manifest, summaries, temporary = sys.argv[1:]
temporary = Path(temporary)
prepare_export(manifest, temporary / "direct-preparation", summaries)
prefix = ["--data-root", str(temporary / "data")]
assert main(prefix + ["status"]) == 0
assert main(prefix + ["adopt", "--manifest", manifest]) == 0
assert main(prefix + ["prepare", "--stage", "1", "--summaries-file", summaries]) == 0
assert main(prefix + ["prepare", "--stage", "1"]) == 0
assert main(prefix + ["export", "--stage", "1"]) == 0
assert not any(name == prefix or name.startswith(prefix + ".")
               for name in sys.modules for prefix in blocked)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(source_manifest), str(summaries), str(tmp_path)],
        cwd=root, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
