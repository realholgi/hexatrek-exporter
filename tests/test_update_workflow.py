"""Update real synthetic exports without cloud or hosted-model traffic."""

import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import xml.etree.ElementTree as ET
import zipfile

import pytest

from hexatrek_exporter import cli
from hexatrek_exporter.hexatrek_export import write_manifest_export
from hexatrek_exporter.poi_details import GPX
from hexatrek_exporter.summaries import summary_document
from hexatrek_exporter.workflow import Workflow


def read(path):
    return json.loads(Path(path).read_text())


def public_bytes(workflow):
    return {str(path.relative_to(workflow.output_root)): path.read_bytes()
            for path in workflow.output_root.rglob('*.gpx')}


def state_bytes(workflow):
    return (workflow.root / 'stages/01/stage.json').read_bytes()


@pytest.fixture
def exported(tmp_path):
    manifest = read(Path(__file__).parent.parent / 'examples/synthetic/source/manifest.json')
    manifest['source'].update(comments_coverage='cloud-complete', photos_coverage='not-requested', photo_mode='links')
    source = Path(write_manifest_export(manifest, str(tmp_path/'source'))['manifest_path'])
    workflow = Workflow(tmp_path/'data')
    adopted = workflow.adopt_snapshot(source)
    texts = summary_document(adopted['manifest_path'], language='en')
    texts['summaries'] = {identity: f'Reviewed synthetic summary {number}.'
                          for number, identity in enumerate(texts['summaries'], 1)}
    summaries = tmp_path/'original-summaries.json'
    summaries.write_text(json.dumps(texts))
    workflow.prepare(1, summaries, language='en')
    original = workflow.export(1)
    return workflow, Path(adopted['manifest_path']), texts['summaries'], original


def install_refresh(monkeypatch, edit=None):
    calls = []
    def refresh(manifest_path, output_directory, **options):
        calls.append(options)
        manifest = read(manifest_path)
        manifest['exported_at'] = '2026-02-01T12:00:00Z'
        manifest['source']['comments_retrieved_at'] = manifest['exported_at']
        if edit:
            edit(manifest)
        return write_manifest_export(manifest, output_directory)
    monkeypatch.setattr('hexatrek_exporter.hexatrek_refresh.refresh_pois', refresh)
    return calls


def change_report(manifest):
    manifest['pois'][0]['comments'][0]['text'] = 'Synthetic update: spring is dry.'


def fill_review(result, text='Updated synthetic report: spring is dry.'):
    path = Path(result['summaries_path'])
    document = read(path)
    document['summaries']['1:1:1'] = text
    path.write_text(json.dumps(document))
    return path


def forbidden(*_args, **_kwargs):
    pytest.fail('An offline resume or unchanged update attempted external or rendering work')


def test_unchanged_refresh_preserves_active_state_exports_and_modification_times(exported, monkeypatch):
    workflow, source, _, _ = exported
    before, state, original = public_bytes(workflow), state_bytes(workflow), source.read_bytes()
    times = {path: path.stat().st_mtime_ns for path in workflow.output_root.rglob('*.gpx')}
    calls = install_refresh(monkeypatch)
    monkeypatch.setattr('hexatrek_exporter.updates.generate_summaries', forbidden)
    result = workflow.update(1, 'private-existing-session', generate=True)
    assert result['status'] == 'unchanged'
    assert result['changes'] == {'changed': False, 'comments': {'added': 0, 'changed': 0, 'removed': 0},
                                 'photos': {'added': 0, 'changed': 0, 'removed': 0}, 'stale_summaries': []}
    assert state_bytes(workflow) == state
    assert public_bytes(workflow) == before
    assert source.read_bytes() == original
    assert all(path.stat().st_mtime_ns == value for path, value in times.items())
    assert calls[0]['include_photos'] is False


def test_changed_report_pauses_then_resumes_with_reviewed_language_bound_text(exported, monkeypatch, tmp_path):
    workflow, source, original_texts, original_run = exported
    before, state, original = public_bytes(workflow), state_bytes(workflow), source.read_bytes()
    calls = install_refresh(monkeypatch, change_report)
    result = workflow.update(1, 'private-existing-session')
    assert result['status'] == 'needs-review'
    assert result['summary_language'] == 'en'
    assert result['targets'] == ['komoot', 'osmand']
    draft = read(result['summaries_path'])
    assert draft['summaries'] == {**original_texts, '1:1:1': None}
    assert result['changes']['comments'] == {'added': 0, 'changed': 1, 'removed': 0}
    assert state_bytes(workflow) == state and public_bytes(workflow) == before
    reviewed = tmp_path/'reviewed.json'
    reviewed.write_bytes(fill_review(result).read_bytes())
    monkeypatch.setattr('hexatrek_exporter.hexatrek_refresh.refresh_pois', forbidden)
    monkeypatch.setattr('requests.Session', forbidden)
    completed = workflow.update(1, resume=result['update_id'], summaries_file=reviewed)
    assert completed['status'] == 'completed' and len(calls) == 1
    assert source.read_bytes() == original
    assert workflow.status([1])['stages'][0]['summary_language'] == 'en'
    for target in ('komoot', 'osmand'):
        new = ET.parse(Path(completed['outputs'][target])/'stage-01.gpx')
        descriptions = [point.findtext(f'{{{GPX}}}desc') for point in new.findall(f'{{{GPX}}}wpt')]
        assert descriptions[0].startswith('Updated synthetic report: spring is dry.')
        assert descriptions[1].startswith(original_texts['1:1:2'])
        old_route = ET.fromstring(before[f'{target}/stage-01/stage-01.gpx'])
        shape = lambda tree: [(point.attrib, point.findtext(f'{{{GPX}}}ele'))
                              for point in tree.findall(f'.//{{{GPX}}}trkpt')]
        assert shape(new) == shape(old_route)
    old_record = read(original_run['record_path'])
    for target in ('komoot', 'osmand'):
        archived = Path(old_record['archives'][target][0])/'stage-01.gpx'
        assert archived.read_bytes() == before[f'{target}/stage-01/stage-01.gpx']
    prepared_path = Path(workflow.status([1])['stages'][0]['preparation_path'])
    prepared = read(prepared_path/'prepared.json')
    with zipfile.ZipFile(prepared_path/prepared['routes'][0]['details']) as archive:
        archived_source = json.loads(archive.read('source-manifest.json'))
        assert archived_source['pois'][0]['comments'][0]['text'].endswith('spring is dry.')
        assert archive.read('source.gpx') != b''
    assert workflow.prepare(1, language='en')['preparation'] == workflow.status([1])['stages'][0]['preparation']
    assert workflow.update(1, resume=result['update_id'])['status'] == 'completed'


def test_photo_only_update_preserves_all_reviewed_summaries_without_model(exported, monkeypatch):
    workflow, _, original_texts, _ = exported
    def photos(manifest):
        comment = manifest['pois'][0]['comments'][0]
        comment['images'] = ['synthetic-image']
        manifest['photos'] = [{'poi_id': '1:1:1', 'comment_id': comment['id'],
                               'source_image': 'synthetic-image', 'url': 'https://example.invalid/synthetic-photo.jpg'}]
        manifest['source'].update(photo_mode='links', photos_coverage='cloud-complete')
    install_refresh(monkeypatch, photos)
    monkeypatch.setattr('hexatrek_exporter.updates.generate_summaries', forbidden)
    result = workflow.update(1, 'private-existing-session', photo_mode='links')
    assert result['status'] == 'completed'
    assert result['changes']['stale_summaries'] == []
    assert result['changes']['photos'] == {'added': 1, 'changed': 0, 'removed': 0}
    assert read(result['summaries_path'])['summaries'] == original_texts
    navigation = (Path(result['outputs']['osmand'])/'stage-01.gpx').read_text()
    assert 'https://example.invalid/synthetic-photo.jpg' in navigation
    before, state = public_bytes(workflow), state_bytes(workflow)
    assert workflow.update(1, "private-existing-session")["status"] == "unchanged"
    assert public_bytes(workflow) == before and state_bytes(workflow) == state


@pytest.mark.parametrize('failure', ['fetch', 'render', 'model'])
def test_failed_update_keeps_original_state_and_both_exports(exported, monkeypatch, failure):
    workflow, source, _, _ = exported
    before, state, original = public_bytes(workflow), state_bytes(workflow), source.read_bytes()
    install_refresh(monkeypatch, change_report)
    def fail(*_args, **_kwargs):
        raise ValueError('Synthetic update failure')
    options = {}
    if failure == 'fetch':
        monkeypatch.setattr('hexatrek_exporter.hexatrek_refresh.refresh_pois', fail)
    elif failure == 'model':
        monkeypatch.setattr('hexatrek_exporter.updates.generate_summaries', fail)
        options['generate'] = True
    else:
        pending = workflow.update(1, 'private-existing-session')
        fill_review(pending)
        monkeypatch.setattr('hexatrek_exporter.workflow.export_osmand', fail)
        options['resume'] = pending['update_id']
    with pytest.raises(ValueError, match='Synthetic update failure'):
        workflow.update(1, 'private-existing-session', **options)
    assert state_bytes(workflow) == state and public_bytes(workflow) == before
    assert source.read_bytes() == original


@pytest.mark.parametrize('interrupt_at', [1, 2])
def test_interrupted_publication_resumes_without_fetch_model_or_render(exported, monkeypatch, interrupt_at):
    workflow, _, _, _ = exported
    before = public_bytes(workflow)
    install_refresh(monkeypatch, change_report)
    pending = workflow.update(1, 'private-existing-session')
    fill_review(pending)
    rename = Path.rename
    publications = 0
    def interrupt(path, destination):
        nonlocal publications
        if path.name == 'payload':
            publications += 1
            if publications == interrupt_at:
                raise OSError('Synthetic interrupted publication')
        return rename(path, destination)
    monkeypatch.setattr(Path, 'rename', interrupt)
    with pytest.raises(OSError, match='interrupted publication'):
        workflow.update(1, resume=pending['update_id'])
    record = read(pending['record_path'])
    assert record['status'] == 'publishing'
    assert publications == interrupt_at
    monkeypatch.setattr(Path, 'rename', rename)
    monkeypatch.setattr('hexatrek_exporter.hexatrek_refresh.refresh_pois', forbidden)
    monkeypatch.setattr('hexatrek_exporter.updates.prepare_export', forbidden)
    monkeypatch.setattr('hexatrek_exporter.workflow.export_osmand', forbidden)
    monkeypatch.setattr('requests.Session', forbidden)
    completed = workflow.update(1, resume=pending['update_id'])
    assert completed['status'] == 'completed'
    assert public_bytes(workflow) != before
    assert workflow.status([1])['stages'][0]['snapshot'] == read(pending['record_path'])['candidate_state']['snapshot']
    assert set(completed['outputs']) == {'osmand', 'komoot'}


def test_new_update_refuses_pending_review_and_unrelated_state_changes(exported, monkeypatch):
    workflow, source, _, _ = exported
    install_refresh(monkeypatch, change_report)
    pending = workflow.update(1, 'private-existing-session')
    with pytest.raises(ValueError, match='--resume'):
        workflow.update(1, 'private-existing-session')
    fill_review(pending)
    state = read(workflow.root/'stages/01/stage.json')
    state['preparation'] = 'another-preparation'
    (workflow.root/'stages/01/stage.json').write_text(json.dumps(state))
    with pytest.raises(ValueError, match='Stage changed'):
        workflow.update(1, resume=pending['update_id'])


@pytest.mark.parametrize('field, value', [('summary_language', 'de'), ('source_manifest_sha256', 'wrong')])
def test_review_metadata_mismatch_fails_before_model_or_publication(exported, monkeypatch, field, value):
    workflow, _, _, _ = exported
    before, state = public_bytes(workflow), state_bytes(workflow)
    install_refresh(monkeypatch, change_report)
    pending = workflow.update(1, 'private-existing-session')
    document = read(pending['summaries_path'])
    document[field] = value
    Path(pending['summaries_path']).write_text(json.dumps(document))
    monkeypatch.setattr('hexatrek_exporter.updates.generate_summaries', forbidden)
    with pytest.raises(ValueError, match='candidate'):
        workflow.update(1, resume=pending['update_id'], generate=True)
    assert state_bytes(workflow) == state and public_bytes(workflow) == before


def test_edited_public_export_is_rejected_before_fetch(exported, monkeypatch):
    workflow, _, _, run = exported
    navigation = Path(run['outputs']['osmand'])/'stage-01.gpx'
    navigation.write_text('User-edited export')
    monkeypatch.setattr('hexatrek_exporter.hexatrek_refresh.refresh_pois', forbidden)
    with pytest.raises(ValueError, match='Stored data changed'):
        workflow.update(1, 'private-existing-session')
    assert navigation.read_text() == 'User-edited export'


def test_raw_osmand_update_uses_full_new_reports_without_stale_short_text(tmp_path, monkeypatch):
    manifest = read(Path(__file__).parent.parent/'examples/synthetic/source/manifest.json')
    manifest['pois'][0]['summary'] = 'Outdated synthetic water status.'
    path = write_manifest_export(manifest, str(tmp_path/'source'))['manifest_path']
    workflow = Workflow(tmp_path/'data')
    workflow.adopt_snapshot(path)
    workflow.export(1, ('osmand',))
    install_refresh(monkeypatch, change_report)
    result = workflow.update(1, 'private-existing-session')
    assert result['status'] == 'completed' and result['targets'] == ['osmand']
    navigation = (Path(result['outputs']['osmand'])/'stage-01.gpx').read_text()
    assert 'spring is dry' in navigation and 'Outdated synthetic water status' not in navigation


def test_cli_generates_only_stale_summary_through_local_http_and_exports(exported, monkeypatch, capsys):
    workflow, _, original_texts, _ = exported
    install_refresh(monkeypatch, change_report)
    requests = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            evidence = json.loads(body['messages'][1]['content'])
            requests.append(evidence)
            content = json.dumps({'summaries': {point['id']: 'Synthetic spring reported dry.'
                                               for point in evidence['untrusted_pois']}})
            response = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                'role': 'assistant', 'content': content}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert cli.main(['--data-root', str(workflow.root), 'update', '--stage', '1',
                         '--session-file', 'private-existing-session', '--generate',
                         '--api-base', f'http://127.0.0.1:{server.server_port}/v1',
                         '--model', 'synthetic-fixture']) == 0
        result = json.loads(capsys.readouterr().out)['stages'][0]['result']
        assert result['status'] == 'completed'
        assert len(requests) == 1 and requests[0]['summary_language'] == 'en'
        assert [point['id'] for point in requests[0]['untrusted_pois']] == ['1:1:1']
        document = read(result['summaries_path'])
        assert document['summaries'] == {**original_texts, '1:1:1': 'Synthetic spring reported dry.'}
        assert 'private-existing-session' not in Path(result['record_path']).read_text()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_file_photos_enter_detail_archives_and_deletions_leave_no_stale_images(exported, monkeypatch):
    import hashlib
    import struct
    import zlib

    workflow, _, original_texts, _ = exported
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    image = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
             + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b""))
    digest = hashlib.sha256(image).hexdigest()
    def with_files(manifest_path, output_directory, **options):
        assert options["include_photos"] and options["photo_mode"] == "files"
        manifest = read(manifest_path)
        comment = manifest["pois"][0]["comments"][0]
        comment["images"] = ["synthetic-image"]
        manifest["photos"] = [{"poi_id": "1:1:1", "comment_id": comment["id"],
                               "source_image": "synthetic-image", "path": f"photos/{digest}.png", "sha256": digest}]
        manifest["source"].update(photo_mode="files", photos_coverage="cloud-complete")
        result = write_manifest_export(manifest, output_directory)
        path = Path(result["manifest_path"]).parent / f"photos/{digest}.png"
        path.parent.mkdir()
        path.write_bytes(image)
        return result
    monkeypatch.setattr("hexatrek_exporter.hexatrek_refresh.refresh_pois", with_files)
    monkeypatch.setattr("hexatrek_exporter.updates.generate_summaries", forbidden)
    first = workflow.update(1, "private-existing-session", photo_mode="files")
    assert first["status"] == "completed"
    assert read(first["summaries_path"])["summaries"] == original_texts
    def archive_images():
        directory = Path(workflow.status([1])["stages"][0]["preparation_path"])
        prepared = read(directory/"prepared.json")
        with zipfile.ZipFile(directory/prepared["routes"][0]["details"]) as archive:
            return [archive.read(name) for name in archive.namelist() if name.endswith(".png")]
    assert image in archive_images()
    def deleted(manifest):
        manifest["photos"] = []
        manifest["pois"][0]["comments"][0]["images"] = []
    calls = install_refresh(monkeypatch, deleted)
    second = workflow.update(1, "private-existing-session")
    assert second["status"] == "completed" and second["photo_mode"] == "files"
    assert calls[0]["photo_mode"] == "files"
    assert second["changes"]["photos"]["removed"] == 1
    assert archive_images() == []
    assert all(path.suffix == ".gpx" for path in workflow.output_root.glob("*/*/*"))


def test_report_deletion_invalidates_only_its_summary_and_preserves_originals(exported, monkeypatch):
    workflow, source, original_texts, _ = exported
    original = source.read_bytes()
    install_refresh(monkeypatch, lambda manifest: manifest["pois"][1].update(comments=[]))
    result = workflow.update(1, "private-existing-session")
    assert result["status"] == "needs-review"
    assert result["changes"]["comments"]["removed"] == 1
    assert read(result["summaries_path"])["summaries"] == {**original_texts, "1:1:2": None}
    assert source.read_bytes() == original


def test_refresh_cannot_change_route_geometry(exported, monkeypatch):
    workflow, _, _, _ = exported
    before, state = public_bytes(workflow), state_bytes(workflow)
    install_refresh(monkeypatch, lambda manifest: manifest["routes"][0]["points"][0].update(elevation=999))
    with pytest.raises(ValueError, match="geometry"):
        workflow.update(1, "private-existing-session")
    assert public_bytes(workflow) == before and state_bytes(workflow) == state


def test_files_updates_reuse_compressed_library_and_inherit_selected_quality(exported, monkeypatch):
    import base64
    from copy import deepcopy
    import hashlib
    import io
    import shutil
    from PIL import Image

    from hexatrek_exporter.photo_quality import compress_photo
    from hexatrek_exporter.workflow import _inventory, _write

    workflow, source, original_texts, _ = exported
    manifest = read(source)
    image = Image.new("RGB", (1200, 1000), "orange")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    original = buffer.getvalue()
    compressed, metadata = compress_photo(original)
    assert compressed != original
    album = workflow.root / "photos/stage-01"
    (album / "photos").mkdir(parents=True)
    digest = hashlib.sha256(compressed).hexdigest()
    path = f"photos/{digest}.jpg"
    (album / path).write_bytes(compressed)
    cached = deepcopy(manifest)
    cached["source"].update(photo_mode="files", photo_quality="med")
    cached["photos"] = [{"poi_id": "1:1:1", "comment_id": manifest["pois"][0]["comments"][0]["id"],
                         "source_image": "synthetic-photo", "path": path, "sha256": digest, **metadata}]
    for route in cached["routes"]:
        shutil.copyfile(source.parent / route["gpx"], album / route["gpx"])
    _write(album / "manifest.json", cached)
    _write(album / "inventory.json", _inventory(album))
    points = {point["id"]: point for point in manifest["pois"]}
    def comments(_session, _credentials, point):
        result = deepcopy(points[point["id"]]["comments"])
        if point["id"] == "1:1:1":
            result[0]["images"] = ["synthetic-photo"]
        return result
    monkeypatch.setattr("hexatrek_exporter.hexatrek_refresh._comments", comments)
    monkeypatch.setattr("hexatrek_exporter.hexatrek_refresh.FirebaseSession", lambda *_args: object())
    monkeypatch.setattr("hexatrek_exporter.hexatrek_refresh._photo_metadata", lambda *_args: {
        "md5Hash": base64.b64encode(hashlib.md5(original, usedforsecurity=False).digest()).decode()})
    monkeypatch.setattr("hexatrek_exporter.hexatrek_refresh._request", forbidden)
    first = workflow.update(1, "synthetic-session", photo_mode="files")
    assert first["status"] == "completed" and first["photo_quality"] == "med"
    current = workflow.status([1])["stages"][0]
    first_manifest = read(Path(current["snapshot_path"]) / "manifest.json")
    assert first_manifest["photos"][0]["sha256"] == digest
    assert read(first["summaries_path"])["summaries"] == original_texts
    lower = workflow.update(1, "synthetic-session", photo_quality="lo")
    assert lower["status"] == "completed" and lower["photo_quality"] == "lo"
    current = workflow.status([1])["stages"][0]
    lower_manifest = read(Path(current["snapshot_path"]) / "manifest.json")
    photo = lower_manifest["photos"][0]
    assert photo["source_md5"] == metadata["source_md5"]
    with Image.open(Path(current["snapshot_path"]) / photo["path"]) as smaller:
        assert max(smaller.size) <= 640
    repeated = workflow.update(1, "synthetic-session")
    assert repeated["status"] == "unchanged" and repeated["photo_quality"] == "lo"
