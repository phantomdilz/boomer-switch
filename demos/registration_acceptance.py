"""Exercise the local registration demo in a mobile Firefox viewport."""

from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread

from PIL import Image
from playwright.sync_api import sync_playwright
import zxingcpp


ROOT = Path(__file__).resolve().parents[1]
EMAIL = "sample@example.invalid"
STEP = "startup"


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
    global STEP
    scratch = ROOT / ".test-runs"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="registration-demo-", dir=scratch) as directory:
        state = Path(directory)
        process = subprocess.Popen(
            [sys.executable, "-m", "boomer_switch.registration_demo", "serve",
             "--state-dir", str(state), "--port", "0"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            lines: Queue[str] = Queue()
            Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
            try:
                line = lines.get(timeout=10).strip()
            except Empty:
                raise RuntimeError("Synthetic server did not start") from None
            match = re.fullmatch(r"Registration demo ready: (http://localhost:\d+)", line)
            if not match:
                raise RuntimeError("Unexpected synthetic server startup")
            base = match.group(1)
            STEP = "card"
            card = operator(state, "card", "--base-url", base)
            invite_url, short_url = card["invite_url"], card["short_url"]
            assert invite_url.startswith(base + "/invite/")
            assert short_url.startswith(base + "/s/")
            card_path = Path(card["printable_card"]).resolve()
            assert card_path.is_relative_to(state.resolve())
            markup = card_path.read_text(encoding="utf-8")
            assert "<svg" in markup and short_url in markup

            with sync_playwright() as playwright:
                browser = playwright.firefox.launch()
                try:
                    STEP = "printed QR"
                    card_page = browser.new_page(viewport={"width": 375, "height": 667})
                    card_page.route("**/*", lambda route: route.abort())
                    card_page.set_content(markup, wait_until="load")
                    qr = card_page.locator("svg")
                    assert qr.count() == 1 and qr.is_visible()
                    decoded = zxingcpp.read_barcode(Image.open(BytesIO(qr.screenshot())))
                    assert decoded is not None and decoded.text == invite_url
                    card_page.close()
                    context = browser.new_context(viewport={"width": 375, "height": 667})
                    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
                    page = context.new_page()
                    STEP = "anonymous access"
                    assert page.request.get(base + "/api/lights").status == 403
                    assert page.request.post(base + "/api/lights/sofa-lamp", data={"action": "on"}).status == 403
                    for path in ("/demo/mail", "/demo/approve", "/api/mail"):
                        assert page.request.get(base + path).status == 404

                    STEP = "invitation link"
                    page.goto(invite_url, wait_until="domcontentloaded")
                    page.wait_for_url(base + "/guest")
                    page.locator("#name").wait_for(state="visible")
                    context.close()

                    STEP = "short link and registration"
                    context = browser.new_context(viewport={"width": 375, "height": 667})
                    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
                    page = context.new_page()
                    page.goto(short_url, wait_until="domcontentloaded")
                    page.wait_for_url(base + "/guest")
                    page.locator("#name").fill("Sample Guest")
                    page.locator("#email").fill(EMAIL)
                    page.locator("#send-code").click()
                    page.locator("#code").wait_for(state="visible")
                    STEP = "fake email verification"
                    mail = operator(state, "mail", "--email", EMAIL)
                    codes = [message["code"] for message in mail["messages"] if message["kind"] == "code"]
                    assert codes
                    page.locator("#code").fill(codes[-1])
                    page.locator("#verify-code").click()
                    page.get_by_text("Waiting for approval", exact=True).wait_for(state="visible")
                    assert page.url == base + "/guest"
                    STEP = "pending access"
                    assert page.request.get(base + "/api/lights").status == 403
                    assert page.request.post(base + "/api/lights/sofa-lamp", data={"action": "on"}).status == 403
                    assert page.locator('[data-light-id="sofa-lamp"]').count() == 0
                    STEP = "owner approval"
                    operator(state, "approve", "--email", EMAIL)
                    page.locator('[data-light-id="sofa-lamp"]').wait_for(state="visible", timeout=15000)
                    assert page.url == base + "/guest"
                    assert page.locator('[data-light-id="sofa-lamp"] [data-action="on"]').is_visible()
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    STEP = "clean bookmark"
                    page.reload(wait_until="domcontentloaded")
                    page.locator('[data-light-id="sofa-lamp"]').wait_for(state="visible", timeout=15000)
                    assert page.url == base + "/guest"
                    print("PASS: synthetic invitation, verification, approval, and clean bookmark")
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
    except Exception:
        # Browser and operator errors can contain invitation URLs or codes.
        print(f"FAIL: registration demo at {STEP}", file=sys.stderr)
        sys.exit(1)
