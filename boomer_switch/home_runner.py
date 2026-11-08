"""Poll the hosted website over outbound HTTPS without a local listener."""

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import socket
import ssl
import time
from urllib.parse import urlsplit
import uuid

import aiohttp

from .configuration import service_key as validate_service_key, light_metadata, light_catalog
from .kasa_pilot import KasaPilotResolver
from .lights import CommandDenied, LightService, LightState, load_allowlist
from .pilot import CommandJournal, DuplicateCommand, runtime_lock
from .website_grants import PermissionUnavailable, json_object, operation_id


class ReceiptConflict(PermissionUnavailable):
    """A saved physical outcome conflicts with the website record."""


def integer(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("Use a positive bounded integer.")
    return value


def future(value, now, maximum):
    try:
        if type(value) not in (int, float) or not math.isfinite(value) or not now < value <= now + maximum:
            raise ValueError
    except (OverflowError, TypeError):
        raise ValueError from None
    return value


@dataclass(frozen=True)
class Assignment:
    operation_id: str
    action: str
    session_binding: str
    expires: float
    generation: int
    sequence: int
    light_id: str | None = None

    @classmethod
    def parse(cls, value, generation, sequence, now):
        if not isinstance(value, dict) or set(value) not in ({"operation_id", "action", "session_binding", "expires", "generation"}, {"operation_id", "action", "session_binding", "expires", "generation", "light_id"}):
            raise ValueError
        operation_id(value["operation_id"])
        if value["action"] not in ("read", "on", "off") or integer(value["generation"]) != generation:
            raise ValueError
        if not isinstance(value["session_binding"], str) or not re.fullmatch("[a-f0-9]{64}", value["session_binding"]):
            raise ValueError
        if "light_id" in value and (not isinstance(value["light_id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value["light_id"])):
            raise ValueError
        future(value["expires"], now, 8)
        return cls(sequence=sequence, **value)


class OutboundJournal(CommandJournal):
    """Commit sequence numbers and outcomes beside the physical dispatch record."""

    def __init__(self, path, device_id, website_url, *, clock=time.time):
        super().__init__(path, device_id, allow_fleet=getattr(self, "fleet", False))
        self.clock = clock
        binding = hashlib.sha256(website_url.encode()).hexdigest()
        with self._transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS outbound_agent(id INTEGER PRIMARY KEY CHECK(id=1),agent TEXT NOT NULL,sequence INTEGER NOT NULL,site TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS outbound_receipts(operation_id TEXT PRIMARY KEY,body TEXT NOT NULL,action TEXT NOT NULL,expires REAL NOT NULL,acked INTEGER NOT NULL DEFAULT 0)")
            row = db.execute("SELECT agent,site FROM outbound_agent WHERE id=1").fetchone()
            if row is None:
                db.execute("INSERT INTO outbound_agent VALUES(1,?,0,?)", (str(uuid.uuid4()), binding))
                row = db.execute("SELECT agent,site FROM outbound_agent WHERE id=1").fetchone()
            if row[1] != binding:
                raise ValueError("This journal belongs to another website.")
            self.agent_id = operation_id(row[0])

    @contextmanager
    def _transaction(self):
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def next_poll(self):
        with self._transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            value = integer(db.execute("SELECT sequence FROM outbound_agent WHERE id=1").fetchone()[0] + 1)
            db.execute("UPDATE outbound_agent SET sequence=? WHERE id=1", (value,))
        return {"agent_id": self.agent_id, "sequence": value, "request_id": str(uuid.uuid4())}

    def receipt(self, operation):
        with self._transaction() as db:
            row = db.execute("SELECT body FROM outbound_receipts WHERE operation_id=?", (operation,)).fetchone()
        return json_object(row[0]) if row else None

    def command_status(self, operation):
        with self._transaction() as db:
            row = db.execute("SELECT status FROM commands WHERE id=?", (operation,)).fetchone()
        return row[0] if row else None

    def complete(self, assignment, outcome, status):
        body = {"agent_id": self.agent_id, "sequence": assignment.sequence, "operation_id": assignment.operation_id,
            "generation": assignment.generation, "request_id": str(uuid.uuid4()), "outcome": outcome}
        if assignment.light_id is not None:
            body["light_id"] = assignment.light_id
        with self._transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            if status is not None:
                db.execute("UPDATE commands SET status=? WHERE id=?", (status, assignment.operation_id))
            db.execute("INSERT INTO outbound_receipts(operation_id,body,action,expires) VALUES(?,?,?,?)",
                (assignment.operation_id, json.dumps(body), assignment.action, assignment.expires))
        return body

    def pending(self):
        with self._transaction() as db:
            # Expired reads have no physical-effect obligation and cannot be leased again.
            db.execute("DELETE FROM outbound_receipts WHERE action='read' AND expires<=?", (self.clock(),))
            row = db.execute("SELECT body FROM outbound_receipts WHERE acked=0 ORDER BY rowid LIMIT 1").fetchone()
        return json_object(row[0]) if row else None

    def acknowledge(self, operation):
        with self._transaction() as db:
            db.execute("DELETE FROM outbound_receipts WHERE operation_id=? AND action='read'", (operation,))
            db.execute("UPDATE outbound_receipts SET acked=1,body='{}' WHERE operation_id=?", (operation,))


class WebsiteTransport:
    """Use only fixed hosted routes with bounded verified HTTPS and no redirects or proxies."""

    def __init__(self, website_url, service_key, *, clock=time.time, session_factory=aiohttp.ClientSession):
        parsed = urlsplit(website_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Configure the explicit hosted HTTPS website URL.")
        validate_service_key(service_key)
        self.url, self.key, self.clock = website_url.rstrip("/"), service_key, clock
        self.session_factory = session_factory
        self.ssl = ssl.create_default_context()

    async def post(self, route, body):
        if route not in ("poll", "authorize", "result"):
            raise ValueError
        try:
            async with self.session_factory(timeout=aiohttp.ClientTimeout(total=2, connect=1, sock_read=1), trust_env=False,
                    connector=aiohttp.TCPConnector(family=socket.AF_INET)) as client:
                async with client.post(self.url + "/home/" + route, json=body,
                        headers={"Authorization": "Bearer " + self.key}, ssl=self.ssl, allow_redirects=False) as response:
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(1024):
                        raw.extend(chunk)
                        if len(raw) > 8192:
                            raise PermissionUnavailable
                    if response.status == 409 and route == "result":
                        raise ReceiptConflict
                    if response.status != 200:
                        raise PermissionUnavailable
                    result = json_object(raw)
            if result.get("request_id") != body["request_id"]:
                raise PermissionUnavailable
            return result
        except ReceiptConflict:
            raise
        except Exception:
            raise PermissionUnavailable("The hosted home connection is unavailable.") from None

    async def poll(self, body):
        result = await self.post("poll", body)
        if set(result) != {"request_id", "generation", "expires", "operation", "poll_after"}:
            raise PermissionUnavailable
        generation = integer(result["generation"])
        future(result["expires"], self.clock(), 6)
        if type(result["poll_after"]) is not int or not 1 <= result["poll_after"] <= 60:
            raise PermissionUnavailable
        assignment = None if result["operation"] is None else Assignment.parse(result["operation"], generation, body["sequence"], self.clock())
        return assignment, result["poll_after"]

    async def authorize(self, agent_id, assignment):
        body = {"agent_id": agent_id, "sequence": assignment.sequence, "operation_id": assignment.operation_id,
            "generation": assignment.generation, "request_id": str(uuid.uuid4())}
        if assignment.light_id is not None:
            body["light_id"] = assignment.light_id
        result = await self.post("authorize", body)
        if set(result) != {"allowed", "request_id", "operation_id", "generation", "expires"}:
            raise PermissionUnavailable
        if result["allowed"] is not True or result["operation_id"] != assignment.operation_id or integer(result["generation"]) != assignment.generation:
            raise PermissionUnavailable
        return future(result["expires"], self.clock(), 2)

    async def result(self, body):
        result = await self.post("result", body)
        if set(result) != {"accepted", "request_id", "operation_id"} or result["accepted"] is not True or result["operation_id"] != body["operation_id"]:
            raise PermissionUnavailable


class HomeRunner:
    """Dispatch one current website assignment after a fresh authorization at the lamp boundary."""

    def __init__(self, light, resolver, transport, journal, *, clock=time.time):
        lights = tuple(light) if isinstance(light, (list, tuple)) else (light,)
        if journal.identity != hashlib.sha256(lights[0].device_id.encode()).hexdigest():
            raise ValueError("Use the journal bound to the original physical lamp.")
        self.light = lights[0]
        self.lights = {item.light_id: item for item in lights}
        self.resolvers = resolver if isinstance(resolver, dict) else {self.light.light_id: resolver}
        if set(self.resolvers) != set(self.lights):
            raise ValueError("Configure a resolver for every selected light.")
        self.resolver = self.resolvers[self.light.light_id]
        self.transport, self.journal, self.clock = transport, journal, clock
        self.fleet = getattr(journal, 'fleet', False)
        if len(lights) > 1 and not self.fleet:
            raise ValueError("Use a target-bound journal for multiple lights.")
        providers = {item.device_id: self.resolvers[item.light_id] for item in lights}
        async def resolve(identity):
            if identity not in providers:
                raise CommandDenied("Select an allowed physical light.")
            return await providers[identity](identity)
        self.service = LightService(lights, resolve, timeout_seconds=8)
        light_catalog(list(self.service.lights()))
        self.busy = asyncio.Lock()

    def current(self, assignment, deadline):
        now = self.clock()
        if not now < assignment.expires or not now < deadline <= now + 2:
            raise PermissionUnavailable

    async def execute(self, assignment):
        if self.journal.receipt(assignment.operation_id) is not None:
            return
        target = assignment.light_id
        if target is None and not self.fleet and len(self.lights) == 1:
            target = self.light.light_id
        provider = self.resolvers.get(target)
        attempted, reserved = False, False
        outcome, status = {"status": "not_started", "state": "unavailable"}, "not_started"
        try:
            if provider is None:
                raise CommandDenied("Select an allowed light.")
            self.current(assignment, await self.transport.authorize(self.journal.agent_id, assignment))
            if assignment.action != "read":
                if self.fleet:
                    self.journal.reserve_target(assignment.session_binding, target, assignment.action, assignment.operation_id)
                else:
                    self.journal.reserve(assignment.session_binding, target, assignment.action, assignment.operation_id)
                reserved = True

            # The guard obtains a new deadline after refresh. The initial grant cannot authorize dispatch.
            async def final_guard():
                nonlocal attempted
                try:
                    deadline = await self.transport.authorize(self.journal.agent_id, assignment)
                    self.current(assignment, deadline)
                    if assignment.action != "read":
                        self.journal.dispatched(assignment.operation_id)
                        self.current(assignment, deadline)
                        attempted = True
                except PermissionUnavailable:
                    raise CommandDenied from None

            async with asyncio.timeout(8):
                state = (await self.service.state(target, guard=final_guard) if assignment.action == "read"
                    else await self.service.command(target, assignment.action, guard=final_guard))
            if state == LightState.UNAVAILABLE:
                outcome["status"], status = ("unknown", "unknown") if attempted else ("not_started", "not_started")
            else:
                outcome = {"status": "confirmed", "state": str(state)}
                status = "finished"
                if assignment.action == "read":
                    if self.fleet:
                        self.journal.reconcile_target(target)
                    else:
                        self.journal.reconcile()
        except DuplicateCommand:
            prior = self.journal.command_status(assignment.operation_id)
            outcome["status"], status = (("unknown", "unknown") if prior in ("started", "dispatched", "unknown", "finished", "reconciled")
                else ("denied", "denied"))
        except (PermissionUnavailable, CommandDenied):
            outcome["status"], status = "denied", "denied"
        except Exception:
            outcome["status"], status = ("unknown", "unknown") if attempted else ("not_started", "not_started")
        finally:
            if provider is not None:
                await provider.close()
        if not reserved and self.journal.receipt(assignment.operation_id) is not None:
            return
        self.journal.complete(assignment, outcome, status if reserved else None)

    async def cycle(self):
        if self.busy.locked():
            raise DuplicateCommand("The home runner is busy.")
        async with self.busy:
            # Drain old truth before a poll can consume a new one-use assignment.
            pending = self.journal.pending()
            if pending is not None:
                await self.transport.result(pending)
                self.journal.acknowledge(pending["operation_id"])
            metadata = ({"lights": list(self.service.lights()), "catalog_revision": self.journal.catalog_revision}
                        if self.fleet else {"light": self.service.lights()[0]})
            poll = self.journal.next_poll() | metadata
            assignment, delay = await self.transport.poll(poll)
            if assignment is not None:
                if self.fleet and assignment.light_id is None:
                    raise PermissionUnavailable("The home assignment has no selected light.")
                await self.execute(assignment)
                pending = self.journal.pending()
                if pending is not None:
                    await self.transport.result(pending)
                    self.journal.acknowledge(pending["operation_id"])
            return delay


def _prepare_home(path):
    from kasa import Credentials

    config = json_object(Path(path).read_text(encoding="utf-8"))
    common = {"allowlist_file", "light_id", "website_url", "website_key", "operation_db"}
    fleet = "light_ids" in config
    required = common | ({"light_ids", "device_hosts"} if fleet else {"device_host"})
    if not required <= set(config) or set(config) - required - {"username", "password"}:
        raise ValueError("Read an explicit private home configuration.")
    base = Path(path).resolve().parent
    allowed = {item.light_id: item for item in load_allowlist(base / config["allowlist_file"])}
    ids = config["light_ids"] if fleet else [config["light_id"]]
    if (not isinstance(ids, list) or not ids or any(not isinstance(value, str) for value in ids)
            or len(set(ids)) != len(ids) or config["light_id"] not in ids or any(value not in allowed for value in ids)):
        raise ValueError("Select explicit allowed light IDs and retain the journal's original lamp.")
    ids = [config["light_id"]] + [value for value in ids if value != config["light_id"]]
    selected = [allowed[value] for value in ids]
    if any(item.outlet_id is not None for item in selected):
        raise ValueError("Select standalone lamps for this home runner.")
    light_catalog([{"light_id": item.light_id, "name": item.name, "room": item.room} for item in selected])
    hosts = config["device_hosts"] if fleet else {config["light_id"]: config["device_host"]}
    if not isinstance(hosts, dict) or set(hosts) != set(ids):
        raise ValueError("Configure one private discovery hint per selected light.")
    credentials = None
    if "username" in config or "password" in config:
        if not all(isinstance(config.get(key), str) and config[key] for key in ("username", "password")):
            raise ValueError("Configure both private device credentials when required.")
        credentials = Credentials(config["username"], config["password"])
    transport = WebsiteTransport(config["website_url"], config["website_key"])
    resolvers = {item.light_id: KasaPilotResolver(item, hosts[item.light_id], credentials=credentials) for item in selected}
    return selected, resolvers, transport, base / config["operation_db"], fleet


def _build_home(prepared):
    selected, resolvers, transport, database, fleet = prepared
    if fleet:
        from .fleet_journal import FleetJournal
        journal = FleetJournal(database, selected, transport.url, clock=time.time)
        return HomeRunner(selected, resolvers, transport, journal)
    journal = OutboundJournal(database, selected[0].device_id, transport.url)
    return HomeRunner(selected[0], resolvers[selected[0].light_id], transport, journal)


def load_home(path):
    return _build_home(_prepare_home(path))


async def run(runner):
    delay = 60
    print("Outbound home connection started. Press Ctrl+C to stop.", flush=True)
    while True:
        started = time.monotonic()
        try:
            delay = await runner.cycle()
        except ReceiptConflict:
            raise
        except Exception:
            print("Home connection unavailable. Pending outcomes remain saved. Check website access and the home process.", flush=True)
            delay = min(60, max(5, delay * 2))
        await asyncio.sleep(max(1, delay - (time.monotonic() - started)))


def main():
    parser = argparse.ArgumentParser(description="Run the outbound-only selected-light home connection.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check-config", action="store_true", help="Validate private selection without network calls or journal changes.")
    options = parser.parse_args()
    if options.check_config:
        try:
            selected, _, _, _, fleet = _prepare_home(options.config)
        except Exception:
            parser.error("Read a valid explicit private home configuration.")
        print(json.dumps({"valid": True, "selected_lights": len(selected), "multi_light": fleet}))
        return
    try:
        prepared = _prepare_home(options.config)
        database = prepared[3]
        database.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        parser.error("Read a valid explicit private home configuration.")
    try:
        with runtime_lock(database):
            try:
                runner = _build_home(prepared)
            except Exception:
                parser.error("Read a valid explicit private home configuration.")
            provider_log = logging.getLogger("kasa")
            provider_log.handlers = [logging.NullHandler()]
            provider_log.propagate = False
            provider_log.setLevel(logging.CRITICAL + 1)
            asyncio.run(run(runner))
    except KeyboardInterrupt:
        pass
    except ReceiptConflict:
        parser.exit(1, "Saved outcome conflicts with the website. Stop and reconcile its operation record with the retained home journal before restart.\n")


if __name__ == "__main__":
    main()
