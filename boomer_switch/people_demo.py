"""Run separate loopback owner and guest sites with isolated sample authentication."""

import argparse
import hmac
import json
from pathlib import Path
import secrets
from threading import Thread
from urllib.parse import urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .people import OwnerSessions, PeopleApplication, owner_cookie, owner_cookie_path
from .registration_demo import RegistrationDemoServer


class DemoOwnerAuth:
    """Provide fake one-time owner sign-in only in the isolated local demo."""

    def __init__(self, app, directory):
        self.app = app
        self.directory = directory
        self.sessions = OwnerSessions()
        self._rotate_ticket()

    def _rotate_ticket(self):
        self.ticket = secrets.token_urlsafe(32)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "owner-login.json").write_text(json.dumps({"ticket": self.ticket}), encoding="utf-8")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path.startswith("/demo-sign-in/"):
            token = path[len("/demo-sign-in/"):]
            if environ.get("REQUEST_METHOD") == "GET" and hmac.compare_digest(token.encode(), self.ticket.encode()):
                raw, _ = self.sessions.issue("sample-owner")
                self._rotate_ticket()
                start_response("302 Found", [("Location", environ.get("SCRIPT_NAME", "") + "/people"),
                    ("Set-Cookie", f"bs_owner={raw}; HttpOnly; SameSite=Strict; Path={owner_cookie_path(environ)}; Max-Age=3600"),
                    ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"), ("Content-Length", "0")])
                return [b""]
            start_response("403 Forbidden", [("Content-Type", "application/json"), ("Content-Length", "2"),
                ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer")])
            return [b"{}"]
        environ["boomer.resolve_owner"] = lambda: self.sessions.resolve(owner_cookie(environ))
        return self.app(environ, start_response)


class MountedPeople:
    """Keep the local website component inside its dedicated prefix."""

    prefix = "/house-lights"

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if not path.startswith(self.prefix + "/"):
            start_response("404 Not Found", [("Content-Length", "0")])
            return [b""]
        environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + self.prefix
        environ["PATH_INFO"] = path[len(self.prefix):]
        return self.app(environ, start_response)


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *_args):
        # Owner sign-in URLs and guest records must not enter request logs.
        pass


class _BoundedServer(WSGIServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address


def main():
    parser = argparse.ArgumentParser(description="Open isolated owner and guest sample sites.")
    modes = parser.add_subparsers(dest="mode", required=True)
    serve = modes.add_parser("serve")
    serve.add_argument("--state-dir", type=Path, default=Path("data/people-demo"))
    serve.add_argument("--guest-port", type=int, default=0)
    serve.add_argument("--owner-port", type=int, default=0)
    signin = modes.add_parser("sign-in")
    signin.add_argument("--state-dir", type=Path, default=Path("data/people-demo"))
    signin.add_argument("--owner-url", required=True)
    options = parser.parse_args()
    if options.mode == "sign-in":
        parsed = urlsplit(options.owner_url)
        if parsed.scheme != "http" or parsed.hostname != "localhost" or not parsed.port or parsed.path != MountedPeople.prefix or parsed.query or parsed.fragment or parsed.username:
            parser.error("Use the printed local owner address.")
        ticket = json.loads((options.state_dir / "owner-login.json").read_text(encoding="utf-8"))["ticket"]
        print(json.dumps({"login_url": options.owner_url.rstrip("/") + "/demo-sign-in/" + ticket}))
        return
    if not 0 <= options.guest_port <= 65535 or not 0 <= options.owner_port <= 65535:
        parser.error("Use ports from 0 through 65535.")
    with RegistrationDemoServer(options.state_dir, options.guest_port) as guest:
        app = MountedPeople(DemoOwnerAuth(PeopleApplication(guest.registration), options.state_dir))
        with make_server("localhost", options.owner_port, app, server_class=_BoundedServer, handler_class=_QuietHandler) as owner:
            worker = Thread(target=guest.serve_forever, daemon=True)
            worker.start()
            print("People demo ready: " + json.dumps({"owner_url": f"http://localhost:{owner.server_port}" + MountedPeople.prefix,
                "guest_url": f"http://localhost:{guest.server_port}"}), flush=True)
            try:
                owner.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                guest.shutdown()
                worker.join(timeout=5)


if __name__ == "__main__":
    main()
