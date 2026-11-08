from contextlib import contextmanager
from http.client import HTTPConnection, HTTPResponse
from ipaddress import ip_address
import json
from pathlib import Path
import socket
from threading import Event, Thread
import time
from types import SimpleNamespace

import pytest

from boomer_switch.demo import DemoServer, _DemoHandler


@contextmanager
def running_demo(factory=DemoServer):
    with factory() as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def request(server, method, path, payload=None, *, raw=None, content_type="application/json"):
    connection = HTTPConnection(server.server_address[0], server.server_port, timeout=5)
    body = raw if raw is not None else json.dumps(payload) if payload is not None else None
    try:
        connection.request(method, path, body, {"Content-Type": content_type})
        response = connection.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), data
    finally:
        connection.close()


@pytest.mark.smoke
def test_ordinary_demo_path_lists_controls_and_confirms_selected_state():
    with running_demo() as server:
        assert ip_address(server.server_address[0]).is_loopback
        status, headers, body = request(server, "GET", "/")
        assert status == 200
        assert b"House Lights" in body and b"Sample lights" in body
        assert headers["Cache-Control"] == "no-store"
        status, _, body = request(server, "GET", "/api/lights")
        assert status == 200
        assert json.loads(body) == {"lights": [
            {"light_id": "sofa-lamp", "name": "Sofa lamp", "room": "Living room", "state": "off"},
            {"light_id": "reading-lamp", "name": "Reading lamp", "room": "Living room", "state": "off"},
            {"light_id": "bedside-lamp", "name": "Bedside lamp", "room": "Bedroom", "state": "off"},
        ]}
        for action in ("on", "off"):
            status, _, body = request(server, "POST", "/api/lights/sofa-lamp", {"action": action})
            assert status == 200
            assert json.loads(body) == {"light_id": "sofa-lamp", "state": action}
        assert server._devices["reading-lamp"].is_on is False


def test_external_fixture_changes_appear_in_fresh_reads_and_failure_is_unavailable():
    with running_demo() as server:
        for state in ("on", "unavailable", "off"):
            status, _, _ = request(server, "POST", "/demo/fixtures", {"light_id": "sofa-lamp", "state": state})
            assert status == 200
            _, _, body = request(server, "GET", "/api/lights")
            assert json.loads(body)["lights"][0]["state"] == state
            if state == "unavailable":
                _, _, body = request(server, "POST", "/api/lights/sofa-lamp", {"action": "on"})
                assert json.loads(body) == {"light_id": "sofa-lamp", "state": "unavailable"}


@pytest.mark.parametrize("path,payload,expected", [
    ("/api/lights/unselected-light", {"action": "on"}, 403),
    ("/api/lights/sofa-lamp", {"action": "toggle"}, 403),
    ("/api/lights/sofa-lamp", {"action": "on", "device_id": "SYNTHETIC_PRIVATE"}, 400),
    ("/api/lights/sofa-lamp", [], 400),
    ("/demo/fixtures", {"light_id": "unselected-light", "state": "on"}, 403),
    ("/demo/fixtures", {"light_id": "sofa-lamp", "state": "unknown"}, 400),
])
def test_invalid_or_unselected_requests_do_not_change_sample_devices(path, payload, expected):
    with running_demo() as server:
        status, _, body = request(server, "POST", path, payload)
        assert status == expected
        assert b"SYNTHETIC_PRIVATE" not in body
        assert all(not device.is_on for device in server._devices.values())


@pytest.mark.parametrize("raw,content_type", [
    ("invalid json", "application/json"),
    ('{"action":"on"}'.ljust(1025), "application/json"),
    ('{"action":"on"}', "text/plain"),
])
def test_bad_bodies_fail_without_command(raw, content_type):
    with running_demo() as server:
        status, _, _ = request(server, "POST", "/api/lights/sofa-lamp", raw=raw, content_type=content_type)
        assert status == 400
        assert all(not device.is_on for device in server._devices.values())


@pytest.mark.parametrize("registered,path,length,content_type,origin,expected,error", [
    (False, "/api/lights/sofa-lamp", 1025, "application/json", None, 400, "Use a valid sample request."),
    pytest.param(True, "/api/lights/sofa-lamp", 1025, "application/json", None, 400, "Use a valid sample request.", marks=pytest.mark.registration),
    pytest.param(True, "/api/register", 2049, "application/json", None, 400, "Use a valid registration request.", marks=pytest.mark.registration),
    pytest.param(True, "/api/verify", 14, "text/plain", None, 400, "Use a valid registration request.", marks=pytest.mark.registration),
    pytest.param(True, "/api/register", 14, "application/json", "https://example.invalid", 403, "Open the local registration page.", marks=pytest.mark.registration),
    pytest.param(True, "/unknown", 14, "application/json", None, 404, "Page unavailable.", marks=pytest.mark.registration),
], ids=["sample-size", "registered-control-size", "registration-size", "registration-type", "registration-origin", "registration-route"])
def test_early_post_rejection_survives_body_still_in_flight(
        monkeypatch, tmp_path, registered, path, length, content_type, origin, expected, error):
    from boomer_switch.registration_demo import RegistrationDemoServer

    replied, closed = Event(), Event()
    reply, shutdown = _DemoHandler._reply, DemoServer.shutdown_request
    def observe_reply(handler, *args, **kwargs):
        reply(handler, *args, **kwargs)
        replied.set()
    def observe_close(server, connection):
        try: shutdown(server, connection)
        finally: closed.set()
    monkeypatch.setattr(_DemoHandler, "_reply", observe_reply)
    monkeypatch.setattr(DemoServer, "shutdown_request", observe_close)
    factory = (lambda: RegistrationDemoServer(tmp_path)) if registered else DemoServer
    with running_demo(factory) as server:
        connection = HTTPConnection(*server.server_address, timeout=3)
        try:
            connection.putrequest("POST", path)
            connection.putheader("Content-Type", content_type)
            connection.putheader("Content-Length", str(length))
            if origin is not None:
                connection.putheader("Origin", origin)
            connection.endheaders()
            assert replied.wait(timeout=1)
            closed.wait(timeout=0.1)
            body = b'{"action":"on"}'.ljust(length, b" ") if length >= 15 else b"x" * length
            for offset in range(0, length, 64):
                connection.send(body[offset:offset + 64])
            response = connection.getresponse()
            assert response.status == expected
            assert response.getheader("Connection") == "close"
            assert json.loads(response.read()) == {"error": error}
            assert all(not device.is_on for device in server._devices.values())
            if registered:
                assert server.registration.people() == []
                assert server.registration.mailbox("sample@example.invalid") == {"messages": []}
        finally:
            connection.close()


@pytest.mark.parametrize("stream", ["flood", "trickle"])
def test_rejected_body_discard_has_absolute_byte_and_time_limits(monkeypatch, stream):
    import boomer_switch.demo as demo

    now, received, sizes, timeouts = [0.0], [], [], []
    monkeypatch.setattr(demo, "time", SimpleNamespace(monotonic=lambda: now[0]))
    def read(maximum):
        sizes.append(maximum)
        now[0] += 0.125 if stream == "trickle" else 0
        chunk = b"x" if stream == "trickle" else b"x" * maximum
        received.append(len(chunk))
        return chunk
    replies, shutdowns = [], []
    handler = SimpleNamespace(close_connection=False,
        _reply=lambda status, body, **kwargs: replies.append((status, body, kwargs)),
        wfile=SimpleNamespace(flush=lambda: None), rfile=SimpleNamespace(read1=read),
        connection=SimpleNamespace(shutdown=lambda mode: shutdowns.append(mode), settimeout=timeouts.append))
    _DemoHandler._reject_unread_body(handler, 10**12)
    assert handler.close_connection and replies[0][0] == 400 and shutdowns == [socket.SHUT_WR]
    assert sizes and max(sizes) <= 4096 and all(0 < seconds <= 0.5 for seconds in timeouts)
    assert sum(received) == (65536 if stream == "flood" else 4)
    assert now[0] == (0 if stream == "flood" else 0.5)


def test_missing_oversized_body_cannot_hold_the_next_sample_request():
    with running_demo() as server:
        with socket.create_connection(server.server_address, timeout=3) as client:
            client.sendall(b"POST /api/lights/sofa-lamp HTTP/1.0\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: 1000000000\r\n\r\n")
            response = HTTPResponse(client)
            try:
                response.begin()
                assert response.status == 400 and json.loads(response.read()) == {"error": "Use a valid sample request."}
                started = time.monotonic()
                assert request(server, "GET", "/api/lights")[0] == 200
                assert time.monotonic() - started < 2
                assert all(not device.is_on for device in server._devices.values())
            finally:
                response.close()


def test_static_routes_do_not_serve_private_files_and_all_assets_are_local():
    with running_demo() as server:
        for path in ("/config.local.devices.json", "/../.env", "/unknown"):
            assert request(server, "GET", path)[0] == 404
        for path, content_type in (("/app.js", "text/javascript"), ("/style.css", "text/css")):
            status, headers, _ = request(server, "GET", path)
            assert status == 200
            assert headers["Content-Type"].startswith(content_type)
        _, _, listing = request(server, "GET", "/api/lights")
        assert b"device_id" not in listing
        assert b"sample-sofa" not in listing


def test_demo_never_reads_local_allowlist_or_credential_files(monkeypatch):
    def reject_read(*_args, **_kwargs):
        raise AssertionError("The demo must not read private configuration.")

    monkeypatch.setattr(Path, "read_text", reject_read)
    with running_demo() as server:
        assert request(server, "GET", "/api/lights")[0] == 200
        assert request(server, "POST", "/api/lights/sofa-lamp", {"action": "on"})[0] == 200
