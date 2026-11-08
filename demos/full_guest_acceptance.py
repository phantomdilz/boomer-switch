"""Exercise the complete isolated guest journey with a private card template."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread

from PIL import Image
from playwright.sync_api import expect, sync_playwright
from pypdf import PdfReader
import zxingcpp


ROOT = Path(__file__).resolve().parents[1]
EMAIL = "guest@example.invalid"
STEP = "startup"
HTTP_STATUS = None


def command(*args: str) -> dict:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError("Synthetic command failed")
    return json.loads(result.stdout)


def run(template: Path) -> None:
    global STEP, HTTP_STATUS
    scratch = ROOT / ".test-runs"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="full-guest-demo-", dir=scratch) as directory:
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
            operator = [sys.executable, "-m", "boomer_switch.registration_demo", "operator", "--state-dir", str(state)]
            STEP = "fill private card"
            card = command(*operator, "card", "--base-url", guest_url, "--pdf-template", str(template))
            invite_url, short_url = card["invite_url"], card["short_url"]
            assert invite_url.startswith(guest_url + "/guest#invitation=")
            assert short_url.startswith(guest_url + "/s/")
            pdf = Path(card["printable_pdf"]).resolve()
            assert pdf.is_relative_to(state.resolve())
            STEP = "decode both printed layouts"
            reader = PdfReader(pdf)
            assert len(reader.pages) == 2
            typed_links = []
            short_prefix = guest_url + "/s/"
            alias = short_url[len(short_prefix):]
            for page in reader.pages:
                text = page.extract_text()
                matches = list(re.finditer(re.escape(short_prefix) + r"\s*" + re.escape(alias), text))
                assert len(matches) == 1
                typed_links.append("".join(matches[0].group().split()))
            assert typed_links[0] == typed_links[1] == short_url
            rendered = state / "card-page"
            result = subprocess.run(["pdftoppm", "-png", "-r", "144", str(pdf), str(rendered)],
                                    cwd=ROOT, capture_output=True, timeout=30, check=False)
            if result.returncode:
                raise RuntimeError("Card rendering failed")
            decoded = []
            for number in (1, 2):
                with Image.open(state / f"card-page-{number}.png") as image:
                    codes = zxingcpp.read_barcodes(image)
                assert len(codes) == 1
                assert codes[0].text == invite_url
                decoded.append(codes[0].text)
            assert decoded[0] == decoded[1] == invite_url
            STEP = "owner sign-in fixture"
            login_url = command(sys.executable, "-m", "boomer_switch.people_demo", "sign-in",
                                "--state-dir", str(state), "--owner-url", owner_url)["login_url"]
            assert login_url.startswith(owner_url + "/")

            with sync_playwright() as playwright:
                browser = playwright.firefox.launch()
                try:
                    def context_for(origin):
                        context = browser.new_context(viewport={"width": 375, "height": 667})
                        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
                        return context

                    STEP = "independent browser forbidden"
                    stranger_context = context_for(guest_url)
                    stranger = stranger_context.new_page()
                    assert stranger.request.get(guest_url + "/api/lights").status == 403
                    assert stranger.request.post(guest_url + "/api/lights/sofa-lamp", data={"action": "on"}).status == 403
                    STEP = "printed short link"
                    short_context = context_for(guest_url)
                    short_page = short_context.new_page()
                    short_page.goto(typed_links[1], wait_until="domcontentloaded")
                    short_page.wait_for_url(guest_url + "/guest")
                    short_page.locator("#name").wait_for(state="visible")
                    short_context.close()
                    STEP = "guest registers from decoded QR"
                    guest_context = context_for(guest_url)
                    guest = guest_context.new_page()
                    guest.goto(decoded[0], wait_until="domcontentloaded")
                    guest.wait_for_url(guest_url + "/guest")
                    guest.locator("#name").fill("Sample Guest")
                    guest.locator("#email").fill(EMAIL)
                    guest.locator("#send-code").click()
                    guest.locator("#code").wait_for(state="visible")
                    messages = command(*operator, "mail", "--email", EMAIL)["messages"]
                    codes = [message["code"] for message in messages if message["kind"] == "code"]
                    assert codes
                    guest.locator("#code").fill(codes[-1])
                    guest.locator("#verify-code").click()
                    guest.get_by_text("Waiting for approval", exact=True).wait_for(state="visible")
                    STEP = "owner approval"
                    owner_context = context_for(owner_url)
                    owner = owner_context.new_page()
                    owner.goto(login_url, wait_until="domcontentloaded")
                    owner.wait_for_url(owner_url + "/people")
                    row = owner.locator(f'[data-email="{EMAIL}"]')
                    row.wait_for(state="visible", timeout=15000)
                    expect(row).to_have_attribute("data-status", "pending")
                    row.locator('[data-action="approve"]').click()
                    expect(row).to_have_attribute("data-status", "approved", timeout=15000)
                    guest.bring_to_front()
                    lamp = guest.locator('[data-light-id="sofa-lamp"]')
                    lamp.wait_for(state="visible", timeout=15000)
                    assert guest.get_by_text("Living room", exact=True).is_visible()
                    assert lamp.get_by_text("Sofa lamp", exact=True).is_visible()
                    STEP = "independent browser still forbidden"
                    assert stranger.request.get(guest_url + "/api/lights").status == 403
                    assert stranger.request.post(guest_url + "/api/lights/sofa-lamp", data={"action": "on"}).status == 403
                    STEP = "sample lamp on and off"
                    for action in ("on", "off"):
                        with guest.expect_response(lambda response: response.url == guest_url + "/api/lights/sofa-lamp" and response.request.method == "POST") as response:
                            lamp.locator(f'[data-action="{action}"]').click()
                        HTTP_STATUS = response.value.status
                        assert HTTP_STATUS == 200
                        assert response.value.json() == {"light_id": "sofa-lamp", "state": action}
                        expect(lamp.locator("[data-state]")).to_have_attribute("data-state", action)
                    STEP = "clean bookmark reopened"
                    HTTP_STATUS = None
                    assert guest.url == guest_url + "/guest"
                    bookmark = guest_context.new_page()
                    bookmark.goto(guest_url + "/guest", wait_until="domcontentloaded")
                    bookmark.locator('[data-light-id="sofa-lamp"]').wait_for(state="visible", timeout=15000)
                    assert bookmark.url == guest_url + "/guest"
                    STEP = "owner revokes open guest"
                    HTTP_STATUS = None
                    with owner.expect_response(lambda response: response.url == owner_url + "/api/people" and response.request.method == "POST") as response:
                        row.locator('[data-action="remove"]').click()
                    HTTP_STATUS = response.value.status
                    assert HTTP_STATUS == 200
                    STEP = "revoked guest command"
                    HTTP_STATUS = None
                    HTTP_STATUS = bookmark.evaluate("""async () => (await fetch('/api/lights/sofa-lamp', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({action: 'on'})
                    })).status""")
                    assert HTTP_STATUS == 403
                    STEP = "revoked controls hidden"
                    HTTP_STATUS = None
                    bookmark.bring_to_front()
                    expect(bookmark.locator("#controls")).to_be_hidden(timeout=15000)
                    print("PASS: both cards, registration, owner approval, sample lamp, isolation, bookmark, and revocation")
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
    parser = argparse.ArgumentParser(description="Run the isolated full guest demo.")
    parser.add_argument("--pdf-template", type=Path, required=True)
    options = parser.parse_args()
    try:
        run(options.pdf_template.resolve(strict=True))
    except Exception as error:
        status = f", HTTP {HTTP_STATUS}" if type(HTTP_STATUS) is int else ""
        print(f"FAIL: full guest demo at {STEP} ({type(error).__name__}{status})", file=sys.stderr)
        sys.exit(1)
