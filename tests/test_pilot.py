import asyncio
from enum import Enum
from io import BytesIO
import json
import sqlite3
import ssl
import time
import uuid

from kasa.iot.iotplug import IotPlug
import pytest

from boomer_switch.kasa_pilot import KasaPilotResolver, SingleAttemptProtocol
from boomer_switch.lights import Light
from boomer_switch.pilot import CommandJournal, runtime_lock
from boomer_switch.website_grants import HTTPSGrantClient, PermissionUnavailable, PersistedGrants, WebsiteGrantApplication, session_binding


SESSION = "s" * 43
WEBSITE_SESSION = "w" * 43
KEY = "SYNTHETIC_SERVICE_KEY_" + "x" * 32
LIGHT = Light("pilot-lamp", "Sample lamp", "Sample room", "synthetic-device")


def store(path, *, status="approved", verified=1, expires=None):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE guests(email TEXT,status TEXT,email_verified INTEGER,expires REAL)")
        db.execute("CREATE TABLE sessions(id TEXT,email TEXT,verified INTEGER,expires REAL)")
        db.execute("CREATE TABLE home_sessions(id TEXT PRIMARY KEY,website_session TEXT NOT NULL,expires REAL NOT NULL)")
        db.execute("INSERT INTO guests VALUES ('sample@example.invalid',?,1,?)", (status, expires))
        db.execute("INSERT INTO sessions VALUES (?,'sample@example.invalid',?,?)", (session_binding(WEBSITE_SESSION), verified, time.time() + 1000))
        db.execute("INSERT INTO home_sessions VALUES (?,?,?)", (session_binding(SESSION), session_binding(WEBSITE_SESSION), time.time() + 500))


def wsgi(app, *, method="POST", path="/grant", body=None, headers=None, scheme="https"):
    encoded = json.dumps(body).encode() if body is not None else b""
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "wsgi.url_scheme": scheme,
        "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(encoded)), "wsgi.input": BytesIO(encoded)}
    environ.update(headers or {})
    result = {}

    def response(status, values):
        result.update(status=int(status.split()[0]), headers=dict(values))

    payload = b"".join(app(environ, response))
    return result["status"], json.loads(payload), environ


@pytest.mark.parametrize("status,verified,expires,allowed", [
    ("approved", 1, None, True), ("pending", 1, None, False),
    ("approved", 0, None, False), ("revoked", 1, None, False), ("approved", 1, 0, False),
])
def test_permission_endpoint_reads_persisted_session_mapping_without_bootstrap(tmp_path, status, verified, expires, allowed):
    path = tmp_path / "authorization.sqlite"
    store(path, status=status, verified=verified, expires=expires)
    before = path.read_bytes()
    app = WebsiteGrantApplication(PersistedGrants(path), KEY)
    request_id = str(uuid.uuid4())
    code, result, _ = wsgi(app, body={"session": SESSION, "request_id": request_id}, headers={"HTTP_AUTHORIZATION": "Bearer " + KEY})
    assert code == 200 and result["allowed"] is allowed
    assert result["request_id"] == request_id
    assert result["session_binding"] == session_binding(SESSION)
    assert "sample@example.invalid" not in json.dumps(result)
    assert KEY not in json.dumps(result) and SESSION not in json.dumps(result)
    assert path.read_bytes() == before


def test_permission_endpoint_denies_missing_store_and_unverified_transport(tmp_path):
    path = tmp_path / "missing.sqlite"
    app = WebsiteGrantApplication(PersistedGrants(path), KEY)
    body = {"session": SESSION, "request_id": str(uuid.uuid4())}
    assert wsgi(app, body=body)[0] == 403
    assert wsgi(app, body=body, headers={"HTTP_AUTHORIZATION": "Bearer " + KEY}, scheme="http")[0] == 403
    assert wsgi(app, body=body, headers={"HTTP_AUTHORIZATION": "Bearer " + KEY})[0] == 503
    assert not path.exists()


def test_website_cookie_cannot_substitute_for_a_home_scoped_session(tmp_path):
    path = tmp_path / "authorization.sqlite"
    store(path)
    grants = PersistedGrants(path)
    assert grants.deadline(WEBSITE_SESSION) is None
    assert grants.deadline(SESSION) > time.time()
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM sessions")
    assert grants.deadline(SESSION) is None


class GrantResponse:
    status = 200

    def __init__(self, payload):
        self.data = json.dumps(payload).encode()
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def iter_chunked(self, size):
        yield self.data


class GrantSession:
    def __init__(self, response):
        self.response, self.calls = response, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    def post(self, url, **options):
        self.calls.append((url, options))
        return self.response


def test_https_client_binds_grant_to_session_and_operation_without_redirects_or_retries():
    request_id = str(uuid.uuid4())
    response = GrantResponse({"allowed": True, "request_id": request_id, "session_binding": session_binding(SESSION), "valid_until": time.time() + 1.5})
    session = GrantSession(response)
    factory_options = []

    def factory(**options):
        factory_options.append(options)
        return session

    client = HTTPSGrantClient("https://example.invalid/grant", KEY, session_factory=factory)
    assert asyncio.run(client.require(SESSION, request_id)) > time.time()
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
    assert factory_options[0]["trust_env"] is False
    assert factory_options[0]["timeout"].total == 2
    assert client.ssl.check_hostname and client.ssl.verify_mode == ssl.CERT_REQUIRED
    response.status = 302
    with pytest.raises(PermissionUnavailable):
        asyncio.run(client.require(SESSION, request_id))
    assert len(session.calls) == 2


class Protocol:
    _transport = object()

    def __init__(self):
        self.calls = []

    async def query(self, request, retry_count=3):
        self.calls.append((request, retry_count))
        return {"system": {"set_relay_state": {"err_code": 0}}}

    async def close(self):
        pass


def test_actual_kasa_plug_command_consumer_uses_one_query_attempt():
    protocol = Protocol()
    device = IotPlug("synthetic-device-host", protocol=SingleAttemptProtocol(protocol))
    asyncio.run(device.turn_on())
    assert len(protocol.calls) == 1
    assert protocol.calls[0][1] == 0
    assert protocol.calls[0][0] == {"system": {"set_relay_state": {"state": 1}}}


class DeviceType(Enum):
    Bulb = "bulb"


class Device:
    device_type = DeviceType.Bulb
    children = []

    def __init__(self, identity=LIGHT.device_id):
        self.device_id = identity
        self.protocol = Protocol()
        self.is_on = False
        self.commands, self.updates, self.closed = 0, 0, 0
        self.ambiguous = False
        self.on_update = lambda: None

    async def update(self):
        self.updates += 1
        self.on_update()

    async def turn_on(self):
        self.commands += 1
        self.is_on = True
        if self.ambiguous:
            raise TimeoutError("SYNTHETIC_PRIVATE_PROVIDER_DETAIL")

    async def turn_off(self):
        self.commands += 1
        self.is_on = False

    async def disconnect(self):
        self.closed += 1


def resolver(device):
    calls = []

    async def discover(host, **options):
        calls.append(options)
        return device

    return KasaPilotResolver(LIGHT, "synthetic-private-host", discover=discover), calls


def test_normal_runtime_lock_excludes_duplicate_process_owners(tmp_path):
    path = tmp_path / "commands.sqlite"
    with runtime_lock(path):
        with pytest.raises(ValueError, match="Another"):
            with runtime_lock(path):
                pytest.fail("A duplicate runtime acquired the journal.")
    with runtime_lock(path):
        pass


def test_one_physical_identity_owns_each_journal_and_reconciliation(tmp_path):
    path = tmp_path / "commands.sqlite"
    journal = CommandJournal(path, LIGHT.device_id)
    request_id = str(uuid.uuid4())
    journal.reserve(session_binding(SESSION), LIGHT.light_id, "on", request_id)
    journal.dispatched(request_id)
    with pytest.raises(ValueError, match="another physical"):
        CommandJournal(path, "synthetic-other-device")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status FROM commands").fetchone()[0] == "dispatched"
