from contextlib import contextmanager
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from threading import Thread

import pytest

from boomer_switch.registration import AccessDenied, RegistrationStore
from boomer_switch.registration_demo import RegistrationDemoServer


EMAIL = "sample@example.invalid"


def code(store, email=EMAIL):
    return [item["code"] for item in store.mailbox(email)["messages"] if item["kind"] == "code"][-1]


def pending(store, *, email=EMAIL):
    browser = store.browser(store.invitation()["token"])
    store.start(browser, "Sample guest", email)
    store.verify(browser, code(store, email))
    return browser


def notices(store):
    return [item for item in store.mailbox(EMAIL)["messages"] if item["kind"] == "owner_notice"]


def test_persistent_invitation_session_challenge_approval_and_clean_reverification(tmp_path):
    store = RegistrationStore(tmp_path)
    invite = store.invitation()
    assert re.fullmatch(r"[a-z0-9]+", invite["alias"])
    assert len(invite["words"].split()) == 4
    assert invite["alias"] == "".join(invite["words"].split())
    assert store.resolve_alias(invite["alias"]) == invite["token"]
    browser = store.browser(invite["token"])
    store.start(browser, "Sample guest", EMAIL)
    store = RegistrationStore(tmp_path)
    assert store.invitation() == invite
    store.verify(browser, code(store))
    assert store.status(browser) == "pending"
    assert len(notices(store)) == 1
    store.operator("approve", EMAIL)
    assert RegistrationStore(tmp_path).status(browser) == "approved"
    store.rotate()
    assert store.status(browser) == "approved"
    phone = store.browser()
    # A second browser verifies the already-approved identity without the old card.
    now = store.clock()
    store.clock = lambda: now + 31
    store.start(phone, "Sample guest", EMAIL)
    store.verify(phone, code(store))
    assert store.status(phone) == "approved"
    assert len(notices(store)) == 1


def test_invalid_and_retired_invitation_never_send_mail(tmp_path):
    store = RegistrationStore(tmp_path)
    with pytest.raises(AccessDenied):
        store.browser("invalid")
    browser = store.browser(store.invitation()["token"])
    store.rotate()
    with pytest.raises(AccessDenied):
        store.start(browser, "Sample guest", EMAIL)
    assert store.mailbox(EMAIL) == {"messages": []}


def test_rotating_invitation_invalidates_unverified_challenges_but_not_pending_guests(tmp_path):
    store = RegistrationStore(tmp_path)
    verified = pending(store)
    second = store.browser(store.invitation()["token"])
    store.start(second, "Another sample", "other@example.invalid")
    value = code(store, "other@example.invalid")
    old_alias = store.invitation()["alias"]
    store.rotate()
    with pytest.raises(AccessDenied):
        store.resolve_alias(old_alias)
    with pytest.raises(AccessDenied):
        store.verify(second, value)
    assert store.status(verified) == "pending"


def test_attempts_survive_restart_and_resend_does_not_reset_them(tmp_path):
    now = [1000.0]
    store = RegistrationStore(tmp_path, clock=lambda: now[0])
    browser = store.browser(store.invitation()["token"])
    store.start(browser, "Sample guest", EMAIL)
    wrong = "000000" if code(store) != "000000" else "111111"
    for _ in range(4):
        with pytest.raises(AccessDenied):
            store.verify(browser, wrong)
    now[0] += 31
    store = RegistrationStore(tmp_path, clock=lambda: now[0])
    store.resend(browser)
    current = code(store)
    wrong = "000000" if current != "000000" else "111111"
    with pytest.raises(AccessDenied):
        store.verify(browser, wrong)
    with pytest.raises(AccessDenied):
        store.verify(browser, current)
    assert notices(store) == []


def test_resends_have_cooldown_total_limit_and_old_codes_expire(tmp_path):
    now = [1000.0]
    store = RegistrationStore(tmp_path, clock=lambda: now[0])
    browser = store.browser(store.invitation()["token"])
    store.start(browser, "Sample guest", EMAIL)
    with pytest.raises(AccessDenied):
        store.resend(browser)
    for _ in range(2):
        now[0] += 31
        store.resend(browser)
    now[0] += 31
    with pytest.raises(AccessDenied):
        store.resend(browser)
    assert len(store.mailbox(EMAIL)["messages"]) == 3
    now[0] += 301
    with pytest.raises(AccessDenied):
        store.verify(browser, code(store))


def test_duplicate_pending_verifications_send_exactly_one_notice(tmp_path):
    now = [1000.0]
    store = RegistrationStore(tmp_path, clock=lambda: now[0])
    first = pending(store)
    with pytest.raises(AccessDenied):
        store.verify(first, code(store))
    with pytest.raises(AccessDenied):
        store.start(first, "Sample guest", EMAIL)
    now[0] += 31
    second = store.browser(store.invitation()["token"])
    store.start(second, "Sample guest", EMAIL)
    store.verify(second, code(store))
    assert store.status(second) == "pending"
    assert len(notices(store)) == 1


@pytest.mark.parametrize("action", ["expire", "revoke"])
def test_current_expiry_or_revocation_blocks_existing_session(tmp_path, action):
    store = RegistrationStore(tmp_path)
    browser = pending(store)
    store.operator("approve", EMAIL)
    store.require_access(browser)
    store.operator(action, EMAIL)
    with pytest.raises(AccessDenied):
        store.require_access(browser)


def test_valid_new_card_allows_expired_guest_to_request_fresh_approval(tmp_path):
    now = [1000.0]
    store = RegistrationStore(tmp_path, clock=lambda: now[0])
    original = pending(store)
    store.operator("approve", EMAIL)
    store.operator("expire", EMAIL)
    assert store.status(original) == "anonymous"
    current = store.browser(store.invitation()["token"], existing=original)
    assert current != original
    now[0] += 31
    store.start(current, "Sample guest", EMAIL)
    store.verify(current, code(store))
    assert store.status(current) == "pending"
    assert len(notices(store)) == 2


@pytest.mark.parametrize("expires", [math.nan, math.inf, -math.inf])
def test_nonfinite_expiration_cannot_become_unlimited_permission(tmp_path, expires):
    store = RegistrationStore(tmp_path)
    browser = pending(store)
    with pytest.raises(ValueError, match="finite"):
        store.operator("approve", EMAIL, expires=expires)
    assert store.status(browser) == "pending"


def test_revocation_invalidates_pending_recovery_code_even_after_reapproval(tmp_path):
    now = [1000.0]
    store = RegistrationStore(tmp_path, clock=lambda: now[0])
    pending(store)
    store.operator("approve", EMAIL)
    phone = store.browser()
    now[0] += 31
    store.start(phone, "Sample guest", EMAIL)
    before_revocation = code(store)
    store.operator("revoke", EMAIL)
    store.operator("approve", EMAIL)
    with pytest.raises(AccessDenied):
        store.verify(phone, before_revocation)


def test_session_and_challenge_material_are_hashed_outside_fake_mailbox(tmp_path):
    store = RegistrationStore(tmp_path)
    raw = store.browser(store.invitation()["token"])
    store.start(raw, "Sample guest", EMAIL)
    plaintext = code(store)
    with store._transaction() as db:
        assert db.execute("SELECT id FROM sessions").fetchone()[0] != raw
        assert db.execute("SELECT digest FROM challenges").fetchone()[0] != plaintext


@contextmanager
def server_at(directory):
    with RegistrationDemoServer(directory) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


def http(server, method, path, payload=None, cookie=None):
    connection = HTTPConnection(server.server_address[0], server.server_port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f"bs_session={cookie}"
    try:
        connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def invite_browser(server):
    status, headers, _ = http(server, "GET", "/invite/" + server.registration.invitation()["token"])
    assert status == 302 and headers["Location"].startswith("/guest#invitation=")
    assert "Set-Cookie" not in headers
    _, headers, _ = http(server, "GET", "/api/access")
    parsed = SimpleCookie()
    parsed.load(headers["Set-Cookie"])
    raw = parsed["bs_session"].value
    _, accepted, _ = http(server, "POST", "/api/invite", {"invitation": server.registration.invitation()["token"]}, raw)
    parsed.load(accepted.get("Set-Cookie", ""))
    return parsed["bs_session"].value


@pytest.mark.smoke
def test_http_vertical_slice_gates_direct_control_routes_and_checks_revoke_after_body(tmp_path):
    with server_at(tmp_path) as server:
        assert http(server, "GET", "/api/lights")[0] == 403
        assert http(server, "POST", "/api/lights/sofa-lamp", {"action": "on"})[0] == 403
        cookie = invite_browser(server)
        assert http(server, "POST", "/api/register", {"name": "Sample guest", "email": EMAIL}, cookie)[0] == 200
        assert http(server, "POST", "/api/verify", {"code": code(server.registration)}, cookie)[0] == 200
        assert http(server, "GET", "/api/lights", cookie=cookie)[0] == 403
        server.registration.operator("approve", EMAIL)
        status, _, body = http(server, "POST", "/api/lights/sofa-lamp", {"action": "on"}, cookie)
        assert status == 200 and json.loads(body)["state"] == "on"

        connection = HTTPConnection(server.server_address[0], server.server_port, timeout=5)
        try:
            payload = b'{"action":"off"}'
            connection.putrequest("POST", "/api/lights/sofa-lamp")
            connection.putheader("Cookie", f"bs_session={cookie}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(payload)))
            connection.endheaders()
            connection.send(payload[:2])
            server.registration.operator("revoke", EMAIL)
            connection.send(payload[2:])
            response = connection.getresponse()
            assert response.status == 403
            response.read()
            assert server._devices["sofa-lamp"].is_on is True
        finally:
            connection.close()


def test_invalid_invite_preserves_approved_bookmark_and_public_routes_do_not_expose_mail(tmp_path):
    with server_at(tmp_path) as server:
        cookie = pending(server.registration)
        server.registration.operator("approve", EMAIL)
        count = len(server.registration.mailbox(EMAIL)["messages"])
        status, headers, body = http(server, "GET", "/invite/invalid", cookie=cookie)
        assert status == 302 and headers["Location"].startswith("/guest#invitation=")
        assert headers["Referrer-Policy"] == "no-referrer"
        assert "Set-Cookie" not in headers
        assert http(server, "POST", "/api/invite", {"invitation": "invalid"}, cookie)[0] == 403
        assert http(server, "POST", "/api/register", {"name": "Sample guest", "email": EMAIL}, cookie)[0] == 403
        assert len(server.registration.mailbox(EMAIL)["messages"]) == count
        assert http(server, "GET", "/api/lights", cookie=cookie)[0] == 200
        valid = server.registration.invitation()["token"]
        _, valid_headers, _ = http(server, "GET", "/invite/" + valid, cookie=cookie)
        assert "Set-Cookie" not in valid_headers
        assert http(server, "POST", "/api/invite", {"invitation": valid}, cookie)[0] == 200
        assert server.registration.status(cookie) == "approved"
        for route in ("/demo/mail", "/demo/approve", "/api/mail", "/registration.sqlite"):
            assert http(server, "GET", route)[0] == 404
            assert http(server, "POST", route, {})[0] == 404
        assert b"code" not in body


def test_actual_operator_card_and_mail_cli_are_local_only(tmp_path):
    root = Path(__file__).parents[1]
    base = [sys.executable, "-m", "boomer_switch.registration_demo", "operator", "--state-dir", str(tmp_path)]
    card = subprocess.run(base + ["card", "--base-url", "http://localhost:12345"], cwd=root,
                          capture_output=True, text=True, timeout=10)
    assert card.returncode == 0, card.stderr
    result = json.loads(card.stdout)
    assert Path(result["printable_card"]).parent == tmp_path
    markup = Path(result["printable_card"]).read_text(encoding="utf-8")
    assert "<svg" in markup and result["short_url"] in markup
    assert "/guest#invitation=" in result["invite_url"]
    assert re.fullmatch(r"[a-z0-9]+", result["short_url"].rsplit("/", 1)[1])
    mail = subprocess.run(base + ["mail", "--email", EMAIL], cwd=root,
                          capture_output=True, text=True, timeout=10)
    assert mail.returncode == 0
    assert json.loads(mail.stdout) == {"messages": []}


def test_invalid_invite_invalidates_unverified_recovery_without_mail(tmp_path):
    with server_at(tmp_path) as server:
        original = pending(server.registration)
        server.registration.operator("approve", EMAIL)
        browser = server.registration.browser()
        now = server.registration.clock()
        server.registration.clock = lambda: now + 31
        server.registration.start(browser, "Sample guest", EMAIL)
        before = len(server.registration.mailbox(EMAIL)["messages"])
        status, headers, _ = http(server, "GET", "/invite/invalid", cookie=browser)
        assert status == 302 and "Set-Cookie" not in headers
        status, headers, _ = http(server, "POST", "/api/invite", {"invitation": "invalid"}, browser)
        assert status == 403 and "Max-Age=0" in headers["Set-Cookie"]
        assert http(server, "POST", "/api/register", {"name": "Sample guest", "email": EMAIL}, browser)[0] == 403
        assert len(server.registration.mailbox(EMAIL)["messages"]) == before
        assert server.registration.status(original) == "approved"


def test_global_send_cap_survives_restart_rotation_and_distinct_addresses(tmp_path):
    now = [1000.0]
    store = RegistrationStore(tmp_path, clock=lambda: now[0])
    for index in range(store.MAX_TOTAL_SENDS):
        browser = store.browser(store.invitation()["token"])
        store.start(browser, "Sample guest", f"sample{index}@example.invalid")
    store = RegistrationStore(tmp_path, clock=lambda: now[0])
    store.rotate()
    browser = store.browser(store.invitation()["token"])
    with pytest.raises(AccessDenied, match="busy"):
        store.start(browser, "Sample guest", "extra@example.invalid")
    assert store.mailbox("extra@example.invalid") == {"messages": []}
    with store._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM guests").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM mailbox WHERE kind='owner_notice'").fetchone()[0] == 0
    now[0] += store.WINDOW_SECONDS
    store.start(browser, "Sample guest", "extra@example.invalid")
    assert len(store.mailbox("extra@example.invalid")["messages"]) == 1
