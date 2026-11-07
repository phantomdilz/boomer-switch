"""Serve private People controls behind server-verified owner authentication."""

from dataclasses import dataclass
import hashlib
import hmac
from http.cookies import SimpleCookie
from importlib.resources import files
import json
import secrets
import time
from urllib.parse import quote

from .registration import AccessDenied, RegistrationStore, StalePerson


@dataclass(frozen=True)
class OwnerPrincipal:
    subject: str
    session_id: str
    csrf: str


def owner_cookie(environ):
    cookie = SimpleCookie()
    try:
        cookie.load(environ.get("HTTP_COOKIE", ""))
        return cookie["bs_owner"].value if "bs_owner" in cookie else ""
    except Exception:
        return ""


def owner_cookie_path(environ):
    return quote(environ.get("SCRIPT_NAME", "") or "/", safe="/-._~")


class OwnerSessions:
    """Bind a verified owner identity to a server-side session and CSRF token."""

    def __init__(self, *, clock=time.time):
        self.sessions = {}
        self.clock = clock

    def issue(self, subject):
        self.sessions = {key: value for key, value in self.sessions.items() if value[1] > self.clock()}
        raw = secrets.token_urlsafe(32)
        identity = hashlib.sha256(raw.encode()).hexdigest()
        principal = OwnerPrincipal(subject, identity, secrets.token_urlsafe(32))
        self.sessions[identity] = (principal, self.clock() + 3600)
        return raw, principal

    def resolve(self, raw):
        identity = hashlib.sha256(raw.encode()).hexdigest()
        item = self.sessions.get(identity)
        if item is None or item[1] <= self.clock():
            return None
        return item[0]


class RemoteUserOwnerAuth:
    """Adapt an authenticated WSGI host's REMOTE_USER to the People application."""

    def __init__(self, app, owner_identity, *, clock=time.time):
        if not isinstance(owner_identity, str) or not owner_identity:
            raise ValueError("Configure the authenticated owner identity.")
        self.app = app
        self.owner_identity = owner_identity
        self.sessions = OwnerSessions(clock=clock)

    def __call__(self, environ, start_response):
        authenticated = environ.get("REMOTE_USER")
        owner = environ.get("wsgi.url_scheme") == "https" and isinstance(authenticated, str) and hmac.compare_digest(authenticated.encode(), self.owner_identity.encode())
        raw = owner_cookie(environ)
        principal = self.sessions.resolve(raw) if owner else None
        cookie = None
        if owner and (principal is None or principal.subject != authenticated):
            raw, principal = self.sessions.issue(authenticated)
            cookie = f"bs_owner={raw}; HttpOnly; SameSite=Strict; Path={owner_cookie_path(environ)}; Max-Age=3600"
            if environ.get("wsgi.url_scheme") == "https":
                cookie += "; Secure"
        # HTTP_REMOTE_USER and other request headers never establish identity.
        environ["boomer.owner_principal"] = principal

        def resolve_owner():
            current = environ.get("REMOTE_USER")
            resolved = self.sessions.resolve(raw) if owner else None
            if resolved is None or current != self.owner_identity or resolved.subject != current:
                return None
            return resolved

        environ["boomer.resolve_owner"] = resolve_owner

        def response(status, headers, exc_info=None):
            if cookie is not None:
                headers.append(("Set-Cookie", cookie))
            return start_response(status, headers, exc_info)

        return self.app(environ, response)


class PeopleApplication:
    """Project only owner-authorized records and commit versioned People actions."""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def _owner(environ):
        resolver = environ.get("boomer.resolve_owner")
        principal = resolver() if callable(resolver) else None
        if not isinstance(principal, OwnerPrincipal):
            raise AccessDenied("Sign in as the owner to open People.")
        return principal

    def __call__(self, environ, start_response):
        status = "200 OK"
        content_type = "application/json"
        try:
            principal = self._owner(environ)
            path, method = environ.get("PATH_INFO"), environ.get("REQUEST_METHOD")
            assets = {"/people": ("people.html", "text/html; charset=utf-8"),
                      "/people.js": ("people.js", "text/javascript; charset=utf-8")}
            if method == "GET" and path in assets:
                name, content_type = assets[path]
                payload = files("boomer_switch").joinpath("web", name).read_bytes()
            elif method == "GET" and path == "/style.css":
                content_type = "text/css; charset=utf-8"
                payload = files("boomer_switch").joinpath("web", "style.css").read_bytes()
            elif path == "/api/people" and method in ("GET", "POST"):
                if method == "POST":
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    if not 0 < length <= 4096 or environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
                        raise ValueError("Use a valid People request.")
                    body = json.loads(environ["wsgi.input"].read(length))
                    principal = self._owner(environ)
                    csrf = environ.get("HTTP_X_CSRF_TOKEN", "")
                    if not isinstance(csrf, str) or not hmac.compare_digest(csrf.encode(), principal.csrf.encode()):
                        raise AccessDenied("Refresh People before this action.")
                    expected = {"action", "email", "revision"}
                    if isinstance(body, dict) and body.get("action") == "expiry":
                        expected.add("expires")
                    if not isinstance(body, dict) or set(body) != expected:
                        raise ValueError("Use a valid People request.")
                    self.store.change_person(body["action"], body["email"], body["revision"], expires=body.get("expires"))
                payload = {"people": self.store.people(), "csrf": principal.csrf}
            else:
                status, payload = "404 Not Found", {"error": "Page unavailable."}
        except AccessDenied as error:
            status, payload = "403 Forbidden", {"error": str(error)}
        except StalePerson as error:
            status, payload = "409 Conflict", {"error": str(error)}
        except (ValueError, TypeError):
            status, payload = "400 Bad Request", {"error": "Use a valid People request."}
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode()
        headers = [("Content-Type", content_type), ("Content-Length", str(len(payload))),
                   ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
                   ("X-Content-Type-Options", "nosniff"),
                   ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")]
        start_response(status, headers)
        return [payload]


def create_people_application(directory, owner_identity):
    """Create the reusable website component for a trusted authenticated WSGI host."""
    return RemoteUserOwnerAuth(PeopleApplication(RegistrationStore(directory)), owner_identity)
