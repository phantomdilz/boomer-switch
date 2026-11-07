"""Read current persisted authorization through an authenticated website endpoint."""

import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import sqlite3
import ssl
import time
from urllib.parse import urlsplit
import uuid


class PermissionUnavailable(ValueError):
    """Current website permission cannot be confirmed."""


def json_object(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field.")
            result[key] = value
        return result

    result = json.loads(data, object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError("Use a JSON object.")
    return result


def session_binding(session):
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", session):
        raise PermissionUnavailable("Current website permission is unavailable.")
    return hashlib.sha256(session.encode()).hexdigest()


def operation_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Use a canonical operation ID.")
    return value


class PersistedGrants:
    """Read the canonical verified-session and People tables without creating demo state."""

    def __init__(self, database: Path, *, clock=time.time):
        self.database = Path(database)
        self.clock = clock

    def deadline(self, session):
        identity = session_binding(session)
        now = self.clock()
        # Read-only mode must never create a missing authorization store.
        uri = self.database.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            db.execute("PRAGMA query_only=ON")
            row = db.execute("""SELECT home_sessions.expires,sessions.expires,guests.expires FROM home_sessions
                JOIN sessions ON sessions.id=home_sessions.website_session
                JOIN guests ON guests.email=sessions.email
                WHERE home_sessions.id=? AND home_sessions.expires>?
                AND sessions.verified=1 AND sessions.expires>?
                AND guests.status='approved' AND guests.email_verified=1
                AND (guests.expires IS NULL OR guests.expires>?)""", (identity, now, now, now)).fetchone()
            if row is None:
                return None
            values = [now + 2, row[0], row[1]] + ([] if row[2] is None else [row[2]])
            if not all(type(value) in (int, float) and math.isfinite(value) for value in values):
                return None
            return min(values)
        finally:
            db.close()


class WebsiteGrantApplication:
    """Expose only current permission to the authenticated home service over HTTPS."""

    def __init__(self, grants, service_key):
        if not isinstance(service_key, str) or len(service_key) < 32:
            raise ValueError("Configure a private service key of at least 32 characters.")
        self.grants, self.service_key = grants, service_key

    def __call__(self, environ, start_response):
        status, payload = "403 Forbidden", {"error": "Permission unavailable."}
        try:
            supplied = environ.get("HTTP_AUTHORIZATION", "")
            expected = "Bearer " + self.service_key
            authenticated = isinstance(supplied, str) and hmac.compare_digest(supplied.encode(), expected.encode())
            if not authenticated or environ.get("wsgi.url_scheme") != "https":
                raise PermissionUnavailable
            if environ.get("PATH_INFO") != "/grant" or environ.get("REQUEST_METHOD") != "POST":
                status, payload = "404 Not Found", {"error": "Page unavailable."}
            else:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                if not 0 < length <= 2048 or environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
                    raise ValueError
                raw = environ["wsgi.input"].read(length)
                if len(raw) != length:
                    raise ValueError
                body = json_object(raw)
                if not isinstance(body, dict) or set(body) != {"session", "request_id"}:
                    raise ValueError
                identity = session_binding(body["session"])
                operation_id(body["request_id"])
                # Check authentication again after a potentially slow body read.
                if environ.get("wsgi.url_scheme") != "https" or not hmac.compare_digest(environ.get("HTTP_AUTHORIZATION", "").encode(), expected.encode()):
                    raise PermissionUnavailable
                deadline = self.grants.deadline(body["session"])
                payload = {"allowed": deadline is not None, "request_id": body["request_id"],
                           "session_binding": identity, "valid_until": deadline or 0}
                status = "200 OK"
        except PermissionUnavailable:
            pass
        except (ValueError, TypeError, AttributeError):
            status, payload = "400 Bad Request", {"error": "Invalid permission request."}
        except Exception:
            status, payload = "503 Service Unavailable", {"error": "Permission unavailable."}
        content = json.dumps(payload).encode()
        start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(content))),
                                ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer")])
        return [content]


class HTTPSGrantClient:
    """Request a fresh website decision without redirects, proxy fallback, or cached grants."""

    def __init__(self, url, service_key, *, clock=time.time, session_factory=None):
        import aiohttp

        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Configure an explicit HTTPS permission endpoint.")
        if not isinstance(service_key, str) or len(service_key) < 32:
            raise ValueError("Configure a private service key of at least 32 characters.")
        self.url, self.service_key, self.clock = url, service_key, clock
        self.session_factory = session_factory or aiohttp.ClientSession
        self.timeout = aiohttp.ClientTimeout(total=2, connect=1, sock_read=1)
        self.ssl = ssl.create_default_context()

    async def require(self, session, request_id):
        identity = session_binding(session)
        operation_id(request_id)
        try:
            async with self.session_factory(timeout=self.timeout, trust_env=False) as client:
                async with client.post(self.url, json={"session": session, "request_id": request_id},
                        headers={"Authorization": "Bearer " + self.service_key}, ssl=self.ssl, allow_redirects=False) as response:
                    if response.status != 200:
                        raise PermissionUnavailable
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(1024):
                        data.extend(chunk)
                        if len(data) > 4096:
                            raise PermissionUnavailable
                    grant = json_object(data)
            if not isinstance(grant, dict) or set(grant) != {"allowed", "request_id", "session_binding", "valid_until"}:
                raise PermissionUnavailable
            deadline = grant["valid_until"]
            if grant["allowed"] is not True or grant["request_id"] != request_id or grant["session_binding"] != identity:
                raise PermissionUnavailable
            if type(deadline) not in (int, float) or not self.clock() < deadline <= self.clock() + 2:
                raise PermissionUnavailable
            return deadline
        except Exception:
            raise PermissionUnavailable("Current website permission is unavailable.") from None


def create_website_grant_application(database, service_key):
    """Mount at a protected website prefix with /grant as its only route."""
    return WebsiteGrantApplication(PersistedGrants(database), service_key)
