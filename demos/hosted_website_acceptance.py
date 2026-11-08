"""Exercise the hosted route across synthetic home address changes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from enum import Enum
from http.cookies import SimpleCookie
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
from socketserver import ThreadingMixIn
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
import time
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from reportlab.pdfgen.canvas import Canvas
import zxingcpp

from boomer_switch.home_runner import HomeRunner, OutboundJournal, WebsiteTransport
from boomer_switch.home_rendezvous import HomeRendezvous
from boomer_switch.kasa_pilot import KasaPilotResolver
from boomer_switch.lights import Light
from boomer_switch.registration import RegistrationStore
from boomer_switch.website import WebsiteApplication
from boomer_switch.website_card import render_hosted_card
from boomer_switch.website_mail import WebsiteMail


ROOT = Path(__file__).resolve().parents[1]
EMAIL = "guest@example.invalid"
STEP = "fixture startup"
LOOPBACK = str(ipaddress.IPv4Address(socket.INADDR_LOOPBACK))
if not ipaddress.ip_address(LOOPBACK).is_loopback:
    raise RuntimeError("Localhost must resolve to loopback.")


def check(condition: bool, label: str, detail: str = "") -> None:
    global STEP
    STEP = label
    print(f"CHECK: {label}{': ' + detail if detail else ''}", flush=True)
    if not condition:
        raise AssertionError


class FixtureServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True

    def enable_tls(self, context):
        self.socket = context.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)

    def shutdown_request(self, request):
        try:
            if isinstance(request, ssl.SSLSocket) and request.version() is not None:
                # The WSGI TCP server otherwise closes TLS without close_notify.
                request.settimeout(1)
                request.unwrap()
        except OSError:
            self.handle_error(request, None, phase="TLS close")
        finally:
            super().shutdown_request(request)

    def handle_error(self, _request, _client_address, *, phase="request"):
        print(f"CHECK: fixture {phase} error ({type(sys.exception()).__name__})", flush=True)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *_args):
        pass


class Inbox:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


class SyntheticPeers:
    """Set test-only direct peers outside browser-controlled input."""

    def __init__(self):
        values = []
        while len(values) < 3:
            address = ipaddress.IPv4Address(secrets.randbits(32))
            if address.is_global and not address.is_multicast and str(address) not in values:
                values.append(str(address))
        self.a, self.b, self.cellular = values
        self.browser = self.home = self.a
        self.lock = Lock()

    def set(self, *, browser=None, home=None):
        with self.lock:
            if browser is not None:
                self.browser = browser
            if home is not None:
                self.home = home

    def peer(self, machine):
        with self.lock:
            return self.home if machine else self.browser


class SyntheticProtocol:
    _transport = object()

    async def query(self, request, retry_count=0):
        return {"system": {"set_relay_state": {"err_code": 0}}}

    async def close(self):
        pass


class DeviceType(Enum):
    Bulb = "bulb"


class SyntheticDevice:
    device_type = DeviceType.Bulb
    children = []
    device_id = "synthetic-device"

    def __init__(self):
        self.protocol = SyntheticProtocol()
        self.is_on = False
        self.commands = 0
        self.updates = 0
        self.paused = Event()
        self.release = Event()
        self.store = None
        self.arm = False
        self.command_updates = 0
        self.pending_id = None

    async def update(self):
        self.updates += 1
        if self.arm:
            with self.store._transaction() as db:
                row = db.execute("SELECT id FROM home_operations WHERE action='on' AND status IN ('leased','authorized') ORDER BY created DESC LIMIT 1").fetchone()
            if row is not None:
                self.command_updates += 1
                if self.command_updates == 2:
                    self.pending_id = row["id"]
                    self.paused.set()
                    if not await asyncio.to_thread(self.release.wait, 4):
                        raise TimeoutError

    async def turn_on(self):
        self.commands += 1
        self.is_on = True

    async def turn_off(self):
        self.commands += 1
        self.is_on = False

    async def disconnect(self):
        pass


def make_certificate(directory: Path) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address(LOOPBACK))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "test-cert.pem", directory / "test-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return cert_path, key_path


def run() -> None:
    global STEP
    scratch = ROOT / ".test-runs"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="website-route-demo-", dir=scratch) as directory:
        root = Path(directory)
        cert, key = make_certificate(root)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        store = RegistrationStore(root / "state", mode="production")
        inbox = Inbox()
        mail = WebsiteMail(store, inbox, "sender@example.invalid", "owner@example.invalid")
        owner_fixture, service_key = secrets.token_urlsafe(32), secrets.token_urlsafe(40)
        peers = SyntheticPeers()
        device = SyntheticDevice()
        device.store = store
        server = make_server(LOOPBACK, 0, lambda *_: [], server_class=FixtureServer, handler_class=QuietHandler)
        server.enable_tls(tls)
        origin = f"https://{LOOPBACK}:{server.server_port}"
        mount = origin + "/house-lights"
        rendezvous = HomeRendezvous(store, service_key)
        website = WebsiteApplication(store, mail, origin=origin, mount="/house-lights", owner_identity="sample-owner", rendezvous=rendezvous)
        owner_changes = []

        def app(environ, start_response):
            path = environ.get("PATH_INFO", "")
            if not path.startswith("/house-lights/"):
                start_response("404 Not Found", [("Content-Length", "0")])
                return [b""]
            environ["PATH_INFO"] = path[len("/house-lights"):]
            environ["SCRIPT_NAME"] = "/house-lights"
            environ["wsgi.url_scheme"] = "https"
            environ["boomer.direct_peer"] = True
            environ["REMOTE_ADDR"] = peers.peer(environ["PATH_INFO"].startswith("/home/"))
            cookie = SimpleCookie()
            cookie.load(environ.get("HTTP_COOKIE", ""))
            if "fixture_owner" in cookie and secrets.compare_digest(cookie["fixture_owner"].value, owner_fixture):
                environ["REMOTE_USER"] = "sample-owner"
            if environ["PATH_INFO"] == "/api/people" and environ.get("REQUEST_METHOD") == "POST":
                owner_changes.append(1)
            return website(environ, start_response)

        server.set_app(app)
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        light = Light("pilot-lamp", "Sample lamp", "Sample room", device.device_id)
        async def discover(_host, **_options):
            return device
        resolver = KasaPilotResolver(light, "synthetic-private-host", discover=discover)
        transport = WebsiteTransport(mount, service_key)
        transport.ssl = ssl.create_default_context(cafile=str(cert))
        runner = HomeRunner(light, resolver, transport, OutboundJournal(root / "journal.sqlite", device.device_id, mount))
        stop = Event()
        runner_errors = []
        def serve_runner():
            async def cycles():
                while not stop.is_set():
                    try:
                        await runner.cycle()
                    except Exception as error:
                        runner_errors.append(type(error).__name__)
                    await asyncio.sleep(0.2)
            asyncio.run(cycles())
        runner_thread = Thread(target=serve_runner, daemon=True)
        runner_thread.start()
        try:
            STEP = "synthetic printed card"
            template, output = root / "synthetic-template.pdf", root / "card.pdf"
            canvas = Canvas(str(template), pagesize=(612, 792))
            for number in (1, 2):
                canvas.drawString(80, 740, f"Synthetic layout {number}")
                canvas.showPage()
            canvas.save()
            invitation = store.invitation()
            invite_url = mount + "/invite/" + invitation["token"]
            short_url = mount + "/s/" + invitation["alias"]
            render_hosted_card(template, output, {"verified": True, "invite_url": invite_url, "short_url": short_url})
            raster = subprocess.run(["pdftoppm", "-png", "-r", "144", str(output), str(root / "page")], capture_output=True, timeout=30, check=False)
            check(raster.returncode == 0, "render card", f"exit={raster.returncode}")
            with Image.open(root / "page-1.png") as image:
                qr = zxingcpp.read_barcodes(image)
            check(len(qr) == 1 and qr[0].text == invite_url, "card QR", f"count={len(qr)}")
            with sync_playwright() as playwright:
                browser = playwright.firefox.launch()
                try:
                    def context():
                        result = browser.new_context(viewport={"width": 375, "height": 667}, ignore_https_errors=True)
                        result.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
                        return result
                    STEP = "register guest"
                    guest_context = context()
                    guest = guest_context.new_page()
                    guest.goto(qr[0].text, wait_until="domcontentloaded")
                    guest.wait_for_url(mount + "/guest")
                    guest.locator("#name").fill("Sample Guest")
                    guest.locator("#email").fill(EMAIL)
                    guest.locator("#details button").click()
                    guest.locator("#code").wait_for(state="visible")
                    messages = [m for m in inbox.messages if str(m["To"]) == EMAIL]
                    check(bool(messages), "fake mailbox", f"messages={len(messages)}")
                    code = re.search(r"\b[0-9]{6}\b", messages[-1].get_content())
                    check(code is not None, "fake code present")
                    guest.locator("#code").fill(code.group())
                    guest.locator("#verification button[type=submit], #verification button:not([type])").first.click()
                    guest.get_by_text("Waiting for approval", exact=True).wait_for(state="visible")
                    STEP = "owner approves"
                    owner_context = context()
                    owner_context.add_cookies([{"name": "fixture_owner", "value": owner_fixture, "url": origin, "secure": True, "httpOnly": True, "sameSite": "Strict"}])
                    owner = owner_context.new_page()
                    owner.goto(mount + "/people", wait_until="domcontentloaded")
                    row = owner.locator(f'[data-email="{EMAIL}"]')
                    row.wait_for(state="visible", timeout=15000)
                    row.locator('[data-action="approve"]').click()
                    expect(row).to_have_attribute("data-status", "approved", timeout=15000)
                    guest.bring_to_front()
                    expect(guest.locator("#status")).to_have_text("Access approved", timeout=15000)
                    expect(guest.locator("#light-state")).to_have_text("Off", timeout=15000)
                    expect(guest.locator("#light-on")).to_be_enabled(timeout=15000)
                    expect(guest.locator("#light-name")).to_have_text("Sample lamp")
                    expect(guest.locator("#light-room")).to_have_text("Sample room")
                    cookies = [c for c in guest_context.cookies(mount) if c["name"] == "bs_session"]
                    check(len(cookies) == 1, "approved cookie", f"count={len(cookies)}")
                    cookie_value = cookies[0]["value"]
                    mail_count, owner_count = len(inbox.messages), len(owner_changes)

                    STEP = "pause old address command before dispatch"
                    device.arm = True
                    guest.locator("#light-on").click()
                    paused = device.paused.wait(5)
                    check(paused and device.pending_id is not None, "old command paused after lease", f"paused={paused}")
                    peers.set(home=peers.b, browser=peers.b)
                    device.release.set()
                    deadline = time.monotonic() + 5
                    outcome = None
                    while time.monotonic() < deadline:
                        with store._transaction() as db:
                            record = db.execute("SELECT outcome FROM home_operations WHERE id=?", (device.pending_id,)).fetchone()
                        if record and record["outcome"]:
                            outcome = json.loads(record["outcome"])
                            break
                        time.sleep(0.05)
                    device.arm = False
                    check(outcome is not None and outcome["status"] in ("denied", "not_started"), "old command rejected", f"recorded={outcome is not None}")
                    check(device.commands == 0, "old command never dispatched", f"commands={device.commands}")

                    STEP = "bookmark at new home address"
                    guest.goto(mount + "/guest", wait_until="domcontentloaded")
                    expect(guest.locator("#status")).to_have_text("Access approved", timeout=15000)
                    expect(guest.locator("#light-state")).to_have_text("Off", timeout=15000)
                    expect(guest.locator("#light-on")).to_be_enabled(timeout=15000)
                    after = [c for c in guest_context.cookies(mount) if c["name"] == "bs_session"]
                    check(len(after) == 1 and after[0]["value"] == cookie_value and guest.url == mount + "/guest", "same clean bookmark and cookie", f"cookies={len(after)}")
                    check(len(inbox.messages) == mail_count and len(owner_changes) == owner_count, "no new verification or approval", f"mail={len(inbox.messages)} owner_actions={len(owner_changes)}")
                    guest.locator("#light-on").click()
                    expect(guest.locator("#light-state")).to_have_text("On", timeout=15000)
                    check(device.commands == 1 and device.is_on, "new home address command", f"commands={device.commands}")

                    def command_status():
                        return guest.evaluate("""async () => {
                            const access = await (await fetch('api/access', {cache:'no-store'})).json();
                            const result = await fetch('api/lights', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':access.csrf}, body:JSON.stringify({operation_id:crypto.randomUUID(),action:'off'})});
                            return {status:result.status, access:access.status};
                        }""")
                    STEP = "old browser source forbidden"
                    peers.set(browser=peers.a)
                    old = command_status()
                    check(old["status"] == 403 and old["access"] == "approved", "old browser source forbidden", f"HTTP={old['status']}")
                    expect(guest.locator("#controls")).to_be_hidden(timeout=10000)
                    STEP = "cellular source forbidden"
                    peers.set(browser=peers.cellular)
                    cell = command_status()
                    check(cell["status"] == 403 and cell["access"] == "approved", "cellular source forbidden", f"HTTP={cell['status']}")
                    expect(guest.locator("#controls")).to_be_hidden(timeout=10000)
                    check(device.commands == 1, "no unauthorized device command", f"commands={device.commands}")
                    check(not runner_errors, "home runner errors", f"count={len(runner_errors)}")
                    print("PASS: same website bookmark and cookie across address change; old and cellular sources denied; stale command blocked; new source command confirmed")
                finally:
                    browser.close()
        finally:
            device.release.set()
            stop.set()
            runner_thread.join(timeout=10)
            server.shutdown()
            server_thread.join(timeout=10)
            server.server_close()


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(f"FAIL: website route demo at {STEP} ({type(error).__name__})", file=sys.stderr)
        sys.exit(1)
