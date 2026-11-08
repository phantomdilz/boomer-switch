"""Retain legacy adapter imports and route the executable entry point to outbound polling."""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from http.cookies import SimpleCookie
from pathlib import Path
import sqlite3
import ssl
import time
import uuid
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

from .kasa_pilot import KasaPilotResolver
from .lights import CommandDenied, LightService, LightState, load_allowlist
from .website_grants import HTTPSGrantClient, PermissionUnavailable, json_object, operation_id, session_binding


@dataclass(frozen=True)
class GuestSession:
    session: str
    deadline: float


class ClosedIngress:
    """Deny all ingress until a real identity-bound Wi-Fi proof producer is integrated."""

    async def verify(self, environ):
        raise PermissionUnavailable("Verified home Wi-Fi ingress is not configured.")


class MTLSIngress:
    """Accept only the verified TLS peer of the dedicated guest-side producer."""

    def __init__(self, fingerprint, *, clock=time.time):
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
            raise ValueError("Configure the dedicated guest proxy certificate fingerprint.")
        self.fingerprint, self.clock = fingerprint, clock

    async def verify(self, environ):
        connection = environ.get("boomer.tls_connection")
        if not isinstance(connection, (ssl.SSLSocket, ssl.SSLObject)) or connection.context.verify_mode != ssl.CERT_REQUIRED:
            raise PermissionUnavailable("Authenticated guest ingress is unavailable.")
        certificate = connection.getpeercert(binary_form=True)
        if not certificate or hashlib.sha256(certificate).hexdigest() != self.fingerprint:
            raise PermissionUnavailable("Authenticated guest ingress is unavailable.")
        deadline = float(environ.get("HTTP_X_PILOT_DEADLINE", "0"))
        now = self.clock()
        if not now < deadline <= now + 8:
            raise PermissionUnavailable("The accepted request lifetime expired.")
        if any(key.startswith(("HTTP_X_FORWARDED", "HTTP_TAILSCALE")) or key in ("HTTP_FORWARDED", "HTTP_X_REAL_IP") for key in environ):
            raise PermissionUnavailable("Forwarded ingress is unavailable.")
        cookie = SimpleCookie()
        cookie.load(environ.get("HTTP_COOKIE", ""))
        raw = cookie["bs_session"].value if "bs_session" in cookie else ""
        session_binding(raw)
        return GuestSession(raw, deadline)


class DuplicateCommand(ValueError):
    """This operation ID already has a recorded submission."""


class CommandJournal:
    """Reserve operation IDs durably before a possible physical effect."""

    def __init__(self, path, device_id, *, allow_fleet=False):
        self.path = Path(path)
        self.identity = hashlib.sha256(device_id.encode()).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if not allow_fleet and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_targets'").fetchone():
                    raise ValueError("Use the multi-light runner for this journal.")
                db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,status TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS pilot_identity (id INTEGER PRIMARY KEY CHECK(id=1),binding TEXT NOT NULL)")
                bound = db.execute("SELECT binding FROM pilot_identity WHERE id=1").fetchone()
                if bound is None:
                    if db.execute("SELECT 1 FROM commands LIMIT 1").fetchone():
                        raise ValueError("Preserve and reconcile the existing unbound command journal before use.")
                    db.execute("INSERT INTO pilot_identity VALUES (1,?)", (self.identity,))
                elif bound[0] != self.identity:
                    raise ValueError("This journal belongs to another physical pilot device.")
        finally:
            db.close()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=1)

    def reserve(self, identity, light_id, action, request_id):
        fingerprint = hashlib.sha256(json.dumps([identity, light_id, action]).encode()).hexdigest()
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM commands WHERE status IN ('started','dispatched','unknown') LIMIT 1").fetchone():
                    raise DuplicateCommand("Read confirmed state before another command.")
                db.execute("INSERT INTO commands VALUES (?,?,'reserved')", (request_id, fingerprint))
        except sqlite3.IntegrityError:
            raise DuplicateCommand("Command already submitted. Read state before a new command.") from None
        finally:
            db.close()

    def dispatched(self, request_id):
        db = self._connect()
        try:
            with db:
                changed = db.execute("UPDATE commands SET status='dispatched' WHERE id=? AND status='reserved'", (request_id,)).rowcount
                if changed != 1:
                    raise DuplicateCommand("Command already submitted.")
        finally:
            db.close()

    def reconcile(self):
        db = self._connect()
        try:
            with db:
                db.execute("UPDATE commands SET status='reconciled' WHERE status IN ('started','dispatched','unknown')")
        finally:
            db.close()

    def finish(self, request_id, state):
        db = self._connect()
        try:
            with db:
                db.execute("UPDATE commands SET status=? WHERE id=?", (state, request_id))
        finally:
            db.close()


class PilotBackend:
    """Own current authorization, one selected lamp, and duplicate protection."""

    def __init__(self, light, resolver, grants, journal, *, ingress=None, clock=time.time):
        self.light, self.resolver, self.grants, self.journal = light, resolver, grants, journal
        if journal.identity != hashlib.sha256(light.device_id.encode()).hexdigest():
            raise ValueError("Use the journal bound to this physical pilot device.")
        self.ingress = ingress or ClosedIngress()
        self.clock = clock
        self.service = LightService((light,), resolver, timeout_seconds=8)
        self.lock = asyncio.Lock()

    def _current(self, context, grant_deadline):
        now = self.clock()
        if type(grant_deadline) not in (int, float) or not now < grant_deadline <= now + 2 or not now < context.deadline:
            raise PermissionUnavailable("Current authorization lifetime expired.")

    async def operate(self, environ, *, action=None, request_id=None):
        request_id = operation_id(request_id) if action is not None else str(uuid.uuid4())
        if action is not None and action not in ("on", "off"):
            raise ValueError("Use on or off.")
        if self.lock.locked():
            raise DuplicateCommand("The pilot is busy. Read state before another command.")
        async with asyncio.timeout(8), self.lock:
            context = await self.ingress.verify(environ)
            if not isinstance(context, GuestSession) or context.deadline <= self.clock():
                raise PermissionUnavailable("Current guest identity is unavailable.")
            identity = session_binding(context.session)
            grant_deadline = await self.grants.require(context.session, request_id)
            self._current(context, grant_deadline)
            attempted = False

            async def guard():
                nonlocal attempted
                try:
                    fresh = await self.ingress.verify(environ)
                    if not isinstance(fresh, GuestSession) or fresh.session != context.session or fresh.deadline != context.deadline:
                        raise PermissionUnavailable
                    deadline = await self.grants.require(context.session, request_id)
                    self._current(fresh, deadline)
                    if action is not None:
                        self.journal.dispatched(request_id)
                        self._current(fresh, deadline)
                        attempted = True
                except PermissionUnavailable:
                    raise CommandDenied("Current identity, presence, or permission is unavailable.") from None

            if action is not None:
                self.journal.reserve(identity, self.light.light_id, action, request_id)
            try:
                if action is None:
                    state = await self.service.state(self.light.light_id, guard=guard)
                    if state != LightState.UNAVAILABLE:
                        self.journal.reconcile()
                    return {"lights": [self.service.lights()[0] | {"state": str(state)}]}
                state = await self.service.command(self.light.light_id, action, guard=guard)
                outcome = "unknown" if attempted and state == LightState.UNAVAILABLE else "finished" if attempted else "not_started"
                self.journal.finish(request_id, outcome)
                return {"light_id": self.light.light_id, "state": str(state), "operation_id": request_id}
            except CommandDenied:
                if action is not None and not attempted:
                    self.journal.finish(request_id, "denied")
                raise
            finally:
                await self.resolver.close()


class PilotApplication:
    """Expose only guarded state and explicit commands, without guest-auth fallback."""

    def __init__(self, backend):
        self.backend, self.runner = backend, asyncio.Runner()

    def close(self):
        self.runner.close()

    def __call__(self, environ, start_response):
        status, payload = "404 Not Found", {"error": "Page unavailable."}
        try:
            self.runner.run(self.backend.ingress.verify(environ))
            path, method = environ.get("PATH_INFO"), environ.get("REQUEST_METHOD")
            if path == "/api/lights" and method == "GET":
                payload = self.runner.run(self.backend.operate(environ))
                status = "200 OK"
            elif path == "/api/lights/" + self.backend.light.light_id and method == "POST":
                length = int(environ.get("CONTENT_LENGTH") or "0")
                if not 0 < length <= 1024 or environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
                    raise ValueError
                raw = environ["wsgi.input"].read(length)
                if len(raw) != length:
                    raise ValueError
                body = json_object(raw)
                if not isinstance(body, dict) or set(body) != {"action", "operation_id"}:
                    raise ValueError
                payload = self.runner.run(self.backend.operate(environ, action=body["action"], request_id=body["operation_id"]))
                status = "200 OK"
        except (PermissionUnavailable, CommandDenied):
            status, payload = "403 Forbidden", {"error": "Current identity, presence, or permission is unavailable."}
        except DuplicateCommand:
            status, payload = "409 Conflict", {"error": "Command already submitted. Read current state."}
        except (ValueError, TypeError, AttributeError):
            status, payload = "400 Bad Request", {"error": "Use an explicit valid command."}
        except Exception:
            status, payload = "503 Service Unavailable", {"error": "Pilot unavailable."}
        content = json.dumps(payload).encode()
        start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(content))),
            ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer")])
        return [content]


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *_args):
        pass

    def get_environ(self):
        environ = super().get_environ()
        environ["boomer.tls_connection"] = self.connection
        environ["HTTPS"] = "on"
        return environ


class _BoundedServer(WSGIServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        try:
            return self.tls_context.wrap_socket(connection, server_side=True), address
        except BaseException:
            connection.close()
            raise


@contextmanager
def runtime_lock(database):
    """Allow one normal pilot process to own a command journal."""
    path = Path(str(database) + ".lock")
    with path.open("a+b") as file:
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Another pilot process owns this command journal.") from None
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def load_pilot(path):
    from kasa import Credentials

    try:
        config = json_object(Path(path).read_text(encoding="utf-8"))
        required = {"allowlist_file", "light_id", "device_host", "website_grant_url", "website_key", "operation_db", "bind_address", "server_cert", "server_key", "guest_client_ca", "guest_client_fingerprint"}
        if not isinstance(config, dict) or not required <= set(config) or set(config) - required - {"username", "password"}:
            raise ValueError
        base = Path(path).resolve().parent
        selected = [light for light in load_allowlist(base / config["allowlist_file"]) if light.light_id == config["light_id"]]
        if len(selected) != 1 or selected[0].outlet_id is not None:
            raise ValueError
        credentials = None
        if "username" in config or "password" in config:
            if not all(isinstance(config.get(key), str) and config[key] for key in ("username", "password")):
                raise ValueError
            credentials = Credentials(config["username"], config["password"])
        resolver = KasaPilotResolver(selected[0], config["device_host"], credentials=credentials)
        grants = HTTPSGrantClient(config["website_grant_url"], config["website_key"])
        journal = CommandJournal(base / config["operation_db"], selected[0].device_id)
        import ipaddress

        address = ipaddress.ip_address(config["bind_address"])
        if address.is_unspecified or address.is_multicast or address.is_global:
            raise ValueError
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(base / config["server_cert"], base / config["server_key"])
        tls.load_verify_locations(cafile=base / config["guest_client_ca"])
        tls.verify_mode = ssl.CERT_REQUIRED
        backend = PilotBackend(selected[0], resolver, grants, journal, ingress=MTLSIngress(config["guest_client_fingerprint"]))
        return backend, str(address), tls
    except Exception:
        raise ValueError("Read a valid explicit private pilot configuration.") from None


def main():
    from .home_runner import main as outbound_main

    outbound_main()


if __name__ == "__main__":
    main()
