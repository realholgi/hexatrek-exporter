"""Source preservation at the prepared-artifact interface."""
import hashlib
import json
import xml.etree.ElementTree as ET
import zipfile

from hexatrek_exporter.poi_details import GPX
from hexatrek_exporter.text_limits import utf16_units
from hexatrek_exporter.preparation import prepare_export


def test_preparation_keeps_full_source_and_separates_routes(tmp_path):
    full_name = 'Refuge très long au sommet du Grand Ballon'
    source = f'<gpx xmlns="{GPX}" version="1.1"><wpt lat="48" lon="7"><name>{full_name}</name><desc>Full original</desc><type>Refuge non Gardé</type></wpt><trk><trkseg><trkpt lat="48" lon="7"><ele>123</ele></trkpt><trkpt lat="48.1" lon="7.1"><ele>124</ele></trkpt></trkseg></trk></gpx>'
    (tmp_path / 'source.gpx').write_text(source)
    (tmp_path / 'alternative.gpx').write_text(source)
    manifest = {'schema_version': 1, 'source': {'comments_coverage': 'cache-only'},
                'pois': [{'id': '1:1:42', 'comments': []}],
                'routes': [{'id': '1', 'kind': 'main', 'name': 'Main', 'gpx': 'source.gpx', 'poi_ids': ['1:1:42']},
                           {'id': '11', 'kind': 'alternative', 'name': 'Alternative', 'gpx': 'alternative.gpx', 'poi_ids': ['1:1:42']}]}
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))

    summaries = tmp_path / "summaries.json"
    summaries.write_text(json.dumps({
        "schema_version": 1,
        "source_manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "summaries": {"1:1:42": "Unbewartete Hütte."},
    }))
    result = prepare_export(str(path), str(tmp_path / 'prepared'), summaries,
                            include_photos=True)
    index = json.loads((tmp_path / 'prepared/prepared.json').read_text())
    assert result['routes'] == 2
    assert [r['kind'] for r in index['routes']] == ['main', 'alternative']
    for route in index['routes']:
        alias = route['aliases']['1:1:42']
        assert utf16_units(alias) <= 30 and '42' in alias
        with zipfile.ZipFile(tmp_path / 'prepared' / route['details']) as archive:
            details = json.loads(archive.read('details.json'))
            assert details['pois'][0]['name'] == full_name
            assert details['pois'][0]['description'] == 'Full original'
        tree = ET.parse(tmp_path / 'prepared' / route['gpx'])
        assert tree.find(f'{{{GPX}}}wpt/{{{GPX}}}name').text == alias
        assert len(tree.findall(f'.//{{{GPX}}}trkpt')) == 2
    assert (tmp_path / 'source.gpx').read_text() == source




