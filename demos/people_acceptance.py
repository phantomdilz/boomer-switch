"""Exercise the isolated People demo in mobile Firefox."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
STEP = "startup"
HTTP_STATUS = None


def operator(state: Path, *args: str) -> object:
    result = subprocess.run(
        [sys.executable, "-m", "boomer_switch.registration_demo", "operator",
         "--state-dir", str(state), *args],
        cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode:
        raise RuntimeError("Synthetic operator command failed")
    return json.loads(result.stdout)


def run() -> None:
    global STEP, HTTP_STATUS
    scratch = ROOT / ".test-runs"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="people-demo-", dir=scratch) as directory:
        state = Path(directory)
        process = subprocess.Popen(
            [sys.executable, "-m", "boomer_switch.people_demo", "serve",
             "--state-dir", str(state), "--guest-port", "0", "--owner-port", "0"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            lines: Queue[str] = Queue()
            Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
            try:
                line = lines.get(timeout=10).strip()
            except Empty:
                raise RuntimeError("Synthetic server did not start") from None
            prefix = "People demo ready: "
            if not line.startswith(prefix):
                raise RuntimeError("Unexpected synthetic server startup")
            addresses = json.loads(line[len(prefix):])
            guest_url, owner_url = addresses["guest_url"], addresses["owner_url"]
            assert re.fullmatch(r"http://localhost:\d+", guest_url)
            assert re.fullmatch(r"http://localhost:\d+/house-lights", owner_url)
            STEP = "owner sign-in fixture"
            result = subprocess.run(
                [sys.executable, "-m", "boomer_switch.people_demo", "sign-in",
                 "--state-dir", str(state), "--owner-url", owner_url],
                cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode:
                raise RuntimeError("Synthetic sign-in fixture failed")
            login_url = json.loads(result.stdout)["login_url"]
            assert login_url.startswith(owner_url + "/")
            invitation = operator(state, "card", "--base-url", guest_url)["invite_url"]
            assert invitation.startswith(guest_url + "/invite/")

            with sync_playwright() as playwright:
                browser = playwright.firefox.launch()
                try:
                    def context_for(origin):
                        context = browser.new_context(viewport={"width": 375, "height": 667})
                        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
                        return context

                    def register(email, name, invite):
                        context = context_for(guest_url)
                        page = context.new_page()
                        page.goto(invitation if invite else guest_url + "/guest", wait_until="domcontentloaded")
                        page.wait_for_url(guest_url + "/guest")
                        page.locator("#name").fill(name)
                        page.locator("#email").fill(email)
                        page.locator("#send-code").click()
                        page.locator("#code").wait_for(state="visible")
                        messages = operator(state, "mail", "--email", email)["messages"]
                        codes = [message["code"] for message in messages if message["kind"] == "code"]
                        assert codes
                        page.locator("#code").fill(codes[-1])
                        page.locator("#verify-code").click()
                        return context, page

                    STEP = "owner sign-in"
                    owner_context = context_for(owner_url)
                    owner = owner_context.new_page()
                    assert owner.request.get(owner_url + "/people").status == 403
                    assert owner.request.get(owner_url + "/api/people").status == 403
                    owner.goto(login_url, wait_until="domcontentloaded")
                    owner.wait_for_url(owner_url + "/people")
                    owner.locator("#add-email").wait_for(state="visible")
                    assert owner.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

                    STEP = "pending approve"
                    approved_email = "approved@example.invalid"
                    approved_context, approved = register(approved_email, "Sample Approved", True)
                    approved.get_by_text("Waiting for approval", exact=True).wait_for(state="visible")
                    for path in ("/people", "/api/people", "/people.js"):
                        assert approved.request.get(guest_url + path).status == 404
                    assert approved.request.post(guest_url + "/api/people", data={"action": "approve"}).status == 404
                    owner.locator("#refresh-people").click()
                    row = owner.locator(f'[data-email="{approved_email}"]')
                    row.wait_for(state="visible", timeout=15000)
                    expect(row).to_have_attribute("data-status", "pending")
                    row.locator('[data-action="approve"]').click()
                    expect(row).to_have_attribute("data-status", "approved", timeout=15000)
                    approved.locator('[data-light-id="sofa-lamp"]').wait_for(state="visible", timeout=15000)

                    STEP = "pending deny"
                    denied_email = "denied@example.invalid"
                    denied_context, denied = register(denied_email, "Sample Denied", True)
                    denied.get_by_text("Waiting for approval", exact=True).wait_for(state="visible")
                    owner.locator("#refresh-people").click()
                    denied_row = owner.locator(f'[data-email="{denied_email}"]')
                    denied_row.wait_for(state="visible", timeout=15000)
                    denied_row.locator('[data-action="deny"]').click()
                    denied_row.wait_for(state="detached", timeout=15000)
                    assert denied.request.get(guest_url + "/api/lights").status == 403
                    assert denied.locator('[data-light-id="sofa-lamp"]').count() == 0

                    STEP = "manual preapproval"
                    pre_email = "preapproved@example.invalid"
                    owner.locator("#add-email").fill(pre_email)
                    owner.locator("#add-person").click()
                    pre_row = owner.locator(f'[data-email="{pre_email}"]')
                    pre_row.wait_for(state="visible", timeout=15000)
                    expect(pre_row.locator("[data-access]")).to_have_text("Awaiting email proof")
                    pre_context, pre_guest = register(pre_email, "Sample Preapproved", False)
                    pre_guest.locator('[data-light-id="sofa-lamp"]').wait_for(state="visible", timeout=15000)
                    owner.locator("#refresh-people").click()
                    expect(pre_row.locator("[data-access]")).to_have_text("Approved", timeout=15000)

                    STEP = "set, change and clear end date"
                    first = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
                    second = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
                    date = pre_row.locator("[data-end-date]")
                    for value in (first, second):
                        date.fill(value)
                        with owner.expect_response(lambda response: response.url == owner_url + "/api/people" and response.request.method == "POST") as saved:
                            pre_row.locator('[data-action="save-expiry"]').click()
                        assert saved.value.status == 200
                        owner.reload(wait_until="domcontentloaded")
                        expect(date).to_have_value(value, timeout=15000)
                    with owner.expect_response(lambda response: response.url == owner_url + "/api/people" and response.request.method == "POST") as saved:
                        pre_row.locator('[data-action="clear-expiry"]').click()
                    assert saved.value.status == 200
                    owner.reload(wait_until="domcontentloaded")
                    expect(date).to_have_value("", timeout=15000)

                    STEP = "remove: guest initially authorized"
                    HTTP_STATUS = None
                    approved.bring_to_front()
                    HTTP_STATUS = approved.evaluate("async () => (await fetch('/api/lights', {cache: 'no-store'})).status")
                    assert HTTP_STATUS == 200
                    STEP = "remove: owner request"
                    HTTP_STATUS = None
                    with owner.expect_response(lambda response: response.url == owner_url + "/api/people" and response.request.method == "POST") as saved:
                        row.locator('[data-action="remove"]').click()
                    HTTP_STATUS = saved.value.status
                    assert HTTP_STATUS == 200
                    STEP = "remove: guest command"
                    HTTP_STATUS = None
                    HTTP_STATUS = approved.evaluate("""async () => (await fetch('/api/lights/sofa-lamp', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({action: 'on'})
                    })).status""")
                    assert HTTP_STATUS == 403
                    STEP = "remove: controls hidden"
                    HTTP_STATUS = None
                    approved.bring_to_front()
                    expect(approved.locator("#controls")).to_be_hidden(timeout=15000)
                    row.wait_for(state="detached", timeout=15000)

                    STEP = "expiry: guest initially authorized"
                    HTTP_STATUS = None
                    pre_guest.bring_to_front()
                    HTTP_STATUS = pre_guest.evaluate("async () => (await fetch('/api/lights', {cache: 'no-store'})).status")
                    assert HTTP_STATUS == 200
                    STEP = "expiry: owner request"
                    HTTP_STATUS = None
                    date.fill("2000-01-01")
                    with owner.expect_response(lambda response: response.url == owner_url + "/api/people" and response.request.method == "POST") as saved:
                        pre_row.locator('[data-action="save-expiry"]').click()
                    HTTP_STATUS = saved.value.status
                    assert HTTP_STATUS == 200
                    STEP = "expiry: guest command"
                    HTTP_STATUS = None
                    HTTP_STATUS = pre_guest.evaluate("""async () => (await fetch('/api/lights/sofa-lamp', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({action: 'on'})
                    })).status""")
                    assert HTTP_STATUS == 403
                    STEP = "expiry: controls hidden"
                    HTTP_STATUS = None
                    pre_guest.bring_to_front()
                    expect(pre_guest.locator("#controls")).to_be_hidden(timeout=15000)
                    print("PASS: synthetic People approval, denial, preapproval, dates, removal and expiry; removal and expiry each: initial GET 200, owner POST 200, guest POST 403, controls hidden")
                finally:
                    browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        # Browser and operator errors can contain invitation URLs or codes.
        status = f", HTTP {HTTP_STATUS}" if type(HTTP_STATUS) is int else ""
        print(f"FAIL: People demo at {STEP} ({type(error).__name__}{status})", file=sys.stderr)
        sys.exit(1)
