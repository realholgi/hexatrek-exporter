import json
from html import escape
import xml.etree.ElementTree as ET

import pytest

from hexatrek_exporter.osmand_export import GPX, OSMAND, export_osmand


def test_colors_cover_each_real_edge_without_joining_disjoint_runs(tmp_path):
    points = [{'latitude': 48 + i / 1000, 'longitude': 7.0, 'elevation': 120 + i,
               'distance': i * 100, 'category': category} for i, category in enumerate([101, 201, 102, 301, 202, 202])]
    source = {'schema_version': 1, 'routes': [{'id': '1', 'name': 'Test', 'points': points, 'poi_ids': []}], 'pois': []}
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps(source))
    result = export_osmand(str(manifest), str(tmp_path / 'out'), '1')
    assert result['segments'] == {'tolerated': 2, 'forbidden': 2, 'unknown': 1}
    namespace = {'g': GPX, 'o': OSMAND}
    observed = []
    for status, color in [('tolerated', '#159447'), ('forbidden', '#d63131'), ('unknown', '#777777')]:
        root = ET.parse(tmp_path / 'out' / (status + '.gpx'))
        assert root.find('g:extensions/o:color', namespace).text == color
        assert root.find('g:trk/g:extensions/o:color', namespace).text == color
        assert root.find('g:trk/g:extensions/o:coloring_type', namespace).text == 'solid'
        for segment in root.findall('g:trk/g:trkseg', namespace):
            coords = [(p.get('lat'), p.get('lon'), p.find('g:ele', namespace).text) for p in segment]
            observed.extend(zip(coords, coords[1:]))
    expected = [(str(p['latitude']), str(p['longitude']), str(p['elevation'])) for p in points]
    assert sorted(observed) == sorted(zip(expected, expected[1:]))
    assert manifest.read_text() == json.dumps(source)


@pytest.mark.parametrize("include_photo", [False, True])
@pytest.mark.parametrize("summary", [None, "Wasser läuft; leichter Nachgeschmack gemeldet."])
def test_comments_and_photos_can_be_summarized_without_author_attribution(tmp_path, summary, include_photo):
    url = 'https://firebasestorage.googleapis.com/v0/b/photos/o/comment%2F1%2Fphoto?alt=media&token=a"b\'c'
    point = {'id': 'p', 'name': 'Fontaine', 'type': 'Source', 'latitude': 48, 'longitude': 7,
             'description': 'Full background ' * 100, 'comments': [
                 {'id': 'old', 'created_at': '2025-01-01T00:00:00Z', 'author': 'Old', 'text': 'Photo report'},
                 {'id': 'new', 'created_at': '2026-01-01T00:00:00Z', 'author': 'New', 'text': 'Current report'}]}
    if summary is not None:
        point["summary"] = summary
    source = {'routes': [{'id': '1', 'name': 'Test', 'points': [
        {'latitude': 48, 'longitude': 7, 'elevation': 100, 'distance': 0, 'category': 101},
        {'latitude': 48.001, 'longitude': 7, 'elevation': 101, 'distance': 100, 'category': 201}], 'poi_ids': ['p']}],
              'pois': [point], 'photos': [{'poi_id': 'p', 'comment_id': 'old', 'path': 'nonexistent.jpg',
                                          'sha256': 'unused', 'url': url}]}
    if not include_photo:
        source["photos"] = []
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps(source))
    result = export_osmand(str(manifest), str(tmp_path / 'out'), '1', include_photos=True)
    root = ET.parse(tmp_path / 'out/navigation.gpx')
    assert root.find(f'{{{GPX}}}wpt/{{{GPX}}}extensions/{{{OSMAND}}}icon').text == 'natural_spring'
    assert root.find(f'{{{GPX}}}wpt/{{{GPX}}}extensions/{{{OSMAND}}}color').text == '#237bc1'
    desc = root.find(f'{{{GPX}}}wpt/{{{GPX}}}desc').text
    if summary is None:
        assert point['description'] in desc
        assert desc.index('Current report') < desc.index('Photo report')
        if include_photo:
            assert desc.index('Photo report') < desc.index('<img src=')
    else:
        assert summary in desc
        assert point['description'] not in desc
        assert 'Current report' not in desc and 'Photo report' not in desc
        assert 'Letzte Meldung: 01.01.2026' in desc
        if include_photo:
            assert 'Foto zur Meldung vom 01.01.2025' in desc
    assert 'Old' not in desc and 'New' not in desc
    assert "Aktualität nicht bestätigt" not in desc
    assert "können fehlen" not in desc and "nicht geprüft" not in desc
    assert manifest.read_text() == json.dumps(source)
    assert 'data:image/' not in desc and 'base64' not in desc
    if include_photo:
        assert '<img src="' + escape(url, quote=True) + '"' in desc
        assert 'max-width:100%;height:auto' in desc
        assert '01.01.2025' in desc
        saved = json.loads((tmp_path / 'out/export.json').read_text())
        assert saved['photo_display'] == result['photo_display']
        assert sorted(path.name for path in (tmp_path / 'out').iterdir()) == sorted(result['files'] + ['export.json'])
    else:
        assert '<p>' not in desc and '<img' not in desc
    with pytest.raises(FileExistsError):
        export_osmand(str(manifest), str(tmp_path / 'out'), '1')


@pytest.fixture
def photo_manifest(tmp_path):
    source = {'routes': [{'id': '1', 'name': 'Test', 'points': [
        {'latitude': 48, 'longitude': 7, 'elevation': 100, 'distance': 0, 'category': 101},
        {'latitude': 48.001, 'longitude': 7, 'elevation': 101, 'distance': 100, 'category': 201}],
        'poi_ids': ['p']}], 'pois': [
            {'id': 'p', 'name': 'Source', 'type': 'Source', 'latitude': 48, 'longitude': 7,
             'comments': [{'id': 'c', 'created_at': '2026-01-01T00:00:00Z', 'text': 'Report'}]},
            {'id': 'other-route', 'name': 'Other', 'type': 'Source', 'latitude': 49, 'longitude': 8}],
        'photos': [{'poi_id': 'p', 'comment_id': 'c', 'url': 'https://photos.example/test.jpg'}]}
    manifest = tmp_path / 'manifest.json'
    return source, manifest


@pytest.mark.parametrize("malformed_photo", [False, True])
def test_default_export_keeps_summary_and_dates_without_any_photos(tmp_path, photo_manifest, malformed_photo):
    source, manifest = photo_manifest
    source["pois"][0]["summary"] = "Wasser läuft."
    source["pois"][0]["comments"][0]["photo_urls"] = ["https://photos.example/previous.jpg"]
    if malformed_photo:
        source["photos"] = [{"unused": "broken photo metadata"}]
    original = json.dumps(source)
    manifest.write_text(original)
    result = export_osmand(str(manifest), str(tmp_path / 'out'), '1')
    root = ET.parse(tmp_path / 'out/navigation.gpx')
    assert root.findtext(f'{{{GPX}}}wpt/{{{GPX}}}desc') == "Wasser läuft.\n\nLetzte Meldung: 01.01.2026"
    assert root.findall(f'{{{GPX}}}wpt/{{{GPX}}}link') == []
    content = (tmp_path / 'out/navigation.gpx').read_text()
    assert "<img" not in content and "photos.example" not in content and "data:image" not in content
    assert manifest.read_text() == original
    assert result["photo_display"] == "disabled"


@pytest.mark.parametrize("url", [
    "http://photos.example/test.jpg", "data:image/jpeg;base64,eA==", "/photo.jpg",
    "https:///photo.jpg", "https://user:password@photos.example/test.jpg",
    "https://photos.example/test\n.jpg", "https://photos.example/test\x00.jpg",
    "https://photos.example/test\x7f.jpg", "https://photos.example/test\x85.jpg",
    "https://photos.example:bad/test.jpg", "https://[invalid/test.jpg",
    "https://photos.example\\@other.example/test.jpg", 42,
])
def test_photo_urls_reject_malformed_or_insecure_values(tmp_path, photo_manifest, url):
    source, manifest = photo_manifest
    source['photos'][0]['url'] = url
    manifest.write_text(json.dumps(source))
    with pytest.raises(ValueError, match="Photo URL must be HTTPS"):
        export_osmand(str(manifest), str(tmp_path / 'out'), '1', include_photos=True)
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize("url", [None, ""])
def test_selected_files_only_photo_requires_links_refresh(tmp_path, photo_manifest, url):
    source, manifest = photo_manifest
    source['photos'][0] = {'poi_id': 'p', 'comment_id': 'c', 'path': '../missing.jpg', 'sha256': 'unused'}
    if url is not None:
        source['photos'][0]['url'] = url
    manifest.write_text(json.dumps(source))
    with pytest.raises(ValueError, match="refresh with photo_mode='links'"):
        export_osmand(str(manifest), str(tmp_path / 'out'), '1', include_photos=True)


def test_photo_must_belong_to_a_comment_on_its_poi(tmp_path, photo_manifest):
    source, manifest = photo_manifest
    source['photos'][0]['comment_id'] = 'wrong-comment'
    manifest.write_text(json.dumps(source))
    with pytest.raises(ValueError, match="comment_id does not match"):
        export_osmand(str(manifest), str(tmp_path / 'out'), '1', include_photos=True)


def test_unselected_route_photos_do_not_require_urls(tmp_path, photo_manifest):
    source, manifest = photo_manifest
    source['photos'].append({'poi_id': 'other-route', 'comment_id': 'missing', 'path': '../missing.jpg'})
    manifest.write_text(json.dumps(source))
    export_osmand(str(manifest), str(tmp_path / 'out'), '1', include_photos=True)
    root = ET.parse(tmp_path / 'out/navigation.gpx')
    descriptions = root.findall(f'{{{GPX}}}wpt/{{{GPX}}}desc')
    assert len(descriptions) == 1
    assert 'https://photos.example/test.jpg' in descriptions[0].text


@pytest.mark.parametrize("end_m, expected_names", [(None, ["Nearby", "Off trail"]), (100, ["Nearby"])])
def test_full_route_keeps_off_trail_pois_without_importing_other_routes(tmp_path, end_m, expected_names):
    source = {
        "routes": [{"id": "1", "name": "Main", "points": [
            {"latitude": 48, "longitude": 7, "elevation": 100, "distance": 0, "category": 101},
            {"latitude": 48.001, "longitude": 7, "elevation": 110, "distance": 100, "category": 101},
            {"latitude": 48.002, "longitude": 7, "elevation": 120, "distance": 200, "category": 101},
        ], "poi_ids": ["near", "far"]}],
        "pois": [
            {"id": identity, "name": name, "type": "Source", "latitude": latitude, "longitude": 7}
            for identity, name, latitude in [
                ("near", "Nearby", 48), ("far", "Off trail", 48.05), ("other", "Alternative only", 48)
            ]
        ],
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(source))
    export_osmand(str(manifest), str(tmp_path / "out"), "1", end_m=end_m)
    root = ET.parse(tmp_path / "out/navigation.gpx")
    waypoints = root.findall(f"{{{GPX}}}wpt")
    assert [point.find(f"{{{GPX}}}name").text for point in waypoints] == expected_names
    if end_m is None:
        assert waypoints[1].get("lat") == "48.05"
        assert waypoints[1].get("lon") == "7"


def test_conditions_are_readable_in_each_gpx_without_a_detail_package(tmp_path):
    source = {
        "routes": [{"id": "1", "name": "Test", "points": [
            {"latitude": 48 + i / 1000, "longitude": 7, "elevation": 100, "distance": i * 100,
             "category": category}
            for i, category in enumerate([102, 202, 999, 999])
        ], "poi_ids": []}],
        "pois": [],
        "point_types": [
            {"internalId": 102, "name": {"fr": "Parc Naturel", "en": "Natural Park"},
             "description": {"fr": "Une nuit seulement.\nRespecter les restrictions locales.",
                             "en": "One night only. Check local restrictions."}},
            {"internalId": 202, "name": {"fr": "Zone protégée"},
             "description": {"fr": "Bivouac strictement interdit."}},
        ],
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(source))
    result = export_osmand(str(manifest), str(tmp_path / "out"), "1")
    for filename in result["files"]:
        root = ET.parse(tmp_path / "out" / filename)
        for path in [f"{{{GPX}}}metadata/{{{GPX}}}desc", f"{{{GPX}}}trk/{{{GPX}}}desc"]:
            description = root.find(path).text
            assert "Une nuit seulement.\nRespecter les restrictions locales." in description
            assert "One night only. Check local restrictions." in description
            assert "Bivouac strictement interdit." in description
            assert "999" in description and "Originalbedingungen nicht verfügbar" in description
            assert "nicht allgemeine Campingerlaubnis" in description
            assert "Begleitpaket" not in description


@pytest.mark.parametrize("coverage", [
    "cache-only", "previously-retrieved-only", "cloud-complete", "unknown-provider",
])
def test_freshness_is_concise_without_technical_attribution_or_fake_recency(tmp_path, coverage):
    from datetime import datetime, timezone

    source = {
        "exported_at": "2025-01-02T10:00:00Z",
        "source": {"kind": "hexatrek-firestore-cache", "comments_coverage": coverage,
                   "comments_retrieved_at": "2025-01-01T10:00:00Z", "photos_coverage": "not-requested"},
        "routes": [{"id": "1", "name": "Test", "points": [
            {"latitude": 48, "longitude": 7, "elevation": 100, "distance": 0, "category": 101},
            {"latitude": 48.001, "longitude": 7, "elevation": 110, "distance": 100, "category": 101},
        ], "poi_ids": ["empty"]}],
        "pois": [{"id": "empty", "name": "Quelle", "type": "Source", "latitude": 48, "longitude": 7,
                  "description": "Original <source> & notes", "comments": []}],
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(source))
    before = datetime.now(timezone.utc)
    result = export_osmand(str(manifest), str(tmp_path / "out"), "1")
    after = datetime.now(timezone.utc)
    exported_at = datetime.fromisoformat(result["exported_at"].replace("Z", "+00:00"))
    assert before <= exported_at <= after
    for filename in result["files"]:
        root = ET.parse(tmp_path / "out" / filename)
        descriptions = [root.find(f"{{{GPX}}}metadata/{{{GPX}}}desc").text,
                        root.find(f"{{{GPX}}}trk/{{{GPX}}}desc").text]
        for description in descriptions:
            assert "OsmAnd-Export" not in description and "Quelle:" not in description
            assert "hexatrek-firestore-cache" not in description
            assert "2025-01-02T10:00:00Z" not in description
            assert result["exported_at"] not in description
            assert "Fotos" in description and "können fehlen" in description
            if coverage == "cloud-complete":
                assert "01.01.2025" in description and "vor Ort nicht aktuell überprüft" in description
            else:
                assert "Aktualität nicht bestätigt" in description
    point_text = ET.parse(tmp_path / "out/navigation.gpx").find(f"{{{GPX}}}wpt/{{{GPX}}}desc").text
    assert point_text == "Original <source> & notes"
