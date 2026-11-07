"""Serve the guest browser on the selected Windows WLAN and forward only the lamp API."""

import argparse
import asyncio
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import ssl
import time
from urllib.parse import urlencode, urlsplit
import uuid

import aiohttp
from aiohttp import web

from .website_grants import HTTPSGrantClient, PermissionUnavailable, json_object, session_binding
from .windows_wifi import WindowsWifi


PROOF_TIMEOUT = 4


def https_endpoint(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Configure an explicit HTTPS endpoint.")
    return value.rstrip("/")


async def bounded_body(response, limit):
    data = bytearray()
    async for chunk in response.content.iter_chunked(1024):
        data.extend(chunk)
        if len(data) > limit:
            raise PermissionUnavailable
    return bytes(data)


class WebsiteExchange:
    """Exchange only the website's browser-bound one-use authorization code."""

    def __init__(self, url, key, *, clock=time.time, session_factory=aiohttp.ClientSession):
        validated = HTTPSGrantClient(url, key)
        self.url, self.key, self.ssl = validated.url, validated.service_key, validated.ssl
        self.clock, self.session_factory = clock, session_factory

    async def exchange(self, code, challenge, request_id):
        try:
            async with self.session_factory(timeout=aiohttp.ClientTimeout(total=2), trust_env=False) as client:
                async with client.post(self.url, json={"code": code, "challenge": challenge, "request_id": request_id},
                        headers={"Authorization": "Bearer " + self.key}, ssl=self.ssl, allow_redirects=False) as response:
                    raw = await bounded_body(response, 4096)
                    if response.status != 200:
                        raise PermissionUnavailable
                    result = json_object(raw)
            if set(result) != {"session", "expires", "request_id"} or result["request_id"] != request_id:
                raise PermissionUnavailable
            if not isinstance(result["session"], str) or not re.fullmatch("[a-f0-9]{64}", result["session"]):
                raise PermissionUnavailable
            expiry = result["expires"]
            if type(expiry) not in (int, float) or not math.isfinite(expiry) or not self.clock() < expiry <= self.clock() + 86400:
                raise PermissionUnavailable
            return result
        except Exception:
            raise PermissionUnavailable("Website sign-in is unavailable.") from None


class HomeRelay:
    """Use the dedicated client key for one bounded request without proxy or retry fallback."""

    def __init__(self, url, tls, *, clock=time.time, session_factory=aiohttp.ClientSession):
        self.url, self.tls = https_endpoint(url), tls
        if urlsplit(self.url).path not in ("", "/"):
            raise ValueError("Configure the home service origin.")
        self.clock, self.session_factory = clock, session_factory

    async def forward(self, method, path, body, session, deadline):
        session_binding(session)
        remaining = deadline - self.clock()
        if not 0 < remaining <= 6:
            raise PermissionUnavailable
        async with self.session_factory(timeout=aiohttp.ClientTimeout(total=remaining, connect=1), trust_env=False) as client:
            async with client.request(method, self.url + path, data=body or None, ssl=self.tls, allow_redirects=False,
                    headers={"Cookie": "bs_session=" + session, "X-Pilot-Deadline": str(deadline),
                             "Content-Type": "application/json"}) as response:
                raw = await bounded_body(response, 65536)
                if response.status not in (200, 400, 403, 409, 503):
                    raise PermissionUnavailable
                json_object(raw)
                return response.status, raw


class GuestProxy:
    """Bind browser handoffs and route every device operation through fresh WLAN proof."""

    def __init__(self, *, origin, website_guest_url, light_id, wifi, relay, exchange, clock=time.time):
        self.origin = https_endpoint(origin)
        if urlsplit(self.origin).path not in ("", "/"):
            raise ValueError("Configure the guest origin without a path.")
        self.website_guest_url = https_endpoint(website_guest_url)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", light_id):
            raise ValueError("Configure the selected lamp.")
        self.light_id, self.wifi, self.relay, self.exchange = light_id, wifi, relay, exchange
        self.clock, self.pending = clock, {}
        self.busy = asyncio.Lock()
        self.proof_task = None

    async def proof(self, request):
        transport = request.transport
        if not transport or not transport.get_extra_info("ssl_object"):
            raise PermissionUnavailable
        peer, local = transport.get_extra_info("peername"), transport.get_extra_info("sockname")
        if not peer or not local or request.host != urlsplit(self.origin).netloc:
            raise PermissionUnavailable
        forbidden = ("forwarded", "x-real-ip", "x-pilot-deadline", "authorization", "proxy-authorization")
        if any(key.lower() in forbidden or key.lower().startswith(("x-forwarded", "tailscale", "x-pilot-")) for key in request.headers):
            raise PermissionUnavailable
        if self.proof_task is not None and not self.proof_task.done():
            raise PermissionUnavailable
        self.proof_task = asyncio.create_task(asyncio.to_thread(self.wifi.require, peer[0], local[0]))
        self.proof_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        try:
            # A timed-out native query remains the sole worker. Its result cannot authorize a later request.
            return await asyncio.wait_for(asyncio.shield(self.proof_task), PROOF_TIMEOUT)
        except TimeoutError:
            raise PermissionUnavailable("The guest Wi-Fi check timed out.") from None

    def same_origin(self, request):
        if request.headers.get("Origin") != self.origin:
            raise PermissionUnavailable

    async def handle(self, request):
        try:
            routes = {("GET", "/"), ("GET", "/auth/start"), ("POST", "/auth/start"),
                ("GET", "/auth/callback"), ("GET", "/api/lights"), ("POST", "/api/lights/" + self.light_id)}
            if (request.method, request.path) not in routes:
                return web.json_response({"error": "Page unavailable."}, status=404)
            if self.busy.locked():
                return web.json_response({"error": "Please wait, then check current state."}, status=409)
            async with self.busy:
                if request.method == "POST":
                    self.same_origin(request)
                    if request.content_type != "application/json":
                        raise ValueError
                body = await asyncio.wait_for(request.read(), timeout=2)
                if len(body) > 1024:
                    raise ValueError
                deadline = await self.proof(request)
                path = request.path
                if path == "/" and request.method == "GET" and not request.query_string:
                    return self.page()
                if path == "/auth/start" and request.method in ("GET", "POST") and not request.query_string:
                    self.pending = {key: value for key, value in self.pending.items() if value["expires"] > self.clock()}
                    if len(self.pending) >= 128:
                        raise PermissionUnavailable
                    binding, challenge, state = (secrets.token_urlsafe(32) for _ in range(3))
                    self.pending[binding] = {"challenge": challenge, "state": state, "expires": self.clock() + 120,
                                             "request_id": str(uuid.uuid4())}
                    target = self.website_guest_url + "#" + urlencode({"challenge": challenge, "state": state})
                    response = web.HTTPSeeOther(target) if request.method == "GET" else web.json_response({"url": target})
                    response.set_cookie("bs_pending", binding, secure=True, httponly=True, samesite="Lax", max_age=120)
                    return response
                if path == "/auth/callback" and request.method == "GET":
                    pending = self.pending.get(request.cookies.get("bs_pending", ""))
                    if (not pending or pending["expires"] <= self.clock() or set(request.query) != {"code", "state"}
                            or len(request.query.getall("code")) != 1 or len(request.query.getall("state")) != 1
                            or not hmac.compare_digest(pending["state"], request.query["state"])
                            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request.query["code"])):
                        raise PermissionUnavailable
                    result = await self.exchange.exchange(request.query["code"], pending["challenge"], pending["request_id"])
                    response = web.HTTPSeeOther("/")
                    response.set_cookie("bs_session", result["session"], secure=True, httponly=True, samesite="Strict",
                                        max_age=max(1, int(result["expires"] - self.clock())))
                    response.del_cookie("bs_pending", secure=True, httponly=True, samesite="Lax")
                    del self.pending[request.cookies["bs_pending"]]
                    return response
                if request.query_string:
                    raise ValueError
                if ((path == "/api/lights" and request.method == "GET") or
                        (path == "/api/lights/" + self.light_id and request.method == "POST")):
                    session = request.cookies.get("bs_session", "")
                    session_binding(session)
                    if request.method == "POST":
                        parsed = json_object(body)
                        if set(parsed) != {"action", "operation_id"} or parsed["action"] not in ("on", "off"):
                            raise ValueError
                        from .website_grants import operation_id
                        operation_id(parsed["operation_id"])
                    status, raw = await self.relay.forward(request.method, path, body, session, deadline)
                    return web.Response(body=raw, status=status, content_type="application/json")
                return web.json_response({"error": "Page unavailable."}, status=404)
        except PermissionUnavailable:
            return web.json_response({"error": "Access unavailable. Check guest Wi-Fi and website permission."}, status=403)
        except (ValueError, TypeError, KeyError):
            return web.json_response({"error": "Invalid request."}, status=400)
        except Exception:
            return web.json_response({"error": "Unavailable. Check current state before another command."}, status=503)

    def page(self):
        nonce = secrets.token_urlsafe(24)
        base = Path(__file__).parent / "web"
        html = (base / "pilot.html").read_text(encoding="utf-8")
        html = html.replace("STYLE", (base / "style.css").read_text(encoding="utf-8"))
        html = html.replace("SCRIPT", (base / "pilot.js").read_text(encoding="utf-8")).replace("NONCE", nonce)
        return web.Response(text=html, content_type="text/html", headers={"Content-Security-Policy":
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})

    def application(self):
        @web.middleware
        async def headers(request, handler):
            response = await handler(request)
            response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"})
            return response
        app = web.Application(client_max_size=1024, middlewares=[headers])
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app


def load_guest(path):
    config = json_object(Path(path).read_text(encoding="utf-8"))
    required = {"origin", "website_guest_url", "website_exchange_url", "website_key", "home_url", "home_ca",
        "client_cert", "client_key", "server_cert", "server_key", "light_id", "wifi"}
    if set(config) != required or set(config["wifi"]) != {"guid", "index", "address", "prefix", "ssid_hex", "bssid_hex"}:
        raise ValueError("Configure the explicit private guest service.")
    base = Path(path).resolve().parent
    wifi = WindowsWifi(**config["wifi"])
    port = urlsplit(https_endpoint(config["origin"])).port or 443
    upstream = ssl.create_default_context(cafile=base / config["home_ca"])
    upstream.load_cert_chain(base / config["client_cert"], base / config["client_key"])
    listener = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    listener.minimum_version = ssl.TLSVersion.TLSv1_2
    listener.load_cert_chain(base / config["server_cert"], base / config["server_key"])
    proxy = GuestProxy(origin=config["origin"], website_guest_url=config["website_guest_url"], light_id=config["light_id"],
        wifi=wifi, relay=HomeRelay(config["home_url"], upstream),
        exchange=WebsiteExchange(config["website_exchange_url"], config["website_key"]))
    return proxy, wifi.address, port, listener


def main():
    parser = argparse.ArgumentParser(description="Run the private Windows guest Wi-Fi HTTPS entry point.")
    parser.add_argument("--config", type=Path, required=True)
    options = parser.parse_args()
    try:
        proxy, address, port, tls = load_guest(options.config)
    except Exception:
        parser.error("Read a valid explicit private guest configuration and its TLS files.")
    web.run_app(proxy.application(), host=address, port=port, ssl_context=tls, access_log=None, print=None)


if __name__ == "__main__":
    main()
