"""Exercise the synthetic controls in a mobile Firefox viewport."""

from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
from threading import Thread
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def fixture(base: str, light_id: str, state: str) -> None:
    body = json.dumps({"light_id": light_id, "state": state}).encode()
    request = Request(
        f"{base}/demo/fixtures", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert json.load(response) == {"light_id": light_id, "state": state}


def run() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "boomer_switch.demo", "--port", "0"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        lines: Queue[str] = Queue()
        Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
        try:
            line = lines.get(timeout=10).strip()
        except Empty:
            raise RuntimeError("Synthetic server did not report its address") from None
        match = re.fullmatch(r"Demo ready: (http://localhost:\d+)", line)
        if not match:
            raise RuntimeError(f"Unexpected synthetic server startup: {line!r}")
        base = match.group(1)

        with sync_playwright() as playwright:
            browser = playwright.firefox.launch()
            try:
                page = browser.new_page(viewport={"width": 375, "height": 667}, device_scale_factor=1)
                page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
                page.goto(base, wait_until="networkidle")
                page.get_by_text("Living room", exact=True).wait_for()
                page.get_by_text("Bedroom", exact=True).wait_for()
                for light_id, name in (("sofa-lamp", "Sofa lamp"), ("reading-lamp", "Reading lamp"), ("bedside-lamp", "Bedside lamp")):
                    card = page.locator(f'[data-light-id="{light_id}"]')
                    assert card.count() == 1
                    assert card.get_by_text(name, exact=True).is_visible()
                    for action in ("on", "off"):
                        button = card.locator(f'[data-action="{action}"]')
                        assert button.count() == 1 and button.is_visible()
                        bounds = button.bounding_box()
                        assert bounds and bounds["width"] >= 44 and bounds["height"] >= 44, (light_id, action, bounds)
                        assert 0 <= bounds["x"] and bounds["x"] + bounds["width"] <= 375, (light_id, action, bounds)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

                card = page.locator('[data-light-id="sofa-lamp"]')
                state = card.locator("[data-state]")
                fixture(base, "sofa-lamp", "off")
                page.wait_for_function("el => el.dataset.state === 'off' && el.textContent.trim() === 'Off'", arg=state.element_handle(), timeout=10000)
                with page.expect_response(lambda response: response.url == f"{base}/api/lights/sofa-lamp" and response.request.method == "POST") as result:
                    card.locator('[data-action="on"]').click()
                assert result.value.json() == {"light_id": "sofa-lamp", "state": "on"}
                page.wait_for_function("el => el.dataset.state === 'on' && el.textContent.trim() === 'On'", arg=state.element_handle(), timeout=5000)
                with page.expect_response(lambda response: response.url == f"{base}/api/lights/sofa-lamp" and response.request.method == "POST") as result:
                    card.locator('[data-action="off"]').click()
                assert result.value.json() == {"light_id": "sofa-lamp", "state": "off"}
                page.wait_for_function("el => el.dataset.state === 'off' && el.textContent.trim() === 'Off'", arg=state.element_handle(), timeout=5000)
                fixture(base, "sofa-lamp", "on")
                page.wait_for_function("el => el.dataset.state === 'on' && el.textContent.trim() === 'On'", arg=state.element_handle(), timeout=10000)
                fixture(base, "sofa-lamp", "unavailable")
                page.wait_for_function("el => el.dataset.state === 'unavailable' && el.textContent.trim() === 'Unavailable'", arg=state.element_handle(), timeout=10000)
                if os.environ.get("BOOMER_DEMO_SCREENSHOT"):
                    page.screenshot(path=os.environ["BOOMER_DEMO_SCREENSHOT"], full_page=True)
                print("PASS: mobile rooms, controls, confirmed commands, external change, and Unavailable")
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
    run()
