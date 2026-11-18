"""Explicit anonymous Firebase sign-in and private, refreshable HexaTrek sessions."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import quote

import requests

_MAX_JSON_BYTES = 65536
_BUNDLE_HEADERS = {"X-Ios-Bundle-Identifier": "com.hexatrek"}


class SessionError(ValueError):
    """Session failure whose message contains no credentials or response bodies."""


def _secret(value):
    return isinstance(value, str) and 0 < len(value) <= 16384 and all(33 <= ord(c) <= 126 for c in value)


def _api_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", value):
        raise SessionError("Invalid Firebase API key")
    return value


def _validate(data):
    if not isinstance(data, dict) or data.get("creation_pending"):
        raise SessionError("Session creation is pending or uncertain; automatic sign-up is refused")
    if not all(_secret(data.get(key)) for key in ("id_token", "refresh_token", "local_id")):
        raise SessionError("Invalid existing Firebase session")
    _api_key(data.get("api_key"))
    expiry = data.get("expires_at")
    try:
        valid_expiry = not isinstance(expiry, bool) and isinstance(expiry, (int, float)) and math.isfinite(expiry) and expiry > 0
    except OverflowError:
        valid_expiry = False
    if not valid_expiry:
        raise SessionError("Invalid existing Firebase session expiry")
    return data


@contextmanager
def _locked(path, *, create=False):
    fd = None
    created = False
    try:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        if create:
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                pass
        if fd is None:
            fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise SessionError("Session must be a private user-owned regular file (mode 0600), without hard links")
        fcntl.flock(fd, fcntl.LOCK_EX)
        # The inode remains the lock: never replace or unlink the session file.
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise SessionError("Session file changed while acquiring its lock")
        yield fd, created
    except OSError:
        raise SessionError("Cannot securely read or persist the Firebase session") from None
    finally:
        if fd is not None:
            os.close(fd)


def _read(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = bytearray()
    while len(chunks) <= _MAX_JSON_BYTES:
        chunk = os.read(fd, min(8192, _MAX_JSON_BYTES + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
    if len(chunks) > _MAX_JSON_BYTES:
        raise SessionError("Session file exceeds the permitted size")
    try:
        return _validate(json.loads(chunks))
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, SessionError):
            raise
        raise SessionError("Invalid existing Firebase session file; sign-up is refused") from None


def _persist(fd, data):
    raw = json.dumps(data, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > _MAX_JSON_BYTES:
        raise SessionError("Session exceeds the permitted size")
    os.lseek(fd, 0, os.SEEK_SET)
    remaining = memoryview(raw)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise SessionError("Cannot persist the Firebase session")
        remaining = remaining[written:]
    os.ftruncate(fd, len(raw))
    os.fsync(fd)


def _request(http_session, url, **payload):
    # Disable ambient proxy and netrc discovery even for caller-provided sessions.
    http_session.trust_env = False
    try:
        with http_session.request(
            "POST", url, headers=_BUNDLE_HEADERS, allow_redirects=False,
            timeout=(15, 60), stream=True, **payload,
        ) as response:
            if response.status_code != 200:
                raise SessionError(f"Firebase authentication failed (HTTP {response.status_code})")
            raw = bytearray()
            for chunk in response.iter_content(8192):
                if len(raw) + len(chunk) > _MAX_JSON_BYTES:
                    raise SessionError("Firebase authentication response exceeds the permitted size")
                raw.extend(chunk)
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                raise SessionError("Invalid Firebase authentication response") from None
            if not isinstance(data, dict):
                raise SessionError("Invalid Firebase authentication response")
            return data
    except requests.RequestException:
        raise SessionError("Firebase authentication request failed; session state may be uncertain") from None


def _expiry(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError
        seconds = float(value)
        expires_at = time.time() + seconds
        if not math.isfinite(seconds) or seconds <= 60 or not math.isfinite(expires_at):
            raise ValueError
        return expires_at
    except (ValueError, OverflowError):
        raise SessionError("Invalid Firebase authentication expiry") from None


class FirebaseSession:
    """Read the locked session on every use, including during long-running exports."""

    def __init__(self, session_file: str, http_session):
        self._session_file = session_file
        self._http_session = http_session

    def token(self) -> str:
        with _locked(self._session_file) as (fd, _):
            data = _read(fd)
            if time.time() < data["expires_at"] - 60:
                return data["id_token"]
            result = _request(
                self._http_session,
                "https://securetoken.googleapis.com/v1/token?key=" + quote(data["api_key"], safe=""),
                data={"grant_type": "refresh_token", "refresh_token": data["refresh_token"]},
            )
            if not _secret(result.get("id_token")) or not _secret(result.get("refresh_token")) or result.get("user_id") != data["local_id"]:
                raise SessionError("Invalid Firebase refresh response or changed user identity")
            data.update(id_token=result["id_token"], refresh_token=result["refresh_token"], expires_at=_expiry(result.get("expires_in")))
            _persist(fd, data)
            return data["id_token"]


def sign_in_anonymously(api_key: str, session_file: str) -> dict:
    """Create at most one anonymous user; an uncertain attempt requires manual recovery."""
    _api_key(api_key)
    path = os.path.abspath(os.path.expanduser(session_file))
    with _locked(path, create=True) as (fd, created):
        if not created:
            _read(fd)
            return {"session_file": path, "created": False}
        # Persist intent and its directory entry before an account can be created.
        _persist(fd, {"creation_pending": True})
        directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        with requests.Session() as http_session:
            result = _request(
                http_session,
                "https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=" + quote(api_key, safe=""),
                json={"returnSecureToken": True},
            )
        data = {
            "api_key": api_key, "id_token": result.get("idToken"),
            "refresh_token": result.get("refreshToken"), "local_id": result.get("localId"),
            "expires_at": _expiry(result.get("expiresIn")),
        }
        _validate(data)
        _persist(fd, data)
        return {"session_file": path, "created": True}
