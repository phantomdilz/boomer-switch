import asyncio
from dataclasses import asdict, replace
import hashlib
import json
import sqlite3
import subprocess
import sys
import uuid

import pytest

from boomer_switch.fleet_journal import FleetJournal
from boomer_switch.home_runner import Assignment, HomeRunner, OutboundJournal, WebsiteTransport, load_home
from boomer_switch.home_rendezvous import HomeRendezvous, HomeUnavailable, OperationConflict
from boomer_switch.kasa_pilot import KasaPilotResolver
from boomer_switch.lights import Light
from boomer_switch.pilot import CommandJournal, DuplicateCommand
from boomer_switch.registration import AccessDenied, RegistrationStore
from boomer_switch.website_grants import PermissionUnavailable
from test_pilot import Device
from test_rendezvous import env, CELL


SITE = "https://example.invalid/lights"
KEY = "synthetic-service-" + "k" * 32
LIGHTS = tuple(Light(f"sample-{i}", f"Sample {i}", f"Room {i // 2}", f"synthetic-device-{i}") for i in range(15))


class LocalWebsite(WebsiteTransport):
    def __init__(self, rendezvous, clock):
        super().__init__(SITE, KEY, clock=clock)
        self.rendezvous = rendezvous
        self.peer = env()
        self.peer["HTTP_AUTHORIZATION"] = "Bearer " + KEY
        self.lose_result = False

    async def post(self, route, body):
        result = self.rendezvous.machine(self.peer, "/home/" + route, body)
        if route == "result" and self.lose_result:
            self.lose_result = False
            raise TimeoutError
        return result


def fleet(tmp_path):
    now = [1000.0]
    store = RegistrationStore(tmp_path / "site", mode="production", clock=lambda: now[0])
    raw = store.browser(store.invitation()["token"])
    store.start(raw, "Sample guest", "guest@example.invalid")
    store.verify(raw, store.mailbox("guest@example.invalid")["messages"][-1]["code"])
    store.operator("approve", "guest@example.invalid")
    rendezvous = HomeRendezvous(store, KEY)
    rendezvous.access(raw, env())
    transport = LocalWebsite(rendezvous, lambda: now[0])
    devices = [Device(light.device_id) for light in LIGHTS]
    calls = []
    providers = {}
    for light, device in zip(LIGHTS, devices):
        async def discover(host, device=device, **kwargs):
            calls.append(host)
            return device
        providers[light.light_id] = KasaPilotResolver(light, "synthetic-host-" + light.light_id, discover=discover)
    journal = FleetJournal(tmp_path / "journal.sqlite", LIGHTS, SITE, clock=lambda: now[0])
    runner = HomeRunner(LIGHTS, providers, transport, journal, clock=lambda: now[0])
    return runner, rendezvous, raw, devices, now, calls


def enqueue(rv, raw, target, action="read", operation=None):
    return rv.enqueue(raw, env(), {"operation_id": operation or str(uuid.uuid4()), "action": action, "light_id": target})


def test_all_fifteen_targets_read_and_command_only_the_selected_device(tmp_path):
    home, rv, raw, devices, now, calls = fleet(tmp_path)
    async def scenario():
        await home.cycle()
        assert len(rv.display_lights(raw, env())) == 15 and not calls
        for index, light in enumerate(LIGHTS):
            read = enqueue(rv, raw, light.light_id)
            await home.cycle()
            result = rv.operation(raw, env(), read["operation_id"])
            assert result["light_id"] == light.light_id and result["state"] == "off" and result["availability"] == "online"
            command = enqueue(rv, raw, light.light_id, "on")
            await home.cycle()
            assert rv.operation(raw, env(), command["operation_id"])["state"] == "on"
            assert [device.commands for device in devices] == [int(i <= index) for i in range(15)]
            now[0] += .01
    asyncio.run(scenario())
    assert len(calls) == 30


def test_foreign_missing_offsite_and_unread_targets_fail_closed(tmp_path):
    home, rv, raw, devices, now, calls = fleet(tmp_path)
    async def scenario():
        await home.cycle()
        with pytest.raises(AccessDenied):
            enqueue(rv, raw, "excluded-light")
        with pytest.raises(AccessDenied):
            rv.enqueue(raw, env(), {"operation_id": str(uuid.uuid4()), "action": "read"})
        with pytest.raises(AccessDenied):
            rv.enqueue(raw, env(CELL), {"operation_id": str(uuid.uuid4()), "action": "read", "light_id": LIGHTS[0].light_id})
        read = enqueue(rv, raw, LIGHTS[0].light_id)
        await home.cycle()
        with pytest.raises(HomeUnavailable):
            enqueue(rv, raw, LIGHTS[1].light_id, "on")
        with pytest.raises(OperationConflict):
            enqueue(rv, raw, LIGHTS[1].light_id, operation=read["operation_id"])
    asyncio.run(scenario())
    assert len(calls) == 1 and not any(device.commands for device in devices)


def test_unknown_a_is_not_cleared_by_read_b_and_does_not_block_b(tmp_path):
    home, rv, raw, devices, now, calls = fleet(tmp_path)
    a, b = LIGHTS[:2]
    async def scenario():
        await home.cycle()
        enqueue(rv, raw, a.light_id)
        await home.cycle()
        devices[0].ambiguous = True
        command = enqueue(rv, raw, a.light_id, "on")
        await home.cycle()
        assert home.journal.command_status(command["operation_id"]) == "unknown"
        enqueue(rv, raw, b.light_id)
        await home.cycle()
        assert home.journal.command_status(command["operation_id"]) == "unknown"
        enqueue(rv, raw, b.light_id, "on")
        await home.cycle()
        assert devices[1].commands == 1 and devices[0].commands == 1
        enqueue(rv, raw, a.light_id, "off")
        await home.cycle()
        assert devices[0].commands == 1
        enqueue(rv, raw, a.light_id)
        await home.cycle()
        assert home.journal.command_status(command["operation_id"]) == "reconciled"
    asyncio.run(scenario())


def test_migration_retains_agent_sequence_receipts_and_only_anchor_marks(tmp_path):
    path = tmp_path / "journal.sqlite"
    old = OutboundJournal(path, LIGHTS[0].device_id, SITE, clock=lambda: 1000)
    first = old.next_poll()
    operation = str(uuid.uuid4())
    old.reserve("synthetic-session", LIGHTS[0].light_id, "on", operation)
    old.dispatched(operation)
    assignment = Assignment(operation, "on", hashlib.sha256(b"synthetic-session").hexdigest(), 1008, 1, first["sequence"])
    old.complete(assignment, {"status": "unknown", "state": "unavailable"}, "unknown")
    saved = old.pending()
    journal = FleetJournal(path, LIGHTS, SITE, clock=lambda: 1000)
    assert journal.agent_id == old.agent_id and journal.pending() == saved
    assert journal.next_poll()["sequence"] == first["sequence"] + 1
    journal.reconcile_target(LIGHTS[1].light_id)
    assert journal.command_status(operation) == "unknown"
    with pytest.raises(DuplicateCommand):
        journal.reserve_target("synthetic-session", LIGHTS[0].light_id, "off", str(uuid.uuid4()))
    journal.reconcile_target(LIGHTS[0].light_id)
    assert journal.command_status(operation) == "reconciled"
    with pytest.raises(ValueError, match="multi-light"):
        OutboundJournal(path, LIGHTS[0].device_id, SITE)
    with pytest.raises(ValueError, match="physical binding"):
        FleetJournal(path, (LIGHTS[0], replace(LIGHTS[1], device_id="other-synthetic-device")), SITE, clock=lambda: 1000)


def test_unavailable_device_cannot_command_or_hide_other_device(tmp_path):
    home, rv, raw, devices, now, calls = fleet(tmp_path)
    devices[0].device_id = "wrong-synthetic-device"
    async def scenario():
        await home.cycle()
        read = enqueue(rv, raw, LIGHTS[0].light_id)
        await home.cycle()
        result = rv.operation(raw, env(), read["operation_id"])
        assert result["state"] == "unavailable" and result["availability"] == "unavailable"
        with pytest.raises(HomeUnavailable):
            enqueue(rv, raw, LIGHTS[0].light_id, "on")
        enqueue(rv, raw, LIGHTS[1].light_id)
        await home.cycle()
        command = enqueue(rv, raw, LIGHTS[1].light_id, "on")
        await home.cycle()
        assert rv.operation(raw, env(), command["operation_id"])["state"] == "on"
    asyncio.run(scenario())
    assert devices[0].commands == 0 and devices[1].commands == 1


def test_assignment_target_cannot_change_during_authorization_or_receipt(tmp_path):
    home, rv, raw, devices, now, calls = fleet(tmp_path)
    async def scenario():
        await home.cycle()
        queued = enqueue(rv, raw, LIGHTS[0].light_id)
        body = home.journal.next_poll() | {"lights": list(home.service.lights()), "catalog_revision": home.journal.catalog_revision}
        assignment, _ = await home.transport.poll(body)
        assert assignment.light_id == LIGHTS[0].light_id
        wrong = replace(assignment, light_id=LIGHTS[1].light_id)
        with pytest.raises(OperationConflict):
            await home.transport.authorize(home.journal.agent_id, wrong)
        with pytest.raises(OperationConflict):
            await home.transport.authorize(home.journal.agent_id, replace(assignment, light_id=None))
        await home.execute(assignment)
        receipt = home.journal.pending()
        with pytest.raises(OperationConflict):
            await home.transport.result(receipt | {"light_id": LIGHTS[1].light_id})
        await home.transport.result(receipt)
        assert rv.operation(raw, env(), queued["operation_id"])["state"] == "off"
    asyncio.run(scenario())


def test_lost_ack_restart_reuses_receipt_without_second_physical_command(tmp_path):
    home, rv, raw, devices, now, calls = fleet(tmp_path)
    async def scenario():
        await home.cycle()
        enqueue(rv, raw, LIGHTS[3].light_id)
        await home.cycle()
        queued = enqueue(rv, raw, LIGHTS[3].light_id, "on")
        home.transport.lose_result = True
        with pytest.raises(TimeoutError):
            await home.cycle()
        receipt = home.journal.pending()
        journal = FleetJournal(home.journal.path, LIGHTS, SITE, clock=lambda: now[0])
        restarted = HomeRunner(LIGHTS, home.resolvers, home.transport, journal, clock=lambda: now[0])
        assert journal.pending() == receipt
        await restarted.cycle()
        assert journal.pending() is None and devices[3].commands == 1
        assert rv.operation(raw, env(), queued["operation_id"])["state"] == "on"
    asyncio.run(scenario())


def test_catalog_change_retires_old_assignment_and_preserves_its_truth(tmp_path):
    home, rv, raw, devices, now, calls = fleet(tmp_path)
    async def scenario():
        await home.cycle()
        enqueue(rv, raw, LIGHTS[0].light_id)
        assignment, _ = await home.transport.poll(home.journal.next_poll() | {
            "lights": list(home.service.lights()), "catalog_revision": home.journal.catalog_revision})
        old_generation = assignment.generation
        await home.transport.poll(home.journal.next_poll() | {
            "lights": list(home.service.lights())[1:], "catalog_revision": "b" * 64})
        with pytest.raises(PermissionUnavailable):
            await home.transport.authorize(home.journal.agent_id, assignment)
        await home.execute(assignment)
        assert not calls
        receipt = home.journal.pending()
        assert receipt["outcome"]["status"] == "denied" and receipt["generation"] == old_generation
        await home.transport.result(receipt)
        with pytest.raises(AccessDenied):
            enqueue(rv, raw, LIGHTS[0].light_id)
    asyncio.run(scenario())


def test_fleet_config_uses_explicit_allowlist_selection_without_discovery(tmp_path, monkeypatch):
    (tmp_path / "devices.json").write_text(json.dumps({"lights": [asdict(light) for light in LIGHTS]}))
    config = {"allowlist_file": "devices.json", "light_id": LIGHTS[0].light_id,
              "light_ids": [light.light_id for light in LIGHTS],
              "device_hosts": {light.light_id: "synthetic-host" for light in LIGHTS},
              "website_url": SITE, "website_key": KEY, "operation_db": "journal.sqlite"}
    path = tmp_path / "home.json"
    path.write_text(json.dumps(config))
    def forbidden(*args, **kwargs):
        pytest.fail("Configuration performed discovery.")
    monkeypatch.setattr("boomer_switch.kasa_pilot.Discover.discover_single", forbidden)
    runner = load_home(path)
    assert len(runner.service.lights()) == 15
    assert runner.journal.next_poll()["sequence"] == 1
    config["light_ids"] = [LIGHTS[1].light_id]
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="original lamp"):
        load_home(path)
    config["light_ids"] = [LIGHTS[0].light_id, "excluded-light"]
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="explicit allowed"):
        load_home(path)


@pytest.mark.parametrize("module", ["boomer_switch.home_runner", "boomer_switch.pilot"])
def test_both_cli_config_checks_have_no_journal_or_network_effects(tmp_path, module):
    from pathlib import Path
    (tmp_path / "devices.json").write_text(json.dumps({"lights": [asdict(light) for light in LIGHTS]}))
    config = {"allowlist_file": "devices.json", "light_id": LIGHTS[0].light_id,
              "light_ids": [light.light_id for light in LIGHTS],
              "device_hosts": {light.light_id: "synthetic-host" for light in LIGHTS},
              "website_url": SITE, "website_key": KEY, "operation_db": "journal.sqlite"}
    path = tmp_path / "home.json"
    path.write_text(json.dumps(config))
    result = subprocess.run([sys.executable, "-m", module, "--config", str(path), "--check-config"],
                            capture_output=True, text=True, timeout=10, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"valid": True, "selected_lights": 15, "multi_light": True}
    assert not (tmp_path / "journal.sqlite").exists() and not (tmp_path / "journal.sqlite.lock").exists()


def test_normal_entry_acquires_runtime_lock_before_journal_migration(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from boomer_switch import home_runner
    path = tmp_path / "home.json"
    path.write_text(json.dumps({"operation_db": "journal.sqlite"}))
    monkeypatch.setattr("sys.argv", ["home_runner", "--config", str(path)])
    def forbidden(*args):
        pytest.fail("Configuration reached journal migration before the runtime lock.")
    monkeypatch.setattr(home_runner, "_prepare_home", lambda path: (None, None, None, tmp_path / "journal.sqlite", True))
    monkeypatch.setattr(home_runner, "_build_home", forbidden)
    @contextmanager
    def occupied(database):
        raise RuntimeError("Synthetic existing writer")
        yield
    monkeypatch.setattr(home_runner, "runtime_lock", occupied)
    with pytest.raises(RuntimeError, match="existing writer"):
        home_runner.main()


def test_access_projects_catalog_only_to_approved_home_browser(tmp_path):
    from boomer_switch.website import WebsiteApplication
    from test_website import invoke
    home, rv, raw, devices, now, calls = fleet(tmp_path)
    app = WebsiteApplication(rv.store, None, origin="https://site.invalid", mount="/house-lights",
                             owner_identity="synthetic-owner", rendezvous=rv)
    asyncio.run(home.cycle())
    status, _, payload = invoke(app, "/api/access", cookie="bs_session=" + raw, **env())
    result = json.loads(payload)
    assert status == 200 and len(result["lights"]) == 15 and "light" not in result
    assert not any(light.device_id.encode() in payload for light in LIGHTS)
    for cookie, source in (("bs_session=" + raw, env(CELL)), ("", env())):
        result = json.loads(invoke(app, "/api/access", cookie=cookie, **source)[2])
        assert "lights" not in result and "light" not in result and result["home_ready"] is False


def test_atomic_config_replacement_cannot_change_the_locked_journal(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from boomer_switch import home_runner
    (tmp_path / "devices.json").write_text(json.dumps({"lights": [asdict(light) for light in LIGHTS]}))
    config = {"allowlist_file": "devices.json", "light_id": LIGHTS[0].light_id,
              "light_ids": [light.light_id for light in LIGHTS],
              "device_hosts": {light.light_id: "synthetic-host" for light in LIGHTS},
              "website_url": SITE, "website_key": KEY, "operation_db": "first.sqlite"}
    path = tmp_path / "home.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr("sys.argv", ["home_runner", "--config", str(path)])
    observed = []
    @contextmanager
    def replace_during_lock(database):
        observed.append(database)
        path.write_text(json.dumps(config | {"operation_db": "second.sqlite"}))
        yield
    async def capture(runner):
        observed.append(runner.journal.path)
    monkeypatch.setattr(home_runner, "runtime_lock", replace_during_lock)
    monkeypatch.setattr(home_runner, "run", capture)
    home_runner.main()
    assert observed == [tmp_path / "first.sqlite", tmp_path / "first.sqlite"]
    assert not (tmp_path / "second.sqlite").exists()


def test_fleet_lease_requires_target_even_after_mode_change_and_restart(tmp_path):
    home, rv, raw, devices, now, calls = fleet(tmp_path)
    async def scenario():
        await home.cycle()
        enqueue(rv, raw, LIGHTS[0].light_id)
        assignment, _ = await home.transport.poll(home.journal.next_poll() | {
            "lights": list(home.service.lights()), "catalog_revision": home.journal.catalog_revision})
        await home.transport.authorize(home.journal.agent_id, assignment)
        receipt = home.journal.complete(assignment, {"status": "confirmed", "state": "off"}, None)
        await home.transport.poll(home.journal.next_poll() | {"light": home.service.lights()[0]})
        reopened = HomeRendezvous(rv.store, KEY)
        transport = LocalWebsite(reopened, lambda: now[0])
        without_target = {key: value for key, value in receipt.items() if key != "light_id"}
        with pytest.raises(OperationConflict, match="exact selected light"):
            await transport.result(without_target)
        await transport.result(receipt)
    asyncio.run(scenario())


def test_singleton_fleet_runner_rejects_missing_target_before_device_access(tmp_path):
    home, rv, raw, devices, now, calls = fleet(tmp_path)
    journal = FleetJournal(tmp_path / "singleton.sqlite", LIGHTS[:1], SITE, clock=lambda: now[0])
    runner = HomeRunner(LIGHTS[:1], {LIGHTS[0].light_id: home.resolvers[LIGHTS[0].light_id]}, home.transport, journal, clock=lambda: now[0])
    assignment = Assignment(str(uuid.uuid4()), "on", hashlib.sha256(b"session").hexdigest(), 1008, 1, 1)
    asyncio.run(runner.execute(assignment))
    assert not calls and devices[0].commands == 0
    assert journal.pending()["outcome"]["status"] == "denied"


def test_legacy_server_target_backfill_is_once_and_only_proven_generation(tmp_path):
    store = RegistrationStore(tmp_path / "site", mode="production")
    with store._transaction() as db:
        db.executescript('''
            CREATE TABLE home_connection(id INTEGER PRIMARY KEY,agent TEXT,sequence INTEGER,request_id TEXT,peer TEXT,
                generation INTEGER,seen REAL,last_operation TEXT,valid INTEGER,active INTEGER,light TEXT);
            CREATE TABLE home_operations(id TEXT PRIMARY KEY,session TEXT,peer TEXT,generation INTEGER,action TEXT,
                status TEXT,created REAL,expires REAL,poll_sequence INTEGER,outcome TEXT,completed REAL,invalidated INTEGER);
        ''')
        db.execute("INSERT INTO home_connection VALUES(1,'agent',1,'request','peer',2,1000,NULL,1,0,?)",
                   (json.dumps({"light_id": "current-lamp", "name": "Current lamp", "room": "Sample room"}),))
        for generation in (1, 2):
            db.execute("INSERT INTO home_operations VALUES(?, 'session','peer',?,'read','completed',999,1008,1,NULL,1000,0)",
                       (f"operation-{generation}", generation))
    HomeRendezvous(store, KEY)
    with store._transaction() as db:
        rows = dict(db.execute("SELECT id,light_id FROM home_operations"))
        assert rows == {"operation-1": None, "operation-2": "current-lamp"}
        db.execute("INSERT INTO home_operations(id,generation,light_id) VALUES('new-unbound',2,NULL)")
    HomeRendezvous(store, KEY)
    with store._transaction() as db:
        assert db.execute("SELECT light_id FROM home_operations WHERE id='new-unbound'").fetchone()[0] is None
