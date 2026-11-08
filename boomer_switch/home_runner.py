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

from .configuration import service_key as validate_service_key, light_metadata
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

    @classmethod
    def parse(cls, value, generation, sequence, now):
        if not isinstance(value, dict) or set(value) != {"operation_id", "action", "session_binding", "expires", "generation"}:
            raise ValueError
        operation_id(value["operation_id"])
        if value["action"] not in ("read", "on", "off") or integer(value["generation"]) != generation:
            raise ValueError
        if not isinstance(value["session_binding"], str) or not re.fullmatch("[a-f0-9]{64}", value["session_binding"]):
            raise ValueError
        future(value["expires"], now, 8)
        return cls(sequence=sequence, **value)


class OutboundJournal(CommandJournal):
    """Commit sequence numbers and outcomes beside the physical dispatch record."""

    def __init__(self, path, device_id, website_url, *, clock=time.time):
        super().__init__(path, device_id)
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
        if journal.identity != hashlib.sha256(light.device_id.encode()).hexdigest():
            raise ValueError("Use the journal bound to this physical lamp.")
        self.light, self.resolver, self.transport, self.journal, self.clock = light, resolver, transport, journal, clock
        self.service = LightService((light,), resolver, timeout_seconds=8)
        light_metadata(self.service.lights()[0])
        self.busy = asyncio.Lock()

    def current(self, assignment, deadline):
        now = self.clock()
        if not now < assignment.expires or not now < deadline <= now + 2:
            raise PermissionUnavailable

    async def execute(self, assignment):
        if self.journal.receipt(assignment.operation_id) is not None:
            return
        attempted, reserved = False, False
        outcome, status = {"status": "not_started", "state": "unavailable"}, "not_started"
        try:
            self.current(assignment, await self.transport.authorize(self.journal.agent_id, assignment))
            if assignment.action != "read":
                self.journal.reserve(assignment.session_binding, self.light.light_id, assignment.action, assignment.operation_id)
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
                state = (await self.service.state(self.light.light_id, guard=final_guard) if assignment.action == "read"
                    else await self.service.command(self.light.light_id, assignment.action, guard=final_guard))
            if state == LightState.UNAVAILABLE:
                outcome["status"], status = ("unknown", "unknown") if attempted else ("not_started", "not_started")
            else:
                outcome = {"status": "confirmed", "state": str(state)}
                status = "finished"
                if assignment.action == "read":
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
            await self.resolver.close()
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
            poll = self.journal.next_poll() | {"light": self.service.lights()[0]}
            assignment, delay = await self.transport.poll(poll)
            if assignment is not None:
                await self.execute(assignment)
                pending = self.journal.pending()
                if pending is not None:
                    await self.transport.result(pending)
                    self.journal.acknowledge(pending["operation_id"])
            return delay


def load_home(path):
    from kasa import Credentials

    config = json_object(Path(path).read_text(encoding="utf-8"))
    required = {"allowlist_file", "light_id", "device_host", "website_url", "website_key", "operation_db"}
    if not required <= set(config) or set(config) - required - {"username", "password"}:
        raise ValueError("Read an explicit private home configuration.")
    base = Path(path).resolve().parent
    selected = [item for item in load_allowlist(base / config["allowlist_file"]) if item.light_id == config["light_id"]]
    if len(selected) != 1 or selected[0].outlet_id is not None:
        raise ValueError("Select exactly one standalone lamp.")
    light_metadata({"light_id": selected[0].light_id, "name": selected[0].name, "room": selected[0].room})
    credentials = None
    if "username" in config or "password" in config:
        if not all(isinstance(config.get(key), str) and config[key] for key in ("username", "password")):
            raise ValueError("Configure both private device credentials when required.")
        credentials = Credentials(config["username"], config["password"])
    transport = WebsiteTransport(config["website_url"], config["website_key"])
    resolver = KasaPilotResolver(selected[0], config["device_host"], credentials=credentials)
    journal = OutboundJournal(base / config["operation_db"], selected[0].device_id, transport.url)
    return HomeRunner(selected[0], resolver, transport, journal)


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
    parser = argparse.ArgumentParser(description="Run the outbound-only one-lamp home connection.")
    parser.add_argument("--config", type=Path, required=True)
    options = parser.parse_args()
    try:
        runner = load_home(options.config)
    except Exception:
        parser.error("Read a valid explicit private home configuration.")
    try:
        with runtime_lock(runner.journal.path):
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
