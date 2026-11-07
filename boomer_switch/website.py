"""Serve production registration and private People from one canonical store."""

import hashlib
import hmac
from http.cookies import SimpleCookie
from importlib.resources import files
import json
import secrets
from urllib.parse import urlsplit

from .people import OwnerPrincipal, PeopleApplication, owner_cookie
from .registration import AccessDenied, _digest
from .website_mail import MailUnavailable


class WebsiteApplication:
    """Keep public registration separate from current server-authenticated People."""

    def __init__(self, store, mail, *, origin, mount, owner_identity, grant_app=None, handoff=None):
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("Configure the HTTPS website origin.")
        if not mount.startswith("/") or mount.endswith("/") or any(c not in "/abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in mount):
            raise ValueError("Configure a dedicated website mount.")
        if not isinstance(owner_identity, str) or not owner_identity:
            raise ValueError("Configure the owner identity.")
        self.store, self.mail, self.origin, self.mount = store, mail, origin, mount
        self.owner_identity, self.grant_app, self.handoff = owner_identity, grant_app, handoff
        self.people = PeopleApplication(store)
        with store._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS website_owners (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL)")

    def _owner(self, environ, headers):
        if environ.get("REMOTE_USER") != self.owner_identity:
            raise AccessDenied("Sign in as the owner to open People.")
        raw = owner_cookie(environ)
        with self.store._transaction() as db:
            row = db.execute("SELECT * FROM website_owners WHERE id=? AND expires>?", (_digest(raw), self.store.clock())).fetchone()
            if row is None:
                raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                db.execute("DELETE FROM website_owners WHERE expires<=?", (self.store.clock(),))
                db.execute("INSERT INTO website_owners VALUES (?,?,?)", (_digest(raw), csrf, self.store.clock()+3600))
                headers.append(("Set-Cookie", self._cookie("bs_owner", raw, 3600)))
            else:
                csrf = row["csrf"]
        principal = OwnerPrincipal(self.owner_identity, _digest(raw), csrf)
        def resolve():
            if environ.get("REMOTE_USER") != self.owner_identity:
                return None
            with self.store._transaction() as db:
                return principal if db.execute("SELECT 1 FROM website_owners WHERE id=? AND expires>?", (principal.session_id, self.store.clock())).fetchone() else None
        environ["boomer.resolve_owner"] = resolve

    def _cookie(self, name, raw, seconds=86400):
        return f"{name}={raw}; Secure; HttpOnly; SameSite=Strict; Path={self.mount}; Max-Age={seconds}"

    @staticmethod
    def _session(environ):
        cookie = SimpleCookie()
        try:
            cookie.load(environ.get("HTTP_COOKIE", ""))
            return cookie["bs_session"].value if "bs_session" in cookie else ""
        except Exception:
            return ""

    @staticmethod
    def _csrf(raw):
        return hashlib.sha256(("website-csrf:" + raw).encode()).hexdigest()

    def _body(self, environ, raw):
        if environ.get("HTTP_ORIGIN") != self.origin:
            raise AccessDenied("Open the website registration page.")
        csrf = environ.get("HTTP_X_CSRF_TOKEN", "")
        if not raw or not hmac.compare_digest(csrf, self._csrf(raw)):
            raise AccessDenied("Refresh the registration page.")
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if not 0 < length <= 2048 or environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
            raise ValueError
        body = json.loads(environ["wsgi.input"].read(length))
        if not isinstance(body, dict):
            raise ValueError
        return body

    def __call__(self, environ, start_response):
        headers, status = [], "200 OK"
        content_type = "application/json"
        path, method = environ.get("PATH_INFO", ""), environ.get("REQUEST_METHOD", "GET")
        try:
            if environ.get("wsgi.url_scheme") != "https":
                raise AccessDenied("Use HTTPS to open this page.")
            if path in ("/people", "/people.js", "/api/people"):
                if method == "POST" and environ.get("HTTP_ORIGIN") != self.origin:
                    raise AccessDenied("Open the private People page.")
                self._owner(environ, headers)
                def response(code, extra, exc_info=None):
                    return start_response(code, extra + headers, exc_info)
                return self.people(environ, response)
            if self.grant_app is not None and path == "/grant":
                return self.grant_app(environ, start_response)
            if self.handoff is not None and method == "POST" and path == "/exchange":
                payload = self.handoff.exchange(environ)
                encoded = json.dumps(payload).encode()
                start_response("200 OK", [("Content-Type", "application/json"), ("Cache-Control", "no-store"), ("Content-Length", str(len(encoded)))])
                return [encoded]
            raw = self._session(environ)
            if method == "GET" and path.startswith("/s/"):
                token = self.store.resolve_alias(path[3:])
                status, payload = "303 See Other", {}
                headers.append(("Location", self.mount + "/invite/" + token))
            elif method == "GET" and path.startswith("/invite/"):
                raw = self.store.browser(path[8:], existing=raw)
                status, payload = "303 See Other", {}
                headers += [("Location", self.mount + "/guest"), ("Set-Cookie", self._cookie("bs_session", raw))]
            elif method == "GET" and path in ("/", "/guest"):
                if self.store.status(raw) == "anonymous":
                    raw = self.store.browser()
                    headers.append(("Set-Cookie", self._cookie("bs_session", raw)))
                content_type = "text/html; charset=utf-8"
                payload = files("boomer_switch").joinpath("web", "website.html").read_bytes()
            elif method == "GET" and path in ("/website.js", "/style.css"):
                content_type = "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
                payload = files("boomer_switch").joinpath("web", path[1:]).read_bytes()
            elif method == "GET" and path == "/api/access":
                payload = {"status": self.store.status(raw), "csrf": self._csrf(raw)}
            elif method == "POST" and path == "/api/handoff" and self.handoff is not None:
                body = self._body(environ, raw)
                if set(body) != {"challenge", "state"}:
                    raise ValueError
                payload = self.handoff.issue(raw, body["challenge"], body["state"])
            elif method == "POST" and path in ("/api/register", "/api/resend", "/api/verify"):
                body = self._body(environ, raw)
                if path == "/api/register" and set(body) == {"name", "email"}:
                    self.store.start(raw, body["name"], body["email"])
                elif path == "/api/resend" and not body:
                    self.store.resend(raw)
                elif path == "/api/verify" and set(body) == {"code"}:
                    self.store.verify(raw, body["code"])
                else:
                    raise ValueError
                self.mail.deliver()
                payload = {"status": self.store.status(raw), "csrf": self._csrf(raw)}
            else:
                status, payload = "404 Not Found", {"error": "Page unavailable."}
        except AccessDenied as error:
            status, payload = "403 Forbidden", {"error": str(error)}
        except MailUnavailable as error:
            status, payload = "503 Service Unavailable", {"error": str(error)}
        except (ValueError, TypeError, UnicodeError):
            status, payload = "400 Bad Request", {"error": "Use a valid website request."}
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode()
        headers += [("Content-Type", content_type), ("Content-Length", str(len(payload))), ("Cache-Control", "no-store"),
                    ("Referrer-Policy", "no-referrer"), ("X-Content-Type-Options", "nosniff"),
                    ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
        start_response(status, headers)
        return [payload]
