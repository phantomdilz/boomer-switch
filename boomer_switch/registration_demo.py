"""Run persistent sample registration without live mail, permission, or device providers."""

import argparse
import html
from http.cookies import SimpleCookie
from importlib.resources import files
from io import BytesIO
import json
from pathlib import Path
from urllib.parse import urlsplit

from .demo import DemoServer, _DemoHandler
from .registration import AccessDenied, RegistrationStore


class RegistrationDemoServer(DemoServer):
    """Gate sample controls with persistent demo registration."""

    def __init__(self, directory: Path, port=0):
        self.registration = RegistrationStore(directory)
        super().__init__(port)
        self.RequestHandlerClass = _RegistrationHandler


class _RegistrationHandler(_DemoHandler):
    server: RegistrationDemoServer

    def _session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie["bs_session"].value if "bs_session" in cookie else None
        except Exception:
            return None

    @staticmethod
    def _cookie(raw):
        return ("Set-Cookie", f"bs_session={raw}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400")

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path.startswith("/s/"):
                token = self.server.registration.resolve_alias(path[3:])
                self._reply(302, {}, headers=(("Location", f"/invite/{token}"),))
            elif path.startswith("/invite/"):
                raw = self.server.registration.browser(path[len("/invite/"):], existing=self._session())
                self._reply(302, {}, headers=(("Location", "/guest"), self._cookie(raw)))
            elif path == "/":
                self._reply(302, {}, headers=(("Location", "/guest"),))
            elif path == "/guest":
                headers = ()
                if self.server.registration.status(self._session()) == "anonymous":
                    headers = (self._cookie(self.server.registration.browser()),)
                self._reply(200, files("boomer_switch").joinpath("web", "registration.html").read_bytes(),
                            "text/html; charset=utf-8", headers=headers)
            elif path == "/registration.js":
                self._reply(200, files("boomer_switch").joinpath("web", "registration.js").read_bytes(), "text/javascript; charset=utf-8")
            elif path == "/api/access":
                self._reply(200, {"status": self.server.registration.status(self._session())})
            elif path == "/api/lights":
                self.server.registration.require_access(self._session())
                super().do_GET()
            else:
                super().do_GET()
        except AccessDenied as error:
            if path.startswith(("/s/", "/invite/")):
                rejected = self.server.registration.reject_browser(self._session())
                headers = (("Set-Cookie", "bs_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"),) if rejected else ()
                self._reply(403, {"error": str(error)}, headers=headers)
            else:
                self._reply(403, {"error": str(error)})

    def do_POST(self):
        try:
            origin = self.headers.get("Origin")
            if origin is not None and origin != f"http://localhost:{self.server.server_port}":
                raise AccessDenied("Open the local registration page.")
            if self.path.startswith("/api/lights/"):
                super().do_POST()
                return
            if self.path not in ("/api/register", "/api/verify", "/api/resend"):
                self._reply(404, {"error": "Page unavailable."})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2048 or self.headers.get_content_type() != "application/json":
                raise ValueError
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
            raw = self._session()
            if self.path == "/api/register" and set(body) == {"name", "email"}:
                self.server.registration.start(raw, body["name"], body["email"])
            elif self.path == "/api/verify" and set(body) == {"code"}:
                self.server.registration.verify(raw, body["code"])
            elif self.path == "/api/resend" and not body:
                self.server.registration.resend(raw)
            else:
                raise ValueError
            self._reply(200, {"status": self.server.registration.status(raw)})
        except AccessDenied as error:
            self._reply(403, {"error": str(error)})
        except (ValueError, TypeError):
            self._reply(400, {"error": "Use a valid registration request."})

    def _command(self, light_id, action):
        try:
            self.server.registration.require_access(self._session())
        except AccessDenied as error:
            self._reply(403, {"error": str(error)})
            return
        super()._command(light_id, action)


def printable_card(store, directory, base_url):
    import segno

    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname != "localhost" or not parsed.port or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("Use the printed local demo address.")
    base_url = base_url.rstrip("/")
    invitation = store.invitation()
    invite_url = f"{base_url}/invite/{invitation['token']}"
    short_url = f"{base_url}/s/{invitation['alias']}"
    svg = BytesIO()
    segno.make(invite_url, micro=False, error="m").save(svg, kind="svg", scale=5, xmldecl=False)
    card = directory / "card.html"
    card.write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>House Lights invitation</title>'
        '<h1>House Lights</h1><p>Local demo invitation</p>' + svg.getvalue().decode("utf-8") +
        '<p>Scan the code or enter this short link:</p><p>' + html.escape(short_url) +
        '</p><p>Keyword words: ' + html.escape(invitation["words"]) +
        '</p><p>Type the words together without spaces.</p><p>Sample lights only.</p></html>', encoding="utf-8")
    return {"invite_url": invite_url, "short_url": short_url, "printable_card": str(card.resolve())}


def main():
    parser = argparse.ArgumentParser(description="Run isolated sample registration.")
    modes = parser.add_subparsers(dest="mode", required=True)
    serve = modes.add_parser("serve")
    serve.add_argument("--state-dir", type=Path, default=Path("data/registration-demo"))
    serve.add_argument("--port", type=int, default=0)
    operator = modes.add_parser("operator")
    operator.add_argument("--state-dir", type=Path, default=Path("data/registration-demo"))
    actions = operator.add_subparsers(dest="action", required=True)
    card = actions.add_parser("card")
    card.add_argument("--base-url", required=True)
    actions.add_parser("rotate")
    for name in ("mail", "approve", "revoke", "expire"):
        command = actions.add_parser(name)
        command.add_argument("--email", required=True)
        if name == "approve":
            command.add_argument("--expires-at", type=float)
    options = parser.parse_args()
    if options.mode == "serve":
        if not 0 <= options.port <= 65535:
            parser.error("Use a port from 0 through 65535.")
        try:
            with RegistrationDemoServer(options.state_dir, options.port) as server:
                print(f"Registration demo ready: http://localhost:{server.server_port}", flush=True)
                server.serve_forever()
        except KeyboardInterrupt:
            pass
    else:
        store = RegistrationStore(options.state_dir)
        if options.action == "card":
            result = printable_card(store, options.state_dir, options.base_url)
        elif options.action == "mail":
            result = store.mailbox(options.email)
        elif options.action == "rotate":
            store.rotate()
            result = {"status": "rotated"}
        else:
            store.operator(options.action, options.email, expires=getattr(options, "expires_at", None))
            result = {"status": options.action}
        print(json.dumps(result))


if __name__ == "__main__":
    main()
