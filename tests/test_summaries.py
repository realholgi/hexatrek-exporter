"""Local text input is exact, source-bound and validated before any output."""

import hashlib
import json
import xml.etree.ElementTree as ET
import zipfile

import pytest

from hexatrek_exporter.poi_details import GPX
from hexatrek_exporter.preparation import prepare_export
from hexatrek_exporter.summaries import (
    load_summaries, normalize_language, summary_document, text_issues,
)
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


def test_summary_document_preserves_bytes_text_and_first_route_use(source):
    manifest = json.loads(source.read_text())
    manifest["pois"] = [
        {"id": "missing"},
        {"id": "blank", "summary": ""},
        {"id": "shared", "summary": "  Complete source text\n" + "😀" * 126},
        {"id": "unrelated", "summary": "Unused"},
    ]
    manifest["routes"][0]["poi_ids"] = ["shared", "blank", "shared"]
    manifest["routes"].append({"id": "alternative", "poi_ids": ["missing", "shared"]})
    raw = (json.dumps(manifest, ensure_ascii=False, indent=3) + "\r\n").encode("utf-8")
    source.write_bytes(raw)

    result = summary_document(source)

    assert result == {
        "schema_version": 1,
        "summary_language": "de",
        "source_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "summaries": {
            "shared": manifest["pois"][2]["summary"],
            "blank": "",
            "missing": None,
        },
    }
    assert list(result["summaries"]) == ["shared", "blank", "missing"]
    assert source.read_bytes() == raw


def test_summary_document_rejects_unknown_route_poi(source):
    manifest = json.loads(source.read_text())
    manifest["routes"][0]["poi_ids"].append("unknown")
    source.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="Unknown route POI ID: unknown"):
        summary_document(source)


def test_summary_issues_include_cached_supplied_and_unknown_once(tmp_path, source):
    manifest = json.loads(source.read_text())
    manifest["pois"] = [
        {"id": "missing-one"},
        {"id": "missing-two"},
        {"id": "cached-type", "summary": False},
        {"id": "cached-xml", "summary": "\x00"},
        {"id": "cached-long", "summary": "😀" * 126},
        {"id": "supplied-route", "summary": "Valid cache"},
        {"id": "replaced-cache", "summary": "\x00"},
        {"id": "supplied-unused"},
        {"id": "unused-cache", "summary": 123},
    ]
    used = [point["id"] for point in manifest["pois"][:7]]
    manifest["routes"][0]["poi_ids"] = used + used
    manifest["routes"].append({"id": "alternative", "poi_ids": used})
    source.write_text(json.dumps(manifest))
    supplied = {
        "supplied-route": "\ud800" + "X" * 250,
        "replaced-cache": "Valid replacement",
        "supplied-unused": None,
        "unknown-type": 123,
        "unknown-valid": "Valid text",
    }
    summaries = tmp_path / "summaries.json"
    summaries.write_text(json.dumps(document(source, supplied)))
    before = source.read_bytes()

    with pytest.raises(ValueError) as error:
        load_summaries(source, summaries)

    assert str(error.value).splitlines() == [
        "Missing prepared summary for missing-one; supply a local summaries file",
        "Missing prepared summary for missing-two; supply a local summaries file",
        "Summary for cached-type must be a string",
        "Summary for cached-xml must contain only valid XML 1.0 text",
        "Summary for cached-long exceeds 250 UTF-16 units; it was not truncated",
        "Summary for supplied-route must contain only valid XML 1.0 text",
        "Summary for supplied-route exceeds 250 UTF-16 units; it was not truncated",
        "Summary for supplied-unused must be a string",
        "Unknown POI summary ID: unknown-type",
        "Summary for unknown-type must be a string",
        "Unknown POI summary ID: unknown-valid",
    ]
    assert source.read_bytes() == before


@pytest.mark.parametrize("value,expected", [
    ("de", "de"),
    ("DE", "de"),
    ("eng", "eng"),
    ("EN-gb", "en-gb"),
    ("zh-Hant-TW", "zh-hant-tw"),
    ("de-1996", "de-1996"),
])
def test_language_tags_are_normalized(value, expected):
    assert normalize_language(value) == expected


@pytest.mark.parametrize("value", [
    None, 12, "", "d", "germ", " en", "en ", "en\n", "en US",
    "en_US", "de-üö", "de-x", "en-abcdefghi", "en--GB", "en/GB",
    "../de", "en-GB?", "en-\tGB",
])
def test_invalid_language_tags_are_rejected(value):
    with pytest.raises(ValueError, match="Summary language"):
        normalize_language(value)


@pytest.mark.parametrize("cached_language,selected,expected", [
    (None, "de", ""),
    (None, "fr", None),
    ("FR", "fr", ""),
    ("fr", "de", None),
    ("en-GB", "EN-gb", ""),
    ("en", "en-gb", None),
])
def test_summary_draft_only_prefills_matching_language(source, cached_language, selected, expected):
    manifest = json.loads(source.read_text())
    manifest["pois"][0]["summary"] = ""
    if cached_language is not None:
        manifest["summary_language"] = cached_language
    source.write_text(json.dumps(manifest))
    before = source.read_bytes()

    draft = summary_document(source, language=selected)

    assert draft["summary_language"] == selected.lower()
    assert draft["summaries"] == {"stable-poi": expected}
    assert draft["source_manifest_sha256"] == hashlib.sha256(before).hexdigest()
    assert source.read_bytes() == before


@pytest.mark.parametrize("cached_language,selected", [
    (None, "de"), ("de", "DE"), ("fr", "FR"), ("en-GB", "en-gb"),
])
def test_matching_language_reuses_exact_cache(source, cached_language, selected):
    manifest = json.loads(source.read_text())
    text = "  Cached text\n"
    manifest["pois"][0]["summary"] = text
    if cached_language is not None:
        manifest["summary_language"] = cached_language
    source.write_text(json.dumps(manifest))
    before = source.read_bytes()

    loaded, summaries, digest = load_summaries(source, language=selected)

    assert summaries == {"stable-poi": text}
    assert loaded == manifest
    assert digest is None
    assert source.read_bytes() == before


@pytest.mark.parametrize("cached_language,selected", [
    (None, "en"), ("fr", "de"), ("en", "en-gb"),
])
def test_other_language_cache_does_not_fill_missing_text(tmp_path, source, cached_language, selected):
    manifest = json.loads(source.read_text())
    manifest["pois"][0]["summary"] = "Cached text"
    if cached_language is not None:
        manifest["summary_language"] = cached_language
    source.write_text(json.dumps(manifest))
    supplied = document(source, {})
    supplied["summary_language"] = selected
    summaries = tmp_path / "summaries.json"
    summaries.write_text(json.dumps(supplied))

    for summary_input in (None, summaries):
        with pytest.raises(ValueError, match="Missing prepared summary for stable-poi"):
            load_summaries(source, summary_input, language=selected)


@pytest.mark.parametrize("supplied_language,selected", [
    (None, "en"), ("fr", "de"), ("de", "fr"), ("en", "en-gb"),
])
def test_supplied_language_mismatch_fails_before_output(tmp_path, source, supplied_language, selected):
    supplied = document(source, {"stable-poi": "Text"})
    if supplied_language is not None:
        supplied["summary_language"] = supplied_language
    summaries = tmp_path / "summaries.json"
    summaries.write_text(json.dumps(supplied))
    before = source.read_bytes()
    output = tmp_path / "absent" / "prepared"

    with pytest.raises(ValueError, match="summary_language.*does not match requested language"):
        prepare_export(source, output, summaries, language=selected)

    assert not output.parent.exists()
    assert source.read_bytes() == before


def test_selected_language_replaces_other_language_cache_without_changing_sources(tmp_path, source):
    manifest = json.loads(source.read_text())
    manifest["pois"][0]["summary"] = "Deutscher Cache"
    source.write_text(json.dumps(manifest))
    source_bytes = source.read_bytes()
    gpx_bytes = (source.parent / "source.gpx").read_bytes()
    supplied = document(source, {"stable-poi": "  English summary\n"})
    supplied["summary_language"] = "EN-gb"
    summaries = tmp_path / "summaries.json"
    summaries.write_text(json.dumps(supplied))

    loaded, effective, digest = load_summaries(source, summaries, language="en-GB")
    assert loaded == manifest
    assert effective == supplied["summaries"]
    assert digest == hashlib.sha256(summaries.read_bytes()).hexdigest()

    output = tmp_path / "prepared"
    prepare_export(source, output, summaries, language="en-GB")
    index = json.loads((output / "prepared.json").read_text())
    assert index["summary_language"] == "en-gb"
    assert index["source_manifest_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    route = index["routes"][0]
    assert ET.parse(output / route["gpx"]).findtext(f"{{{GPX}}}wpt/{{{GPX}}}desc") == "  English summary\n"
    with zipfile.ZipFile(output / route["details"]) as archive:
        assert archive.read("source.gpx") == gpx_bytes
        assert archive.read("source-manifest.json") == source_bytes
    assert source.read_bytes() == source_bytes


def test_public_text_validator_aggregates_xml_and_utf16_issues():
    assert text_issues("blank", "") == []
    assert text_issues("boundary", "😀" * 125) == []
    assert text_issues("bad", "\ud800" + "X" * 250) == [
        "Summary for bad must contain only valid XML 1.0 text",
        "Summary for bad exceeds 250 UTF-16 units; it was not truncated",
    ]


def test_invalid_requested_language_fails_before_output(tmp_path, source):
    output = tmp_path / "absent" / "prepared"
    before = source.read_bytes()

    with pytest.raises(ValueError, match="Summary language"):
        summary_document(source, language="en_US")
    with pytest.raises(ValueError, match="Summary language"):
        load_summaries(source, language="en_US")
    with pytest.raises(ValueError, match="Summary language"):
        prepare_export(source, output, language="en_US")

    assert not output.parent.exists()
    assert source.read_bytes() == before
