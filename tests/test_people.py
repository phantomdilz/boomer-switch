from contextlib import contextmanager
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from http.cookiejar import CookieJar
from io import BytesIO
import json
import math
import sqlite3
from threading import Thread
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from wsgiref.simple_server import make_server

import pytest

from boomer_switch.people import PeopleApplication, RemoteUserOwnerAuth, create_people_application
from boomer_switch.people_demo import DemoOwnerAuth, MountedPeople, _BoundedServer, _QuietHandler
from boomer_switch.registration import AccessDenied, RegistrationStore, StalePerson
from boomer_switch.registration_demo import RegistrationDemoServer


EMAIL = "sample@example.invalid"


def current(store, email=EMAIL):
    return next(person for person in store.people() if person["email"] == email)


def latest_code(store, email=EMAIL):
    return [item["code"] for item in store.mailbox(email)["messages"] if item["kind"] == "code"][-1]


def pending(store, email=EMAIL):
    browser = store.browser(store.invitation()["token"])
    store.start(browser, "Sample guest", email)
    store.verify(browser, latest_code(store, email))
    return browser


def invoke(app, method="GET", path="/api/people", *, body=None, cookie=None, remote_user="sample-owner", csrf=None, stream=None, **extra):
    payload = json.dumps(body).encode() if body is not None else b""
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_TYPE": "application/json",
               "CONTENT_LENGTH": str(len(payload)), "wsgi.input": stream or BytesIO(payload), "wsgi.url_scheme": "https"}
    if remote_user is not None:
        environ["REMOTE_USER"] = remote_user
    if cookie:
        environ["HTTP_COOKIE"] = f"bs_owner={cookie}"
    if csrf:
        environ["HTTP_X_CSRF_TOKEN"] = csrf
    environ.update(extra)
    output = {}

    def response(status, headers, exc_info=None):
        output.update(status=int(status.split()[0]), headers=dict(headers))

    data = b"".join(app(environ, response))
    return output["status"], output["headers"], data


def owner(app):
    status, headers, body = invoke(app)
    assert status == 200
    cookie = SimpleCookie(headers["Set-Cookie"])["bs_owner"].value
    assert SimpleCookie(headers["Set-Cookie"])["bs_owner"]["path"] == "/"
    assert "Secure" in headers["Set-Cookie"] and "HttpOnly" in headers["Set-Cookie"]
    return cookie, json.loads(body)["csrf"]


@pytest.mark.parametrize("remote,headers", [(None, {}), ("other-user", {}),
    (None, {"HTTP_REMOTE_USER": "sample-owner", "HTTP_X_OWNER": "true"}),
    ("sample-owner", {"wsgi.url_scheme": "http"})])
def test_host_adapter_requires_trusted_current_https_owner_identity(tmp_path, remote, headers):
    app = create_people_application(tmp_path, "sample-owner")
    for path in ("/people", "/api/people", "/people.js", "/style.css"):
        status, _, body = invoke(app, path=path, remote_user=remote, **headers)
        assert status == 403
        assert b"csrf" not in body and b"sample@example.invalid" not in body


def test_host_adapter_csrf_is_bound_to_owner_cookie_and_current_identity(tmp_path):
    app = create_people_application(tmp_path, "sample-owner")
    first, csrf = owner(app)
    second, _ = owner(app)
    body = {"action": "preapprove", "email": EMAIL, "revision": None}
    assert invoke(app, "POST", body=body, cookie=first)[0] == 403
    assert invoke(app, "POST", body=body, cookie=second, csrf=csrf)[0] == 403
    assert invoke(app, "POST", body=body, cookie=first, csrf=csrf, remote_user="other-user")[0] == 403
    assert invoke(app, "POST", body=body, cookie=first, csrf=csrf)[0] == 200
    assert app.app.store.people()[0]["email_verified"] is False


def test_owner_session_expiry_is_rechecked_after_slow_body_read(tmp_path):
    now = [1000.0]
    store = RegistrationStore(tmp_path)
    app = RemoteUserOwnerAuth(PeopleApplication(store), "sample-owner", clock=lambda: now[0])
    cookie, csrf = owner(app)
    body = {"action": "preapprove", "email": EMAIL, "revision": None}

    class SlowBody(BytesIO):
        def read(self, *args):
            now[0] += 3601
            return super().read(*args)

    status, _, _ = invoke(app, "POST", body=body, cookie=cookie, csrf=csrf, stream=SlowBody(json.dumps(body).encode()))
    assert status == 403
    assert store.people() == []


def test_dedicated_mount_keeps_assets_and_api_inside_website_prefix(tmp_path):
    auth = create_people_application(tmp_path, "sample-owner")
    app = MountedPeople(auth)
    assert invoke(app, path="/people")[0] == 404
    status, headers, page = invoke(app, path="/house-lights/people")
    assert status == 200
    assert b'href="style.css"' in page and b'src="people.js"' in page
    cookie = SimpleCookie(headers["Set-Cookie"])["bs_owner"].value
    assert SimpleCookie(headers["Set-Cookie"])["bs_owner"]["path"] == "/house-lights"
    assert invoke(app, path="/house-lights/people.js", cookie=cookie)[0] == 200
    assert invoke(app, path="/house-lights/style.css", cookie=cookie)[0] == 200
    assert invoke(app, path="/house-lights/api/people", cookie=cookie)[0] == 200
    assert invoke(app, path="/api/people", cookie=cookie)[0] == 404


def test_versioned_actions_preserve_accepted_state_and_require_inbox_proof(tmp_path):
    store = RegistrationStore(tmp_path)
    browser = pending(store)
    revision = current(store)["revision"]
    store.change_person("approve", EMAIL, revision)
    store.require_access(browser)
    for action in ("deny", "remove", "expiry"):
        with pytest.raises(StalePerson):
            store.change_person(action, EMAIL, revision)
    assert current(store)["expires"] is None and current(store)["status"] == "approved"
    other = "preapproved@example.invalid"
    store.change_person("preapprove", other, None)
    phone = store.browser()
    store.start(phone, "Another sample", other)
    with pytest.raises(AccessDenied):
        store.require_access(phone)
    store.verify(phone, latest_code(store, other))
    store.require_access(phone)
    assert current(store, other)["email_verified"] is True


def test_deny_invalidates_pending_identity_and_owner_notice_is_not_an_approval(tmp_path):
    store = RegistrationStore(tmp_path)
    browser = pending(store)
    store.change_person("deny", EMAIL, current(store)["revision"])
    assert current(store)["status"] == "denied"
    with pytest.raises(AccessDenied):
        store.require_access(browser)


def test_future_deadline_changes_and_clear_preserve_current_active_session(tmp_path):
    store = RegistrationStore(tmp_path, clock=lambda: 1000)
    browser = pending(store)
    store.change_person("approve", EMAIL, current(store)["revision"])
    for deadline in (2000, 3000, None):
        store.change_person("expiry", EMAIL, current(store)["revision"], expires=deadline)
        assert current(store)["expires"] == deadline
        store.require_access(browser)


def test_expiration_then_restoration_cannot_resurrect_old_sessions_or_codes(tmp_path):
    now = [1000.0]
    store = RegistrationStore(tmp_path, clock=lambda: now[0])
    original = pending(store)
    store.change_person("approve", EMAIL, current(store)["revision"])
    store.change_person("expiry", EMAIL, current(store)["revision"], expires=2000)
    phone = store.browser()
    now[0] += 31
    store.start(phone, "Sample guest", EMAIL)
    old_code = latest_code(store)
    now[0] = 2001
    with pytest.raises(AccessDenied):
        store.require_access(original)
    store.change_person("expiry", EMAIL, current(store)["revision"], expires=None)
    with pytest.raises(AccessDenied):
        store.require_access(original)
    with pytest.raises(AccessDenied):
        store.verify(phone, old_code)
    fresh = store.browser()
    store.start(fresh, "Sample guest", EMAIL)
    store.verify(fresh, latest_code(store))
    store.require_access(fresh)


@pytest.mark.parametrize("deadline", [math.nan, math.inf, -math.inf, 10**400, True, "tomorrow"])
def test_invalid_dates_never_change_accepted_permission(tmp_path, deadline):
    store = RegistrationStore(tmp_path)
    pending(store)
    store.change_person("approve", EMAIL, current(store)["revision"])
    before = current(store)
    with pytest.raises(ValueError):
        store.change_person("expiry", EMAIL, before["revision"], expires=deadline)
    assert current(store) == before


def test_repeated_preapproval_cannot_clear_an_existing_deadline(tmp_path):
    store = RegistrationStore(tmp_path)
    store.change_person("preapprove", EMAIL, None)
    store.change_person("expiry", EMAIL, current(store)["revision"], expires=2000)
    before = current(store)
    with pytest.raises(StalePerson):
        store.change_person("preapprove", EMAIL, before["revision"])
    assert current(store) == before


def test_legacy_verified_guest_metadata_survives_compatible_schema_upgrade(tmp_path):
    with sqlite3.connect(tmp_path / "registration.sqlite") as db:
        db.execute("CREATE TABLE guests (email TEXT PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL,expires REAL,revision INTEGER NOT NULL DEFAULT 1)")
        db.execute("INSERT INTO guests VALUES (?,'Sample guest','approved',3000,4)", (EMAIL,))
    store = RegistrationStore(tmp_path)
    assert current(store) == {"email": EMAIL, "name": "Sample guest", "status": "approved",
                              "expires": 3000, "revision": 4, "email_verified": True}


@contextmanager
def sites(directory, *, mounted=False):
    with RegistrationDemoServer(directory) as guest:
        app = DemoOwnerAuth(PeopleApplication(guest.registration), directory)
        with make_server("localhost", 0, MountedPeople(app) if mounted else app, server_class=_BoundedServer, handler_class=_QuietHandler) as website:
            workers = [Thread(target=server.serve_forever, daemon=True) for server in (guest, website)]
            for worker in workers:
                worker.start()
            try:
                yield guest, website, app
            finally:
                guest.shutdown()
                website.shutdown()
                for worker in workers:
                    worker.join(timeout=5)
                    assert not worker.is_alive()


def http(server, method, path, *, body=None, cookie=None, csrf=None):
    connection = HTTPConnection(server.server_address[0], server.server_port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    try:
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_scoped_owner_cookie_reaches_mounted_api_but_not_guest_routes(tmp_path):
    with sites(tmp_path, mounted=True) as (guest, website, authentication):
        owner_base = f"http://{website.server_address[0]}:{website.server_port}"
        guest_base = f"http://{guest.server_address[0]}:{guest.server_port}"
        cookies = CookieJar()
        browser = build_opener(ProxyHandler({}), HTTPCookieProcessor(cookies))
        with browser.open(owner_base + "/house-lights/demo-sign-in/" + authentication.ticket, timeout=5) as response:
            assert response.status == 200
        assert len(list(cookies)) == 1
        assert list(cookies)[0].path == "/house-lights"
        owner_read = Request(owner_base + "/house-lights/api/people")
        with browser.open(owner_read, timeout=5) as response:
            assert response.status == 200
        assert "bs_owner=" in owner_read.get_header("Cookie")
        guest_read = Request(guest_base + "/api/access")
        with browser.open(guest_read, timeout=5) as response:
            assert response.status == 200
        assert guest_read.get_header("Cookie") is None


@pytest.mark.smoke
@pytest.mark.parametrize("action", ["remove", "expiry"])
def test_owner_website_normal_path_changes_the_next_guest_control_command(tmp_path, action):
    with sites(tmp_path) as (guest, website, authentication):
        browser = pending(guest.registration)
        for path in ("/people", "/api/people", "/people.js"):
            assert http(guest, "GET", path)[0] == 404
            assert http(website, "GET", path)[0] == 403
        _, headers, _ = http(website, "GET", "/demo-sign-in/" + authentication.ticket)
        owner_cookie = headers["Set-Cookie"].split(";", 1)[0]
        assert http(website, "GET", "/people", cookie=owner_cookie)[0] == 200
        status, _, listing = http(website, "GET", "/api/people", cookie=owner_cookie)
        assert status == 200
        people = json.loads(listing)
        person = people["people"][0]
        body = {"action": "approve", "email": EMAIL, "revision": person["revision"]}
        assert http(website, "POST", "/api/people", cookie=owner_cookie, body=body)[0] == 403
        status, _, updated = http(website, "POST", "/api/people", cookie=owner_cookie, csrf=people["csrf"], body=body)
        assert status == 200
        guest_cookie = f"bs_session={browser}"
        assert http(guest, "POST", "/api/lights/sofa-lamp", cookie=guest_cookie, body={"action": "on"})[0] == 200
        assert http(website, "POST", "/api/people", cookie=owner_cookie, csrf=people["csrf"], body=body)[0] == 409
        revision = json.loads(updated)["people"][0]["revision"]
        body = {"action": action, "email": EMAIL, "revision": revision}
        if action == "expiry":
            body["expires"] = 0
        assert http(website, "POST", "/api/people", cookie=owner_cookie, csrf=people["csrf"], body=body)[0] == 200
        assert http(guest, "POST", "/api/lights/sofa-lamp", cookie=guest_cookie, body={"action": "off"})[0] == 403
        assert guest._devices["sofa-lamp"].is_on is True
