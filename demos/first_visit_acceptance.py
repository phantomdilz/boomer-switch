"""Exercise first visits from a local link launcher in isolated Firefox."""

from __future__ import annotations

from html import escape
import json
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import types
from urllib.parse import urlsplit
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from wsgiref.simple_server import make_server

from PIL import Image
from playwright.sync_api import expect, sync_playwright
from reportlab.pdfgen.canvas import Canvas
import zxingcpp

from boomer_switch.registration import RegistrationStore
from boomer_switch.registration_demo import RegistrationDemoServer, printable_card
from boomer_switch.website_card import render_hosted_card
from boomer_switch.website import WebsiteApplication
from boomer_switch.website_mail import WebsiteMail
from hosted_website_acceptance import FixtureServer, Inbox, LOOPBACK, QuietHandler, make_certificate


ROOT = Path(__file__).resolve().parents[1]
STEP = "startup"


def check(condition, label, detail=""):
    global STEP
    STEP = label
    print(f"CHECK: {label}{': ' + detail if detail else ''}", flush=True)
    if not condition:
        raise AssertionError


def card_qr(root, kind, store, origin, legacy, short, expected):
    template = root / f"{kind}-template.pdf"
    canvas = Canvas(str(template), pagesize=(612, 792))
    for number in (1, 2):
        canvas.drawString(60, 740, f"Synthetic card {number}")
        canvas.showPage()
    canvas.save()
    if kind == "demo":
        card = printable_card(store, root / "demo", origin, pdf_template=template)
        check(card["invite_url"] == expected and card["short_url"] == short, "demo card URLs")
        pdf = Path(card["printable_pdf"])
    else:
        pdf = render_hosted_card(template, root / "hosted-card.pdf", {
            "verified": True, "invite_url": legacy, "short_url": short,
        })
    rendered = root / f"{kind}-qr"
    result = subprocess.run(["pdftoppm", "-png", "-r", "144", "-f", "1", "-l", "1", "-singlefile", str(pdf), str(rendered)],
                            capture_output=True, timeout=30, check=False)
    check(result.returncode == 0, f"{kind} card rendering", f"exit={result.returncode}")
    with Image.open(rendered.with_suffix(".png")) as image:
        codes = zxingcpp.read_barcodes(image)
    check(len(codes) == 1 and codes[0].text == expected, f"{kind} actual card QR", f"codes={len(codes)}")
    return codes[0].text


def write_launcher(path, address):
    path.write_text('<!doctype html><html lang="en"><title>Sample message</title>'
                    '<a id="open" href="' + escape(address, quote=True) + '">Open invitation</a></html>',
                    encoding="utf-8")


def baseline_diagnostic(browser, root):
    """Observe the committed pre-fix demo without retaining its source."""
    base = "ddd922017da4c1a6b2725187de8c09229624b48e"
    def read(path):
        result = subprocess.run(["git", "show", f"{base}:{path}"], cwd=ROOT,
                                capture_output=True, timeout=10, check=True)
        return result.stdout
    module = types.ModuleType("boomer_switch._first_visit_baseline")
    module.__package__ = "boomer_switch"
    exec(compile(read("boomer_switch/registration_demo.py"), "<baseline>", "exec"), module.__dict__)
    old_html = read("boomer_switch/web/registration.html")
    old_js = read("boomer_switch/web/registration.js")
    class BaselineHandler(module._RegistrationHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/registration.js":
                self._reply(200, old_js, "text/javascript; charset=utf-8")
            elif path == "/guest":
                headers = ()
                if self.server.registration.status(self._session()) == "anonymous":
                    headers = (self._cookie(self.server.registration.browser()),)
                self._reply(200, old_html, "text/html; charset=utf-8", headers=headers)
            else:
                super().do_GET()
    server = module.RegistrationDemoServer(root / "baseline", 0)
    server.RequestHandlerClass = BaselineHandler
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://localhost:{server.server_port}"
    browser_context = None
    try:
        browser_context = browser.new_context(viewport={"width": 375, "height": 667})
        launcher_root = root.as_uri() + "/"
        browser_context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") or route.request.url.startswith(launcher_root) else route.abort())
        page = browser_context.new_page()
        launcher = root / "baseline-launcher.html"
        write_launcher(launcher, origin + "/invite/" + server.registration.invitation()["token"])
        page.goto(launcher.as_uri(), wait_until="domcontentloaded")
        page.locator("#open").click()
        page.wait_for_url(origin + "/guest", timeout=10000)
        page.locator("#name").fill("Sample Guest")
        page.locator("#email").fill("baseline@example.invalid")
        with page.expect_response(lambda response: response.url == origin + "/api/register" and response.request.method == "POST", timeout=5000) as response:
            page.locator("#send-code").click()
        with server.registration._transaction() as db:
            count = db.execute("SELECT COUNT(*) FROM mailbox").fetchone()[0]
        print(f"BASELINE: first Send code HTTP={response.value.status} fake_mail={count}", flush=True)
    except Exception as error:
        print(f"BASELINE: diagnostic unavailable ({type(error).__name__})", flush=True)
    finally:
        if browser_context is not None:
            browser_context.close()
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()


def run():
    global STEP
    scratch = ROOT / ".test-runs"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="first-visit-", dir=scratch) as directory:
        root = Path(directory)
        cert, key = make_certificate(root)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        website_store = RegistrationStore(root / "hosted", mode="production")
        inbox = Inbox()
        mail = WebsiteMail(website_store, inbox, "sender@example.invalid", "owner@example.invalid")
        hosted_server = make_server(LOOPBACK, 0, lambda *_: [], server_class=FixtureServer, handler_class=QuietHandler)
        hosted_server.enable_tls(tls)
        hosted_origin = f"https://{LOOPBACK}:{hosted_server.server_port}"
        hosted_mount = hosted_origin + "/house-lights"
        website = WebsiteApplication(website_store, mail, origin=hosted_origin, mount="/house-lights", owner_identity="sample-owner")
        def app(environ, start_response):
            path = environ.get("PATH_INFO", "")
            if not path.startswith("/house-lights/"):
                start_response("404 Not Found", [("Content-Length", "0")])
                return [b""]
            environ["PATH_INFO"] = path[len("/house-lights"):]
            environ["SCRIPT_NAME"] = "/house-lights"
            environ["wsgi.url_scheme"] = "https"
            return website(environ, start_response)
        hosted_server.set_app(app)
        hosted_thread = Thread(target=hosted_server.serve_forever, daemon=True)
        demo_server = RegistrationDemoServer(root / "demo", 0)
        demo_origin = f"http://localhost:{demo_server.server_port}"
        demo_thread = Thread(target=demo_server.serve_forever, daemon=True)
        hosted_thread.start()
        demo_thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.firefox.launch()
                try:
                    try:
                        baseline_diagnostic(browser, root)
                    except Exception as error:
                        print(f"BASELINE: diagnostic unavailable ({type(error).__name__})", flush=True)
                    for kind, origin, mount, store in (
                        ("hosted", hosted_origin, hosted_mount, website_store),
                        ("demo", demo_origin, demo_origin, demo_server.registration),
                    ):
                        invitation = store.invitation()
                        full = mount + "/guest#invitation=" + invitation["token"]
                        legacy = mount + "/invite/" + invitation["token"]
                        short = mount + "/s/" + invitation["alias"]
                        decoded = card_qr(root, kind, store, origin, legacy, short, full)
                        def context():
                            result = browser.new_context(viewport={"width": 375, "height": 667}, ignore_https_errors=True)
                            launcher_root = root.as_uri() + "/"
                            result.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") or route.request.url.startswith(launcher_root) else route.abort())
                            return result
                        def count_mail():
                            with store._transaction() as db:
                                return db.execute("SELECT COUNT(*) FROM mailbox").fetchone()[0]
                        def open_link(page, address):
                            nonlocal launcher_count
                            launcher_count += 1
                            launcher = root / f"launcher-{launcher_count}.html"
                            write_launcher(launcher, address)
                            page.goto(launcher.as_uri(), wait_until="domcontentloaded")
                            page.locator("#open").click()
                        def first_visit(label, address, fault):
                            global STEP
                            STEP = f"{kind} {label} link"
                            browser_context = context()
                            page = browser_context.new_page()
                            fault_script = r"""(() => {
                                const mode = MODE;
                                const original = window.fetch.bind(window);
                                let gate = null;
                                window.__faultHold = true;
                                window.__faultStarted = false;
                                window.__registerLost = false;
                                window.__releaseFault = () => { window.__faultHold = false; if (gate) gate(); };
                                window.fetch = async (input, options) => {
                                    const pathname = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
                                    if (mode === 'delay' && pathname.endsWith('/api/access') && window.__faultHold) {
                                        window.__faultStarted = true;
                                        await new Promise(resolve => { gate = resolve; });
                                    }
                                    if ((mode === 'access' || (mode === 'register' && window.__registerLost)) && pathname.endsWith('/api/access') && window.__faultHold) {
                                        window.__faultStarted = true;
                                        throw new TypeError('Synthetic temporary transport failure');
                                    }
                                    if (mode === 'invite' && pathname.endsWith('/api/invite') && window.__faultHold) {
                                        const response = await original(input, options);
                                        window.__faultStarted = true;
                                        throw new TypeError('Synthetic lost invitation response');
                                    }
                                    if (mode === 'register' && pathname.endsWith('/api/register') && !window.__registerLost) {
                                        const response = await original(input, options);
                                        window.__registerLost = true;
                                        throw new TypeError('Synthetic lost registration response');
                                    }
                                    return original(input, options);
                                };
                            })();""".replace("MODE", json.dumps(fault))
                            page.add_init_script(script=fault_script)
                            register_requests = []
                            page.on("request", lambda request: register_requests.append(1) if request.method == "POST" and request.url == mount + "/api/register" else None)
                            before_fault = count_mail()
                            open_link(page, address)
                            page.wait_for_url(mount + "/guest", timeout=15000)
                            if fault == "delay":
                                STEP = f"{kind} delayed bootstrap fast tap"
                                page.wait_for_function("window.__faultStarted === true", timeout=15000)
                                check(page.locator("#send-code").evaluate("element => element.matches(':disabled')"), f"{kind} bootstrap disables Send code")
                                submitted = page.locator("#details").evaluate("""form => {
                                    let calls = 0;
                                    form.addEventListener('submit', () => { calls += 1; }, {once: true});
                                    form.requestSubmit();
                                    return calls;
                                }""")
                                check(submitted == 1, f"{kind} fast submit reached guard", f"events={submitted}")
                                check(len(register_requests) == 0 and count_mail() == before_fault, f"{kind} fast tap sends nothing", f"posts={len(register_requests)}")
                                page.evaluate("window.__releaseFault()")
                            elif fault in ("access", "invite"):
                                STEP = f"{kind} transient {fault} recovery"
                                page.locator("#retry-access").wait_for(state="visible", timeout=15000)
                                check(len(register_requests) == 0 and count_mail() == before_fault, f"{kind} {fault} failure sends nothing", f"posts={len(register_requests)}")
                                page.evaluate("window.__releaseFault()")
                                page.locator("#retry-access").click()
                            expect(page.locator("#name")).to_be_visible(timeout=15000)
                            email = f"{kind}-{label}@example.invalid"
                            page.locator("#name").fill("Sample Guest")
                            page.locator("#email").fill(email)
                            send = page.locator("#send-code") if kind == "demo" else page.locator("#details button").first
                            expect(send).to_be_enabled(timeout=15000)
                            before = count_mail()
                            STEP = f"{kind} {label} first Send code"
                            with page.expect_response(lambda response: response.url == mount + "/api/register" and response.request.method == "POST") as registered:
                                send.click()
                            check(registered.value.status == 200, STEP, f"HTTP={registered.value.status}")
                            if fault == "register":
                                STEP = f"{kind} lost registration response recovery"
                                page.locator("#retry-access").wait_for(state="visible", timeout=15000)
                                page.evaluate("window.__releaseFault()")
                                page.locator("#retry-access").click()
                            page.locator("#code").wait_for(state="visible", timeout=15000)
                            check(len(register_requests) == 1, f"{kind} {label} one explicit registration", f"posts={len(register_requests)}")
                            if kind == "hosted":
                                received = [m for m in inbox.messages if str(m["To"]) == email]
                                check(len(received) == 1, f"{kind} {label} fake code", f"messages={len(received)}")
                                code = re.search(r"\b[0-9]{6}\b", received[0].get_content())
                                check(code is not None, f"{kind} {label} code present")
                                value = code.group()
                            else:
                                received = [m for m in store.mailbox(email)["messages"] if m["kind"] == "code"]
                                check(len(received) == 1, f"{kind} {label} fake code", f"messages={len(received)}")
                                value = received[0]["code"]
                            check(count_mail() == before + 1, f"{kind} {label} one code", f"new={count_mail() - before}")
                            page.locator("#code").fill(value)
                            verify = page.locator("#verify-code") if kind == "demo" else page.locator("#verification button:not([type]), #verification button[type=submit]").first
                            verify.click()
                            page.get_by_text("Waiting for approval", exact=True).wait_for(state="visible", timeout=15000)
                            check(len(register_requests) == 1, f"{kind} {label} no registration retry", f"posts={len(register_requests)}")
                            check(page.url == mount + "/guest", f"{kind} {label} clean URL")
                            return browser_context, page, email

                        launcher_count = 0
                        full_context, full_page, full_email = first_visit("full", full, "delay")
                        short_context, _, _ = first_visit("short", short, "access")
                        qr_context, _, _ = first_visit("qr", decoded, "invite")
                        legacy_context, _, _ = first_visit("legacy", legacy, "register")
                        short_context.close()
                        qr_context.close()
                        legacy_context.close()
                        STEP = f"{kind} existing approved session"
                        store.operator("approve", full_email)
                        full_page.goto(mount + "/guest", wait_until="domcontentloaded")
                        status = full_page.locator("#registration-status" if kind == "demo" else "#status")
                        expect(status).to_contain_text("Access approved", timeout=15000)
                        approved_cookie = [c for c in full_context.cookies(mount) if c["name"] == "bs_session"]
                        check(len(approved_cookie) == 1, f"{kind} approved cookie", f"count={len(approved_cookie)}")
                        store.rotate()
                        def rejected_link(label, address):
                            global STEP
                            STEP = f"{kind} {label} rejected"
                            isolated = context()
                            page = isolated.new_page()
                            before = count_mail()
                            with page.expect_response(lambda response: response.status == 403 and ("/invite/" in response.url or "/s/" in response.url or response.url.endswith("/api/invite")), timeout=15000) as denied:
                                open_link(page, address)
                            check(denied.value.status == 403, STEP, "HTTP=403")
                            check(count_mail() == before, f"{kind} {label} no mail", "new=0")
                            isolated.close()
                        rejected_link("invalid", mount + "/guest#invitation=" + ("z" * 43))
                        rejected_link("retired full", full)
                        rejected_link("retired short", short)
                        rejected_link("retired legacy", legacy)
                        STEP = f"{kind} approved session after retired link"
                        before = count_mail()
                        with full_page.expect_response(lambda response: response.status == 403 and ("/invite/" in response.url or response.url.endswith("/api/invite")), timeout=15000) as denied:
                            open_link(full_page, full)
                        check(denied.value.status == 403, STEP, "HTTP=403")
                        check(count_mail() == before, f"{kind} approved retired link no mail", "new=0")
                        full_page.goto(mount + "/guest", wait_until="domcontentloaded")
                        expect(status).to_contain_text("Access approved", timeout=15000)
                        after = [c for c in full_context.cookies(mount) if c["name"] == "bs_session"]
                        check(len(after) == 1 and after[0]["value"] == approved_cookie[0]["value"], f"{kind} approved session preserved", f"cookies={len(after)}")
                        full_context.close()
                    print("PASS: hosted and demo first visits, QR, short/full/legacy links, verification, rejection and approved-session preservation")
                finally:
                    browser.close()
        finally:
            demo_server.shutdown()
            demo_thread.join(timeout=10)
            demo_server.server_close()
            hosted_server.shutdown()
            hosted_thread.join(timeout=10)
            hosted_server.server_close()


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(f"FAIL: first visit at {STEP} ({type(error).__name__})", file=sys.stderr)
        sys.exit(1)
