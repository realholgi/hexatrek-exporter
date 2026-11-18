"""Local text input is exact, source-bound and validated before any output."""

import hashlib
import json
import xml.etree.ElementTree as ET

import pytest

from hexatrek_exporter.poi_details import GPX
from hexatrek_exporter.preparation import prepare_export
from hexatrek_exporter.summaries import load_summaries
from hexatrek_exporter.text_limits import MAX_DESCRIPTION_UNITS, utf16_units


@pytest.fixture
def source(tmp_path):
    tree = ET.Element(f"{{{GPX}}}gpx", version="1.1")
    point = ET.SubElement(tree, f"{{{GPX}}}wpt", lat="48", lon="7")
    ET.SubElement(point, f"{{{GPX}}}name").text = "Water"
    ET.SubElement(point, f"{{{GPX}}}desc").text = "Original source"
    (tmp_path / "source.gpx").write_bytes(ET.tostring(tree, encoding="utf-8"))
    manifest = {"schema_version": 1, "stage_id": 1,
                "source": {"comments_coverage": "cache-only"},
                "pois": [{"id": "stable-poi", "comments": []}],
                "routes": [{"id": "main", "kind": "main", "name": "Stage 1",
                            "gpx": "source.gpx", "poi_ids": ["stable-poi"]}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def document(source, texts):
    return {"schema_version": 1,
            "source_manifest_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "summaries": texts}


@pytest.mark.parametrize("case,message", [
    ("stale", "does not match"),
    ("unknown", "Unknown POI"),
    ("duplicate", "Duplicate JSON key"),
    ("duplicate-root", "Duplicate JSON key"),
    ("missing", "Missing prepared summary"),
    ("overlong", "250 UTF-16.*not truncated"),
    ("non-string", "must be a string"),
    ("nul", "valid XML 1.0 text"),
    ("control", "valid XML 1.0 text"),
    ("surrogate", "valid XML 1.0 text"),
])
def test_invalid_input_leaves_no_output_or_parent(tmp_path, source, case, message):
    value = document(source, {"stable-poi": "Complete text"})
    if case == "stale":
        value["source_manifest_sha256"] = "0" * 64
    elif case == "unknown":
        value["summaries"]["unknown"] = "Unknown text"
    elif case == "missing":
        value["summaries"] = {}
    elif case == "overlong":
        value["summaries"]["stable-poi"] = "😀" * 126
    elif case == "non-string":
        value["summaries"]["stable-poi"] = 123
    elif case in {"nul", "control", "surrogate"}:
        value["summaries"]["stable-poi"] = {"nul": "\x00", "control": "\x01", "surrogate": "\ud800"}[case]
    raw = json.dumps(value)
    if case == "duplicate":
        raw = raw.replace('"stable-poi": "Complete text"', '"stable-poi": "first", "stable-poi": "second"')
    elif case == "duplicate-root":
        raw = raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    summaries = tmp_path / "summaries.json"
    summaries.write_text(raw)
    before = source.read_bytes()
    output = tmp_path / "absent" / "prepared"
    with pytest.raises(ValueError, match=message):
        prepare_export(source, output, summaries)
    assert not output.parent.exists()
    assert source.read_bytes() == before


@pytest.mark.parametrize("text", ["", "😀" * 125, "X" * 250])
def test_boundary_and_blank_text_are_preserved(tmp_path, source, text):
    summaries = tmp_path / "summaries.json"
    summaries.write_text(json.dumps(document(source, {"stable-poi": text})))
    manifest, effective, digest = load_summaries(source, summaries)
    assert effective == {"stable-poi": text}
    assert "summary" not in manifest["pois"][0]
    assert digest == hashlib.sha256(summaries.read_bytes()).hexdigest()
    assert utf16_units(text) <= MAX_DESCRIPTION_UNITS
    output = tmp_path / "prepared"
    result = prepare_export(source, output, summaries)
    index = json.loads((output / "prepared.json").read_text())
    assert result["pois"] == 1
    assert ET.parse(output / index["routes"][0]["gpx"]).findtext(f"{{{GPX}}}wpt/{{{GPX}}}desc") == text


def test_supplied_text_can_replace_invalid_cached_text(tmp_path, source):
    manifest = json.loads(source.read_text())
    manifest["pois"][0]["summary"] = "😀" * 126
    source.write_text(json.dumps(manifest))
    summaries = tmp_path / "summaries.json"
    summaries.write_text(json.dumps(document(source, {"stable-poi": "Valid replacement"})))
    assert load_summaries(source, summaries)[1] == {"stable-poi": "Valid replacement"}
