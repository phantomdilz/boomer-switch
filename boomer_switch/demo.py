"""Serve sample lights on loopback without private configuration or devices."""

from __future__ import annotations

import argparse
import asyncio
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
import json
import socket
import time

from .lights import CommandDenied, Light, LightService


class _DeviceType(Enum):
    Bulb = "bulb"


class _SampleLight:
    device_type = _DeviceType.Bulb

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.children = []
        self.is_on = False
        self.available = True

    async def update(self):
        if not self.available:
            raise OSError("Sample device unavailable.")

    async def turn_on(self):
        self.is_on = True

    async def turn_off(self):
        self.is_on = False


class DemoServer(HTTPServer):
    """Expose only fixed in-memory sample lights on loopback."""

    def __init__(self, port: int = 0):
        selections = (
            Light("sofa-lamp", "Sofa lamp", "Living room", "sample-sofa"),
            Light("reading-lamp", "Reading lamp", "Living room", "sample-reading"),
            Light("bedside-lamp", "Bedside lamp", "Bedroom", "sample-bedside"),
        )
        self._devices = {light.light_id: _SampleLight(light.device_id) for light in selections}
        by_identity = {device.device_id: device for device in self._devices.values()}

        async def resolve(identity):
            return by_identity[identity]

        self._service = LightService(selections, resolve, timeout_seconds=2)
        self._runner = asyncio.Runner()
        super().__init__(("localhost", port), _DemoHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address

    def server_close(self):
        super().server_close()
        self._runner.close()

    async def snapshot(self):
        result = []
        for light in self._service.lights():
            result.append(light | {"state": str(await self._service.state(light["light_id"]))})
        return {"lights": result}


class _DemoHandler(BaseHTTPRequestHandler):
    server: DemoServer

    def log_message(self, *_args):
        # Keep request data out of local logs.
        pass

    def _reply(self, status, body, content_type="application/json", *, headers=()):
        payload = json.dumps(body).encode() if content_type == "application/json" else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if self.path in assets:
            name, content_type = assets[self.path]
            self._reply(200, files("boomer_switch").joinpath("web", name).read_bytes(), content_type)
        elif self.path == "/api/lights":
            self._reply(200, self.server._runner.run(self.server.snapshot()))
        else:
            self._reply(404, {"error": "Page unavailable."})

    def do_POST(self):
        length = None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 or self.headers.get_content_type() != "application/json":
                self._reject_unread_body(length)
                return
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
            if self.path == "/demo/fixtures":
                if set(body) != {"light_id", "state"} or body["state"] not in ("on", "off", "unavailable"):
                    raise ValueError
                device = self.server._devices.get(body["light_id"])
                if device is None:
                    raise CommandDenied
                device.available = body["state"] != "unavailable"
                if device.available:
                    device.is_on = body["state"] == "on"
                self._reply(200, body)
                return
            prefix = "/api/lights/"
            if not self.path.startswith(prefix):
                self._reply(404, {"error": "Page unavailable."})
                return
            if set(body) != {"action"}:
                raise ValueError
            light_id = self.path[len(prefix):]
            self._command(light_id, body["action"])
        except CommandDenied:
            self._reply(403, {"error": "Select an allowed light and action."})
        except (ValueError, TypeError):
            if length is None:
                self._reject_unread_body(None)
            else:
                self._reply(400, {"error": "Use a valid sample request."})

    def _reject_unread_body(self, length, *, status=400, body=None):
        self.close_connection = True
        self._reply(status, body if body is not None else {"error": "Use a valid sample request."},
            headers=(("Connection", "close"),))
        try:
            self.wfile.flush()
            # Keep the receive side open briefly so an in-flight body cannot reset the response.
            self.connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + 0.5
            remaining = min(length, 65536) if length is not None and length >= 0 else 65536
            while remaining:
                seconds = deadline - time.monotonic()
                if seconds <= 0:
                    break
                self.connection.settimeout(seconds)
                chunk = self.rfile.read1(min(remaining, 4096))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def _command(self, light_id, action):
        state = self.server._runner.run(self.server._service.command(light_id, action))
        self._reply(200, {"light_id": light_id, "state": str(state)})


def main():
    parser = argparse.ArgumentParser(description="Open the local sample-light demo.")
    parser.add_argument("--port", type=int, default=0)
    options = parser.parse_args()
    if not 0 <= options.port <= 65535:
        parser.error("Use a port from 0 through 65535.")
    try:
        with DemoServer(options.port) as server:
            print(f"Demo ready: http://localhost:{server.server_port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
