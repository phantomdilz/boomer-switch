"""Serve production registration and private People from one canonical store."""

import hashlib
import hmac
from http.cookies import SimpleCookie
from importlib.resources import files
import json
import secrets
from urllib.parse import urlencode, urlsplit

from .people import OwnerPrincipal, PeopleApplication, owner_cookie
from .registration import AccessDenied, InvalidInvitation, _digest
from .website_mail import MailUnavailable
from .home_rendezvous import HomeUnavailable, OperationConflict
from .website_grants import json_object


OWNER_PATHS = ("/people", "/people.js", "/api/people")
REGISTRATION_MAIL_PATHS = ("/api/register", "/api/resend", "/api/verify")


class WebsiteApplication:
    """Keep public registration separate from current server-authenticated People."""

    def __init__(self, store, mail, *, origin, mount, owner_identity, grant_app=None, handoff=None, rendezvous=None):
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("Configure the HTTPS website origin.")
        if not mount.startswith("/") or mount.endswith("/") or any(c not in "/abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in mount):
            raise ValueError("Configure a dedicated website mount.")
        if not isinstance(owner_identity, str) or not owner_identity:
            raise ValueError("Configure the owner identity.")
        self.store, self.mail, self.origin, self.mount = store, mail, origin, mount
        self.owner_identity, self.grant_app, self.handoff = owner_identity, grant_app, handoff
        self.rendezvous = rendezvous
        self.people = PeopleApplication(store)
        with store._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS website_owners (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL)")

    def _owner(self, environ, headers):
        def current():
            resolver = environ.get("boomer.resolve_identity")
            return resolver() if callable(resolver) else environ.get("REMOTE_USER")
        if current() != self.owner_identity:
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
            if current() != self.owner_identity:
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

    def _bootstrap(self, raw):
        with self.store._transaction() as db:
            now = self.store.clock()
            if db.execute("SELECT 1 FROM sessions WHERE id=? AND expires>?", (_digest(raw), now)).fetchone():
                return raw, False
            db.execute("DELETE FROM challenges WHERE session IN (SELECT id FROM sessions WHERE expires<=?)", (now,))
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            if db.execute("SELECT COUNT(*) FROM sessions WHERE verified=0").fetchone()[0] >= 1000:
                raise AccessDenied("Registration is busy. Try again later.")
            raw = secrets.token_urlsafe(32)
            db.execute("INSERT INTO sessions(id,expires) VALUES (?,?)", (_digest(raw), now+self.store.SESSION_SECONDS))
            return raw, True

    @staticmethod
    def _csrf(raw):
        return hashlib.sha256(("website-csrf:" + raw).encode()).hexdigest()

    def _access(self, raw, environ=None):
        status = self.store.status(raw)
        result = {"status": status, "csrf": self._csrf(raw)}
        if self.rendezvous is not None:
            if status == "approved":
                result["home_status"] = self.rendezvous.access_status(raw, environ or {})
            result["home_ready"] = result.get("home_status") == "ready"
            if result["home_ready"]:
                result["light"] = self.rendezvous.display(raw, environ or {})
        if self.rendezvous is None and status == "approved" and self.handoff is not None:
            result["home_entry"] = self.handoff.entry
        return result

    def _body(self, environ, raw):
        if environ.get("HTTP_ORIGIN") != self.origin:
            raise AccessDenied("Open the website registration page.")
        csrf = environ.get("HTTP_X_CSRF_TOKEN", "")
        if not raw or not hmac.compare_digest(csrf, self._csrf(raw)):
            raise AccessDenied("Refresh the registration page.")
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if not 0 < length <= 2048 or environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
            raise ValueError
        body = json_object(environ["wsgi.input"].read(length))
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
            if path in OWNER_PATHS:
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
            if self.rendezvous is not None and path.startswith("/home/") and method == "POST":
                length = int(environ.get("CONTENT_LENGTH") or 0)
                if not 0 < length <= 2048 or environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
                    raise ValueError
                body = json_object(environ["wsgi.input"].read(length))
                payload = self.rendezvous.machine(environ, path, body)
                encoded = json.dumps(payload).encode()
                start_response("200 OK", [("Content-Type", "application/json"), ("Cache-Control", "no-store"), ("Content-Length", str(len(encoded)))])
                return [encoded]
            raw = self._session(environ)
            if method == "GET" and path.startswith("/s/"):
                token = self.store.resolve_alias(path[3:])
                status, payload = "303 See Other", {}
                headers.append(("Location", self.mount + "/guest#" + urlencode({"invitation": token})))
            elif method == "GET" and path.startswith("/invite/"):
                # Finish the invitation after a same-origin API call restores Strict cookies.
                token = path[8:]
                status, payload = "303 See Other", {}
                headers.append(("Location", self.mount + "/guest#" + urlencode({"invitation": token})))
            elif method == "GET" and path in ("/", "/guest"):
                # A cross-site navigation can suppress a Strict cookie.
                # Bootstrap only after the page makes its same-origin API call.
                content_type = "text/html; charset=utf-8"
                payload = files("boomer_switch").joinpath("web", "website.html").read_bytes()
            elif method == "GET" and path in ("/entry.js", "/website.js", "/style.css"):
                content_type = "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
                payload = files("boomer_switch").joinpath("web", path[1:]).read_bytes()
            elif method == "GET" and path == "/api/access":
                raw, issued = self._bootstrap(raw)
                if issued:
                    headers.append(("Set-Cookie", self._cookie("bs_session", raw)))
                payload = self._access(raw, environ)
            elif method == "POST" and path == "/api/lights" and self.rendezvous is not None:
                payload = self.rendezvous.enqueue(raw, environ, self._body(environ, raw))
                status = "202 Accepted"
            elif method == "GET" and path.startswith("/api/operations/") and self.rendezvous is not None:
                payload = self.rendezvous.operation(raw, environ, path.rsplit("/",1)[1])
            elif method == "POST" and path == "/api/invite":
                body = self._body(environ, raw)
                if set(body) != {"invitation"}:
                    raise ValueError
                try:
                    if not isinstance(body["invitation"], str):
                        raise InvalidInvitation("Invitation unavailable.")
                    new_raw = self.store.browser(body["invitation"], existing=raw)
                except InvalidInvitation:
                    if self.store.reject_browser(raw):
                        headers.append(("Set-Cookie", self._cookie("bs_session", "", 0)))
                    raise
                if new_raw != raw:
                    headers.append(("Set-Cookie", self._cookie("bs_session", new_raw)))
                payload = self._access(new_raw, environ)
            elif method == "POST" and path == "/api/handoff" and self.handoff is not None:
                body = self._body(environ, raw)
                if set(body) != {"challenge", "state"}:
                    raise ValueError
                payload = self.handoff.issue(raw, body["challenge"], body["state"])
            elif method == "POST" and path in REGISTRATION_MAIL_PATHS:
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
                payload = self._access(raw, environ)
            else:
                status, payload = "404 Not Found", {"error": "Page unavailable."}
        except OperationConflict as error:
            status, payload = "409 Conflict", {"error": str(error)}
        except InvalidInvitation as error:
            if method == "GET":
                status, payload = "303 See Other", {}
                headers.append(("Location", self.mount + "/guest#invitation="))
            else:
                status, payload = "403 Forbidden", {"error": str(error), "code": "invitation_unavailable"}
        except AccessDenied as error:
            status, payload = "403 Forbidden", {"error": str(error)}
        except HomeUnavailable as error:
            status, payload = "503 Service Unavailable", {"error": str(error)}
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
