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
from socketserver import ThreadingMixIn
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from urllib.parse import urlsplit

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


def check(condition: bool, label: str, detail: str = "") -> None:
    global STEP
    STEP = label
    print(f"CHECK: {label}{': ' + detail if detail else ''}", flush=True)
    if not condition:
        raise AssertionError


class FixtureServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True

    def handle_error(self, _request, _client_address):
        print(f"CHECK: fixture request error ({type(sys.exception()).__name__})", flush=True)

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


class SyntheticWifi:
    def require(self, _peer, _local):
        return __import__("time").time() + 6


class SyntheticRelay:
    def __init__(self, grants):
        self.grants = grants

    async def forward(self, method, path, body, session, deadline):
        if not self.grants.deadline(session):
            return 403, b'{"error":"Access unavailable."}'
        if method != "GET" or path != "/api/lights" or body:
            raise ValueError("Unsupported synthetic relay request")
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
        website_server = make_server(LOOPBACK, 0, lambda *_: [], server_class=FixtureServer, handler_class=QuietHandler)
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
            ready = started.wait(10)
            check(ready and not errors, "fixture startup", f"ready={ready} errors={len(errors)}")
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
            check(raster.returncode == 0, "render card", f"exit={raster.returncode}")
            with Image.open(root / "page-1.png") as image:
                qr = zxingcpp.read_barcodes(image)
            check(len(qr) == 1 and qr[0].text == invite_url, "card QR", f"count={len(qr)}")
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
                    check(bool(messages), "fake mailbox", f"messages={len(messages)}")
                    code = re.search(r"\b[0-9]{6}\b", messages[-1].get_content())
                    check(code is not None, "fake verification code", f"present={code is not None}")
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
                    STEP = "owner approval visible"
                    expect(row).to_have_attribute("data-status", "approved", timeout=15000)
                    STEP = "website session before cross-site handoff"
                    guest.bring_to_front()
                    guest.locator("#connect").wait_for(state="visible", timeout=15000)
                    website_cookies = [c for c in guest_context.cookies(website_mount) if c["name"] == "bs_session"]
                    check(len(website_cookies) == 1, "website cookie before handoff", f"count={len(website_cookies)}")
                    website_cookie = website_cookies[0]
                    check(website_cookie["secure"] and website_cookie["httpOnly"] and website_cookie["sameSite"] == "Strict",
                          "website cookie attributes", "secure, HttpOnly and Strict required")
                    check(urlsplit(guest_origin).hostname == "localhost" and urlsplit(website_origin).hostname == LOOPBACK,
                          "distinct fixture sites", "localhost and loopback-address hosts required")
                    STEP = "cross-site navigation from home start"
                    with guest.expect_response(lambda response: response.url.split("#")[0] == website_mount + "/guest") as returned_website:
                        with guest.expect_response(lambda response: response.url == guest_origin + "/auth/start") as started_response:
                            guest.locator("#connect").click()
                    start_status = started_response.value.status
                    check(start_status == 303, "home start response", f"HTTP={start_status}")
                    website_request = returned_website.value.request
                    previous = website_request.redirected_from
                    check(previous is not None and previous.url == guest_origin + "/auth/start",
                          "actual home-to-website navigation", f"redirects={int(previous is not None)}")
                    metadata = cross_site_visits[-1] if cross_site_visits else ""
                    metadata = metadata if metadata in ("same-origin", "same-site", "cross-site", "none") else "absent-or-other"
                    print(f"CHECK: browser fetch metadata: {metadata}", flush=True)
                    guest.wait_for_url(website_mount + "/guest", timeout=15000)
                    STEP = "approved status after cross-site return"
                    expect(guest.locator("#status")).to_contain_text("Access approved", timeout=15000)
                    returned_cookies = [c for c in guest_context.cookies(website_mount) if c["name"] == "bs_session"]
                    check(len(returned_cookies) == 1 and returned_cookies[0]["value"] == website_cookie["value"],
                          "approved website session survived navigation", f"cookies={len(returned_cookies)}")
                    stranger = context()
                    wrong_browser_status = []
                    pending_after_wrong = []
                    def check_browser_binding(route):
                        global STEP
                        STEP = "wrong-browser callback before valid browser"
                        wrong_browser_status.append(stranger.request.get(route.request.url, max_redirects=0).status)
                        pending_after_wrong.append(len(proxy.pending))
                        check(wrong_browser_status == [403], STEP, f"HTTP={wrong_browser_status[-1]}")
                        check(pending_after_wrong == [1], "valid browser binding retained", f"pending={pending_after_wrong[-1]}")
                        route.continue_()
                    guest.route(guest_origin + "/auth/callback?*", check_browser_binding)
                    STEP = "valid browser callback"
                    with guest.expect_response(lambda response: response.url.startswith(guest_origin + "/auth/callback?")) as callback_response:
                        guest.locator("#connect").click()
                    check(len(wrong_browser_status) == 1, "wrong-browser check observed", f"attempts={len(wrong_browser_status)}")
                    callback_url = callback_response.value.url
                    callback_status = callback_response.value.status
                    check(callback_status == 303, "valid callback response", f"HTTP={callback_status}")
                    guest.wait_for_url(guest_origin + "/", timeout=15000)
                    guest.locator('[data-light-id="pilot-lamp"]').wait_for(state="visible", timeout=15000)
                    check(guest.url == guest_origin + "/", "home URL is clean")
                    STEP = "reopen clean home bookmark"
                    bookmark = guest_context.new_page()
                    bookmark.goto(guest_origin + "/", wait_until="domcontentloaded")
                    bookmark.locator('[data-light-id="pilot-lamp"]').wait_for(state="visible", timeout=15000)
                    check(bookmark.url == guest_origin + "/", "bookmark URL is clean")
                    check(not proxy.pending, "browser binding consumed", f"pending={len(proxy.pending)}")
                    replay_status = guest.request.get(callback_url, max_redirects=0).status
                    check(replay_status == 403, "callback replay rejected", f"HTTP={replay_status}")
                    second_status = stranger.request.get(callback_url, max_redirects=0).status
                    check(second_status == 403, "other browser replay rejected", f"HTTP={second_status}")
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
