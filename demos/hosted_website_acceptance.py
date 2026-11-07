"""Exercise hosted registration and guest handoff on isolated HTTPS sites."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from wsgiref.simple_server import WSGIRequestHandler, make_server

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from reportlab.pdfgen.canvas import Canvas
import zxingcpp

from boomer_switch.guest_proxy import GuestProxy, WebsiteExchange
from boomer_switch.registration import RegistrationStore
from boomer_switch.website import WebsiteApplication
from boomer_switch.website_card import render_hosted_card
from boomer_switch.website_grants import PersistedGrants
from boomer_switch.website_handoff import WebsiteHandoff
from boomer_switch.website_mail import WebsiteMail


ROOT = Path(__file__).resolve().parents[1]
EMAIL = "guest@example.invalid"
STEP = "fixture startup"
LOOPBACK = socket.gethostbyname("localhost")
if not ipaddress.ip_address(LOOPBACK).is_loopback:
    raise RuntimeError("Localhost must resolve to loopback.")


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *_args):
        pass


class Inbox:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


class SyntheticWifi:
    def require(self, _peer, _local):
        return __import__("time").time() + 6


class SyntheticRelay:
    def __init__(self, grants):
        self.grants = grants

    async def forward(self, method, path, body, session, deadline):
        if not self.grants.deadline(session):
            return 403, b'{"error":"Access unavailable."}'
        assert method == "GET" and path == "/api/lights" and not body
        return 200, b'{"lights":[{"light_id":"pilot-lamp","name":"Sample lamp","room":"Sample room","state":"off"}]}'


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
    with TemporaryDirectory(prefix="hosted-demo-", dir=scratch) as directory:
        root = Path(directory)
        cert, key = make_certificate(root)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        state = root / "state"
        store = RegistrationStore(state, mode="production")
        inbox = Inbox()
        mail = WebsiteMail(store, inbox, "sender@example.invalid", "owner@example.invalid")
        owner_fixture = secrets.token_urlsafe(32)
        service_key = secrets.token_urlsafe(40)
        website_server = make_server(LOOPBACK, 0, lambda *_: [], handler_class=QuietHandler)
        website_server.socket = tls.wrap_socket(website_server.socket, server_side=True)
        website_origin = f"https://{LOOPBACK}:{website_server.server_port}"
        guest_socket = socket.socket()
        guest_socket.bind((LOOPBACK, 0))
        guest_socket.listen(128)
        guest_socket.setblocking(False)
        guest_origin = f"https://localhost:{guest_socket.getsockname()[1]}"
        website_mount = website_origin + "/house-lights"
        handoff = WebsiteHandoff(store, service_key, guest_origin + "/auth/callback", guest_origin + "/auth/start")
        website = WebsiteApplication(store, mail, origin=website_origin, mount="/house-lights", owner_identity="sample-owner", handoff=handoff)
        cross_site_visits = []

        def app(environ, start_response):
            path = environ.get("PATH_INFO", "")
            if not path.startswith("/house-lights/"):
                start_response("404 Not Found", [("Content-Length", "0")])
                return [b""]
            environ["PATH_INFO"] = path[len("/house-lights"):]
            environ["SCRIPT_NAME"] = "/house-lights"
            environ["wsgi.url_scheme"] = "https"
            cookie = SimpleCookie()
            cookie.load(environ.get("HTTP_COOKIE", ""))
            if "fixture_owner" in cookie and secrets.compare_digest(cookie["fixture_owner"].value, owner_fixture):
                environ["REMOTE_USER"] = "sample-owner"
            if environ["PATH_INFO"] == "/guest":
                cross_site_visits.append(environ.get("HTTP_SEC_FETCH_SITE", ""))
            return website(environ, start_response)

        website_server.set_app(app)
        website_thread = Thread(target=website_server.serve_forever, daemon=True)
        website_thread.start()
        exchange = WebsiteExchange(website_mount + "/exchange", service_key)
        exchange.ssl = ssl.create_default_context(cafile=str(cert))
        proxy = GuestProxy(origin=guest_origin, website_guest_url=website_mount + "/guest", light_id="pilot-lamp",
                           wifi=SyntheticWifi(), relay=SyntheticRelay(PersistedGrants(store.path)), exchange=exchange)
        loop = asyncio.new_event_loop()
        started = __import__("threading").Event()
        errors = []

        def serve_guest():
            asyncio.set_event_loop(loop)
            async def start():
                runner = web.AppRunner(proxy.application(), access_log=None)
                await runner.setup()
                await web.SockSite(runner, guest_socket, ssl_context=tls).start()
                return runner
            try:
                runner = loop.run_until_complete(start())
            except Exception as error:
                errors.append(type(error).__name__)
                started.set()
                return
            started.set()
            try:
                loop.run_forever()
            finally:
                loop.run_until_complete(runner.cleanup())
                loop.close()

        guest_thread = Thread(target=serve_guest, daemon=True)
        guest_thread.start()
        try:
            assert started.wait(10) and not errors
            STEP = "synthetic printed card"
            template, output = root / "synthetic-template.pdf", root / "card.pdf"
            canvas = Canvas(str(template), pagesize=(612, 792))
            for number in (1, 2):
                canvas.drawString(80, 740, f"Synthetic layout {number}")
                canvas.showPage()
            canvas.save()
            invitation = store.invitation()
            invite_url = website_mount + "/invite/" + invitation["token"]
            short_url = website_mount + "/s/" + invitation["alias"]
            render_hosted_card(template, output, {"verified": True, "invite_url": invite_url, "short_url": short_url})
            raster = subprocess.run(["pdftoppm", "-png", "-r", "144", str(output), str(root / "page")],
                                    capture_output=True, timeout=30, check=False)
            assert raster.returncode == 0
            with Image.open(root / "page-1.png") as image:
                qr = zxingcpp.read_barcodes(image)
            assert len(qr) == 1 and qr[0].text == invite_url
            with sync_playwright() as playwright:
                browser = playwright.firefox.launch()
                try:
                    def context():
                        result = browser.new_context(viewport={"width": 375, "height": 667}, ignore_https_errors=True)
                        result.route("**/*", lambda route: route.continue_() if route.request.url.startswith((website_origin + "/", guest_origin + "/")) else route.abort())
                        return result

                    STEP = "guest invitation and fake mail"
                    guest_context = context()
                    guest = guest_context.new_page()
                    guest.goto(qr[0].text, wait_until="domcontentloaded")
                    guest.wait_for_url(website_mount + "/guest")
                    guest.locator("#name").fill("Sample Guest")
                    guest.locator("#email").fill(EMAIL)
                    guest.locator("#details button").click()
                    guest.locator("#code").wait_for(state="visible")
                    messages = [m for m in inbox.messages if str(m["To"]) == EMAIL]
                    assert messages
                    code = re.search(r"\b[0-9]{6}\b", messages[-1].get_content())
                    assert code
                    guest.locator("#code").fill(code.group())
                    guest.locator("#verification button[type=submit], #verification button:not([type])").first.click()
                    guest.get_by_text("Waiting for approval", exact=True).wait_for(state="visible")
                    STEP = "owner People approval"
                    owner_context = context()
                    owner_context.add_cookies([{"name": "fixture_owner", "value": owner_fixture, "url": website_origin, "secure": True, "httpOnly": True, "sameSite": "Strict"}])
                    owner = owner_context.new_page()
                    owner.goto(website_mount + "/people", wait_until="domcontentloaded")
                    row = owner.locator(f'[data-email="{EMAIL}"]')
                    row.wait_for(state="visible", timeout=15000)
                    row.locator('[data-action="approve"]').click()
                    expect(row).to_have_attribute("data-status", "approved", timeout=15000)
                    STEP = "cross-site guest handoff"
                    guest.bring_to_front()
                    guest.locator("#connect").wait_for(state="visible", timeout=15000)
                    with guest.expect_response(lambda response: response.url == guest_origin + "/auth/start") as started_response:
                        guest.locator("#connect").click()
                    assert started_response.value.status == 303
                    guest.wait_for_url(website_mount + "/guest", timeout=15000)
                    expect(guest.locator("#status")).to_contain_text("Access approved", timeout=15000)
                    assert "cross-site" in cross_site_visits
                    stranger = context()
                    wrong_browser_status = []
                    def check_browser_binding(route):
                        wrong_browser_status.append(stranger.request.get(route.request.url, max_redirects=0).status)
                        route.continue_()
                    guest.route(guest_origin + "/auth/callback?*", check_browser_binding)
                    with guest.expect_response(lambda response: response.url.startswith(guest_origin + "/auth/callback?")) as callback_response:
                        guest.locator("#connect").click()
                    assert wrong_browser_status == [403]
                    callback_url = callback_response.value.url
                    assert callback_response.value.status == 303
                    guest.wait_for_url(guest_origin + "/", timeout=15000)
                    guest.locator('[data-light-id="pilot-lamp"]').wait_for(state="visible", timeout=15000)
                    assert not guest.url.endswith("/auth/callback") and "?" not in guest.url
                    STEP = "clean bookmark and one-use binding"
                    bookmark = guest_context.new_page()
                    bookmark.goto(guest_origin + "/", wait_until="domcontentloaded")
                    bookmark.locator('[data-light-id="pilot-lamp"]').wait_for(state="visible", timeout=15000)
                    assert bookmark.url == guest_origin + "/"
                    assert not proxy.pending
                    assert guest.request.get(callback_url, max_redirects=0).status == 403
                    assert stranger.request.get(callback_url, max_redirects=0).status == 403
                    stranger.close()
                    print("PASS: synthetic card, hosted registration, owner approval, cross-site handoff, one-use binding, clean bookmark")
                finally:
                    browser.close()
        finally:
            if guest_thread.is_alive():
                loop.call_soon_threadsafe(loop.stop)
                guest_thread.join(timeout=10)
            website_server.shutdown()
            website_thread.join(timeout=10)
            website_server.server_close()


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(f"FAIL: hosted demo at {STEP} ({type(error).__name__})", file=sys.stderr)
        sys.exit(1)
