from contextlib import contextmanager
from http.client import HTTPConnection
from ipaddress import ip_address
import json
from pathlib import Path
from threading import Thread

import pytest

from boomer_switch.demo import DemoServer


@contextmanager
def running_demo():
    with DemoServer() as server:
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
    ("x" * 1025, "application/json"),
    ('{"action":"on"}', "text/plain"),
])
def test_bad_bodies_fail_without_command(raw, content_type):
    with running_demo() as server:
        status, _, _ = request(server, "POST", "/api/lights/sofa-lamp", raw=raw, content_type=content_type)
        assert status == 400
        assert all(not device.is_on for device in server._devices.values())


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
