"""Cloud refresh tests use synthetic responses only; no sessions or network are read."""

import base64
import copy
import hashlib
import json
import struct
import zlib
from pathlib import Path

import pytest
import requests

from hexatrek_exporter import hexatrek_refresh as refresh
from hexatrek_exporter.photo_quality import compress_photo

TOKEN = "existing.session.signature"
TIME = "2026-11-15T10:00:00Z"


def field(value):
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, list):
        return {"arrayValue": {"values": [field(item) for item in value]}}
    return {"mapValue": {"fields": {key: field(item) for key, item in value.items()}}}


def document(identifier="new", created="2026-11-14T10:00:00Z", images=None, **overrides):
    values = {"trekStageId": 1, "trekStageGroupId": 2, "trekStageInterestPointInternalId": 1000,
              "user": {"name": "Camille"}, "rating": 5, "text": "Texte intégral\nDeuxième ligne", "images": images or []}
    values.update(overrides)
    fields = {key: field(value) for key, value in values.items()}
    fields["createdAt"] = {"timestampValue": created}
    return {"document": {"name": refresh._DOCUMENT_PREFIX + identifier, "fields": fields}, "readTime": TIME}


class Response:
    def __init__(self, value, status=200):
        self.content = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        for offset in range(0, len(self.content), size):
            yield self.content[offset:offset + size]


class Session:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        response = next(self.replies)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def source(tmp_path):
    manifest = {"schema_version": 1, "stage_id": 1, "source": {"comments_coverage": "cache-only"},
                "stage": {"name": "Original"}, "routes": [], "photos": [],
                "pois": [{"id": "1:2:1000", "stage_id": 1, "group_id": 2, "internal_id": 1000,
                          "description": "Original background", "comments": [{"id": "cached", "text": "Keep on failure"}]}]}
    path = tmp_path / "source.json"
    path.write_text(json.dumps(manifest))
    token = tmp_path / "session.secret"
    token.write_text(TOKEN)
    token.chmod(0o600)
    return path, tmp_path / "new-export", token


@pytest.fixture
def renderer(monkeypatch):
    from hexatrek_exporter import hexatrek_export

    calls = []

    def render(manifest, directory):
        calls.append(copy.deepcopy(manifest))
        path = Path(directory)
        assert not path.exists()
        path.mkdir()
        (path / "manifest.json").write_text(json.dumps(manifest))
        (path / "source.gpx").write_text("original track rendered with refreshed comments")
        return {"manifest_path": str(path / "manifest.json"), "route_count": 0}

    monkeypatch.setattr(hexatrek_export, "write_manifest_export", render)
    return calls


def install_session(monkeypatch, *replies):
    session = Session(replies)
    monkeypatch.setattr(refresh.requests, "Session", lambda: session)
    return session


def png():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b"")


def test_complete_refresh_pins_snapshot_and_fetches_all_pages(monkeypatch, source, renderer):
    monkeypatch.setattr(refresh, "_PAGE_SIZE", 1)
    session = install_session(monkeypatch, Response([document()]), Response([document("old", "2026-10-01T00:00:00Z")]), Response([{"readTime": TIME}]))
    before = source[0].read_bytes()
    result = refresh.refresh_pois(*map(str, source), include_photos=False)
    assert result["comment_count"] == 2
    assert result["poi_count"] == 1
    assert result["photo_count"] == 0
    assert Path(result["manifest_path"]) == source[1] / "manifest.json"
    assert source[0].read_bytes() == before
    manifest = renderer[0]
    assert [item["id"] for item in manifest["pois"][0]["comments"]] == ["new", "old"]
    assert manifest["pois"][0]["comments"][0]["text"].endswith("Deuxième ligne")
    assert manifest["source"]["comments_coverage"] == "cloud-complete"
    assert manifest["source"]["photos_coverage"] == "not-requested"
    assert session.trust_env is False and session.auth is None
    first = session.calls[0][2]
    query = first["json"]["structuredQuery"]
    assert query["where"] == {"fieldFilter": {
        "field": {"fieldPath": "trekStageInterestPointInternalId"},
        "op": "EQUAL", "value": {"integerValue": "1000"},
    }}
    assert query["orderBy"] == [
        {"field": {"fieldPath": "createdAt"}, "direction": "DESCENDING"},
        {"field": {"fieldPath": "__name__"}, "direction": "DESCENDING"},
    ]
    second = session.calls[1][2]["json"]
    assert second["readTime"] == TIME
    assert second["structuredQuery"]["startAt"] == {"values": [{"timestampValue": "2026-11-14T10:00:00Z"}, {"referenceValue": refresh._DOCUMENT_PREFIX + "new"}], "before": False}
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    assert all(call[2]["headers"] == {"Authorization": "Bearer " + TOKEN} for call in session.calls)
    assert TOKEN not in (source[1] / "manifest.json").read_text()


@pytest.mark.parametrize("damage", [None, "truncated", "trailing"])
def test_multi_picture_jpeg_is_encoded_only_with_complete_images(monkeypatch, source, renderer, damage):
    # Two real 1x1 JPEGs with an MPF index, generated once using Pillow's MPO writer.
    data = zlib.decompress(base64.b64decode(
        "eJz7f+P/AwYBLzdPNwZGRgYGRiBk+P+IIcM3wI3B01OLgYOBgYGZgWEDOwMLkGVgaGDAuIEFpIiBCYSB4gpA2ogBBpgZHjMxoIBKIP84VEwBDfy/zeDMwMHGxs7GysHOzs7JycHFI8LLw83NIykkzC8iKyUvJyslI6OgoqeuoKSjLCOjYa6pY2BoYmIir25pa2Fko2dsYgQyhJGTk5OHm0eCl1fCSFFG0Yhk8P8AgyAHyPvMjEoMTIKMzIKM/48wyANDhJURDGDeYWRiZmFlY+fg5OIGKtgqwMDEyMzMxMLMysoCDCHGWqA8A4sgq5CioSObcGAiu1KhiFHjxIUcyk4bD4oGXfygYpxU1MTJJSYuISmlqqauoallYmpmbmFp5ezi6ubu4ekVHBIaFh4RGZWckpqWnpGZVVxSWlZeUVnV3NLa1t7R2TVp8pSp06bPmDlr0eIlS5ctX7Fy1abNW7Zu275j565Dh48cPXb8xMlTly5fuXrt+o2btx4+evzk6bPnL16++vjp85ev377/+PkL5C9GBmZGGMDqL0Ggv5hYWJhZ2EH+YmQqBykQZGFVNGQTcgxkTywUVjJq5BBxmrhw40FOZeOgD6JJRRe5xFRMHqp+BHkN7DPiPNZEls/gHkP46xYDDzMjMPKYBRnsGR69WNT1c/J34f83/2NL4qMJbzTh0SbhfTylsWh/oc3h/zcBLWmrvg=="
    ))
    if damage == "truncated":
        data = data[:-1]
    elif damage == "trailing":
        data += b"garbage"
    install_session(monkeypatch, Response([document(images=["hdr.jpg"])]), Response(data))
    if damage:
        with pytest.raises(refresh.RefreshError):
            refresh.refresh_pois(*map(str, source), photo_mode="files")
        assert not source[1].exists()
    else:
        result = refresh.refresh_pois(*map(str, source), photo_mode="files")
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        photo = manifest["photos"][0]
        assert photo["path"].endswith(".jpg")
        assert (source[1] / photo["path"]).read_bytes() == compress_photo(data)[0]
        assert photo["source_sha256"] == hashlib.sha256(data).hexdigest()
        assert photo["comment_id"] == "new"


def test_exact_comment_photo_binding_and_content_hash(monkeypatch, source, renderer):
    data = png()
    session = install_session(monkeypatch, Response([document(images=["photo space.png"]), document("old", "2026-10-01T00:00:00Z", ["other.png"])]), Response(data), Response(data))
    result = refresh.refresh_pois(*map(str, source), photo_mode="files")
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert result["photo_count"] == 2
    photos = manifest["photos"]
    assert [(photo["poi_id"], photo["comment_id"], photo["source_image"]) for photo in photos] == [("1:2:1000", "new", "photo space.png"), ("1:2:1000", "old", "other.png")]
    encoded, metadata = compress_photo(data)
    digest = hashlib.sha256(encoded).hexdigest()
    assert {photo["path"] for photo in photos} == {f"photos/{digest}.jpg"}
    assert (source[1] / photos[0]["path"]).read_bytes() == encoded
    assert all(all(photo[key] == value for key, value in metadata.items()) for photo in photos)
    assert session.calls[1][1] == refresh._STORAGE_PREFIX + "comment%2F1000%2Fphoto%20space.png?alt=media"
    assert session.calls[1][2]["headers"] == {"Authorization": "Firebase " + TOKEN}


@pytest.mark.parametrize("response", [Response([], 401), Response([], 403), Response([], 302), Response(b"not-json"), Response({"error": {"message": TOKEN}}), requests.ConnectionError(TOKEN)])
def test_cloud_failures_leave_original_and_no_partial_output(monkeypatch, source, renderer, response):
    original = source[0].read_bytes()
    install_session(monkeypatch, response)
    with pytest.raises(refresh.RefreshError) as error:
        refresh.refresh_pois(*map(str, source))
    assert TOKEN not in str(error.value)
    assert not source[1].exists()
    assert source[0].read_bytes() == original
    assert not list(source[1].parent.glob(".hexatrek-refresh-*"))
    assert not renderer


@pytest.mark.parametrize("row", [document(trekStageInterestPointInternalId=1001), document(trekStageId=11, trekStageGroupId=99, trekStageInterestPointInternalId=1001), document(images=["../escape.png"]), document(images=["https://attacker.invalid/x"]), document(created="invalid")])
def test_foreign_or_malformed_comments_fail_closed(monkeypatch, source, renderer, row):
    install_session(monkeypatch, Response([row]))
    with pytest.raises(refresh.RefreshError):
        refresh.refresh_pois(*map(str, source))
    assert not source[1].exists()


def test_duplicate_page_is_not_accepted_as_complete(monkeypatch, source, renderer):
    monkeypatch.setattr(refresh, "_PAGE_SIZE", 1)
    install_session(monkeypatch, Response([document()]), Response([document()]))
    with pytest.raises(refresh.RefreshError, match="pagination"):
        refresh.refresh_pois(*map(str, source))
    assert not source[1].exists()


def test_page_limit_is_explicit_failure(monkeypatch, source, renderer):
    monkeypatch.setattr(refresh, "_PAGE_SIZE", 1)
    monkeypatch.setattr(refresh, "_MAX_PAGES", 1)
    install_session(monkeypatch, Response([document()]))
    with pytest.raises(refresh.RefreshError, match="incomplete"):
        refresh.refresh_pois(*map(str, source))
    assert not source[1].exists()


@pytest.mark.parametrize("photo", [Response(b"<html>error</html>"), Response(b"\xff\xd8truncated"), Response(png()[:-1]), Response([], 403)])
def test_inaccessible_or_invalid_photos_abort_entire_export(monkeypatch, source, renderer, photo):
    install_session(monkeypatch, Response([document(images=["p.png"])]), photo)
    with pytest.raises(refresh.RefreshError):
        refresh.refresh_pois(*map(str, source), photo_mode="files")
    assert not source[1].exists()
    assert not renderer


def test_response_byte_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(refresh, "_MAX_IMAGE_BYTES", 3)
    session = Session([Response(b"1234")])
    with pytest.raises(refresh.RefreshError, match="size"):
        refresh._request(session, "GET", refresh._STORAGE_PREFIX + "comment%2F1%2Fp?alt=media", TOKEN, photo=True)


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_token_must_be_private(source, mode):
    source[2].chmod(mode)
    with pytest.raises(refresh.RefreshError, match="private"):
        refresh.refresh_pois(*map(str, source))
    assert not source[1].exists()


def test_token_symlinks_are_refused(source):
    alias = source[2].with_name("alias")
    alias.symlink_to(source[2])
    with pytest.raises(refresh.RefreshError):
        refresh.refresh_pois(str(source[0]), str(source[1]), str(alias))


def test_existing_destination_is_untouched(source):
    source[1].mkdir()
    sentinel = source[1] / "original"
    sentinel.write_text("preserve")
    with pytest.raises(refresh.RefreshError, match="already exist"):
        refresh.refresh_pois(*map(str, source))
    assert sentinel.read_text() == "preserve"


def test_renderer_failure_does_not_publish(monkeypatch, source, renderer):
    from hexatrek_exporter import hexatrek_export

    install_session(monkeypatch, Response([{"readTime": TIME}]))

    def fail(manifest, directory):
        Path(directory).mkdir()
        (Path(directory) / "partial").write_text("partial")
        raise ValueError("render failed")

    monkeypatch.setattr(hexatrek_export, "write_manifest_export", fail)
    with pytest.raises(ValueError, match="render failed"):
        refresh.refresh_pois(*map(str, source))
    assert not source[1].exists()
    assert not list(source[1].parent.glob(".hexatrek-refresh-*"))


def test_migrated_stage_and_group_preserve_raw_fields_and_bind_photo(monkeypatch, source, renderer):
    row = document(trekStageId=11, trekStageGroupId=99, images=["migrated.png"], legacy={"label": "original group"})
    session = install_session(monkeypatch, Response([row]), Response(png()))
    result = refresh.refresh_pois(*map(str, source), photo_mode="files")
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    poi = manifest["pois"][0]
    assert (poi["stage_id"], poi["group_id"], poi["internal_id"]) == (1, 2, 1000)
    raw = poi["comments"][0]["raw"]
    assert raw["trekStageId"] == 11 and raw["trekStageGroupId"] == 99
    assert raw["legacy"] == {"label": "original group"}
    assert raw["images"] == ["migrated.png"]
    assert manifest["photos"][0]["poi_id"] == "1:2:1000"
    assert manifest["photos"][0]["comment_id"] == "new"
    assert session.calls[1][1] == refresh._STORAGE_PREFIX + "comment%2F1000%2Fmigrated.png?alt=media"
    assert len(session.calls) == 2  # Direct authenticated media; no metadata lookup.


@pytest.mark.parametrize("both", [False, True])
def test_exactly_one_auth_source_is_required(monkeypatch, source, both):
    monkeypatch.setattr(refresh.requests, "Session", lambda: pytest.fail("Auth validation must precede network"))
    with pytest.raises(refresh.RefreshError, match="exactly one"):
        refresh.refresh_pois(
            str(source[0]), str(source[1]),
            token_file=str(source[2]) if both else None,
            session_file="synthetic-session.json" if both else None,
        )
    assert not source[1].exists()


@pytest.mark.parametrize("mode", ["links", "files"])
def test_persisted_session_renews_for_pagination_and_media(monkeypatch, source, renderer, mode):
    monkeypatch.setattr(refresh, "_PAGE_SIZE", 1)
    photo = png() if mode == "files" else {
        "bucket": "hexatrek-44801.appspot.com", "name": "comment/1000/p.png", "downloadTokens": "download-token",
    }
    session = install_session(monkeypatch, Response([document(images=["p.png"])]), Response([{"readTime": TIME}]), Response(photo))
    clock = [0]
    providers = []
    tokens = []

    class Provider:
        def __init__(self, session_file, http_session):
            assert session_file == "synthetic-session.json"
            assert http_session is session
            assert http_session.trust_env is False and http_session.auth is None
            self.expires_at = -1
            self.current = None
            self.calls = 0
            providers.append(self)

        def token(self):
            self.calls += 1
            if clock[0] >= self.expires_at:
                self.current = f"synthetic.session{len(tokens)}.signature"
                self.expires_at = clock[0] + 60
                tokens.append(self.current)
            return self.current

    request = session.request

    def advance_clock(*args, **kwargs):
        response = request(*args, **kwargs)
        clock[0] += 61  # Each page/download can outlive the current token.
        return response

    monkeypatch.setattr(session, "request", advance_clock)
    monkeypatch.setattr(refresh, "FirebaseSession", Provider)
    original = source[0].read_bytes()
    result = refresh.refresh_pois(
        str(source[0]), str(source[1]), session_file="synthetic-session.json", photo_mode=mode,
    )
    assert len(providers) == 1 and providers[0].calls == 3
    assert len(tokens) == 3
    assert [call[2]["headers"]["Authorization"] for call in session.calls] == [
        "Bearer " + tokens[0], "Bearer " + tokens[1], "Firebase " + tokens[2],
    ]
    assert result["comment_count"] == 1 and result["photo_count"] == 1
    assert session.calls[1][2]["json"]["readTime"] == TIME
    assert source[0].read_bytes() == original
    exported = Path(result["manifest_path"]).read_text()
    assert all(token not in exported for token in tokens)
    assert "synthetic-session.json" not in exported
    assert session.calls[2][1] == refresh._STORAGE_PREFIX + "comment%2F1000%2Fp.png" + ("?alt=media" if mode == "files" else "")
    assert json.loads(exported)["source"]["photo_mode"] == mode


def test_session_renewal_failure_does_not_publish_or_disclose_token(monkeypatch, source, renderer):
    session = install_session(monkeypatch, Response([document(images=["p.png"])]))

    class Provider:
        def __init__(self, session_file, http_session):
            assert http_session is session
            self.calls = 0

        def token(self):
            self.calls += 1
            if self.calls > 1:
                raise refresh.SessionError(TOKEN)
            return TOKEN

    monkeypatch.setattr(refresh, "FirebaseSession", Provider)
    original = source[0].read_bytes()
    with pytest.raises(refresh.RefreshError) as error:
        refresh.refresh_pois(str(source[0]), str(source[1]), session_file="synthetic-session.json")
    assert TOKEN not in str(error.value)
    assert source[0].read_bytes() == original
    assert not source[1].exists()
    assert not list(source[1].parent.glob(".hexatrek-refresh-*"))
    assert not renderer
    assert len(session.calls) == 1


@pytest.mark.parametrize("change", [{"id": "11:2:1000"}, {"stage_id": "1"}, {"group_id": True}, {"internal_id": "1000"}])
def test_source_identity_is_still_validated(monkeypatch, source, change):
    manifest = json.loads(source[0].read_text())
    manifest["pois"][0].update(change)
    source[0].write_text(json.dumps(manifest))
    monkeypatch.setattr(refresh.requests, "Session", lambda: pytest.fail("Invalid source must not reach network"))
    with pytest.raises(refresh.RefreshError, match="identit"):
        refresh.refresh_pois(*map(str, source))
    assert not source[1].exists()


def test_default_photo_links_bind_metadata_without_downloading_media(monkeypatch, source, renderer):
    first = "download token/+?&=%é"
    session = install_session(
        monkeypatch,
        Response([document(images=["photo space.png"]), document("old", "2026-10-01T00:00:00Z", ["other.png"])]),
        Response({"bucket": "hexatrek-44801.appspot.com", "name": "comment/1000/photo space.png",
                  "downloadTokens": first + ",unused-second-token", "contentType": "image/png"}),
        Response({"bucket": "hexatrek-44801.appspot.com", "name": "comment/1000/other.png",
                  "downloadTokens": "other-token", "contentType": "image/png"}),
    )
    original = source[0].read_bytes()
    result = refresh.refresh_pois(*map(str, source))
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert result["photo_count"] == 2
    assert manifest["photos"] == [
        {"poi_id": "1:2:1000", "comment_id": "new", "source_image": "photo space.png",
         "url": refresh._STORAGE_PREFIX + "comment%2F1000%2Fphoto%20space.png?alt=media&token=download%20token%2F%2B%3F%26%3D%25%C3%A9"},
        {"poi_id": "1:2:1000", "comment_id": "old", "source_image": "other.png",
         "url": refresh._STORAGE_PREFIX + "comment%2F1000%2Fother.png?alt=media&token=other-token"},
    ]
    assert manifest["source"]["photos_coverage"] == "cloud-complete"
    assert manifest["source"]["photo_mode"] == "links"
    assert source[0].read_bytes() == original
    assert not (source[1] / "photos").exists()
    assert len(session.calls) == 3
    assert [(method, url) for method, url, _ in session.calls[1:]] == [
        ("GET", refresh._STORAGE_PREFIX + "comment%2F1000%2Fphoto%20space.png"),
        ("GET", refresh._STORAGE_PREFIX + "comment%2F1000%2Fother.png"),
    ]
    assert all(call[2]["headers"] == {"Authorization": "Firebase " + TOKEN} for call in session.calls[1:])
    exported = (source[1] / "manifest.json").read_text()
    assert TOKEN not in exported and "unused-second-token" not in exported
    assert all(TOKEN not in photo["url"] for photo in manifest["photos"])


@pytest.mark.parametrize("metadata", [
    {"bucket": "hexatrek-44801.appspot.com", "name": "comment/1000/p.png"},
    {"bucket": "hexatrek-44801.appspot.com", "name": "comment/1000/p.png", "downloadTokens": ""},
    {"bucket": "hexatrek-44801.appspot.com", "name": "comment/1000/p.png", "downloadTokens": ",second-secret"},
    {"bucket": "foreign-" + TOKEN, "name": "comment/1000/p.png", "downloadTokens": "download-secret"},
    {"bucket": "hexatrek-44801.appspot.com", "name": "comment/1001/p.png", "downloadTokens": "download-secret"},
    {"bucket": "hexatrek-44801.appspot.com", "name": "comment/1000/other.png", "downloadTokens": "download-secret"},
    {"bucket": "hexatrek-44801.appspot.com", "name": "comment/1000/p.png", "downloadTokens": [TOKEN]},
    {"error": {"message": TOKEN}},
    [],
    b"invalid-json-" + TOKEN.encode(),
])
def test_invalid_photo_metadata_fails_atomically_without_token_disclosure(monkeypatch, source, renderer, metadata):
    original = source[0].read_bytes()
    session = install_session(
        monkeypatch, Response([document(images=["good.png", "p.png"])]),
        Response({"bucket": "hexatrek-44801.appspot.com", "name": "comment/1000/good.png", "downloadTokens": "good-secret"}),
        Response(metadata),
    )
    with pytest.raises(refresh.RefreshError) as error:
        refresh.refresh_pois(*map(str, source))
    assert all(secret not in str(error.value) for secret in (TOKEN, "download-secret", "second-secret", "good-secret"))
    assert source[0].read_bytes() == original
    assert not source[1].exists()
    assert not list(source[1].parent.glob(".hexatrek-refresh-*"))
    assert not renderer
    assert len(session.calls) == 3


@pytest.mark.parametrize("mode", ["embedded", "", None, True, [], {}])
def test_unknown_photo_mode_is_rejected_before_network(monkeypatch, source, renderer, mode):
    monkeypatch.setattr(refresh.requests, "Session", lambda: pytest.fail("Invalid mode must not reach network"))
    with pytest.raises(refresh.RefreshError, match="photo_mode"):
        refresh.refresh_pois(*map(str, source), photo_mode=mode)
    assert not source[1].exists()
    assert not renderer


@pytest.mark.parametrize("mode", ["links", "files"])
def test_photos_not_requested_skip_all_photo_requests(monkeypatch, source, renderer, mode):
    session = install_session(monkeypatch, Response([document(images=["p.png"])]))
    result = refresh.refresh_pois(*map(str, source), include_photos=False, photo_mode=mode)
    assert result["photo_count"] == 0
    assert renderer[0]["photos"] == []
    assert renderer[0]["source"]["photos_coverage"] == "not-requested"
    assert len(session.calls) == 1
    assert not (source[1] / "photos").exists()


@pytest.fixture
def local_photo(source):
    data = png()
    digest = hashlib.sha256(data).hexdigest()
    photo = {"poi_id": "1:2:1000", "comment_id": "new", "source_image": "p.png",
             "path": f"photos/{digest}.png", "sha256": digest}
    path = source[0].parent / photo["path"]
    path.parent.mkdir()
    path.write_bytes(data)
    manifest = json.loads(source[0].read_text())
    manifest["photos"] = [photo]
    source[0].write_text(json.dumps(manifest))
    return photo, path, data


def forbid_media_gets(monkeypatch):
    request = Session.request

    def comments_only(self, method, url, **kwargs):
        assert method != "GET" or "?alt=media" not in url, "Unchanged local photos must not be downloaded"
        return request(self, method, url, **kwargs)

    monkeypatch.setattr(Session, "request", comments_only)


def test_files_reuse_verified_bytes_and_stable_metadata(monkeypatch, source, renderer, local_photo):
    photo, path, data = local_photo
    original = source[0].read_bytes()
    forbid_media_gets(monkeypatch)
    session = install_session(
        monkeypatch, Response([document(images=["p.png"], text="Updated comment")]),
        Response(photo_metadata(data)),
    )
    result = refresh.refresh_pois(*map(str, source), photo_mode="files")
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    encoded, metadata = compress_photo(data)
    digest = hashlib.sha256(encoded).hexdigest()
    assert manifest["photos"] == [{**photo, "path": f"photos/{digest}.jpg", "sha256": digest, **metadata}]
    assert manifest["pois"][0]["comments"][0]["text"] == "Updated comment"
    assert result["photo_count"] == 1
    assert [call[0] for call in session.calls] == ["POST", "GET"]
    assert (source[1] / manifest["photos"][0]["path"]).read_bytes() == encoded
    assert path.read_bytes() == data
    assert source[0].read_bytes() == original


def test_files_remove_deleted_comments_and_photos(monkeypatch, source, renderer, local_photo):
    photo, path, data = local_photo
    original = source[0].read_bytes()
    forbid_media_gets(monkeypatch)
    install_session(monkeypatch, Response([]))
    result = refresh.refresh_pois(*map(str, source), photo_mode="files")
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert manifest["pois"][0]["comments"] == []
    assert manifest["photos"] == []
    assert result["photo_count"] == 0
    assert not (source[1] / photo["path"]).exists()
    assert path.read_bytes() == data
    assert source[0].read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("poi_id", "1:2:2000"), ("comment_id", "deleted"), ("source_image", "replaced.png"),
])
def test_files_download_new_exact_identity(monkeypatch, source, renderer, local_photo, field, value):
    photo, path, data = local_photo
    manifest = json.loads(source[0].read_text())
    manifest["photos"][0][field] = value
    source[0].write_text(json.dumps(manifest))
    original = source[0].read_bytes()
    session = install_session(monkeypatch, Response([document(images=["p.png"])]), Response(data))
    result = refresh.refresh_pois(*map(str, source), photo_mode="files")
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    encoded, metadata = compress_photo(data)
    digest = hashlib.sha256(encoded).hexdigest()
    assert manifest["photos"] == [{**photo, "path": f"photos/{digest}.jpg", "sha256": digest, **metadata}]
    assert [call[0] for call in session.calls] == ["POST", "GET"]
    assert path.read_bytes() == data
    assert source[0].read_bytes() == original


@pytest.mark.parametrize("damage", ["digest", "corrupt", "missing"])
def test_files_reject_damaged_existing_photo_without_download(monkeypatch, source, renderer, local_photo, damage):
    _photo, path, data = local_photo
    manifest = json.loads(source[0].read_text())
    if damage == "digest":
        path.write_bytes(data + b"tampered")
    elif damage == "corrupt":
        path.write_bytes(data[:-1])
        manifest["photos"][0]["sha256"] = hashlib.sha256(data[:-1]).hexdigest()
        source[0].write_text(json.dumps(manifest))
    else:
        path.unlink()
    original = source[0].read_bytes()
    existing = path.read_bytes() if path.exists() else None
    forbid_media_gets(monkeypatch)
    install_session(monkeypatch, Response([document(images=["p.png"])]))
    with pytest.raises(refresh.RefreshError):
        refresh.refresh_pois(*map(str, source), photo_mode="files")
    assert not source[1].exists()
    assert not renderer
    assert source[0].read_bytes() == original
    assert (path.read_bytes() if path.exists() else None) == existing


@pytest.mark.parametrize("unsafe", ["absolute", "parent", "file-symlink", "directory-symlink"])
def test_files_reject_unsafe_source_photo_paths(monkeypatch, source, renderer, local_photo, unsafe):
    photo, path, data = local_photo
    manifest = json.loads(source[0].read_text())
    root = source[0].parent
    if unsafe == "absolute":
        relative = str(path)
    elif unsafe == "parent":
        relative = "../" + root.name + "/" + photo["path"]
    elif unsafe == "file-symlink":
        alias = root / "alias.png"
        alias.symlink_to(path)
        relative = alias.name
    else:
        alias = root / "alias"
        alias.symlink_to(path.parent, target_is_directory=True)
        relative = alias.name + "/" + path.name
    manifest["photos"][0]["path"] = relative
    source[0].write_text(json.dumps(manifest))
    original = source[0].read_bytes()
    forbid_media_gets(monkeypatch)
    install_session(monkeypatch, Response([document(images=["p.png"])]))
    with pytest.raises(refresh.RefreshError):
        refresh.refresh_pois(*map(str, source), photo_mode="files")
    assert not source[1].exists()
    assert not renderer
    assert path.read_bytes() == data
    assert source[0].read_bytes() == original


def test_existing_files_do_not_replace_fresh_photo_links(monkeypatch, source, renderer, local_photo):
    session = install_session(
        monkeypatch, Response([document(images=["p.png"])]),
        Response({"bucket": "hexatrek-44801.appspot.com", "name": "comment/1000/p.png",
                  "downloadTokens": "fresh-token", "contentType": "image/png"}),
    )
    result = refresh.refresh_pois(*map(str, source), photo_mode="links")
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert manifest["photos"] == [
        {"poi_id": "1:2:1000", "comment_id": "new", "source_image": "p.png",
         "url": refresh._STORAGE_PREFIX + "comment%2F1000%2Fp.png?alt=media&token=fresh-token"}
    ]
    assert [call[0] for call in session.calls] == ["POST", "GET"]


def photo_metadata(data) -> dict:
    return {"bucket": "hexatrek-44801.appspot.com", "name": "comment/1000/p.png",
            "md5Hash": base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii")}


@pytest.mark.parametrize("md5", ["different", "missing", "malformed", "null"])
def test_existing_identity_downloads_replaced_or_unversioned_photo(monkeypatch, source, renderer, local_photo, md5):
    photo, path, original_data = local_photo
    # A valid PNG with an additional ancillary chunk differs without recompressing its image.
    body = b"Note\0updated"
    chunk = (struct.pack(">I", len(body)) + b"tEXt" + body
             + struct.pack(">I", zlib.crc32(b"tEXt" + body) & 0xffffffff))
    data = original_data[:-12] + chunk + original_data[-12:]
    metadata = photo_metadata(data)
    if md5 == "missing":
        del metadata["md5Hash"]
    elif md5 == "malformed":
        metadata["md5Hash"] = {"private": "private-body-secret"}
    elif md5 == "null":
        metadata["md5Hash"] = None
    original_manifest = source[0].read_bytes()
    session = install_session(
        monkeypatch, Response([document(images=["p.png"])]), Response(metadata), Response(data),
    )
    result = refresh.refresh_pois(*map(str, source), photo_mode="files")
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    encoded, encoding = compress_photo(data)
    digest = hashlib.sha256(encoded).hexdigest()
    assert manifest["photos"] == [{**photo, "path": f"photos/{digest}.jpg", "sha256": digest, **encoding}]
    assert (source[1] / manifest["photos"][0]["path"]).read_bytes() == encoded
    assert [call[0] for call in session.calls] == ["POST", "GET", "GET"]
    assert "?alt=media" not in session.calls[1][1]
    assert session.calls[2][1].endswith("?alt=media")
    assert path.read_bytes() == original_data
    assert source[0].read_bytes() == original_manifest
    assert "md5Hash" not in (source[1] / "manifest.json").read_text()


@pytest.mark.parametrize("metadata", [
    {"bucket": "private-body-secret", "name": "comment/1000/p.png"},
    {"bucket": "hexatrek-44801.appspot.com", "name": "comment/1000/private-body-secret"},
    {"error": {"message": "private-body-secret"}},
    b"private-body-secret",
])
def test_existing_photo_metadata_fails_closed_without_private_body(monkeypatch, source, renderer, local_photo, metadata):
    _photo, path, data = local_photo
    original_manifest = source[0].read_bytes()
    forbid_media_gets(monkeypatch)
    session = install_session(monkeypatch, Response([document(images=["p.png"])]), Response(metadata))
    with pytest.raises(refresh.RefreshError) as error:
        refresh.refresh_pois(*map(str, source), photo_mode="files")
    assert "private-body-secret" not in str(error.value)
    assert TOKEN not in str(error.value)
    assert [call[0] for call in session.calls] == ["POST", "GET"]
    assert not source[1].exists()
    assert not renderer
    assert path.read_bytes() == data
    assert source[0].read_bytes() == original_manifest


@pytest.fixture
def optimized_photo(source, local_photo):
    photo, path, original = local_photo
    data, metadata = compress_photo(original, "med")
    digest = hashlib.sha256(data).hexdigest()
    cached = {**photo, **metadata, "path": f"photos/{digest}.jpg", "sha256": digest}
    (path.parent / f"{digest}.jpg").write_bytes(data)
    manifest = json.loads(source[0].read_text())
    manifest["photos"] = [cached]
    source[0].write_text(json.dumps(manifest))
    return cached, data, original


@pytest.mark.parametrize("quality", ["med", "lo", "hi"])
def test_optimized_original_md5_controls_reuse_and_quality(monkeypatch, source, renderer, optimized_photo, quality):
    photo, data, original = optimized_photo
    replies = [Response([document(images=["p.png"])]), Response(photo_metadata(original))]
    if quality == "hi":
        replies.append(Response(original))
    else:
        forbid_media_gets(monkeypatch)
    session = install_session(monkeypatch, *replies)
    result = refresh.refresh_pois(*map(str, source), photo_mode="files", photo_quality=quality)
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    updated = manifest["photos"][0]
    assert updated["quality"] == quality
    assert updated["source_md5"] == photo["source_md5"]
    assert updated["source_sha256"] == photo["source_sha256"]
    assert manifest["source"]["photo_quality"] == quality
    assert len(session.calls) == (3 if quality == "hi" else 2)
    if quality == "med":
        assert updated == photo
        assert (source[1] / updated["path"]).read_bytes() == data


def test_optimized_changed_cloud_object_resets_original_fingerprints(monkeypatch, source, renderer, optimized_photo):
    photo, _data, original = optimized_photo
    body = b"Note\0new cloud object"
    chunk = struct.pack(">I", len(body)) + b"tEXt" + body + struct.pack(">I", zlib.crc32(b"tEXt" + body) & 0xffffffff)
    replacement = original[:-12] + chunk + original[-12:]
    install_session(monkeypatch, Response([document(images=["p.png"])]),
                    Response(photo_metadata(replacement)), Response(replacement))
    result = refresh.refresh_pois(*map(str, source), photo_mode="files")
    updated = json.loads(Path(result["manifest_path"]).read_text())["photos"][0]
    assert updated["source_sha256"] == hashlib.sha256(replacement).hexdigest()
    assert updated["source_sha256"] != photo["source_sha256"]
    assert updated["source_md5"] == photo_metadata(replacement)["md5Hash"]
    assert updated["source_size_bytes"] == len(replacement)


@pytest.mark.parametrize("mode,include", [("links", True), ("files", False)])
def test_non_file_modes_never_encode(monkeypatch, source, renderer, mode, include):
    monkeypatch.setattr(refresh, "compress_photo", lambda *args, **kwargs: pytest.fail("No photo encoding"))
    replies = [Response([document(images=["p.png"])])]
    if include:
        replies.append(Response({**photo_metadata(png()), "downloadTokens": "public-token"}))
    install_session(monkeypatch, *replies)
    result = refresh.refresh_pois(*map(str, source), photo_mode=mode, include_photos=include)
    assert "photo_quality" not in json.loads(Path(result["manifest_path"]).read_text())["source"]


def secondary_cache(source, photo, data):
    root = source[0].parent / "library"
    root.mkdir()
    (root / "photos").mkdir()
    (root / photo["path"]).write_bytes(data)
    manifest = {"schema_version": 1, "stage_id": 1, "source": {"photo_mode": "files"}, "photos": [photo]}
    cache = root / "manifest.json"
    cache.write_text(json.dumps(manifest))
    inventory = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in root.rglob("*") if path.is_file()}
    (root / "inventory.json").write_text(json.dumps(inventory))
    return cache


def test_secondary_library_serves_links_only_snapshot_without_media_download(monkeypatch, source, renderer, optimized_photo):
    photo, data, original = optimized_photo
    cache = secondary_cache(source, photo, data)
    manifest = json.loads(source[0].read_text())
    manifest["source"]["photo_mode"] = "links"
    manifest["photos"] = [{"poi_id": photo["poi_id"], "comment_id": "new", "source_image": "p.png", "url": "https://example.invalid/photo"}]
    source[0].write_text(json.dumps(manifest))
    before = cache.read_bytes(), source[0].read_bytes()
    forbid_media_gets(monkeypatch)
    install_session(monkeypatch, Response([document(images=["p.png"])]), Response(photo_metadata(original)))
    result = refresh.refresh_pois(*map(str, source), photo_mode="files", photo_cache_path=str(cache))
    exported = json.loads(Path(result["manifest_path"]).read_text())
    assert exported["photos"] == [photo]
    assert (source[1] / photo["path"]).read_bytes() == data
    assert (cache.read_bytes(), source[0].read_bytes()) == before


@pytest.mark.parametrize("damage", ["bytes", "forged"])
def test_secondary_library_rejects_tampering_before_cloud(monkeypatch, source, renderer, optimized_photo, damage):
    photo, data, _original = optimized_photo
    cache = secondary_cache(source, photo, data)
    if damage == "bytes":
        (cache.parent / photo["path"]).write_bytes(b"tampered")
    else:
        manifest = json.loads(cache.read_text())
        manifest["photos"][0]["source_md5"] = "forged"
        cache.write_text(json.dumps(manifest))
        inventory = json.loads((cache.parent / "inventory.json").read_text())
        inventory["manifest.json"] = hashlib.sha256(cache.read_bytes()).hexdigest()
        (cache.parent / "inventory.json").write_text(json.dumps(inventory))
    monkeypatch.setattr(refresh.requests, "Session", lambda: pytest.fail("Invalid cache must precede network"))
    with pytest.raises(refresh.RefreshError, match="photo cache"):
        refresh.refresh_pois(*map(str, source), photo_mode="files", photo_cache_path=str(cache))
    assert not source[1].exists()


@pytest.mark.parametrize("fresh_location", ["snapshot", "library"])
def test_fresh_original_is_reused_when_other_cache_is_stale(monkeypatch, source, renderer, optimized_photo, fresh_location):
    old, encoded, original = optimized_photo
    body = b"Note\0new original metadata"
    chunk = struct.pack(">I", len(body)) + b"tEXt" + body + struct.pack(">I", zlib.crc32(b"tEXt" + body) & 0xffffffff)
    replacement = original[:-12] + chunk + original[-12:]
    current_data, metadata = compress_photo(replacement, "med")
    current = {**old, **metadata, "sha256": hashlib.sha256(current_data).hexdigest()}
    assert current_data == encoded
    library_photo = current if fresh_location == "library" else old
    primary_photo = current if fresh_location == "snapshot" else old
    cache = secondary_cache(source, library_photo, encoded)
    manifest = json.loads(source[0].read_text())
    manifest["photos"] = [primary_photo]
    source[0].write_text(json.dumps(manifest))
    forbid_media_gets(monkeypatch)
    install_session(monkeypatch, Response([document(images=["p.png"])]), Response(photo_metadata(replacement)))
    result = refresh.refresh_pois(*map(str, source), photo_mode="files", photo_cache_path=str(cache))
    exported = json.loads(Path(result["manifest_path"]).read_text())["photos"][0]
    assert exported["source_md5"] == current["source_md5"]
    assert exported["source_sha256"] == current["source_sha256"]
