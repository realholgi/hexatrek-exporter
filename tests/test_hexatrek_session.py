"""Synthetic public-interface checks; these tests never contact Firebase."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat
import threading

import pytest
import requests

from hexatrek_exporter import hexatrek_session as sessions
from hexatrek_exporter.hexatrek_session import FirebaseSession, SessionError, sign_in_anonymously


class Response:
    def __init__(self, body, status=200):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


class HTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live HTTP is forbidden")
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)


def signup_response():
    return Response({"idToken": "secret-id", "refreshToken": "secret-refresh", "localId": "user-1", "expiresIn": "3600"})


def existing(tmp_path, **changes):
    data = {"api_key": "secret-api", "id_token": "secret-id", "refresh_token": "secret-refresh", "local_id": "user-1", "expires_at": 4600.0}
    data.update(changes)
    path = tmp_path / "session.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def test_signup_reuses_existing_session_without_second_account(tmp_path, monkeypatch):
    http = HTTP(signup_response())
    monkeypatch.setattr(sessions.requests, "Session", lambda: http)
    path = tmp_path / "session.json"
    assert sign_in_anonymously("secret-api", str(path)) == {"session_file": str(path), "created": True}
    assert sign_in_anonymously("secret-api", str(path)) == {"session_file": str(path), "created": False}
    assert FirebaseSession(str(path), http).token() == "secret-id"
    assert len(http.calls) == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert http.trust_env is False
    method, url, kwargs = http.calls[0]
    assert method == "POST"
    assert url == "https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=secret-api"
    assert kwargs["json"] == {"returnSecureToken": True}
    assert kwargs["headers"] == {"X-Ios-Bundle-Identifier": "com.hexatrek"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True


def test_refresh_rotation_persists_across_restart_and_long_batch(tmp_path, monkeypatch):
    path = existing(tmp_path)
    http = HTTP(Response({"id_token": "new-id", "refresh_token": "rotated-refresh", "user_id": "user-1", "expires_in": "3600"}))
    session = FirebaseSession(str(path), http)
    assert session.token() == "secret-id"
    monkeypatch.setattr(sessions.time, "time", lambda: 4540.0)
    assert session.token() == "new-id"
    assert http.calls[0][2]["data"] == {"grant_type": "refresh_token", "refresh_token": "secret-refresh"}
    assert http.calls[0][1] == "https://securetoken.googleapis.com/v1/token?key=secret-api"
    assert http.calls[0][2]["allow_redirects"] is False
    stored = json.loads(path.read_text())
    assert stored["refresh_token"] == "rotated-refresh"
    assert stored["local_id"] == "user-1"
    assert stored["expires_at"] == 8140.0
    assert FirebaseSession(str(path), HTTP()).token() == "new-id"
    monkeypatch.setattr(sessions.time, "time", lambda: 8080.0)
    second = HTTP(Response({"id_token": "third-id", "refresh_token": "third-refresh", "user_id": "user-1", "expires_in": "3600"}))
    assert FirebaseSession(str(path), second).token() == "third-id"
    assert second.calls[0][2]["data"]["refresh_token"] == "rotated-refresh"


@pytest.mark.parametrize("body", [b"", b"not-json", b"{}", b'{"creation_pending":true}', b"x" * 65537])
def test_invalid_existing_file_never_signs_up(tmp_path, monkeypatch, body):
    path = tmp_path / "session.json"
    path.write_bytes(body)
    path.chmod(0o600)
    http = HTTP()
    monkeypatch.setattr(sessions.requests, "Session", lambda: http)
    with pytest.raises(SessionError):
        sign_in_anonymously("secret-api", str(path))
    assert not http.calls


@pytest.mark.parametrize("mode", [0o644, 0o660])
def test_insecure_session_permissions_are_rejected(tmp_path, mode):
    path = existing(tmp_path)
    path.chmod(mode)
    with pytest.raises(SessionError):
        FirebaseSession(str(path), HTTP()).token()
    with pytest.raises(SessionError):
        sign_in_anonymously("secret-api", str(path))


def test_symlinks_and_hardlinks_are_rejected(tmp_path):
    path = existing(tmp_path)
    link = tmp_path / "symlink"
    link.symlink_to(path)
    for operation in (lambda: FirebaseSession(str(link), HTTP()).token(), lambda: sign_in_anonymously("secret-api", str(link))):
        with pytest.raises(SessionError):
            operation()
    hardlink = tmp_path / "hardlink"
    os.link(path, hardlink)
    with pytest.raises(SessionError):
        FirebaseSession(str(path), HTTP()).token()


def test_non_user_owned_file_is_rejected(tmp_path, monkeypatch):
    path = existing(tmp_path)
    monkeypatch.setattr(sessions.os, "getuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(SessionError):
        FirebaseSession(str(path), HTTP()).token()


def test_ambiguous_signup_failure_is_never_retried(tmp_path, monkeypatch):
    path = tmp_path / "session.json"
    http = HTTP(requests.Timeout("https://secret-api/?secret-refresh"))
    monkeypatch.setattr(sessions.requests, "Session", lambda: http)
    with pytest.raises(SessionError) as error:
        sign_in_anonymously("secret-api", str(path))
    assert "secret" not in str(error.value)
    assert json.loads(path.read_text()) == {"creation_pending": True}
    with pytest.raises(SessionError):
        sign_in_anonymously("secret-api", str(path))
    assert len(http.calls) == 1


def test_marker_is_durable_before_signup(tmp_path, monkeypatch):
    path = tmp_path / "session.json"
    http = HTTP(signup_response())
    original = http.request
    syncs = []
    real_fsync = sessions.os.fsync
    def fsync(fd):
        real_fsync(fd)
        syncs.append(fd)
    def request(*args, **kwargs):
        assert len(syncs) == 2  # file plus directory entry
        assert json.loads(path.read_text()) == {"creation_pending": True}
        return original(*args, **kwargs)
    http.request = request
    monkeypatch.setattr(sessions.os, "fsync", fsync)
    monkeypatch.setattr(sessions.requests, "Session", lambda: http)
    sign_in_anonymously("secret-api", str(path))


def test_failed_signup_persistence_never_creates_second_user(tmp_path, monkeypatch):
    path = tmp_path / "session.json"
    http = HTTP(signup_response())
    monkeypatch.setattr(sessions.requests, "Session", lambda: http)
    real_write = sessions.os.write
    calls = []
    def write(fd, raw):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError("secret-refresh")
        return real_write(fd, raw)
    monkeypatch.setattr(sessions.os, "write", write)
    with pytest.raises(SessionError):
        sign_in_anonymously("secret-api", str(path))
    with pytest.raises(SessionError):
        sign_in_anonymously("secret-api", str(path))
    assert len(http.calls) == 1


@pytest.mark.parametrize("response", [Response(b"secret-id secret-refresh secret-api", 401), requests.ConnectionError("https://secret-api/?secret-refresh"), Response(b"secret-id"), Response(b"x" * 65537), Response({}, 302)])
def test_auth_errors_redact_secrets_and_do_not_sign_up(tmp_path, response):
    path = existing(tmp_path, expires_at=1059.0)
    http = HTTP(response)
    session = FirebaseSession(str(path), http)
    with pytest.raises(SessionError) as error:
        session.token()
    assert all(secret not in str(error.value) for secret in ("secret-id", "secret-refresh", "secret-api"))
    assert "secret" not in repr(session)
    assert len(http.calls) == 1
    assert "accounts:signUp" not in http.calls[0][1]
    assert json.loads(path.read_text())["refresh_token"] == "secret-refresh"


def test_refresh_rejects_changed_identity(tmp_path):
    path = existing(tmp_path, expires_at=1059.0)
    http = HTTP(Response({"id_token": "new-id", "refresh_token": "new-refresh", "user_id": "another-user", "expires_in": "3600"}))
    with pytest.raises(SessionError):
        FirebaseSession(str(path), http).token()
    assert json.loads(path.read_text())["local_id"] == "user-1"


def test_concurrent_signup_uses_single_locked_file(tmp_path, monkeypatch):
    path = tmp_path / "session.json"
    entered = threading.Event()
    release = threading.Event()
    http = HTTP(signup_response())
    original = http.request
    def request(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    http.request = request
    monkeypatch.setattr(sessions.requests, "Session", lambda: http)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(sign_in_anonymously, "secret-api", str(path))
        assert entered.wait(5)
        second = executor.submit(sign_in_anonymously, "secret-api", str(path))
        release.set()
        assert first.result()["created"] is True
        assert second.result()["created"] is False
    assert len(http.calls) == 1


def test_concurrent_refresh_preserves_rotation(tmp_path):
    path = existing(tmp_path, expires_at=1059.0)
    http = HTTP(Response({"id_token": "new-id", "refresh_token": "rotated-refresh", "user_id": "user-1", "expires_in": "3600"}))
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(FirebaseSession(str(path), http).token) for _ in range(2)]
        assert [future.result() for future in futures] == ["new-id", "new-id"]
    assert len(http.calls) == 1
    assert json.loads(path.read_text())["refresh_token"] == "rotated-refresh"
