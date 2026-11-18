"""Offline fixtures exercise the same cache/manifest interfaces consumers use."""
import copy
import json
from pathlib import Path
import struct
import xml.etree.ElementTree as ET

import pytest

from hexatrek_exporter.hexatrek_export import CacheError, export_stage, load_documents, write_manifest_export


PREFIX = 'projects/hexatrek-44801/databases/(default)/documents/'
NS = {'g': 'http://www.topografix.com/GPX/1/1', 'h': 'urn:hexatrek-exporter:hexatrek:1'}


def vint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def pb(number, value):
    if isinstance(value, int):
        return vint(number << 3) + vint(value)
    return vint(number << 3 | 2) + vint(len(value)) + value


def encoded_value(value):
    if value is None:
        return pb(11, 0)
    if isinstance(value, str):
        return pb(17, value.encode())
    if isinstance(value, bool):
        return pb(1, int(value))
    if isinstance(value, int):
        return pb(2, value)
    if isinstance(value, float):
        return vint(3 << 3 | 1) + struct.pack('<d', value)
    if isinstance(value, list):
        return pb(9, b''.join(pb(1, encoded_value(v)) for v in value))
    return pb(6, b''.join(pb(1, pb(1, k.encode()) + pb(2, encoded_value(v))) for k, v in value.items()))


def document(name, fields):
    raw = pb(1, (PREFIX + name).encode())
    for key, value in fields.items():
        raw += pb(2, pb(1, key.encode()) + pb(2, encoded_value(value)))
    return pb(2, raw)


def remote_key(name):
    collection, identity = name.split('/')
    return b'\x85remote_document\0\1\xbe' + collection.encode() + b'\0\1\xbe' + identity.encode() + b'\0\1\x80'


def masked_crc(data):
    crc = 0xffffffff
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82f63b78 if crc & 1 else 0)
    crc ^= 0xffffffff
    return (((crc >> 15) | (crc << 17)) + 0xa282ead8) & 0xffffffff


def physical(payload, kind=1):
    crc = masked_crc(bytes([kind]) + payload)
    return struct.pack('<IHB', crc, len(payload), kind) + payload


def batch(sequence, entries):
    result = struct.pack('<QI', sequence, len(entries))
    for name, fields in entries:
        key = remote_key(name)
        result += bytes([0 if fields is None else 1]) + vint(len(key)) + key
        if fields is not None:
            raw = document(name, fields)
            result += vint(len(raw)) + raw
    return physical(result)


def cache_at(tmp_path, entries):
    root = tmp_path / 'cache'
    root.mkdir()
    (root / 'CURRENT').write_text('MANIFEST-000001\n')
    (root / 'MANIFEST-000001').write_bytes(physical(vint(2) + vint(2)))
    (root / '000002.log').write_bytes(batch(100, entries))
    return root


def point(identity, route=1, lon=7.0):
    return {'internalId': identity, 'stageId': route, 'groupId': 1,
            'category': 301 if route == 11 else 101, 'latitude': 48.0,
            'longitude': lon, 'elevation': 123.5, 'distance': identity * 10.0}


def poi(identity, route=1, name=None, subtype=51):
    value = {'internalId': identity, 'stageId': route, 'groupId': 1,
             'latitude': 48.0, 'longitude': 7.0, 'category': 5, 'subCategory': subtype,
             'description': {'fr': 'Texte français intégral & <détails>\n' + 'x' * 1000},
             'extra': {'keep': [1, 2, 3]}, 'url': 'https://example.org/details'}
    if name is not None:
        value['name'] = {'fr': name}
    return value


def stage_entries():
    return [
        ('Trek/trek', {'stages': [{'internalId': 1, 'name': {'fr': 'Etape 1 – Le Grand Est'},
                                 'startPoint': 'Wissembourg', 'endPoint': 'Nyon', 'distance': 670167}]}),
        ('TrekStagePointType/alternative', {'internalId': 301, 'subStagePointType': [
            {'internalId': 11, 'name': {'fr': 'Les Crêtes du Jura Suisse'}}]}),
        ('TrekStageInterestPointType/water', {'type': 51, 'typeName': {'fr': 'Fontaine'}}),
        ('TrekStageInterestPointType/shelter', {'type': 21, 'typeName': {'fr': 'Refuge non Gardé'}}),
        ('TrekStageOfflinePoint/b', {'trekStageId': 1, 'points': [point(3), point(1), point(900, 11, 6.0)]}),
        ('TrekStageOfflinePoint/a', {'trekStageId': 1, 'points': [point(2), point(901, 11, 6.001)]}),
        ('TrekStageOfflineInterestPoint/a', {'trekStageId': 1, 'interestPoints': [
            poi(1000), poi(1001, name='Même nom'), poi(1002, name='Même nom', subtype=21), poi(1003, 11)]}),
        ('Comment/old', {'createdAt': {'seconds': 1700000000}, 'trekStageId': 1, 'trekStageGroupId': 1,
                         'trekStageInterestPointInternalId': 1000, 'user': {'name': 'Ancien'}, 'rating': 4.0,
                         'text': 'Ancien commentaire complet', 'images': ['old.jpg']}),
        ('Comment/new', {'createdAt': {'seconds': 1800000000}, 'trekStageId': 1, 'trekStageGroupId': 1,
                         'trekStageInterestPointInternalId': 1000, 'user': {'name': 'Récent'}, 'rating': 5.0,
                         'text': 'Nouveau commentaire\n' + 'z' * 1500, 'images': []}),
    ]


def test_export_separates_alternative_without_connector_and_preserves_full_details(tmp_path):
    cache = cache_at(tmp_path, stage_entries())
    before = {p.name: p.read_bytes() for p in cache.iterdir()}
    output = tmp_path / 'export'
    result = export_stage(str(cache), 1, str(output))
    manifest = json.loads(Path(result['manifest_path']).read_text())
    assert result['route_count'] == 2
    assert result['point_count'] == 5
    assert result['poi_count'] == 4
    assert manifest['stage']['distance'] == 670167
    main, alternative = manifest['routes']
    assert main['name'] == 'HexaTrek – Stage 1 – Le Grand Est'
    assert alternative['name'] == 'Les Crêtes du Jura Suisse'
    assert [p['internalId'] for p in main['points']] == [1, 2, 3]
    assert [p['stageId'] for p in alternative['points']] == [11, 11]
    for route, count, longitude in ((main, 3, 7.0), (alternative, 2, 6.0)):
        tree = ET.parse(output / route['gpx'])
        track = tree.findall('.//g:trkpt', NS)
        assert len(track) == count
        assert float(track[0].get('lon')) == longitude
        assert len(tree.findall('.//g:trkseg', NS)) == 1
        assert tree.find('.//g:ele', NS).text == '123.5'
    unnamed, water, shelter, alt_poi = manifest['pois']
    assert unnamed['name'] == 'Fontaine – 1:1:1000'
    assert unnamed['name_derived'] is True
    assert unnamed['raw']['extra'] == {'keep': [1, 2, 3]}
    assert water['name'] != shelter['name']
    assert water['raw']['name'] == shelter['raw']['name'] == {'fr': 'Même nom'}
    assert alt_poi['id'] not in main['poi_ids']
    assert [c['id'] for c in unnamed['comments']] == ['new', 'old']
    waypoint = ET.parse(output / main['gpx']).find('g:wpt', NS)
    description = waypoint.find('g:desc', NS).text
    assert unnamed['description'] in description
    assert 'z' * 1500 in description
    assert description.index('Nouveau commentaire') < description.index('Ancien commentaire')
    assert 'User comments (newest first; retrieved ' in description
    assert waypoint.find('g:extensions/h:poi_id', NS).text == unnamed['id']
    assert waypoint.find('g:link', NS).get('href') == unnamed['links'][0]
    assert manifest['source']['comments_coverage'] == 'cache-only'
    assert before == {p.name: p.read_bytes() for p in cache.iterdir()}


def test_latest_sequences_and_tombstones_win_independently_of_file_order(tmp_path):
    cache = cache_at(tmp_path, [('Trek/a', {'name': 'old'}), ('Trek/b', {'name': 'old'})])
    # Filename order is not sequence order. Batch offsets matter too.
    (cache / '000003.log').write_bytes(batch(50, [('Trek/a', {'name': 'obsolete'})]))
    (cache / '000004.log').write_bytes(batch(101, [('Trek/a', {'name': 'new'}), ('Trek/b', None)]))
    docs = load_documents(str(cache))
    assert docs == {PREFIX + 'Trek/a': {'name': 'new'}}


@pytest.mark.parametrize('damage', ['checksum', 'truncation', 'protobuf'])
def test_corrupt_cache_is_explicit_and_does_not_create_export(tmp_path, damage):
    cache = cache_at(tmp_path, stage_entries())
    log = cache / '000002.log'
    raw = log.read_bytes()
    if damage == 'checksum':
        raw = raw[:-1] + bytes([raw[-1] ^ 1])
    elif damage == 'truncation':
        raw = raw[:-1]
    else:
        key = remote_key('Trek/broken')
        payload = struct.pack('<QI', 900, 1) + b'\x01' + vint(len(key)) + key + vint(1) + b'\xff'
        raw = physical(payload)
    log.write_bytes(raw)
    output = tmp_path / 'bad-export'
    with pytest.raises((CacheError, UnicodeDecodeError)):
        export_stage(str(cache), 1, str(output))
    assert not output.exists()


@pytest.mark.parametrize('mutation', ['unknown_alternative', 'duplicate_point', 'bad_coordinate', 'unreadable_group'])
def test_strict_stage_source_checks(tmp_path, mutation):
    entries = stage_entries()
    if mutation == 'unknown_alternative':
        entries[1][1]['subStagePointType'] = []
    elif mutation == 'duplicate_point':
        entries[5][1]['points'].append(point(1))
    elif mutation == 'bad_coordinate':
        entries[5][1]['points'][0]['longitude'] = 181
    else:
        del entries[5][1]['points']
    cache = cache_at(tmp_path, entries)
    with pytest.raises(CacheError):
        export_stage(str(cache), 1, str(tmp_path / 'export'))


def test_export_refuses_existing_directory_without_changes(tmp_path):
    cache = cache_at(tmp_path, stage_entries())
    output = tmp_path / 'existing'
    output.mkdir()
    sentinel = output / 'stage-0.gpx'
    sentinel.write_text('untouched')
    with pytest.raises(FileExistsError):
        export_stage(str(cache), 1, str(output))
    assert sentinel.read_text() == 'untouched'
    assert list(output.iterdir()) == [sentinel]


def test_shared_renderer_preserves_raw_manifest_and_uses_refresh_timestamp(tmp_path):
    cache = cache_at(tmp_path, stage_entries())
    result = export_stage(str(cache), 1, str(tmp_path / 'original'))
    manifest = json.loads(Path(result['manifest_path']).read_text())
    manifest['source']['comments_retrieved_at'] = '2026-11-15T12:00:00Z'
    manifest['source']['comments_coverage'] = 'cloud-complete'
    original = copy.deepcopy(manifest)
    output = tmp_path / 'refreshed'
    result = write_manifest_export(manifest, str(output))
    assert manifest == original
    assert all(not Path(r['gpx']).is_absolute() for r in result['routes'])
    tree = ET.parse(output / 'stage-1.gpx')
    assert 'retrieved 2026-11-15T12:00:00Z' in tree.find('g:wpt/g:desc', NS).text
    assert json.loads((output / 'manifest.json').read_text()) == manifest


def table_bytes(entries):
    def block(body):
        # The table checksum covers body + compression byte (uncompressed=0).
        crc = struct.pack('<I', masked_crc(body + b'\0'))
        return body + b'\0' + crc

    def entry_block(rows):
        body = b''
        for key, value in rows:
            body += vint(0) + vint(len(key)) + vint(len(value)) + key + value
        return body + struct.pack('<II', 0, 1)

    rows = []
    for sequence, name, fields in entries:
        trailer = (sequence << 8) | (0 if fields is None else 1)
        rows.append((remote_key(name) + struct.pack('<Q', trailer),
                     b'' if fields is None else document(name, fields)))
    data_body = entry_block(rows)
    data = block(data_body)
    meta_offset = len(data)
    meta_body = entry_block([])
    data += block(meta_body)
    index_offset = len(data)
    index_body = entry_block([(b'last', vint(0) + vint(len(data_body)))])
    data += block(index_body)
    footer = vint(meta_offset) + vint(len(meta_body)) + vint(index_offset) + vint(len(index_body))
    return data + footer.ljust(40, b'\0') + struct.pack('<Q', 0xdb4775248b80fb57)


def test_table_tombstones_and_manifest_active_files_are_authoritative(tmp_path):
    cache = cache_at(tmp_path, [('Trek/b', {'name': 'old-log'})])
    active = table_bytes([(200, 'Trek/a', {'name': 'active'}),
                          (201, 'Trek/b', None)])
    (cache / '000005.ldb').write_bytes(active)
    # An obsolete table must neither resurrect a document nor be inspected.
    (cache / '000006.ldb').write_bytes(b'obsolete garbage')
    addition = b''.join(vint(v) for v in (7, 0, 5, len(active), 0, 0))
    (cache / 'MANIFEST-000001').write_bytes(physical(vint(2) + vint(2) + addition))
    assert load_documents(str(cache)) == {PREFIX + 'Trek/a': {'name': 'active'}}
    damaged = bytearray(active)
    damaged[10] ^= 1
    (cache / '000005.ldb').write_bytes(damaged)
    with pytest.raises(CacheError, match='checksum'):
        load_documents(str(cache))


def test_unreadable_latest_document_cannot_fall_back_to_an_old_version(tmp_path):
    cache = cache_at(tmp_path, [('Trek/a', {'name': 'old'})])
    key = remote_key('Trek/a')
    payload = struct.pack('<QI', 1000, 1) + b'\x01' + vint(len(key)) + key + vint(1) + b'\xff'
    (cache / '000003.log').write_bytes(physical(payload))
    with pytest.raises(CacheError):
        load_documents(str(cache))


def test_integer_valued_double_ids_keep_exact_identity_and_raw_source(tmp_path):
    entries = stage_entries()
    identity = 2026202698877653248
    for name, fields in entries:
        if name == 'TrekStageOfflineInterestPoint/a':
            fields['interestPoints'][0].update(
                internalId=float(identity), stageId=1.0, groupId=1.0)
        elif name.startswith('Comment/'):
            fields.update(trekStageInterestPointInternalId=float(identity),
                          trekStageId=1.0, trekStageGroupId=1.0)
    cache = cache_at(tmp_path, entries)
    before = {path.name: path.read_bytes() for path in cache.iterdir()}
    result = export_stage(str(cache), 1, str(tmp_path / 'export'))
    manifest = json.loads(Path(result['manifest_path']).read_text())
    target = next(poi for poi in manifest['pois'] if poi['internal_id'] == identity)
    assert target['id'] == '1:1:2026202698877653248'
    assert type(target['internal_id']) is int
    assert type(target['group_id']) is int
    assert type(target['stage_id']) is int
    assert type(target['raw']['internalId']) is float
    assert target['raw']['internalId'] == identity
    assert len(target['comments']) == 2
    assert {path.name: path.read_bytes() for path in cache.iterdir()} == before


@pytest.mark.parametrize('identity', [1000.5, True, '1000', 1e20, float('nan'), float('inf')])
def test_invalid_numeric_source_ids_are_rejected_without_output(tmp_path, identity):
    entries = stage_entries()
    for name, fields in entries:
        if name == 'TrekStageOfflineInterestPoint/a':
            fields['interestPoints'][0]['internalId'] = identity
    cache = cache_at(tmp_path, entries)
    output = tmp_path / 'export'
    with pytest.raises(CacheError):
        export_stage(str(cache), 1, str(output))
    assert not output.exists()
