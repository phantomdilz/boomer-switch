"""Measure a simulated guest command through the real queue and home runner."""

import argparse
import asyncio
from collections import Counter
from enum import Enum
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import types
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from boomer_switch.fleet_journal import FleetJournal
from boomer_switch.home_runner import HomeRunner, WebsiteTransport
from boomer_switch.home_rendezvous import HomeRendezvous
from boomer_switch.lights import Light
from boomer_switch.registration import RegistrationStore
from boomer_switch.website import WebsiteApplication

import ipaddress
PEER = str(ipaddress.IPv4Address(134744072))
KEY = "synthetic-service-key-" + "x" * 32
SITE = "https://example.invalid/lights"
LIGHT = Light("sample-lamp", "Sample lamp", "Sample room", "synthetic-device")


def baseline_class(ref, file, name):
    source = subprocess.check_output(["git", "show", ref + ":boomer_switch/" + file], cwd=ROOT, text=True, timeout=10)
    module = types.ModuleType("boomer_switch._latency_" + file.replace(".", "_"))
    module.__package__ = "boomer_switch"
    exec(compile(source, file, "exec"), module.__dict__)
    return getattr(module, name)


def environment():
    return {"boomer.direct_peer": True, "wsgi.url_scheme": "https", "REMOTE_ADDR": PEER,
            "HTTP_AUTHORIZATION": "Bearer " + KEY}


class DeviceType(Enum):
    Bulb = "bulb"


class Device:
    device_type = DeviceType.Bulb
    device_id = LIGHT.device_id
    children = []

    def __init__(self, latency):
        self.latency, self.is_on, self.actuated, self.updates = latency, False, None, 0

    async def update(self):
        self.updates += 1
        await asyncio.sleep(self.latency)

    async def turn_on(self):
        await asyncio.sleep(self.latency)
        self.is_on, self.actuated = True, time.monotonic()

    async def turn_off(self):
        await asyncio.sleep(self.latency)
        self.is_on, self.actuated = False, time.monotonic()


class Resolver:
    def __init__(self, device):
        self.device = device

    async def __call__(self, identity):
        assert identity == LIGHT.device_id
        await self.device.update()
        return self.device

    async def close(self):
        pass


class Transport(WebsiteTransport):
    def __init__(self, rendezvous, latency):
        super().__init__(SITE, KEY)
        self.rendezvous, self.latency, self.calls = rendezvous, latency, Counter()

    async def post(self, route, body):
        self.calls[route] += 1
        await asyncio.sleep(self.latency)
        return self.rendezvous.machine(environment(), "/home/" + route, body)


async def measure(directory, rendezvous_type, website_type, state, http_seconds, device_seconds):
    store = RegistrationStore(directory / "site", mode="production")
    raw = store.browser(store.invitation()["token"])
    store.start(raw, "Sample guest", "guest@example.invalid")
    store.verify(raw, store.mailbox("guest@example.invalid")["messages"][-1]["code"])
    store.operator("approve", "guest@example.invalid")
    rv = rendezvous_type(store, KEY)
    rv.access(raw, environment())
    device = Device(device_seconds)
    transport = Transport(rv, http_seconds)
    journal = FleetJournal(directory / "journal.sqlite", [LIGHT], SITE, clock=time.time)
    home = HomeRunner([LIGHT], {LIGHT.light_id: Resolver(device)}, transport, journal)
    await home.cycle()
    if state == "warm":
        rv.enqueue(raw, environment(), {"operation_id": str(uuid.uuid4()), "action": "read", "light_id": LIGHT.light_id})
        await home.cycle()
    website = website_type(store, None, origin="https://example.invalid", mount="/lights",
                           owner_identity="synthetic-owner", rendezvous=rv)
    direct = website._access(raw, environment()).get("direct_commands") is True
    device.updates = 0
    transport.calls.clear()
    posts = []
    tap = time.monotonic()
    stop = asyncio.Event()

    async def worker():
        next_poll = tap + .25
        while not stop.is_set():
            await asyncio.sleep(max(0, next_poll - time.monotonic()))
            if stop.is_set():
                return
            await home.cycle()
            next_poll += 1

    task = asyncio.create_task(worker())
    next_ui_poll = tap + .8

    async def request(action):
        nonlocal next_ui_poll
        operation = str(uuid.uuid4())
        await asyncio.sleep(http_seconds)
        rv.enqueue(raw, environment(), {"operation_id": operation, "action": action, "light_id": LIGHT.light_id})
        posts.append(action)
        while True:
            await asyncio.sleep(max(0, next_ui_poll - time.monotonic()))
            next_ui_poll += 1
            await asyncio.sleep(http_seconds)
            result = rv.operation(raw, environment(), operation)
            if result["status"] == "confirmed":
                return result
            if result["status"] in ("unknown", "denied", "not_started") or time.monotonic() - tap > 8:
                raise RuntimeError("Synthetic operation did not confirm.")

    try:
        if state == "idle" and not direct:
            await request("read")
        result = await request("on")
        confirmed = time.monotonic()
        assert result["state"] == "on" and device.is_on and device.actuated is not None
        return {"tap_to_actuation_ms": round((device.actuated - tap) * 1000, 2),
                "tap_to_confirmation_ms": round((confirmed - tap) * 1000, 2),
                "guest_operations": posts, "home_calls": dict(transport.calls), "device_updates": device.updates,
                "direct_commands": direct}
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def run(ref, repeats, output):
    old_rv = baseline_class(ref, "home_rendezvous.py", "HomeRendezvous")
    old_web = baseline_class(ref, "website.py", "WebsiteApplication")
    scratch = ROOT / ".test-runs"
    scratch.mkdir(exist_ok=True)
    evidence = {"baseline_ref": subprocess.check_output(["git", "rev-parse", ref], cwd=ROOT, text=True, timeout=10).strip(),
                "conditions": {"http_call_ms": 50, "device_query_ms": 50, "home_poll_ms": 1000,
                               "ui_result_poll_ms": 1000, "home_phase_ms": 250, "ui_phase_ms": 800},
                "scope": "Real queue, journal, authorization and HomeRunner; simulated UI request schedule, transport and device. No browser, WAN or hardware timing.",
                "idle_definition": "Guest controls ready, no prior confirmed device state; not the 15-second no-browser heartbeat.",
                "warm_definition": "Same light has a fresh confirmed read before the tap.", "samples": []}
    with TemporaryDirectory(prefix="command-latency-", dir=scratch) as directory:
        for repeat in range(repeats):
            for state in ("idle", "warm"):
                for variant, rv, web in (("baseline", old_rv, old_web), ("revised", HomeRendezvous, WebsiteApplication)):
                    path = Path(directory) / f"{repeat}-{state}-{variant}"
                    path.mkdir()
                    sample = await measure(path, rv, web, state, .05, .05)
                    evidence["samples"].append({"repeat": repeat, "state": state, "variant": variant, **sample})
    evidence["median_ms"] = {state: {variant: statistics.median(row["tap_to_actuation_ms"] for row in evidence["samples"] if row["state"] == state and row["variant"] == variant)
                                     for variant in ("baseline", "revised")} for state in ("idle", "warm")}
    current_runner = (ROOT / "boomer_switch" / "home_runner.py").read_bytes().replace(b"\r\n", b"\n")
    baseline_runner = subprocess.check_output(["git", "show", ref + ":boomer_switch/home_runner.py"], cwd=ROOT, timeout=10).replace(b"\r\n", b"\n")
    evidence["home_runner_unchanged"] = hashlib.sha256(current_runner).digest() == hashlib.sha256(baseline_runner).digest()
    evidence["ranges_ms"] = {state: {variant: [min(row["tap_to_actuation_ms"] for row in evidence["samples"] if row["state"] == state and row["variant"] == variant),
                                                       max(row["tap_to_actuation_ms"] for row in evidence["samples"] if row["state"] == state and row["variant"] == variant)]
                                     for variant in ("baseline", "revised")} for state in ("idle", "warm")}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence["median_ms"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default="d283d02")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "pj88-latency" / "measurement.json")
    options = parser.parse_args()
    if not 1 <= options.repeats <= 10:
        parser.error("Use from 1 through 10 repeats.")
    asyncio.run(run(options.baseline, options.repeats, options.output))
