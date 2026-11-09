"""Check mobile app assets with isolated API fixtures in Firefox."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "boomer_switch" / "web"
ORIGIN = "https://layout.invalid"
LIGHT = {"light_id": "sample-lamp", "name": "Sample lamp with a long descriptive name", "room": "Sample room"}


def geometry(page):
    problems = page.evaluate(r"""() => {
      const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
      const controls = [...document.querySelectorAll('button,input')].filter(visible);
      const errors = [];
      const luminance = color => {
        const rgb = color.match(/[\d.]+/g).slice(0, 3).map(Number).map(v => {
          v /= 255;
          return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
        });
        return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
      };
      for (const [i, a] of controls.entries()) {
        const x = a.getBoundingClientRect();
        if (x.left < -1 || x.right > innerWidth + 1) errors.push('outside viewport: ' + a.outerHTML);
        if (a.tagName === 'BUTTON' && a.scrollWidth > a.clientWidth + 1) errors.push('button text overflow');
        if (a.tagName === 'BUTTON') {
          const walker = document.createTreeWalker(a, NodeFilter.SHOW_TEXT);
          while (walker.nextNode()) {
            const node = walker.currentNode;
            for (const word of node.textContent.matchAll(/\S+/g)) {
              const range = document.createRange();
              range.setStart(node, word.index);
              range.setEnd(node, word.index + word[0].length);
              const lines = new Set([...range.getClientRects()].filter(r => r.width > 0).map(r => Math.round(r.top)));
              if (lines.size > 1) errors.push('button word split across lines: ' + word[0]);
            }
          }
          if (a.disabled) {
            const style = getComputedStyle(a);
            const colors = [luminance(style.color), luminance(style.backgroundColor)].sort((a,b) => b-a);
            if (Number(style.opacity) !== 1 || (colors[0] + .05) / (colors[1] + .05) < 4.5) errors.push('disabled button contrast');
          }
        }
        for (const b of controls.slice(i + 1)) {
          const y = b.getBoundingClientRect();
          if (Math.min(x.right, y.right) - Math.max(x.left, y.left) > 1 &&
              Math.min(x.bottom, y.bottom) - Math.max(x.top, y.top) > 1) errors.push('control overlap');
        }
      }
      if (document.documentElement.scrollWidth > innerWidth + 1) errors.push('horizontal overflow');
      const rows = [...document.querySelectorAll('.person')];
      for (let i=1; i<rows.length; i++) {
        if (rows[i].getBoundingClientRect().top < rows[i-1].getBoundingClientRect().bottom) errors.push('row overlap');
      }
      return errors;
    }""")
    assert not problems, problems


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as playwright:
        browser = playwright.firefox.launch()
        try:
            for width in (320, 375):
                state = {"status": "invited", "home": "source_mismatch"}
                people = [
                    {"email": "pending@example.invalid", "name": "Pending sample guest", "status": "pending", "revision": 1, "expires": None, "email_verified": True},
                    {"email": "long-approved-sample-guest@example.invalid", "name": "Approved sample guest", "status": "approved", "revision": 1, "expires": None, "email_verified": True},
                ]
                context = browser.new_context(viewport={"width": width, "height": 740})
                def route(request):
                    path = urlsplit(request.request.url).path
                    if not request.request.url.startswith(ORIGIN + "/"):
                        request.abort()
                        return
                    name = path.rsplit("/", 1)[-1]
                    if "/api/" not in path and name in ("guest", "registration", "people"):
                        asset = {"guest": "website.html", "registration": "registration.html", "people": "people.html"}[name]
                        request.fulfill(path=str(WEB / asset), content_type="text/html")
                    elif (WEB / name).is_file():
                        request.fulfill(path=str(WEB / name), content_type="text/css" if name.endswith(".css") else "text/javascript")
                    elif "/api/" in path:
                        if path.endswith("/register"):
                            state["status"] = "verify"
                        elif path.endswith("/verify"):
                            state["status"] = "pending"
                        body = {"status": state["status"], "csrf": "synthetic-csrf"}
                        if path.endswith("/people"):
                            body = {"people": people, "csrf": "synthetic-csrf"}
                            if request.request.method == "POST":
                                action = request.request.post_data_json
                                person = next((p for p in people if p["email"] == action["email"]), None)
                                if person and action["action"] == "approve":
                                    person["status"] = "approved"
                                if person and action["action"] == "expiry":
                                    person["expires"] = action["expires"]
                                if person and action["action"] == "remove":
                                    people.remove(person)
                        elif state["status"] == "approved":
                            body.update(home_status=state["home"], home_ready=state["home"] == "ready")
                            if state["home"] == "ready":
                                body["light"] = LIGHT
                        if "/operations/" in path:
                            body = {"status": "confirmed", "state": "off"}
                        if path.endswith("/lights"):
                            body = {"lights": [{**LIGHT, "state": "off"}]}
                        request.fulfill(json=body)
                    else:
                        request.fulfill(status=404)
                context.route("**/*", route)
                page = context.new_page()
                page.goto(ORIGIN + "/host/guest")
                expect(page.locator("#details")).to_be_visible()
                geometry(page)
                page.locator("#name").fill("Sample guest")
                page.locator("#email").fill("guest@example.invalid")
                page.locator("#send-code").click()
                expect(page.locator("#verification")).to_be_visible()
                expect(page.locator("#code-help")).to_contain_text("10 minutes")
                geometry(page)
                page.screenshot(path=str(output / f"guest-verification-{width}.png"), full_page=True)
                page.locator("#code").fill("123456")
                page.locator("#verify-code").click()
                expect(page.locator("#status")).to_have_text("Waiting for approval")
                state["status"] = "approved"
                page.reload()
                alert = page.locator("#connection")
                expect(alert).to_have_attribute("role", "alert")
                expect(alert).to_contain_text("Connect to the home Wi-Fi")
                assert alert.evaluate("e => getComputedStyle(e).color") == "rgb(161, 27, 27)"
                expect(page.locator("#controls")).to_be_hidden()
                geometry(page)
                page.screenshot(path=str(output / f"guest-mismatch-{width}.png"), full_page=True)
                # A second normal access poll keeps a known mismatch explicit.
                page.wait_for_timeout(3200)
                expect(alert).to_have_attribute("role", "alert")
                state["home"] = "unavailable"
                expect(alert).to_contain_text("home connection is unavailable", timeout=6000)
                expect(alert).to_have_attribute("role", "status")
                state["home"] = "ready"
                expect(page.locator("#light-on")).to_be_enabled(timeout=10000)
                expect(alert).not_to_contain_text("Error:")
                expect(alert).not_to_contain_text("Wi-Fi")
                page.locator("#refresh-light").click()
                expect(page.locator("#light-on")).to_be_disabled()
                geometry(page)
                page.screenshot(path=str(output / f"guest-controls-disabled-{width}.png"), full_page=True)
                expect(page.locator("#light-on")).to_be_enabled(timeout=10000)
                geometry(page)
                page.screenshot(path=str(output / f"guest-controls-{width}.png"), full_page=True)
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                geometry(page)
                page.screenshot(path=str(output / f"guest-controls-large-text-{width}.png"), full_page=True)
                page.locator("#refresh-light").click()
                expect(page.locator("#light-on")).to_be_disabled()
                geometry(page)
                page.screenshot(path=str(output / f"guest-controls-large-text-disabled-{width}.png"), full_page=True)
                expect(page.locator("#light-on")).to_be_enabled(timeout=10000)
                page.evaluate("document.documentElement.style.fontSize = ''")
                state["status"] = "pending"
                page.reload()
                expect(page.locator("#status")).to_have_text("Waiting for approval")
                expect(alert).to_be_hidden()
                page.goto(ORIGIN + "/people")
                expect(page.locator(".person")).to_have_count(2)
                geometry(page)
                page.locator('[data-action="approve"]').click()
                expect(page.locator('[data-action="save-expiry"]')).to_have_count(2)
                date = page.locator('[data-end-date]').first
                date.fill("2099-01-01")
                page.locator('[data-action="save-expiry"]').first.click()
                expect(page.locator("#people-status")).to_have_text("People updated.")
                geometry(page)
                page.screenshot(path=str(output / f"people-{width}.png"), full_page=True)
                state["status"] = "invited"
                page.goto(ORIGIN + "/registration")
                expect(page.locator("#details")).to_be_visible()
                geometry(page)
                page.locator("#name").fill("Sample guest")
                page.locator("#email").fill("guest@example.invalid")
                page.locator("#send-code").click()
                expect(page.locator("#verification")).to_be_visible()
                geometry(page)
                page.screenshot(path=str(output / f"registration-{width}.png"), full_page=True)
                state["status"] = "approved"
                page.reload()
                expect(page.locator(".light")).to_have_count(1)
                geometry(page)
                page.screenshot(path=str(output / f"sample-controls-{width}.png"), full_page=True)
                page.locator('[data-action="on"]').click()
                geometry(page)
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                geometry(page)
                page.screenshot(path=str(output / f"sample-controls-large-text-{width}.png"), full_page=True)
                results.append({"width": width, "result": "passed"})
                context.close()
        finally:
            browser.close()
    (output / "result.json").write_text(json.dumps(results, indent=2))
    print("PASS: mobile registration, People, controls, mismatch alert, unavailable state, and polling recovery")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".test-runs" / "mobile-layout")
    run(parser.parse_args().output)
