import asyncio
import hashlib
import json
import sqlite3
import socket
import subprocess
import sys
from pathlib import Path
import uuid

import pytest

from boomer_switch.home_runner import Assignment, HomeRunner, OutboundJournal, ReceiptConflict, WebsiteTransport
from boomer_switch.website_grants import PermissionUnavailable
from test_pilot import Device, LIGHT, resolver


SITE = "https://example.invalid/lights"
KEY = "SYNTHETIC_SERVICE_KEY_" + "k" * 32
SUBJECT = hashlib.sha256(b"synthetic-verified-browser").hexdigest()


class Hosted(WebsiteTransport):
    """Model trusted server source observations, without browser authority headers."""
    def __init__(self):
        self.now = 1000.0
        super().__init__(SITE, KEY, clock=lambda: self.now)
        self.home_peer = self.guest_peer = "synthetic-egress-A"
        self.observed, self.generation, self.fresh, self.sequence = None, 0, 0, 0
        self.agent, self.queue, self.leases, self.results = None, [], {}, {}
        self.allowed, self.lose_ack = True, False
        self.calls = []

    def enqueue(self, action):
        if not self.allowed or self.guest_peer != self.observed or self.now >= self.fresh:
            raise PermissionUnavailable
        item = {"operation_id": str(uuid.uuid4()), "action": action, "session_binding": SUBJECT,
            "expires": self.now + 8, "generation": self.generation}
        self.queue.append(item)
        return item

    async def post(self, route, body):
        self.calls.append((route, body.copy()))
        if route == "poll":
            if self.agent is not None and self.agent != body["agent_id"] or body["sequence"] <= self.sequence:
                raise PermissionUnavailable
            self.agent, self.sequence = body["agent_id"], body["sequence"]
            assert body["light"] == {"light_id": LIGHT.light_id, "name": LIGHT.name, "room": LIGHT.room}
            if self.home_peer != self.observed or self.now >= self.fresh:
                self.generation += 1
                self.observed = self.home_peer
            self.fresh = self.now + 6
            item = self.queue.pop(0) if self.queue else None
            if item and (item["generation"] != self.generation or item["expires"] <= self.now):
                item = None
            if item:
                self.leases[item["operation_id"]] = (item, self.sequence)
            return {"request_id": body["request_id"], "generation": self.generation, "expires": self.fresh,
                "operation": item, "poll_after": 1 if item else 15}
        if self.home_peer != self.observed and self.fresh:
            self.generation += 1
            self.fresh = 0
        lease, sequence = self.leases[body["operation_id"]]
        assert body["agent_id"] == self.agent and body["sequence"] == sequence and body["generation"] == lease["generation"]
        if route == "authorize":
            allowed = (self.allowed and self.home_peer == self.observed and body["generation"] == self.generation and body["sequence"] == self.sequence
                and self.now < min(self.fresh, lease["expires"]) and body["operation_id"] not in self.results)
            return {"allowed": allowed, "request_id": body["request_id"], "operation_id": body["operation_id"],
                "generation": lease["generation"], "expires": min(lease["expires"], self.now + 2, self.fresh)}
        assert route == "result"
        prior = self.results.get(body["operation_id"])
        assert prior is None or prior == body
        self.results[body["operation_id"]] = body.copy()
        if self.lose_ack:
            self.lose_ack = False
            raise PermissionUnavailable
        return {"accepted": True, "request_id": body["request_id"], "operation_id": body["operation_id"]}


def runner(tmp_path, site=None, device=None):
    site, device = site or Hosted(), device or Device()
    provider, calls = resolver(device)
    journal = OutboundJournal(tmp_path / "operations.sqlite", LIGHT.device_id, SITE, clock=lambda: site.now)
    return HomeRunner(LIGHT, provider, site, journal, clock=lambda: site.now), site, device, calls


def test_ordinary_outbound_flow_and_isp_change_keep_same_browser_identity(tmp_path):
    home, site, device, discovery = runner(tmp_path)
    async def scenario():
        assert await home.cycle() == 15
        assert not discovery
        first = site.enqueue("on")
        assert await home.cycle() == 1
        assert device.commands == 1 and site.results[first["operation_id"]]["outcome"] == {"status": "confirmed", "state": "on"}
        stale = site.enqueue("off")
        def rotate_during_refresh():
            if device.updates == 4:
                site.home_peer = site.guest_peer = "synthetic-egress-B"
        # Each operation refreshes once during resolution and once before its final guard.
        device.on_update = rotate_during_refresh
        await home.cycle()
        assert device.commands == 1
        assert site.results[stale["operation_id"]]["outcome"]["status"] == "denied"
        await home.cycle()  # Authenticated outbound poll learns ISP B automatically.
        assert site.generation > 1 and site.observed == site.guest_peer
        generation_b = site.generation
        changed = site.enqueue("off")
        assert changed["session_binding"] == first["session_binding"]
        await home.cycle()
        assert device.commands == 2 and device.is_on is False
        assert site.results[changed["operation_id"]]["outcome"]["state"] == "off"
        # A→B→A has a third generation; the old A assignment cannot regain authority.
        site.home_peer = site.guest_peer = "synthetic-egress-A"
        await home.cycle()
        assert site.generation > generation_b
        old = Assignment.parse(first, 1, 2, 1000)
        with pytest.raises(PermissionUnavailable):
            await site.authorize(home.journal.agent_id, old)
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["revoked", "expired", "offline"])
def test_stale_permission_and_offline_work_never_dispatch(tmp_path, failure):
    home, site, device, discovery = runner(tmp_path)
    async def scenario():
        await home.cycle()
        operation = site.enqueue("on")
        if failure == "offline":
            site.now += 9
        else:
            def invalidate():
                if failure == "revoked": site.allowed = False
                else: site.now += 9
            device.on_update = invalidate
        await home.cycle()
        assert device.commands == 0
        if failure == "offline":
            assert not discovery and operation["operation_id"] not in site.results
        else:
            assert site.results[operation["operation_id"]]["outcome"]["status"] in ("denied", "not_started")
    asyncio.run(scenario())


def test_unknown_blocks_new_uuid_until_authorized_confirmed_read_after_restart(tmp_path):
    home, site, device, _ = runner(tmp_path)
    async def scenario():
        await home.cycle()
        device.ambiguous = True
        unknown = site.enqueue("on")
        await home.cycle()
        assert site.results[unknown["operation_id"]]["outcome"]["status"] == "unknown"
        restarted, _, _, _ = runner(tmp_path, site, device)
        blocked = site.enqueue("off")
        await restarted.cycle()
        assert device.commands == 1 and site.results[blocked["operation_id"]]["outcome"]["status"] == "denied"
        with sqlite3.connect(restarted.journal.path) as db:
            assert db.execute("SELECT status FROM commands WHERE id=?", (unknown["operation_id"],)).fetchone()[0] == "unknown"
        site.enqueue("read")
        await restarted.cycle()
        device.ambiguous = False
        site.enqueue("off")
        await restarted.cycle()
        assert device.commands == 2 and not device.is_on
    asyncio.run(scenario())


def test_lost_result_ack_restarts_and_isp_rotation_reuse_receipt_without_effect(tmp_path):
    home, site, device, _ = runner(tmp_path)
    async def scenario():
        await home.cycle()
        operation = site.enqueue("on")
        site.lose_ack = True
        with pytest.raises(PermissionUnavailable): await home.cycle()
        saved = home.journal.pending()
        assert saved and device.commands == 1
        restarted, _, _, _ = runner(tmp_path, site, device)
        assert restarted.journal.agent_id == home.journal.agent_id
        site.home_peer = site.guest_peer = "synthetic-egress-B"
        await restarted.cycle()
        assert restarted.journal.pending() is None and device.commands == 1 and site.generation > 1
        results = [body for route, body in site.calls if route == "result"]
        assert results == [saved, saved]
        assert site.results[operation["operation_id"]]["outcome"]["state"] == "on"
    asyncio.run(scenario())


def test_durable_poll_counter_skips_lost_attempt_and_rejects_other_site_or_device(tmp_path):
    journal = OutboundJournal(tmp_path / "operations.sqlite", LIGHT.device_id, SITE)
    first = journal.next_poll()
    reopened = OutboundJournal(journal.path, LIGHT.device_id, SITE)
    second = reopened.next_poll()
    assert first["sequence"] == 1 and second["sequence"] == 2 and first["agent_id"] == second["agent_id"]
    with pytest.raises(ValueError): OutboundJournal(journal.path, "other-synthetic-device", SITE)
    with pytest.raises(ValueError): OutboundJournal(journal.path, LIGHT.device_id, SITE + "/other")


def test_poll_and_receipt_operations_close_their_database_handles(tmp_path, monkeypatch):
    journal = OutboundJournal(tmp_path / "operations.sqlite", LIGHT.device_id, SITE, clock=lambda: 1000)
    opened = []
    class Tracked(sqlite3.Connection):
        closed = False
        def close(self):
            self.closed = True
            super().close()
    def connect():
        connection = sqlite3.connect(journal.path, factory=Tracked)
        opened.append(connection)
        return connection
    monkeypatch.setattr(journal, "_connect", connect)
    assignment = Assignment(str(uuid.uuid4()), "read", SUBJECT, 1008, 1, 1)
    journal.next_poll()
    journal.receipt(assignment.operation_id)
    journal.command_status(assignment.operation_id)
    journal.complete(assignment, {"status": "confirmed", "state": "off"}, None)
    journal.pending()
    journal.acknowledge(assignment.operation_id)
    assert opened and all(connection.closed for connection in opened)


@pytest.mark.parametrize("change", [
    {"host": "client-chosen-host"}, {"action": "toggle"}, {"generation": True},
    {"expires": float("nan")}, {"expires": 10**400}, {"session_binding": "unverified"},
])
def test_assignment_rejects_arbitrary_targets_and_malformed_authority(change):
    value = {"operation_id": str(uuid.uuid4()), "action": "on", "session_binding": SUBJECT, "expires": 1008, "generation": 1}
    value.update(change)
    with pytest.raises(ValueError): Assignment.parse(value, 1, 1, 1000)


def test_website_transport_uses_fixed_https_routes_without_redirect_proxy_or_retry():
    calls = []
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        @property
        def content(self): return self
        async def iter_chunked(self, _size):
            yield json.dumps({"request_id": calls[-1][1]["json"]["request_id"], "generation": 1,
                "expires": 1006, "operation": None, "poll_after": 15}).encode()
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): await self.connector.close()
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            assert kwargs["ssl"].check_hostname and kwargs["allow_redirects"] is False
            return Response()
    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["timeout"].total == 2
        assert kwargs["connector"].family == socket.AF_INET
        session = Session()
        session.connector = kwargs["connector"]
        return session
    client = WebsiteTransport(SITE, KEY, clock=lambda: 1000, session_factory=factory)
    body = {"agent_id": str(uuid.uuid4()), "sequence": 1, "request_id": str(uuid.uuid4()),
        "light": {"light_id": LIGHT.light_id, "name": LIGHT.name, "room": LIGHT.room}}
    assert asyncio.run(client.poll(body)) == (None, 15)
    assert len(calls) == 1 and calls[0][0] == SITE + "/home/poll"
    assert set(calls[0][1]["json"]) == {"agent_id", "sequence", "request_id", "light"}
    Response.status = 409
    with pytest.raises(ReceiptConflict):
        asyncio.run(client.result({"request_id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4())}))


def test_main_entry_has_no_listener_and_example_has_no_network_ingress_fields():
    root = Path(__file__).parents[1]
    source = (root / "boomer_switch/home_runner.py").read_text()
    assert "make_server" not in source and "serve_forever" not in source and "bind(" not in source
    example = json.loads((root / "home.example.json").read_text())
    assert set(example) == {"allowlist_file", "light_id", "device_host", "website_url", "website_key", "operation_db"}
    help_result = subprocess.run([sys.executable, "-m", "boomer_switch.pilot", "--help"], capture_output=True, text=True, timeout=10, cwd=root)
    assert help_result.returncode == 0 and "outbound-only" in help_result.stdout and "--port" not in help_result.stdout
    retired = subprocess.run([sys.executable, "-m", "boomer_switch.guest_proxy"], capture_output=True, text=True, timeout=10, cwd=root)
    assert retired.returncode == 2 and "No guest listener is needed" in retired.stderr


def test_crash_left_duplicate_cannot_clear_uncertain_dispatch(tmp_path):
    home, site, device, _ = runner(tmp_path)
    async def scenario():
        await home.cycle()
        repeated = site.enqueue("on")
        home.journal.reserve(SUBJECT, LIGHT.light_id, "on", repeated["operation_id"])
        home.journal.dispatched(repeated["operation_id"])
        await home.cycle()
        with sqlite3.connect(home.journal.path) as db:
            assert db.execute("SELECT status FROM commands WHERE id=?", (repeated["operation_id"],)).fetchone()[0] == "dispatched"
        assert site.results[repeated["operation_id"]]["outcome"]["status"] == "unknown"
        site.enqueue("off")
        await home.cycle()
        assert device.commands == 0
    asyncio.run(scenario())


def test_unacked_result_does_not_lease_and_drop_new_guest_command(tmp_path):
    home, site, device, _ = runner(tmp_path)
    async def scenario():
        await home.cycle()
        site.enqueue("on")
        site.lose_ack = True
        with pytest.raises(PermissionUnavailable): await home.cycle()
        fresh = site.enqueue("off")
        last_sequence = site.sequence
        site.lose_ack = True
        with pytest.raises(PermissionUnavailable): await home.cycle()
        assert site.sequence == last_sequence and site.queue == [fresh] and device.commands == 1
        await home.cycle()
        assert device.commands == 2 and not device.is_on
        assert site.results[fresh["operation_id"]]["outcome"] == {"status": "confirmed", "state": "off"}
    asyncio.run(scenario())


def test_read_receipts_compact_and_expired_unacked_read_does_not_block_recovery(tmp_path):
    home, site, device, _ = runner(tmp_path)
    async def scenario():
        await home.cycle()
        for _ in range(5):
            site.enqueue("read")
            await home.cycle()
        with sqlite3.connect(home.journal.path) as db:
            assert db.execute("SELECT COUNT(*) FROM outbound_receipts").fetchone()[0] == 0
        site.enqueue("read")
        result = site.result
        async def drop_before_record(_body): raise PermissionUnavailable
        site.result = drop_before_record
        with pytest.raises(PermissionUnavailable): await home.cycle()
        assert home.journal.pending() is not None
        site.now += 9
        site.result = result
        await home.cycle()
        assert home.journal.pending() is None
        changed = site.enqueue("on")
        await home.cycle()
        assert device.commands == 1
        with sqlite3.connect(home.journal.path) as db:
            assert db.execute("SELECT body FROM outbound_receipts WHERE operation_id=?", (changed["operation_id"],)).fetchone()[0] == "{}"
    asyncio.run(scenario())
