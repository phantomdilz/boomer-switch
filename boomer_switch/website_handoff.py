"""Exchange a browser-bound authorization code for a home-scoped session."""

import hashlib
import hmac
import json
import re
import secrets
from urllib.parse import urlencode, urlsplit
import uuid

from .registration import AccessDenied, _digest


class WebsiteHandoff:
    def __init__(self, store, service_key, callback):
        target = urlsplit(callback)
        if target.scheme != "https" or not target.netloc or target.username or target.query or target.fragment:
            raise ValueError("Configure the exact HTTPS home callback.")
        if not isinstance(service_key, str) or len(service_key) < 32:
            raise ValueError("Configure a private service key of at least 32 characters.")
        self.store, self.key, self.callback = store, service_key, callback
        with store._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS home_sessions (id TEXT PRIMARY KEY, website_session TEXT NOT NULL, expires REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS website_handoffs (id TEXT PRIMARY KEY, website_session TEXT NOT NULL, challenge TEXT NOT NULL, expires REAL NOT NULL, request_id TEXT, home_expires REAL)")

    @staticmethod
    def _opaque(value):
        return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value)

    def issue(self, raw, challenge, state):
        if not self._opaque(challenge) or not self._opaque(state):
            raise ValueError
        self.store.require_access(raw)
        code = secrets.token_urlsafe(32)
        with self.store._transaction() as db:
            # Check access in the same transaction that creates the exchange.
            session = self.store._session(db, raw)
            guest = db.execute("SELECT * FROM guests WHERE email=?", (session["email"],)).fetchone()
            if not session["verified"] or not self.store._active(guest) or not guest["email_verified"]:
                raise AccessDenied("Current permission is unavailable.")
            db.execute("DELETE FROM website_handoffs WHERE expires<=?", (self.store.clock(),))
            db.execute("DELETE FROM website_handoffs WHERE website_session=? AND request_id IS NULL", (session["id"],))
            db.execute("INSERT INTO website_handoffs VALUES (?,?,?,?,NULL,NULL)", (_digest(code), session["id"], _digest(challenge), self.store.clock()+60))
        return {"redirect": self.callback + "?" + urlencode({"code": code, "state": state})}

    def exchange(self, environ):
        authorization = environ.get("HTTP_AUTHORIZATION", "")
        if environ.get("wsgi.url_scheme") != "https" or not hmac.compare_digest(authorization.encode(), ("Bearer " + self.key).encode()):
            raise AccessDenied("Service authentication required.")
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if not 0 < length <= 2048 or environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
            raise ValueError
        body = json.loads(environ["wsgi.input"].read(length))
        if not isinstance(body, dict) or set(body) != {"code", "challenge", "request_id"}:
            raise ValueError
        code, challenge, request_id = body["code"], body["challenge"], body["request_id"]
        if not self._opaque(code) or not self._opaque(challenge) or not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
            raise ValueError
        with self.store._transaction() as db:
            row = db.execute("SELECT * FROM website_handoffs WHERE id=? AND expires>?", (_digest(code), self.store.clock())).fetchone()
            if row is None or not hmac.compare_digest(row["challenge"], _digest(challenge)) or row["request_id"] not in (None, request_id):
                raise AccessDenied("Authorization exchange unavailable.")
            session = db.execute("SELECT * FROM sessions WHERE id=? AND verified=1 AND expires>?", (row["website_session"], self.store.clock())).fetchone()
            guest = db.execute("SELECT * FROM guests WHERE email=?", (session["email"],)).fetchone() if session else None
            if not self.store._active(guest) or not guest["email_verified"]:
                raise AccessDenied("Current permission is unavailable.")
            # Exact retries return the same opaque token after an ambiguous network result.
            token = hmac.new(self.key.encode(), ("home:"+code+":"+request_id).encode(), hashlib.sha256).hexdigest()
            expires = row["home_expires"] or min(session["expires"], self.store.clock()+86400)
            db.execute("INSERT OR IGNORE INTO home_sessions VALUES (?,?,?)", (_digest(token), row["website_session"], expires))
            db.execute("UPDATE website_handoffs SET request_id=?,home_expires=? WHERE id=?", (request_id, expires, row["id"]))
            return {"session": token, "expires": expires, "request_id": request_id}
