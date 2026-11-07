import asyncio
import base64
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
from http.client import HTTPSConnection, RemoteDisconnected
import ipaddress
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import socket
import ssl
import subprocess
import time
from threading import Thread
from wsgiref.simple_server import make_server
import uuid

from kasa.iot.iotplug import IotPlug
import pytest

from boomer_switch.guest_reachability import MARKER, RESPONSE, serve_marker
from boomer_switch.kasa_pilot import KasaPilotResolver, SingleAttemptProtocol
from boomer_switch.lights import CommandDenied, Light
from boomer_switch.pilot import ClosedIngress, CommandJournal, DuplicateCommand, GuestSession, MTLSIngress, PilotApplication, PilotBackend, runtime_lock, _BoundedServer, _QuietHandler
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


class Ingress:
    def __init__(self):
        self.deadline = time.time() + 7
        self.available = True

    async def verify(self, environ):
        if not self.available:
            raise PermissionUnavailable
        return GuestSession(SESSION, self.deadline)


class Grants:
    def __init__(self):
        self.allowed, self.calls = True, 0

    async def require(self, session, request_id):
        self.calls += 1
        if not self.allowed:
            raise PermissionUnavailable
        return time.time() + 1.5


def backend(tmp_path, device, *, ingress=None):
    provider, calls = resolver(device)
    grants = Grants()
    result = PilotBackend(LIGHT, provider, grants, CommandJournal(tmp_path / "commands.sqlite", LIGHT.device_id), ingress=ingress)
    return result, grants, calls


def test_closed_ingress_denies_before_body_permission_or_discovery(tmp_path):
    target, grants, calls = backend(tmp_path, Device())
    app = PilotApplication(target)
    try:
        assert wsgi(app, path="/api/lights", method="GET")[0] == 403
        assert wsgi(app, path="/api/lights/pilot-lamp", body={"action": "on", "operation_id": str(uuid.uuid4())},
            headers={"HTTP_X_FORWARDED_FOR": "SYNTHETIC_SOURCE", "HTTP_X_HOME_WIFI": "true"})[0] == 403
    finally:
        app.close()
    assert grants.calls == 0 and calls == []


@pytest.mark.smoke
def test_one_lamp_command_duplicate_and_ambiguous_result_do_not_repeat_dispatch(tmp_path):
    device = Device()
    device.ambiguous = True
    target, grants, discovery = backend(tmp_path, device, ingress=Ingress())
    request_id = str(uuid.uuid4())

    async def scenario():
        result = await target.operate({}, action="on", request_id=request_id)
        assert result["state"] == "unavailable"
        with pytest.raises(DuplicateCommand):
            await target.operate({}, action="on", request_id=request_id)
        with pytest.raises(DuplicateCommand):
            await target.operate({}, action="off", request_id=str(uuid.uuid4()))
        state = await target.operate({})
        assert state["lights"][0]["state"] == "on"

    asyncio.run(scenario())
    assert device.commands == 1 and device.closed == 2
    assert len(discovery) == 2


def test_wrong_discovered_identity_and_revocation_during_refresh_cannot_mutate(tmp_path):
    wrong = Device("synthetic-other-device")
    target, _, _ = backend(tmp_path, wrong, ingress=Ingress())
    with pytest.raises(CommandDenied):
        asyncio.run(target.operate({}, action="on", request_id=str(uuid.uuid4())))
    assert wrong.commands == 0 and wrong.closed == 1
    device = Device()
    target, grants, _ = backend(tmp_path, device, ingress=Ingress())
    device.on_update = lambda: setattr(grants, "allowed", False)
    with pytest.raises(CommandDenied):
        asyncio.run(target.operate({}, action="on", request_id=str(uuid.uuid4())))
    assert device.commands == 0 and device.closed == 1


def test_busy_commands_do_not_wait_for_a_stale_presence_context(tmp_path):
    target, grants, discovery = backend(tmp_path, Device(), ingress=Ingress())

    async def scenario():
        await target.lock.acquire()
        try:
            with pytest.raises(DuplicateCommand):
                await target.operate({}, action="on", request_id=str(uuid.uuid4()))
        finally:
            target.lock.release()

    asyncio.run(scenario())
    assert grants.calls == 0 and discovery == []


def test_home_ingress_requires_real_tls_metadata_not_http_peer_claims():
    ingress = MTLSIngress("a" * 64)
    with pytest.raises(PermissionUnavailable):
        asyncio.run(ingress.verify({"HTTP_X_SSL_CLIENT_VERIFY": "SUCCESS", "HTTP_X_HOME_WIFI": "true"}))


def test_last_grant_cannot_outlive_the_accepted_request_at_dispatch(tmp_path):
    now = [1000.0]
    ingress = Ingress()
    ingress.deadline = 1001
    device = Device()
    provider, _ = resolver(device)

    class AdvancingGrants:
        calls = 0

        async def require(self, session, request_id):
            self.calls += 1
            if self.calls == 2:
                now[0] = 1002
            return now[0] + 1

    target = PilotBackend(LIGHT, provider, AdvancingGrants(), CommandJournal(tmp_path / "ops.sqlite", LIGHT.device_id), ingress=ingress, clock=lambda: now[0])
    with pytest.raises(CommandDenied):
        asyncio.run(target.operate({}, action="on", request_id=str(uuid.uuid4())))
    assert device.commands == 0
    with sqlite3.connect(tmp_path / "ops.sqlite") as db:
        assert db.execute("SELECT status FROM commands").fetchone()[0] == "denied"


def test_uncertain_dispatch_survives_restart_until_an_authenticated_confirmed_read(tmp_path):
    device = Device()
    target, grants, _ = backend(tmp_path, device, ingress=Ingress())
    abandoned = str(uuid.uuid4())
    target.journal.reserve(session_binding(SESSION), LIGHT.light_id, "on", abandoned)
    target.journal.dispatched(abandoned)
    restarted, grants, _ = backend(tmp_path, device, ingress=Ingress())

    async def scenario():
        with pytest.raises(DuplicateCommand):
            await restarted.operate({}, action="on", request_id=str(uuid.uuid4()))
        grants.allowed = False
        with pytest.raises(PermissionUnavailable):
            await restarted.operate({})
        with sqlite3.connect(tmp_path / "commands.sqlite") as db:
            assert db.execute("SELECT status FROM commands WHERE id=?", (abandoned,)).fetchone()[0] == "dispatched"
        grants.allowed = True
        assert (await restarted.operate({}))["lights"][0]["state"] == "off"
        await restarted.operate({}, action="on", request_id=str(uuid.uuid4()))

    asyncio.run(scenario())
    assert device.commands == 1


def test_normal_runtime_lock_excludes_duplicate_process_owners(tmp_path):
    path = tmp_path / "commands.sqlite"
    with runtime_lock(path):
        with pytest.raises(ValueError, match="Another"):
            with runtime_lock(path):
                pytest.fail("A duplicate runtime acquired the journal.")
    with runtime_lock(path):
        pass


def synthetic_tls_contexts(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-test-ca")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256()))
    ca_path = tmp_path / "ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    certificates = {}
    for role, usage in (("server", ExtendedKeyUsageOID.SERVER_AUTH), ("client", ExtendedKeyUsageOID.CLIENT_AUTH)):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-" + role)])
        builder = (x509.CertificateBuilder().subject_name(name).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False))
        if role == "server":
            loopback = ipaddress.ip_address(socket.gethostbyname("localhost"))
            assert loopback.is_loopback
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(loopback)]), critical=False)
        certificate = builder.sign(ca_key, hashes.SHA256())
        cert_path, key_path = tmp_path / (role + ".pem"), tmp_path / (role + "-key.pem")
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        certificates[role] = (cert_path, key_path, certificate)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(*certificates["server"][:2])
    server_context.load_verify_locations(cafile=ca_path)
    server_context.verify_mode = ssl.CERT_REQUIRED
    client_context = ssl.create_default_context(cafile=ca_path)
    client_context.load_cert_chain(*certificates["client"][:2])
    no_client_certificate = ssl.create_default_context(cafile=ca_path)
    fingerprint = hashlib.sha256(certificates["client"][2].public_bytes(serialization.Encoding.DER)).hexdigest()
    return server_context, client_context, no_client_certificate, fingerprint


def verified_tls_peer(tmp_path):
    server_context, client_context, _, fingerprint = synthetic_tls_contexts(tmp_path)
    incoming_server, outgoing_server, incoming_client, outgoing_client = [ssl.MemoryBIO() for _ in range(4)]
    server = server_context.wrap_bio(incoming_server, outgoing_server, server_side=True)
    client = client_context.wrap_bio(incoming_client, outgoing_client, server_hostname="localhost")
    done = [False, False]
    for _ in range(100):
        for index, channel in enumerate((client, server)):
            if not done[index]:
                try:
                    channel.do_handshake()
                    done[index] = True
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    pass
        data = outgoing_client.read()
        if data:
            incoming_server.write(data)
        data = outgoing_server.read()
        if data:
            incoming_client.write(data)
        if all(done):
            break
    assert all(done)
    return server, fingerprint


def test_actual_mutual_tls_peer_and_persisted_grant_reach_the_normal_backend(tmp_path):
    peer, fingerprint = verified_tls_peer(tmp_path)
    path = tmp_path / "authorization.sqlite"
    store(path)
    endpoint = WebsiteGrantApplication(PersistedGrants(path), KEY)

    class EndpointSession(GrantSession):
        def post(self, url, **options):
            status, payload, _ = wsgi(endpoint, body=options["json"], headers={"HTTP_AUTHORIZATION": options["headers"]["Authorization"]})
            response = GrantResponse(payload)
            response.status = status
            return response

    grants = HTTPSGrantClient("https://example.invalid/grant", KEY, session_factory=lambda **kwargs: EndpointSession(None))
    device = Device()
    provider, _ = resolver(device)
    app = PilotApplication(PilotBackend(LIGHT, provider, grants, CommandJournal(tmp_path / "commands.sqlite", LIGHT.device_id), ingress=MTLSIngress(fingerprint)))
    headers = {"boomer.tls_connection": peer, "HTTP_X_PILOT_DEADLINE": str(time.time() + 7), "HTTP_COOKIE": "bs_session=" + SESSION}
    try:
        status, result, _ = wsgi(app, path="/api/lights/pilot-lamp", body={"action": "on", "operation_id": str(uuid.uuid4())}, headers=headers)
        assert status == 200 and result["state"] == "on"
        app.backend.ingress = MTLSIngress("0" * 64)
        assert wsgi(app, method="GET", path="/api/lights", headers=headers)[0] == 403
    finally:
        app.close()
    assert device.commands == 1


def test_localhost_tls_server_rejects_no_client_cert_and_accepts_only_pinned_peer(tmp_path):
    server_tls, client_tls, anonymous_tls, fingerprint = synthetic_tls_contexts(tmp_path)
    device = Device()
    target, grants, calls = backend(tmp_path, device, ingress=MTLSIngress(fingerprint))
    app = PilotApplication(target)
    with make_server("localhost", 0, app, server_class=_BoundedServer, handler_class=_QuietHandler) as server:
        server.tls_context = server_tls
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            connection = HTTPSConnection(server.server_address[0], server.server_port, context=anonymous_tls, timeout=3)
            try:
                with pytest.raises((ssl.SSLError, ConnectionError, RemoteDisconnected)):
                    connection.request("GET", "/api/lights")
                    connection.getresponse()
            finally:
                connection.close()
            assert calls == [] and grants.calls == 0
            connection = HTTPSConnection(server.server_address[0], server.server_port, context=client_tls, timeout=3)
            try:
                connection.request("GET", "/api/lights", headers={"Cookie": "bs_session=" + SESSION, "X-Pilot-Deadline": str(time.time() + 6)})
                response = connection.getresponse()
                assert response.status == 200
                assert json.loads(response.read())["lights"][0]["state"] == "off"
            finally:
                connection.close()
            target.ingress = MTLSIngress("0" * 64)
            connection = HTTPSConnection(server.server_address[0], server.server_port, context=client_tls, timeout=3)
            try:
                connection.request("GET", "/api/lights", headers={"Cookie": "bs_session=" + SESSION, "X-Pilot-Deadline": str(time.time() + 6)})
                response = connection.getresponse()
                assert response.status == 403
                response.read()
            finally:
                connection.close()
        finally:
            server.shutdown()
            worker.join(timeout=5)
            app.close()
            assert not worker.is_alive()
    assert device.commands == 0


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


def test_reachability_marker_is_fixed_bounded_and_independent_of_devices(capsys):
    now = [0.0]

    class Connection:
        sent = b""
        closed = False

        def __enter__(self): return self
        def __exit__(self, *_args): self.closed = True
        def settimeout(self, value): assert 0 < value <= 1
        def recv(self, maximum):
            assert maximum == 2048
            now[0] += 0.1
            return b"GET /private-file HTTP/1.1\r\n\r\n"
        def sendall(self, data):
            self.sent = data
            now[0] += 0.1

    connection = Connection()

    class Listener:
        accepted = False
        timeout = 0
        bound = None
        closed = False

        def __enter__(self): return self
        def __exit__(self, *_args): self.closed = True
        def bind(self, address): self.bound = address
        def listen(self, backlog): assert backlog == 2
        def settimeout(self, value): self.timeout = value
        def accept(self):
            if not self.accepted:
                self.accepted = True
                return connection, None
            now[0] += self.timeout
            raise TimeoutError

    listener = Listener()
    address = str(ipaddress.IPv4Address((192 << 24) + (2 << 8) + 1))
    serve_marker(address, 8769, 2, clock=lambda: now[0], socket_factory=lambda *_args: listener)
    assert listener.bound == (address, 8769)
    assert connection.sent == RESPONSE and RESPONSE.endswith(MARKER)
    assert listener.closed and connection.closed and now[0] <= 2
    assert f"http://{address}:8769/" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="The downloadable check uses native Windows PowerShell.")
def test_downloadable_marker_native_selection_and_finite_loopback():
    source = (Path(__file__).parents[1] / "guest-reachability.cmd").read_text(encoding="ascii")
    functions = source.split(":POWERSHELL\n", 1)[1].split("# RUN", 1)[0]
    synthetic = r"""
$private = [Net.IPAddress]::new([byte[]](192,168,250,2))
$wifi = [pscustomobject]@{Kind='Wireless80211';Up=$true;Addresses=@($private)}
if ((Select-MarkerAddress @($wifi)).ToString() -ne $private.ToString()) { throw 'Selection failed' }
foreach ($items in @(@(), @($wifi,$wifi), @([pscustomobject]@{Kind='Ethernet';Up=$true;Addresses=@($private)}))) {
    $denied = $false
    try { Select-MarkerAddress $items | Out-Null } catch { $denied = $true }
    if (-not $denied) { throw 'Ambiguous or absent wireless adapter was accepted' }
}
Start-Marker ([Net.IPAddress]::Loopback) 0 1
"""
    encoded = base64.b64encode((functions + synthetic).encode("utf-16-le")).decode()
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    process = subprocess.Popen([str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # The real .NET listener uses only an ephemeral loopback port and fixed synthetic text.
        lines = []
        for line in process.stdout:
            lines.append(line)
            if line.strip().startswith("http://"):
                port = int(line.strip().rsplit(":", 1)[1].rstrip("/"))
                break
        else:
            raise AssertionError("The synthetic marker did not start: " + "".join(lines) + process.stderr.read())
        with socket.create_connection((str(ipaddress.IPv4Address((127 << 24) + 1)), port), timeout=2) as client:
            client.sendall(b"GET /not-a-file HTTP/1.1\r\nHost: localhost\r\n\r\n")
            payload = bytearray()
            while chunk := client.recv(4096):
                payload.extend(chunk)
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, stderr
        assert bytes(payload) == RESPONSE
        assert "listener is closed" in stdout
        with pytest.raises(OSError):
            socket.create_connection((str(ipaddress.IPv4Address((127 << 24) + 1)), port), timeout=0.2)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)
