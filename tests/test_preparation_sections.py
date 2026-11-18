"""Preparation retains complete source material while producing bounded sections."""

import io
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

import pytest

from hexatrek_exporter.poi_details import GPX
from hexatrek_exporter.preparation import prepare_export


def source_export(tmp_path, count=1, *, summaries=True, track_points=None):
    root = ET.Element(f"{{{GPX}}}gpx", version="1.1")
    pois = []
    for index in range(count):
        identity = f"1:1:{index}"
        # Off-track longitude must remain unchanged, not snapped to the track.
        point = ET.SubElement(root, f"{{{GPX}}}wpt", lat=str(index / 1000), lon="0.0001")
        ET.SubElement(point, f"{{{GPX}}}name").text = f"POI {index}"
        ET.SubElement(point, f"{{{GPX}}}desc").text = "Original français. " * 30
        ET.SubElement(point, f"{{{GPX}}}type").text = "Source"
        extension = ET.SubElement(point, f"{{{GPX}}}extensions")
        ET.SubElement(extension, "{urn:hexatrek-exporter:hexatrek:1}poi_id").text = identity
        poi = {"id": identity, "comments": []}
        if summaries:
            poi["summary"] = f"Quelle {index}; Wasser unsicher."
        pois.append(poi)
    segment = ET.SubElement(ET.SubElement(root, f"{{{GPX}}}trk"), f"{{{GPX}}}trkseg")
    for index in range(track_points if track_points is not None else count + 1):
        point = ET.SubElement(segment, f"{{{GPX}}}trkpt", lat=str(index / 1000), lon="0")
        ET.SubElement(point, f"{{{GPX}}}ele").text = str(index + 100)
    source = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    (tmp_path / "source.gpx").write_bytes(source)
    manifest = {"schema_version": 1, "source": {"comments_coverage": "cache-only"},
                "pois": pois, "routes": [{"id": "1", "kind": "main", "name": "Stage 1",
                                           "gpx": "source.gpx", "poi_ids": [p["id"] for p in pois]}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path, source, manifest


def test_sections_preserve_every_poi_and_continuous_full_geometry(tmp_path):
    path, source, manifest = source_export(tmp_path, 1025)
    output = tmp_path / "prepared"
    result = prepare_export(path, output)
    routes = json.loads((output / "prepared.json").read_text())["routes"]
    assert result["routes"] == 3
    assert [r["id"] for r in routes] == ["1-part-1", "1-part-2", "1-part-3"]
    assert [r["source_route_id"] for r in routes] == ["1"] * 3
    assert [r["section"]["number"] for r in routes] == [1, 2, 3]
    assert all(r["section"]["count"] == 3 for r in routes)
    identities, geometry, previous = [], [], None
    for route in routes:
        tree = ET.parse(output / route["gpx"])
        waypoints = tree.findall(f"{{{GPX}}}wpt")
        assert len(waypoints) <= 512
        assert set(route["aliases"]) == set(route["poi_ids"])
        identities.extend(route["poi_ids"])
        for identity, point in zip(route["poi_ids"], waypoints):
            assert point.get("lat") == str(int(identity.split(":")[-1]) / 1000)
            assert point.get("lon") == "0.0001"
        track = [(p.get("lat"), p.get("lon"), p.findtext(f"{{{GPX}}}ele"))
                 for p in tree.findall(f".//{{{GPX}}}trkpt")]
        if previous is not None:
            assert previous == track[0]
            geometry.extend(track[1:])
        else:
            geometry.extend(track)
        previous = track[-1]
        with zipfile.ZipFile(output / route["details"]) as archive:
            assert archive.read("source.gpx") == source
            details = json.loads(archive.read("details.json"))
            assert len(details["pois"]) == len(waypoints)
            assert all(p["description"] == "Original français. " * 30 for p in details["pois"])
    assert identities == manifest["routes"][0]["poi_ids"]
    assert len(set(identities)) == 1025
    original = ET.fromstring(source)
    assert geometry == [(p.get("lat"), p.get("lon"), p.findtext(f"{{{GPX}}}ele"))
                        for p in original.findall(f".//{{{GPX}}}trkpt")]
    assert (tmp_path / "source.gpx").read_bytes() == source


def test_offline_summaries_are_verbatim_and_source_details_are_lossless(tmp_path):
    path, source, _ = source_export(tmp_path)
    output = tmp_path / "prepared"
    result = prepare_export(path, output)
    index = json.loads((output / "prepared.json").read_text())
    route = index["routes"][0]
    assert route["id"] == "1"
    assert result["comments_coverage"] == "cache-only"
    assert ET.parse(output / route["gpx"]).findtext(f"{{{GPX}}}wpt/{{{GPX}}}desc") == "Quelle 0; Wasser unsicher."
    with zipfile.ZipFile(output / route["details"]) as archive:
        assert archive.read("source.gpx") == source
        assert json.loads(archive.read("details.json"))["pois"][0]["description"] == "Original français. " * 30


def test_missing_summary_fails_before_creating_output(tmp_path):
    path, _, _ = source_export(tmp_path, summaries=False)
    output = tmp_path / "absent-parent" / "prepared"
    with pytest.raises(ValueError, match="Missing prepared summary.*local summaries"):
        prepare_export(path, output)
    assert not output.parent.exists()


def test_utf16_limit_is_checked_without_truncation(tmp_path):
    path, _, manifest = source_export(tmp_path)
    manifest["pois"][0]["summary"] = "😀" * 126
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="250 UTF-16.*not truncated"):
        prepare_export(path, tmp_path / "prepared")
    assert not (tmp_path / "prepared").exists()


def test_supplied_summaries_override_cache_in_route_order(tmp_path):
    path, source, manifest = source_export(tmp_path, count=3)
    original_manifest = path.read_bytes()
    texts = {"1:1:2": "Dernier 🌊", "1:1:0": "Quelle; geprüft 2026-01-01"}
    summaries = tmp_path / "summaries.json"
    summaries.write_text(json.dumps({
        "schema_version": 1,
        "source_manifest_sha256": hashlib.sha256(original_manifest).hexdigest(),
        "summaries": texts,
    }))
    output = tmp_path / "prepared"
    result = prepare_export(path, output, summaries)
    route = json.loads(Path(result["prepared_path"]).read_text())["routes"][0]
    descriptions = [p.findtext(f"{{{GPX}}}desc") for p in ET.parse(output / route["gpx"]).findall(f"{{{GPX}}}wpt")]
    assert descriptions == [texts["1:1:0"], manifest["pois"][1]["summary"], texts["1:1:2"]]
    assert path.read_bytes() == original_manifest
    with zipfile.ZipFile(output / route["details"]) as archive:
        assert archive.read("source.gpx") == source
        assert archive.read("source-manifest.json") == original_manifest


def test_link_only_photos_fail_explicitly_without_publishing(tmp_path):
    path, _, manifest = source_export(tmp_path)
    manifest["photos"] = [{"poi_id": "1:1:0", "comment_id": "c", "url": "https://example.com/photo.jpg"}]
    path.write_text(json.dumps(manifest))
    output = tmp_path / "prepared"
    with pytest.raises(ValueError, match="photo_mode='files'.*not discarded"):
        prepare_export(path, output, include_photos=True)
    assert not output.exists()


def test_photo_free_default_records_omission_and_retains_link_metadata(tmp_path):
    path, _, manifest = source_export(tmp_path)
    photo = {"poi_id": "1:1:0", "comment_id": "c", "url": "https://example.com/photo.jpg"}
    manifest["photos"] = [photo]
    path.write_text(json.dumps(manifest))
    output = tmp_path / "prepared"
    prepare_export(path, output)
    index = json.loads((output / "prepared.json").read_text())
    assert index["photo_mode"] == "omitted"
    with zipfile.ZipFile(output / index["routes"][0]["details"]) as archive:
        details = json.loads(archive.read("details.json"))
        assert details["source_photos"] == [photo]
        assert json.loads(archive.read("source-manifest.json")) == manifest


def test_local_photo_uses_original_gpx_comment_order(tmp_path):
    path, _, manifest = source_export(tmp_path)
    date = "2026-01-01T00:00:00Z"
    # Identical timestamps: source order, not manifest IDs, determines indices.
    manifest["pois"][0]["comments"] = [
        {"id": "z", "created_at": date, "author": "A", "rating": "", "text": "Second"},
        {"id": "a", "created_at": date, "author": "A", "rating": "", "text": "First"},
    ]
    source_path = tmp_path / "source.gpx"
    tree = ET.fromstring(source_path.read_bytes())
    tree.find(f"{{{GPX}}}wpt/{{{GPX}}}desc").text = (
        "Original\n\nUser comments (newest first; retrieved now):\n\n"
        f"{date} | A | \nFirst\n\n{date} | A | \nSecond"
    )
    source_path.write_bytes(ET.tostring(tree, encoding="utf-8"))
    photo_path = tmp_path / "photo.jpg"
    photo_path.write_bytes(b"\xff\xd8\xfforiginal-photo")
    manifest["photos"] = [{"poi_id": "1:1:0", "comment_id": "z", "path": "photo.jpg", "source_image": "image"}]
    path.write_text(json.dumps(manifest))
    output = tmp_path / "prepared"
    prepare_export(path, output, include_photos=True)
    index = json.loads((output / "prepared.json").read_text())
    assert index["photo_mode"] == "local-files"
    with zipfile.ZipFile(output / index["routes"][0]["details"]) as archive:
        point = json.loads(archive.read("details.json"))["pois"][0]
        assert point["comments"][1]["text"] == "Second"
        assert point["photos"][0]["comment_id"] == 1
        assert archive.read(point["photos"][0]["path"]) == photo_path.read_bytes()


def test_cached_summaries_leave_archives_extendable(tmp_path):
    from hexatrek_exporter.poi_details import add_photos_to_bundle

    path, source, _ = source_export(tmp_path)

    output = tmp_path / "prepared"
    result = prepare_export(str(path), str(output))
    route = json.loads(Path(result["prepared_path"]).read_text())["routes"][0]
    archive_path = output / route["details"]
    extended = add_photos_to_bundle(archive_path.read_bytes(), [])["content"]
    with zipfile.ZipFile(io.BytesIO(extended)) as archive:
        assert archive.read("source.gpx") == source
        assert archive.read("source-manifest.json") == path.read_bytes()


def test_preparation_requires_identifiable_stage_before_creating_output(tmp_path):
    path, _, manifest = source_export(tmp_path)
    manifest["routes"][0]["id"] = "unknown"
    path.write_text(json.dumps(manifest))
    output = tmp_path / "absent-parent" / "prepared"
    with pytest.raises(ValueError, match="stage_id.*numeric main source route ID"):
        prepare_export(path, output)
    assert not output.parent.exists()


def test_preparation_uses_explicit_stage_with_nonnumeric_source_ids(tmp_path):
    path, source, manifest = source_export(tmp_path)
    manifest["stage_id"] = 2
    manifest["routes"][0]["id"] = "source-main"
    manifest["routes"].append(dict(manifest["routes"][0], id="source-alternative",
                                   kind="alternative", name="La Traversée du Jura"))
    path.write_text(json.dumps(manifest))
    output = tmp_path / "prepared"
    result = prepare_export(path, output)
    routes = json.loads(Path(result["prepared_path"]).read_text())["routes"]
    assert len({route["gpx"] for route in routes}) == 2
    for route in routes:
        assert (output / route["gpx"]).parent.is_dir()
        with zipfile.ZipFile(output / route["details"]) as archive:
            assert archive.read("source.gpx") == source
            assert archive.read("source-manifest.json") == path.read_bytes()
