"""Adapt the production website to a verified Apache Passenger boundary."""

import hashlib
from http.cookies import SimpleCookie
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

from .registration import AccessDenied
from .website import OWNER_PATHS, REGISTRATION_MAIL_PATHS
from .website_cgi import create_application
from .website_grants import json_object
from .website_mail import MailPaused
from .private_process import exact_private_reply, private_command
from .owner_notifications import OWNER_DECISION_PATH


SWITCHES = {"registration_enabled", "mail_enabled", "people_write_enabled", "pilot_enabled", "apache_peer_verified"}


class PHPValidator:
    """Pass one bounded cookie to the existing private foreground PHP validator."""

    def __init__(self, command, cookie_name):
        command = private_command(command)
        if not isinstance(cookie_name, str) or not cookie_name or len(cookie_name) > 128 or any(char in cookie_name for char in " ;=\r\n"):
            raise ValueError("Configure the existing owner cookie name.")
        self.command, self.cookie_name = tuple(command), cookie_name

    def check(self, environ):
        header = environ.get("HTTP_COOKIE", "")
        try:
            if not isinstance(header, str) or len(header) > 8192:
                return False
            cookies = SimpleCookie(header)
            if self.cookie_name not in cookies:
                return False
            value = cookies[self.cookie_name].value.encode("utf-8")
            if not 0 < len(value) <= 1024:
                return False
            return exact_private_reply(self.command, value, b"MATCH")
        except Exception:
            return False


class _PolicyInput:
    def __init__(self, source, check):
        self.source, self.check = source, check

    def read(self, length=-1):
        if not 0 <= length <= 4096:
            raise AccessDenied("Use a bounded website request.")
        body = self.source.read(length)
        self.check()
        return body


class _GuardedMail:
    def __init__(self, transport, enabled):
        self.transport, self.enabled = transport, enabled

    def send(self, message):
        try:
            enabled = self.enabled()
        except Exception:
            enabled = False
        if not enabled:
            raise MailPaused("Website mail is paused.")
        self.transport.send(message)

    def prepare(self, db, row):
        return self.transport.prepare(db, row)


def _read_config(path):
    config = json_object(Path(path).read_text(encoding="utf-8"))
    passenger = config.get("passenger")
    if not isinstance(passenger, dict) or set(passenger) != SWITCHES | {"owner_cookie", "owner_validator_command"}:
        raise ValueError("Configure the private Passenger boundary and explicit switches.")
    if any(type(passenger[key]) is not bool for key in SWITCHES):
        raise ValueError("Use explicit Boolean Passenger switches.")
    return config


def _identity(config):
    fixed = config | {"passenger": {key: value for key, value in config["passenger"].items() if key not in SWITCHES}}
    return hashlib.sha256(json.dumps(fixed, sort_keys=True, separators=(",", ":")).encode()).digest()


class PassengerApplication:
    """Keep hosting proof, owner validation, and reversible rollout gates outside app policy."""

    def __init__(self, config_path, *, factory=create_application, validator_factory=PHPValidator):
        self.config_path = Path(config_path)
        if not self.config_path.is_absolute():
            raise ValueError("Configure an absolute private Passenger configuration path.")
        self.config = _read_config(self.config_path)
        self.identity = _identity(self.config)
        self.origin = urlsplit(self.config["origin"])
        self.mount = self.config["mount"]
        policy = self.config["passenger"]
        self.validator = validator_factory(policy["owner_validator_command"], policy["owner_cookie"])
        self.app = factory(self.config)
        self.app.mail.transport = _GuardedMail(self.app.mail.transport, self._mail_enabled)
        if self.app.mail.owner_notifications is not None:
            self.app.mail.owner_notifications = _GuardedMail(self.app.mail.owner_notifications, self._mail_enabled)

    def _policy(self):
        current = _read_config(self.config_path)
        if _identity(current) != self.identity:
            raise ValueError("Restart Passenger after an approved fixed configuration change.")
        return current["passenger"]

    def _mail_enabled(self):
        policy = self._policy()
        return policy["registration_enabled"] and policy["mail_enabled"]

    def _transport(self, environ):
        host = urlsplit("//" + environ.get("HTTP_HOST", ""))
        if (environ.get("wsgi.url_scheme") != "https" or environ.get("SCRIPT_NAME") != self.mount
                or host.username is not None or host.password is not None or host.path or host.query or host.fragment
                or host.hostname != self.origin.hostname or (host.port or 443) != (self.origin.port or 443)):
            raise AccessDenied("Open the configured HTTPS website.")
        peer = environ.get("REMOTE_ADDR", "")
        observed = environ.get("HTTP_X_HL_APACHE_PEER", "")
        return bool(peer and observed and ipaddress.ip_address(peer) == ipaddress.ip_address(observed))

    def _allowed(self, environ):
        policy = self._policy()
        proof = self._transport(environ) and policy["apache_peer_verified"]
        path, method = environ.get("PATH_INFO"), environ.get("REQUEST_METHOD")
        environ["boomer.direct_peer"] = proof and (policy["pilot_enabled"] or path == "/home/result")
        if path in OWNER_PATHS and (not proof or (method == "POST" and not policy["people_write_enabled"])):
            raise AccessDenied("Private People access is paused or unverified.")
        if path == OWNER_DECISION_PATH and (not proof or not policy['people_write_enabled']):
            raise AccessDenied("Owner decisions are paused or unverified.")
        if method == "POST" and path == "/api/invite" and not policy["registration_enabled"]:
            raise AccessDenied("Website registration is paused.")
        if method == "POST" and path in REGISTRATION_MAIL_PATHS and not (policy["registration_enabled"] and policy["mail_enabled"]):
            raise AccessDenied("Website registration mail is paused.")
        device = path == "/api/lights" or path.startswith("/api/operations/") or (path.startswith("/home/") and path != "/home/result")
        if device and (not proof or not policy["pilot_enabled"]):
            raise AccessDenied("Home control is paused or unverified.")

    @staticmethod
    def _reply(start_response, status):
        body = b'{"error":"Website service unavailable."}'
        start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer")])
        return [body]

    def __call__(self, environ, start_response):
        try:
            environ.pop("REMOTE_USER", None)
            for key in ("boomer.resolve_owner", "boomer.owner_principal", "boomer.resolve_identity"):
                environ.pop(key, None)
            if environ.get("PATH_INFO") == "":
                environ["PATH_INFO"] = "/"
            length = int(environ.get("CONTENT_LENGTH") or 0)
            if not 0 <= length <= 4096:
                return self._reply(start_response, "413 Content Too Large")
            self._allowed(environ)
            if environ.get("PATH_INFO") in OWNER_PATHS:
                def identity():
                    self._allowed(environ)
                    return self.config["owner_identity"] if self.validator.check(environ) else None
                environ["boomer.resolve_identity"] = identity
            environ["wsgi.input"] = _PolicyInput(environ["wsgi.input"], lambda: self._allowed(environ))
            return self.app(environ, start_response)
        except AccessDenied:
            return self._reply(start_response, "403 Forbidden")
        except Exception:
            return self._reply(start_response, "503 Service Unavailable")


def load_application(config_path):
    return PassengerApplication(config_path)
