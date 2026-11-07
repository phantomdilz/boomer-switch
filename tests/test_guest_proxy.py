import asyncio
import ctypes
import ipaddress
import json
import os
import ssl
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import uuid
import time
from threading import Event, Thread
from wsgiref.simple_server import make_server

import aiohttp
from aiohttp import web
import pytest
from multidict import MultiDict

from boomer_switch.guest_proxy import GuestProxy, HomeRelay, WebsiteExchange
from boomer_switch.website_grants import PermissionUnavailable
from boomer_switch.windows_wifi import Association, Connection, WindowsWifi, current_wifi


GUID = "00000000-0000-0000-0000-000000000001"
ADDRESS = str(ipaddress.IPv4Address((192 << 24) + (168 << 16) + (250 << 8) + 2))
PEER = str(ipaddress.ip_address(ADDRESS) + 1)
NETWORK = str(ipaddress.ip_network(ADDRESS + "/24", strict=False))
ORIGIN = "https://example.invalid"
HOME_SESSION = "b" * 64


def topology():
    return {"adapters": [{"index": 9, "guid": GUID, "hardware": True, "virtual": False, "up": True}],
        "addresses": [{"index": 9, "address": ADDRESS, "prefix": 24, "preferred": True}],
        "interfaces": [{"index": 9, "forward": False, "weakReceive": False, "weakSend": False}],
        "routes": [{"index": 9, "prefix": NETWORK, "next": str(ipaddress.IPv4Address(0))}],
        "neighbors": [{"index": 9, "address": PEER, "state": "Reachable"}]}


def wifi(state=None, association=None, clock=lambda: 1000):
    return WindowsWifi(guid=GUID, index=9, address=ADDRESS, prefix=24, ssid_hex=b"synthetic-wifi".hex(),
        bssid_hex=bytes(range(6)).hex(), snapshot=lambda: state or topology(),
        wifi=lambda _: association or (b"synthetic-wifi", bytes(range(6))), clock=clock)


def test_native_wlan_abi_and_selected_direct_attachment():
    assert ctypes.sizeof(Association) == 68
    assert ctypes.sizeof(Connection) == 604
    assert wifi().require(PEER, ADDRESS) == 1006


@pytest.mark.skipif(os.name != "nt", reason="The native WLAN provider requires Windows.")
def test_native_wlan_query_releases_resources_and_rejects_open_or_unknown_state(monkeypatch):
    import boomer_switch.windows_wifi as module
    value = Connection()
    value.state, value.association.bss_type = 1, 1
    value.association.ssid.length = 4
    value.association.ssid.value[:4] = b"test"
    value.association.bssid[:] = bytes(range(6))
    value.security[0], value.security[2] = 1, 7
    released = []
    class Function:
        def __init__(self, callback): self.callback = callback
        def __call__(self, *args): return self.callback(*args)
    def opened(_version, _reserved, _negotiated, handle):
        ctypes.cast(handle, ctypes.POINTER(ctypes.c_void_p))[0] = 1
        return 0
    def queried(_handle, _guid, opcode, _reserved, size, data, _type):
        assert opcode == 7
        ctypes.cast(size, ctypes.POINTER(ctypes.c_uint32))[0] = ctypes.sizeof(value)
        ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(value)
        return 0
    api = SimpleNamespace(WlanOpenHandle=Function(opened), WlanQueryInterface=Function(queried),
        WlanFreeMemory=Function(lambda _data: released.append("free")),
        WlanCloseHandle=Function(lambda *_: released.append("close") or 0))
    monkeypatch.setattr(module.C, "WinDLL", lambda name: api)
    assert current_wifi(GUID) == (b"test", bytes(range(6)))
    assert released == ["free", "close"]
    for field, changed in (("state", 4), ("security", 0)):
        released.clear()
        if field == "state": value.state = changed
        else: value.state, value.security[0] = 1, changed
        with pytest.raises(PermissionUnavailable): current_wifi(GUID)
        assert released == ["free", "close"]


@pytest.mark.parametrize("change", [
    lambda s: s["adapters"][0].update(guid=str(uuid.UUID(int=2))),
    lambda s: s["adapters"][0].update(virtual=True),
    lambda s: s["adapters"][0].update(up=False),
    lambda s: s["addresses"].append(dict(s["addresses"][0], index=10)),
    lambda s: s["addresses"][0].update(prefix=25),
    lambda s: s["interfaces"][0].update(weakReceive=True),
    lambda s: s["interfaces"].append({"index": 10, "forward": True}),
    lambda s: s["interfaces"].append({"index": 10, "forward": False, "weakReceive": True, "weakSend": False}),
    lambda s: s["routes"].append({"index": 10, "prefix": PEER + "/32", "next": str(ipaddress.IPv4Address(0))}),
    lambda s: s["routes"].append({"index": 9, "prefix": str(ipaddress.ip_network((0, 0))), "next": PEER}),
    lambda s: s["neighbors"].clear(),
])
def test_changed_or_routed_attachment_fails_closed(change):
    state = topology()
    change(state)
    with pytest.raises(PermissionUnavailable):
        wifi(state).require(PEER, ADDRESS)


def test_roam_loss_outside_peer_and_slow_proof_deny():
    for association in ((b"other-network", bytes(range(6))), (b"synthetic-wifi", b"other!")):
        with pytest.raises(PermissionUnavailable):
            wifi(association=association).require(PEER, ADDRESS)
    with pytest.raises(PermissionUnavailable):
        wifi().require(str(ipaddress.IPv4Address((100 << 24) + 1)), ADDRESS)
    times = iter((1000, 1004))
    with pytest.raises(PermissionUnavailable):
        wifi(clock=lambda: next(times)).require(PEER, ADDRESS)


class Request:
    def __init__(self, path="/api/lights", method="GET", body=None, cookies=None, headers=None, query=None):
        self.path, self.method, self.host = path, method, "example.invalid"
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.content_type = "application/json"
        self.query = MultiDict(query or {})
        self.query_string = str(self.query) if query else ""
        self.body = json.dumps(body).encode() if body is not None else b""
        self.transport = SimpleNamespace(get_extra_info=lambda name: {"ssl_object": object(),
            "peername": (PEER, 30000), "sockname": (ADDRESS, 443)}.get(name))

    async def read(self):
        return self.body


class Relay:
    def __init__(self): self.calls = []
    async def forward(self, *args):
        self.calls.append(args)
        return 200, b'{"lights":[{"light_id":"pilot-lamp","name":"Sample lamp","room":"Sample room","state":"off"}]}'


class Exchange:
    def __init__(self): self.calls = []
    async def exchange(self, *args):
        self.calls.append(args)
        return {"session": HOME_SESSION, "expires": 1200, "request_id": args[2]}


def proxy():
    return GuestProxy(origin=ORIGIN, website_guest_url=ORIGIN + "/guest", light_id="pilot-lamp", wifi=wifi(),
        relay=Relay(), exchange=Exchange(), clock=lambda: 1000)


def test_browser_handoff_then_ordinary_read_and_command_with_no_direct_bypass():
    app = proxy()

    async def scenario():
        assert (await app.handle(Request())).status == 403
        start = await app.handle(Request("/auth/start", "POST", {}, headers={"Origin": ORIGIN}))
        pending_cookie = start.cookies["bs_pending"].value
        assert start.cookies["bs_pending"]["secure"] and start.cookies["bs_pending"]["httponly"]
        assert start.cookies["bs_pending"]["samesite"] == "Lax"
        fragment = parse_qs(urlsplit(json.loads(start.body)["url"]).fragment)
        callback = Request("/auth/callback", cookies={"bs_pending": pending_cookie},
            query={"code": "c" * 43, "state": fragment["state"][0]})
        result = await app.handle(callback)
        assert result.status == 303 and result.headers["Location"] == "/"
        assert result.cookies["bs_session"].value == HOME_SESSION
        assert result.cookies["bs_session"]["samesite"] == "Strict"
        assert app.exchange.calls[0][1] == fragment["challenge"][0]
        assert (await app.handle(callback)).status == 403  # Consumed browser binding.
        assert (await app.handle(Request(cookies={"bs_session": HOME_SESSION}))).status == 200
        command = {"action": "on", "operation_id": str(uuid.uuid4())}
        assert (await app.handle(Request("/api/lights/pilot-lamp", "POST", command,
            cookies={"bs_session": HOME_SESSION}, headers={"Origin": ORIGIN}))).status == 200
        assert json.loads(app.relay.calls[-1][2]) == command
        assert app.relay.calls[-1][3:] == (HOME_SESSION, 1006)
        assert (await app.handle(Request("/api/people", cookies={"bs_session": HOME_SESSION}))).status == 404
        assert (await app.handle(Request("/", method="GET"))).status == 200
    asyncio.run(scenario())


def test_untrusted_headers_csrf_body_read_loss_and_callback_binding():
    app = proxy()

    async def scenario():
        for headers in ({"X-Pilot-Deadline": "1006"}, {"X-Forwarded-For": PEER}, {"Forwarded": "for=sample"}):
            assert (await app.handle(Request(headers=headers, cookies={"bs_session": HOME_SESSION}))).status == 403
        assert (await app.handle(Request("/auth/start", "POST", {}))).status == 403
        callback = Request("/auth/callback", query={"state": "s" * 43, "code": "c" * 43})
        assert (await app.handle(callback)).status == 403
        command = Request("/api/lights/pilot-lamp", "POST", {"action": "on", "operation_id": str(uuid.uuid4())},
            cookies={"bs_session": HOME_SESSION}, headers={"Origin": ORIGIN})
        async def lose_wifi_while_reading():
            app.wifi.wifi = lambda _: (b"wrong-network", bytes(range(6)))
            return command.body
        command.read = lose_wifi_while_reading
        assert (await app.handle(command)).status == 403
        assert not app.relay.calls and not app.exchange.calls
    asyncio.run(scenario())


def test_new_requests_do_not_wait_behind_an_accepted_request():
    app = proxy()
    async def scenario():
        await app.busy.acquire()
        try:
            assert (await app.handle(Request(cookies={"bs_session": HOME_SESSION}))).status == 409
            assert (await app.handle(Request("/favicon.ico"))).status == 404
        finally:
            app.busy.release()
        assert not app.relay.calls
    asyncio.run(scenario())


def test_stalled_native_query_denies_with_one_worker_and_discards_late_proof(monkeypatch):
    import boomer_switch.guest_proxy as module
    monkeypatch.setattr(module, "PROOF_TIMEOUT", 0.05)
    app = proxy()
    release = Event()
    started = Event()
    calls = []
    def stalled(*_args):
        calls.append(True)
        started.set()
        release.wait(timeout=2)
        return 1006
    app.wifi.require = stalled
    async def scenario():
        try:
            request = Request(cookies={"bs_session": HOME_SESSION})
            assert (await asyncio.wait_for(app.handle(request), 0.5)).status == 403
            assert started.is_set() and not app.busy.locked()
            assert (await asyncio.wait_for(app.handle(request), 0.5)).status == 403
            assert len(calls) == 1 and not app.relay.calls
        finally:
            release.set()
            await app.proof_task
        assert not app.relay.calls
        app.wifi = wifi()
        assert (await app.handle(Request(cookies={"bs_session": HOME_SESSION}))).status == 200
    asyncio.run(scenario())


class Response:
    status = 200
    def __init__(self, data):
        self.raw = json.dumps(data).encode()
        self.content = self
    async def __aenter__(self): return self
    async def __aexit__(self, *_args): pass
    async def iter_chunked(self, size):
        for offset in range(0, len(self.raw), 7):
            yield self.raw[offset:offset + 7]


def test_exchange_validates_machine_binding_expiry_and_bounded_transport():
    request_id = str(uuid.uuid4())
    payload = {"session": HOME_SESSION, "expires": 1200, "request_id": request_id}
    calls = []
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        def post(self, url, **kwargs):
            calls.append(kwargs)
            assert url == ORIGIN + "/exchange"
            assert kwargs["allow_redirects"] is False and kwargs["ssl"].check_hostname
            return Response(payload)
    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["timeout"].total == 2
        return Session()
    client = WebsiteExchange(ORIGIN + "/exchange", "SYNTHETIC_KEY_" + "k" * 32, clock=lambda: 1000, session_factory=factory)
    async def scenario():
        assert (await client.exchange("c" * 43, "q" * 43, request_id))["session"] == HOME_SESSION
        assert calls[0]["json"] == {"code": "c" * 43, "challenge": "q" * 43, "request_id": request_id}
        for changed in ({"request_id": str(uuid.uuid4())}, {"session": "x" * 64}, {"expires": float("nan")}, {"expires": 1000}):
            original = payload.copy()
            payload.update(changed)
            with pytest.raises(PermissionUnavailable):
                await client.exchange("c" * 43, "q" * 43, request_id)
            payload.clear()
            payload.update(original)
    asyncio.run(scenario())


def test_real_guest_https_to_home_mtls_normal_path_and_attachment_loss(tmp_path):
    from test_pilot import Device, backend, synthetic_tls_contexts, SESSION
    from boomer_switch.pilot import MTLSIngress, PilotApplication, _BoundedServer, _QuietHandler

    server_tls, client_tls, browser_tls, fingerprint = synthetic_tls_contexts(tmp_path)
    guest_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    guest_tls.load_cert_chain(tmp_path / "server.pem", tmp_path / "server-key.pem")
    device = Device()
    target, grants, discovery = backend(tmp_path, device, ingress=MTLSIngress(fingerprint))
    home = PilotApplication(target)
    with make_server("localhost", 0, home, server_class=_BoundedServer, handler_class=_QuietHandler) as server:
        server.tls_context = server_tls
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        class Probe:
            available = True
            def require(self, peer, local):
                assert ipaddress.ip_address(peer).is_loopback and ipaddress.ip_address(local).is_loopback
                if not self.available:
                    raise PermissionUnavailable
                return time.time() + 6
        probe = Probe()
        app = GuestProxy(origin=ORIGIN, website_guest_url=ORIGIN + "/guest", light_id="pilot-lamp", wifi=probe,
            relay=HomeRelay(f"https://localhost:{server.server_port}", client_tls), exchange=Exchange())
        async def scenario():
            runner = web.AppRunner(app.application(), access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "localhost", 0, ssl_context=guest_tls)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            origin = f"https://localhost:{port}"
            app.origin = origin
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5), trust_env=False) as client:
                    async def call(path, body=None, headers=None):
                        async with client.request("POST" if body else "GET", origin + path, json=body,
                                headers=headers or {}, ssl=browser_tls) as response:
                            return response.status, await response.json()
                    headers = {"Cookie": "bs_session=" + SESSION, "Origin": origin}
                    assert (await call("/api/lights", headers=headers))[0] == 200
                    operation = {"action": "on", "operation_id": str(uuid.uuid4())}
                    assert (await call("/api/lights/pilot-lamp", operation, headers))[1]["state"] == "on"
                    assert device.commands == 1 and grants.calls >= 4
                    assert (await call("/api/lights/pilot-lamp", operation, headers))[0] == 409
                    assert device.commands == 1
                    probe.available = False
                    assert (await call("/api/lights/pilot-lamp", dict(operation, operation_id=str(uuid.uuid4())), headers))[0] == 403
                    assert device.commands == 1
                    probe.available = True
                    grants.allowed = False
                    assert (await call("/api/lights", headers=headers))[0] == 403
                    assert device.commands == 1
            finally:
                await runner.cleanup()
        try:
            asyncio.run(scenario())
        finally:
            server.shutdown()
            worker.join(timeout=5)
            home.close()
            assert not worker.is_alive()
