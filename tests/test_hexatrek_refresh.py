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
def test_multi_picture_jpeg_is_preserved_only_with_complete_images(monkeypatch, source, renderer, damage):
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
        assert (source[1] / photo["path"]).read_bytes() == data
        assert photo["comment_id"] == "new"


def test_exact_comment_photo_binding_and_content_hash(monkeypatch, source, renderer):
    data = png()
    session = install_session(monkeypatch, Response([document(images=["photo space.png"]), document("old", "2026-10-01T00:00:00Z", ["other.png"])]), Response(data), Response(data))
    result = refresh.refresh_pois(*map(str, source), photo_mode="files")
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert result["photo_count"] == 2
    photos = manifest["photos"]
    assert [(photo["poi_id"], photo["comment_id"], photo["source_image"]) for photo in photos] == [("1:2:1000", "new", "photo space.png"), ("1:2:1000", "old", "other.png")]
    digest = hashlib.sha256(data).hexdigest()
    assert {photo["path"] for photo in photos} == {f"photos/{digest}.png"}
    assert (source[1] / photos[0]["path"]).read_bytes() == data
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
