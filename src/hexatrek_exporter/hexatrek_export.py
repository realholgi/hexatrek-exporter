"""Read-only HexaTrek Firestore-cache exports. No session or preference access.

Only CURRENT, its manifest, active tables and write-ahead logs are read. An
in-memory snapshot is checked against source identity/size/mtime before use.
Unsupported encodings and damaged/incomplete records fail closed.
"""
from __future__ import annotations

import base64
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct
import xml.etree.ElementTree as ET


class CacheError(ValueError):
    """The cache cannot be decoded completely and safely."""


def _varint(data, pos=0):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise CacheError("Truncated varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise CacheError("Invalid varint")


def _slice(data, pos, size):
    if pos < 0 or size < 0 or pos + size > len(data):
        raise CacheError("Truncated record")
    return data[pos:pos + size]


def _crc_table():
    table = []
    for value in range(256):
        for _ in range(8):
            value = (value >> 1) ^ (0x82f63b78 if value & 1 else 0)
        table.append(value)
    return tuple(table)


_CRC_TABLE = _crc_table()


def _crc32c(data):
    crc = 0xffffffff
    for byte in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ byte) & 255]
    return crc ^ 0xffffffff


def _check_crc(data, expected):
    crc = _crc32c(data)
    masked = (((crc >> 15) | (crc << 17)) + 0xa282ead8) & 0xffffffff
    if masked != expected:
        raise CacheError("LevelDB checksum mismatch")


def _snappy(data):
    size, pos = _varint(data)
    out = bytearray()
    while pos < len(data):
        tag = data[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:
            length = tag >> 2
            if length < 60:
                length += 1
            else:
                count = length - 59
                length = int.from_bytes(_slice(data, pos, count), 'little') + 1
                pos += count
            out.extend(_slice(data, pos, length))
            pos += length
        else:
            count = (1, 2, 4)[kind - 1]
            offset = int.from_bytes(_slice(data, pos, count), 'little')
            pos += count
            if kind == 1:
                offset |= (tag & 224) << 3
                length = 4 + ((tag >> 2) & 7)
            else:
                length = 1 + (tag >> 2)
            if not offset or offset > len(out):
                raise CacheError("Invalid Snappy copy")
            for _ in range(length):
                out.append(out[-offset])
        if len(out) > size:
            raise CacheError("Snappy size overflow")
    if len(out) != size:
        raise CacheError("Snappy size mismatch")
    return bytes(out)


def _block(data, offset, size):
    raw = _slice(data, offset, size)
    trailer = _slice(data, offset + size, 5)
    _check_crc(raw + trailer[:1], int.from_bytes(trailer[1:], 'little'))
    if trailer[0] == 0:
        return raw
    if trailer[0] == 1:
        return _snappy(raw)
    raise CacheError("Unsupported LevelDB compression")


def _entries(raw):
    if len(raw) < 8:
        raise CacheError("Truncated LevelDB restart table")
    count = int.from_bytes(raw[-4:], 'little')
    end = len(raw) - 4 * (count + 1)
    if not count or end < 0:
        raise CacheError("Invalid LevelDB restart count")
    restarts = struct.unpack('<' + 'I' * count, raw[end:-4])
    if restarts[0] != 0 or any(a >= b for a, b in zip(restarts, restarts[1:])):
        raise CacheError("Invalid LevelDB restarts")
    if any(offset >= end for offset in restarts) and end != 0:
        raise CacheError("LevelDB restart outside entries")
    pos = 0
    old = b''
    boundaries = set()
    while pos < end:
        boundaries.add(pos)
        start = pos
        shared, pos = _varint(raw, pos)
        length, pos = _varint(raw, pos)
        size, pos = _varint(raw, pos)
        if shared > len(old) or (start in restarts and shared):
            raise CacheError("Invalid LevelDB prefix")
        key = old[:shared] + _slice(raw[:end], pos, length)
        pos += length
        value = _slice(raw[:end], pos, size)
        pos += size
        old = key
        yield key, value
    if end and not set(restarts).issubset(boundaries):
        raise CacheError("Unaligned LevelDB restart")


def _table_records(data):
    if len(data) < 48 or int.from_bytes(data[-8:], 'little') != 0xdb4775248b80fb57:
        raise CacheError("Invalid LevelDB table footer")
    footer = data[-48:-8]
    meta_offset, pos = _varint(footer)
    meta_size, pos = _varint(footer, pos)
    offset, pos = _varint(footer, pos)
    size, pos = _varint(footer, pos)
    # Validate metadata block too, even when no filter is needed.
    for _, handle in _entries(_block(data, meta_offset, meta_size)):
        off, p = _varint(handle)
        length, p = _varint(handle, p)
        _block(data, off, length)
    for _, handle in _entries(_block(data, offset, size)):
        off, p = _varint(handle)
        length, p = _varint(handle, p)
        if p != len(handle):
            raise CacheError("Invalid LevelDB block handle")
        for key, value in _entries(_block(data, off, length)):
            if len(key) < 8:
                raise CacheError("Truncated internal key")
            trailer = int.from_bytes(key[-8:], 'little')
            kind = trailer & 255
            if kind not in (0, 1):
                raise CacheError("Unsupported LevelDB value type")
            yield trailer >> 8, key[:-8], value if kind else None


def _physical_records(data):
    pending = None
    for start in range(0, len(data), 32768):
        block = data[start:start + 32768]
        pos = 0
        while pos + 7 <= len(block):
            crc, length, kind = struct.unpack('<IHB', block[pos:pos + 7])
            pos += 7
            if crc == length == kind == 0:
                if any(block[pos:]):
                    raise CacheError("Nonzero LevelDB log padding")
                pos = len(block)
                break
            fragment = _slice(block, pos, length)
            pos += length
            _check_crc(bytes([kind]) + fragment, crc)
            if kind == 1 and pending is None:
                yield fragment
            elif kind == 2 and pending is None:
                pending = bytearray(fragment)
            elif kind in (3, 4) and pending is not None:
                pending.extend(fragment)
                if kind == 4:
                    yield bytes(pending)
                    pending = None
            else:
                raise CacheError("Invalid LevelDB log fragment order")
        if any(block[pos:]):
            raise CacheError("Truncated LevelDB log header")
    if pending is not None:
        raise CacheError("Incomplete LevelDB log record")


def _log_records(data):
    for record in _physical_records(data):
        if len(record) < 12:
            raise CacheError("Truncated write batch")
        sequence, count = struct.unpack('<QI', record[:12])
        pos = 12
        for index in range(count):
            tag = _slice(record, pos, 1)[0]
            pos += 1
            size, pos = _varint(record, pos)
            key = _slice(record, pos, size)
            pos += size
            value = None
            if tag == 1:
                size, pos = _varint(record, pos)
                value = _slice(record, pos, size)
                pos += size
            elif tag != 0:
                raise CacheError("Unknown write batch type")
            yield sequence + index, key, value
        if pos != len(record):
            raise CacheError("Write batch count mismatch")


def _version_files(data):
    tables = set()
    log = previous = 0
    for record in _physical_records(data):
        pos = 0
        while pos < len(record):
            tag, pos = _varint(record, pos)
            if tag in (2, 3, 4, 9):
                number, pos = _varint(record, pos)
                if tag == 2:
                    log = number
                elif tag == 9:
                    previous = number
            elif tag == 1:
                length, pos = _varint(record, pos)
                _slice(record, pos, length)
                pos += length
            elif tag in (5, 6, 7):
                _, pos = _varint(record, pos)  # level
                if tag in (6, 7):
                    number, pos = _varint(record, pos)
                    if tag == 6:
                        tables.discard(number)
                        continue
                    tables.add(number)
                    _, pos = _varint(record, pos)  # file size
                for _ in range(2 if tag == 7 else 1):
                    length, pos = _varint(record, pos)
                    _slice(record, pos, length)
                    pos += length
            else:
                raise CacheError(f"Unsupported LevelDB manifest tag {tag}")
    return tables, log, previous


def _snapshot(cache_path):
    root = Path(cache_path).expanduser()
    observed = {}
    listing = {p.name for p in root.glob('*.log')}
    def take(name):
        path = root / name
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if identity(before) != identity(after) or len(data) != after.st_size:
            raise CacheError("Cache changed during snapshot")
        observed[name] = (identity(after), data)
        return data
    current = take('CURRENT').decode('ascii').strip()
    if not current.startswith('MANIFEST-') or not current[9:].isdigit():
        raise CacheError("Invalid CURRENT manifest name")
    tables, log, previous = _version_files(take(current))
    for number in sorted(tables):
        name = f'{number:06d}.ldb'
        if not (root / name).exists():
            name = f'{number:06d}.sst'
        take(name)
    # Recovery replays every log at/above the manifest log number, plus prev.
    for path in sorted(root.glob('*.log')):
        if not path.stem.isdigit():
            raise CacheError("Invalid LevelDB log filename")
        if int(path.stem) >= log or int(path.stem) == previous:
            take(path.name)
    for name, (identity, _) in observed.items():
        st = (root / name).stat()
        if identity != (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns):
            raise CacheError("Cache changed during snapshot")
    if listing != {p.name for p in root.glob('*.log')}:
        raise CacheError("Cache log inventory changed during snapshot")
    return {name: data for name, (_, data) in observed.items()}


def _fields(data):
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if not number:
            raise CacheError("Invalid protobuf field")
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value = _slice(data, pos, size)
            pos += size
        elif wire == 2:
            size, pos = _varint(data, pos)
            value = _slice(data, pos, size)
            pos += size
        else:
            raise CacheError("Unsupported protobuf wire type")
        yield number, wire, value


def _field(data, number, default=None):
    return next((value for n, _, value in _fields(data) if n == number), default)


def _map(data):
    result = {}
    for n, w, entry in _fields(data):
        if n != 1 or w != 2:
            raise CacheError("Unknown Firestore map encoding")
        key, value = _field(entry, 1), _field(entry, 2)
        if key is None or value is None or key.decode() in result:
            raise CacheError("Invalid Firestore map entry")
        result[key.decode()] = _value(value)
    return result


def _value(data):
    values = list(_fields(data))
    if len(values) != 1:
        raise CacheError("Invalid Firestore value union")
    n, w, value = values[0]
    if n in (5, 17) and w == 2:
        return value.decode('utf-8')
    if n == 18 and w == 2:
        return {'bytes_base64': base64.b64encode(value).decode('ascii')}
    if n == 1 and w == 0:
        return bool(value)
    if n == 2 and w == 0:
        return value if value < 2**63 else value - 2**64
    if n == 3 and w == 1:
        result = struct.unpack('<d', value)[0]
        if not math.isfinite(result):
            raise CacheError("Nonfinite Firestore number")
        return result
    if n == 6 and w == 2:
        return _map(value)
    if n == 9 and w == 2:
        result = []
        for field, wire, item in _fields(value):
            if field != 1 or wire != 2:
                raise CacheError("Invalid Firestore array")
            result.append(_value(item))
        return result
    if n == 10 and w == 2:
        seconds = _field(value, 1, 0)
        return {'seconds': seconds if seconds < 2**63 else seconds - 2**64,
                'nanos': _field(value, 2, 0)}
    if n == 8 and w == 2:
        return {key: struct.unpack('<d', _field(value, i, b'\0' * 8))[0]
                for i, key in ((1, 'latitude'), (2, 'longitude'))}
    if n == 11 and w == 0:
        return None
    raise CacheError(f"Unsupported Firestore value field {n}/{w}")


def _document(raw):
    # firestore.client.MaybeDocument: document=2; no_document=1; unknown=3.
    wrapper = list(_fields(raw))
    variants = [(n, v) for n, w, v in wrapper if n in (1, 2, 3) and w == 2]
    if len(variants) != 1:
        raise CacheError("Unreadable or ambiguous remote document wrapper")
    kind, body = variants[0]
    if kind == 3:
        raise CacheError("Firestore unknown document has no readable fields")
    fields = list(_fields(body))
    name = next((v.decode() for n, w, v in fields if n == 1 and w == 2), None)
    if not name or not name.startswith('projects/hexatrek-44801/databases/(default)/documents/'):
        raise CacheError("Unexpected Firestore document name")
    if kind == 1:
        return None  # Valid explicit Firestore no_document, not a decode omission.
    result = {}
    for n, w, entry in fields:
        if n == 2 and w == 2:
            key, value = _field(entry, 1), _field(entry, 2)
            if key is None or value is None or key.decode() in result:
                raise CacheError("Invalid document field")
            result[key.decode()] = _value(value)
    return name, result


def _decode_documents(snapshot):
    latest = {}
    for name, data in snapshot.items():
        records = (_table_records(data) if name.endswith(('.ldb', '.sst')) else
                   _log_records(data) if name.endswith('.log') else ())
        try:
            for sequence, key, raw in records:
                # Resolve all latest user keys BEFORE decoding: tombstones have no name.
                if not key.startswith(b'\x85remote_document\x00\x01'):
                    continue
                if key not in latest or sequence > latest[key][0]:
                    latest[key] = sequence, raw
                elif sequence == latest[key][0] and raw != latest[key][1]:
                    raise CacheError("Conflicting equal-sequence records")
        except (ValueError, IndexError, struct.error) as exc:
            raise CacheError(f"{name}: {exc}") from exc
    docs = {}
    for _, raw in latest.values():
        if raw is not None:
            parsed = _document(raw)
            if parsed is not None:
                name, fields = parsed
                if name in docs:
                    raise CacheError("Duplicate remote document identity")
                docs[name] = fields
    return docs


def load_documents(cache_path: str) -> dict[str, dict]:
    """Return complete latest remote document fields; raise on corruption/change."""
    return _decode_documents(_snapshot(cache_path))


def _french(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get('fr'), str):
        return value['fr']
    raise CacheError("Missing French source text")


def _integer(value, label):
    if type(value) is float and value.is_integer() and -(2**63) <= value < 2**63:
        return int(value)
    if type(value) is not int:
        raise CacheError(f"Invalid {label}")
    return value


def _coordinates(point):
    for key, bound in (('latitude', 90), ('longitude', 180)):
        value = point.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > bound:
            raise CacheError(f"Invalid {key}")
    if 'elevation' in point and (type(point['elevation']) not in (int, float) or not math.isfinite(point['elevation'])):
        raise CacheError("Invalid elevation")


def _timestamp(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    return datetime.fromtimestamp(value['seconds'] + value.get('nanos', 0) / 1e9, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _build_manifest(documents, stage_id, source):
    _integer(stage_id, 'stage_id')
    collections = defaultdict(list)
    for name, fields in documents.items():
        collections[name.split('/documents/', 1)[1].split('/')[0]].append((name, fields))
    stages = [stage for _, trek in collections['Trek'] for stage in trek['stages'] if stage.get('internalId') == stage_id]
    if len(stages) != 1:
        raise CacheError("Stage metadata missing or ambiguous")
    stage = stages[0]
    point_types = {d['internalId']: d for _, d in collections['TrekStagePointType']}
    poi_types = {d['type']: d for _, d in collections['TrekStageInterestPointType']}
    points = defaultdict(list)
    raw_pois = []
    source_documents = {}
    for collection, field in (('TrekStageOfflinePoint', 'points'), ('TrekStageOfflineInterestPoint', 'interestPoints')):
        for name, group in collections[collection]:
            if group.get('trekStageId') != stage_id:
                continue
            if not isinstance(group.get(field), list) or not group[field]:
                raise CacheError(f"Unreadable stage point group: {name}")
            source_documents[name] = group
            for point in group[field]:
                if not isinstance(point, dict):
                    raise CacheError("Invalid stage point")
                route_id = _integer(point.get('stageId'), 'point stageId')
                _integer(point.get('internalId'), 'point internalId')
                _integer(point.get('groupId'), 'point groupId')
                _coordinates(point)
                if field == 'points':
                    points[route_id].append(point)
                else:
                    raw_pois.append(point)
    if not points.get(stage_id):
        raise CacheError("Main stage points not cached")
    routes = []
    for route_id in sorted(points, key=lambda i: (i != stage_id, i)):
        route_points = sorted(points[route_id], key=lambda p: p['internalId'])
        if len({p['internalId'] for p in route_points}) != len(route_points):
            raise CacheError("Duplicate route point internalId")
        if route_id == stage_id:
            title = _french(stage['name'])
            # Source French title already includes the stage number.
            title = f"HexaTrek – {title.replace('Etape ', 'Stage ', 1)}"
            metadata = stage
        else:
            candidates = [sub for category in {p['category'] for p in route_points}
                          for sub in point_types.get(category, {}).get('subStagePointType', [])
                          if sub.get('internalId') == route_id]
            unique = {json.dumps(c, sort_keys=True, ensure_ascii=False): c for c in candidates}
            if len(unique) != 1:
                raise CacheError(f"Alternative {route_id} lacks unambiguous source type metadata")
            metadata = next(iter(unique.values()))
            title = _french(metadata['name'])
        routes.append({'id': str(route_id), 'stage_id': route_id,
                       'kind': 'main' if route_id == stage_id else 'alternative', 'name': title,
                       'gpx': f'stage-{stage_id}.gpx' if route_id == stage_id else f'stage-{stage_id}-alternative-{route_id}.gpx',
                       'points': route_points, 'poi_ids': [], 'raw': metadata})
    route_by_id = {r['stage_id']: r for r in routes}
    comments = defaultdict(list)
    for name, raw in collections['Comment']:
        identity = (raw.get('trekStageId'), raw.get('trekStageGroupId'), raw.get('trekStageInterestPointInternalId'))
        rating = raw.get('rating')
        comments[identity].append({'id': name.rsplit('/', 1)[1], 'created_at': _timestamp(raw['createdAt']),
                                   'author': raw.get('user', {}).get('name', ''),
                                   'rating': f'{rating:g}/5' if isinstance(rating, (int, float)) else str(rating or ''),
                                   'text': raw.get('text', ''), 'images': raw.get('images', []), 'raw': raw})
    pois = []
    for raw in sorted(raw_pois, key=lambda p: (p['stageId'], p['groupId'], p['internalId'])):
        route_id = _integer(raw['stageId'], 'point stageId')
        group_id = _integer(raw['groupId'], 'point groupId')
        internal_id = _integer(raw['internalId'], 'point internalId')
        if route_id not in route_by_id:
            raise CacheError(f"POI refers to uncached route {route_id}")
        type_id = raw.get('subCategory')
        if type_id not in poi_types:
            raise CacheError(f"Unknown POI type {type_id}")
        source_type = poi_types[type_id]
        kind = _french(source_type['typeName'])
        name = _french(raw.get('name'))
        identity = f'{route_id}:{group_id}:{internal_id}'
        description = _french(raw.get('description'))
        poi = {'id': identity, 'stage_id': route_id, 'group_id': group_id, 'internal_id': internal_id,
               'name': name or f'{kind} – {identity}', 'name_derived': not bool(name), 'type': kind,
               'latitude': raw['latitude'], 'longitude': raw['longitude'], 'description': description,
               'links': [raw['url']] if raw.get('url') else [],
               'comments': sorted(comments[(route_id, group_id, internal_id)], key=lambda c: (c['created_at'], c['id']), reverse=True),
               'raw': raw, 'raw_type': source_type}
        pois.append(poi)
        route_by_id[route_id]['poi_ids'].append(identity)
    if len({p['id'] for p in pois}) != len(pois):
        raise CacheError("Duplicate source POI identity")
    names = defaultdict(set)
    for poi in pois:
        names[poi['name'].casefold()].add(poi['type'])
    for poi in pois:
        if len(names[poi['name'].casefold()]) > 1:
            poi['name'] += f" – {poi['type']} [{poi['id']}]"
            poi['name_disambiguated'] = True
    return {'schema_version': 1, 'stage_id': stage_id, 'exported_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'source': source, 'stage': stage, 'routes': routes, 'pois': pois,
            'source_documents': source_documents, 'point_types': [d for _, d in collections['TrekStagePointType']]}


def _render_gpx(manifest, route, pois):
    ns = 'http://www.topografix.com/GPX/1/1'
    hx = 'urn:hexatrek-exporter:hexatrek:1'
    ET.register_namespace('', ns)
    ET.register_namespace('hexatrek', hx)
    def child(parent, name, text=None, **attrs):
        element = ET.SubElement(parent, f'{{{ns}}}{name}', attrs)
        if text is not None:
            element.text = str(text)
        return element
    root = ET.Element(f'{{{ns}}}gpx', {'version': '1.1', 'creator': 'hexatrek-exporter'})
    metadata = child(root, 'metadata')
    child(metadata, 'name', route['name'])
    child(metadata, 'time', manifest['exported_at'])
    for identity in route['poi_ids']:
        poi = pois[identity]
        if poi['stage_id'] != route['stage_id']:
            raise CacheError("Cross-route POI assignment")
        _coordinates(poi)
        waypoint = child(root, 'wpt', lat=str(poi['latitude']), lon=str(poi['longitude']))
        if 'elevation' in poi.get('raw', {}):
            child(waypoint, 'ele', poi['raw']['elevation'])
        child(waypoint, 'name', poi['name'])
        description = poi.get('description', '')
        comments = sorted(poi.get('comments', []), key=lambda c: (_timestamp(c['created_at']), c['id']), reverse=True)
        if comments:
            retrieved = manifest.get('source', {}).get('comments_retrieved_at', manifest.get('refreshed_at', manifest['exported_at']))
            blocks = [f"{_timestamp(c['created_at'])} | {c.get('author', '')} | {c.get('rating', '')}\n{c.get('text', '')}" for c in comments]
            description += ('\n\n' if description else '') + f'User comments (newest first; retrieved {retrieved}):\n\n' + '\n\n'.join(blocks)
        child(waypoint, 'desc', description)
        for link in poi.get('links', []):
            child(waypoint, 'link', href=link)
        child(waypoint, 'type', poi['type'])
        extension = child(waypoint, 'extensions')
        ET.SubElement(extension, f'{{{hx}}}poi_id').text = identity
    track = child(root, 'trk')
    child(track, 'name', route['name'])
    segment = child(track, 'trkseg')
    for point in sorted(route['points'], key=lambda p: p['internalId']):
        if point['stageId'] != route['stage_id']:
            raise CacheError("Cross-route track point assignment")
        _coordinates(point)
        trkpt = child(segment, 'trkpt', lat=str(point['latitude']), lon=str(point['longitude']))
        if 'elevation' in point:
            child(trkpt, 'ele', point['elevation'])
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def write_manifest_export(manifest: dict, output_directory: str) -> dict:
    """Render full GPXs and manifest into an absent directory; never overwrite.

    Validate/render everything before creating the directory. Refresher-owned
    photo files may be added afterward; paths in its manifest remain relative.
    """
    target = Path(output_directory).expanduser()
    if target.exists() or target.is_symlink():
        raise FileExistsError(f'Export directory already exists: {target}')
    if manifest.get('schema_version') != 1:
        raise CacheError("Unsupported export schema")
    pois = {p['id']: p for p in manifest['pois']}
    if len(pois) != len(manifest['pois']):
        raise CacheError("Duplicate manifest POI identity")
    files = {}
    assigned = []
    for route in manifest['routes']:
        filename = route['gpx']
        if not filename.endswith('.gpx') or Path(filename).name != filename or filename in files:
            raise CacheError("Invalid or duplicate GPX filename")
        files[filename] = _render_gpx(manifest, route, pois)
        assigned.extend(route['poi_ids'])
    if set(assigned) != set(pois) or len(assigned) != len(pois):
        raise CacheError("Manifest route POI coverage mismatch")
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    target.mkdir(parents=False, exist_ok=False)
    for name, data in files.items():
        (target / name).write_bytes(data)
    (target / 'manifest.json').write_text(encoded, encoding='utf-8')
    return {'manifest_path': str(target / 'manifest.json'),
            'stage_id': manifest['stage_id'], 'route_count': len(files), 'poi_count': len(pois),
            'point_count': sum(len(r['points']) for r in manifest['routes']),
            'routes': [{'id': r['id'], 'name': r['name'], 'kind': r['kind'], 'gpx': r['gpx'],
                        'point_count': len(r['points']), 'poi_count': len(r['poi_ids'])} for r in manifest['routes']]}


def export_stage(cache_path: str, stage_id: int, output_directory: str) -> dict:
    """Export all cached groups for one source stage, alternatives separately."""
    if Path(output_directory).expanduser().exists():
        raise FileExistsError('Export directory already exists')
    snapshot = _snapshot(cache_path)
    source = {'kind': 'hexatrek-firestore-cache', 'comments_coverage': 'cache-only',
              'files': [{'name': name, 'sha256': hashlib.sha256(data).hexdigest()} for name, data in sorted(snapshot.items())]}
    return write_manifest_export(_build_manifest(_decode_documents(snapshot), stage_id, source), output_directory)
