from io import BytesIO
import ipaddress
import json
from pathlib import Path
import re
import sys
import time
import uuid

import pytest

from boomer_switch.website_passenger import PassengerApplication, PHPValidator


PEER = str(ipaddress.IPv4Address(134744072))
LIGHT = {'light_id': 'selected-lamp', 'room': 'Guest room', 'name': 'Selected lamp'}


class Validator:
    valid = True

    def __init__(self, command, cookie):
        self.cookie = cookie

    def check(self, env):
        return self.valid and self.cookie + '=synthetic' in env.get('HTTP_COOKIE', '')


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    class Transport:
        def __init__(self, executable, *, signer=None):
            assert signer is None
            self.messages = []

        def send(self, message):
            self.messages.append(message)

    monkeypatch.setattr('boomer_switch.website_cgi.SendmailTransport', Transport)
    config = json.loads(Path('hosting/passenger-config.example.json').read_text())
    config['state_directory'] = str(tmp_path / 'state')
    config['service_key'] = 'k'*40
    notice_log = tmp_path / 'notices.jsonl'
    notice_script = 'import pathlib,sys; p=pathlib.Path('+repr(str(notice_log))+'); p.open("ab").write(sys.stdin.buffer.read()+b"\\n"); sys.stdout.write("SENT")'
    config['owner_notifications'] = {'command': [sys.executable, '-c', notice_script], 'decision_key': 'd'*40}
    config['passenger']['owner_validator_command'] = [sys.executable]
    path = tmp_path / 'config.local.passenger.json'
    path.write_text(json.dumps(config))
    app = PassengerApplication(path, validator_factory=Validator)
    app.notice_log = notice_log

    def switches(**changes):
        config['passenger'].update(changes)
        path.write_text(json.dumps(config))

    return app, switches


def invoke(app, path, body=None, cookie='', csrf='', **extra):
    data = json.dumps(body).encode() if body is not None else b''
    env = {'PATH_INFO': path, 'SCRIPT_NAME': '/house-lights', 'REQUEST_METHOD': 'POST' if body is not None else 'GET',
           'wsgi.url_scheme': 'https', 'HTTP_HOST': 'example.invalid', 'REMOTE_ADDR': PEER,
           'HTTP_X_HL_APACHE_PEER': PEER, 'CONTENT_TYPE': 'application/json', 'CONTENT_LENGTH': str(len(data)),
           'wsgi.input': BytesIO(data), 'HTTP_ORIGIN': 'https://example.invalid', 'HTTP_COOKIE': cookie,
           'HTTP_X_CSRF_TOKEN': csrf}
    env.update(extra)
    response = {}
    def start(status, headers, exc_info=None):
        response.update(status=int(status.split()[0]), headers=dict(headers))
    payload = b''.join(app(env, start))
    return response['status'], response['headers'], json.loads(payload) if payload.startswith(b'{') else payload


def enabled(hosted):
    app, switches = hosted
    switches(registration_enabled=True, mail_enabled=True, people_write_enabled=True,
             pilot_enabled=True, apache_peer_verified=True)
    return app


def verified(app):
    _, headers, result = invoke(app, '/api/access')
    cookie, csrf = headers['Set-Cookie'].split(';')[0], result['csrf']
    _, headers, result = invoke(app, '/api/invite', {'invitation': app.app.store.invitation()['token']}, cookie, csrf)
    cookie, csrf = headers['Set-Cookie'].split(';')[0], result['csrf']
    assert invoke(app, '/api/register', {'name': 'Guest', 'email': 'guest@example.invalid'}, cookie, csrf)[0] == 200
    transport = app.app.mail.transport.transport
    code = re.search(r'\b[0-9]{6}\b', transport.messages[-1].get_content()).group()
    assert invoke(app, '/api/verify', {'code': code}, cookie, csrf)[2]['status'] == 'pending'
    assert len(transport.messages) == 1
    assert len(app.notice_log.read_text().splitlines()) == 1
    return cookie, csrf


def test_production_registration_owner_and_paused_pilot_truth(hosted):
    app = enabled(hosted)
    cookie, csrf = verified(app)
    status, headers, people = invoke(app, '/api/people', cookie='EXISTING_OWNER_COOKIE=synthetic')
    assert status == 200
    owner = 'EXISTING_OWNER_COOKIE=synthetic; ' + headers['Set-Cookie'].split(';')[0]
    person = people['people'][0]
    assert invoke(app, '/api/people', {'action': 'approve', 'email': person['email'], 'revision': person['revision']}, owner, people['csrf'])[0] == 200
    assert invoke(app, '/api/access', cookie=cookie)[2]['status'] == 'approved'
    agent = str(uuid.uuid4())
    def poll(sequence):
        return invoke(app, '/home/poll', {'agent_id': agent, 'sequence': sequence, 'request_id': str(uuid.uuid4()), 'light': LIGHT}, HTTP_AUTHORIZATION='Bearer '+app.config['service_key'])
    assert poll(1)[0] == 200
    assert invoke(app, '/api/access', cookie=cookie)[2]['light'] == LIGHT
    operation = str(uuid.uuid4())
    assert invoke(app, '/api/lights', {'operation_id': operation, 'action': 'read'}, cookie, csrf)[0] == 202
    assignment = poll(2)[2]['operation']
    result = {'agent_id': agent, 'sequence': 2, 'request_id': str(uuid.uuid4()), 'operation_id': operation,
              'generation': assignment['generation'], 'outcome': {'status': 'confirmed', 'state': 'on'}}
    authority = {key: value for key, value in result.items() if key != 'outcome'}
    assert invoke(app, '/home/authorize', authority, HTTP_AUTHORIZATION='Bearer '+app.config['service_key'])[2]['allowed']
    hosted[1](pilot_enabled=False)
    access = invoke(app, '/api/access', cookie=cookie)[2]
    assert access['home_ready'] is False and 'light' not in access
    assert poll(3)[0] == 403
    assert invoke(app, '/api/lights', {'operation_id': str(uuid.uuid4()), 'action': 'on'}, cookie, csrf)[0] == 403
    assert invoke(app, '/home/result', result, HTTP_AUTHORIZATION='Bearer '+app.config['service_key'])[2]['accepted']
    assert not app.app.rendezvous.access(app.app._session({'HTTP_COOKIE': cookie}), {'boomer.direct_peer': False})


def test_stage_defaults_and_host_evidence_fail_closed(hosted):
    app, switches = hosted
    assert invoke(app, '/guest')[0] == 200
    assert invoke(app, '/entry.js')[0] == 200
    for path in ('/api/invite', '/api/register', '/api/people', '/home/poll'):
        assert invoke(app, path, {})[0] == 403
    enabled(hosted)
    for extra in ({'REMOTE_USER': app.config['owner_identity']}, {'HTTP_X_OWNER': 'owner'},
                  {'HTTP_COOKIE': 'EXISTING_OWNER_COOKIE=wrong'}, {'HTTP_X_HL_APACHE_PEER': ''},
                  {'HTTP_X_HL_APACHE_PEER': str(ipaddress.ip_address(PEER)+1)},
                  {'wsgi.url_scheme': 'http'}, {'HTTP_HOST': 'other.invalid'}, {'SCRIPT_NAME': '/other'}):
        assert invoke(app, '/api/people', **extra)[0] == 403
    assert not app.app.mail.transport.transport.messages


@pytest.mark.parametrize('change', ['owner', 'switch'])
def test_private_dispatch_rechecks_after_body(hosted, change):
    app = enabled(hosted)
    _, headers, people = invoke(app, '/api/people', cookie='EXISTING_OWNER_COOKIE=synthetic')
    owner = 'EXISTING_OWNER_COOKIE=synthetic; '+headers['Set-Cookie'].split(';')[0]
    body = {'action': 'preapprove', 'email': 'next@example.invalid', 'revision': None}
    class ChangingBody(BytesIO):
        def read(self, length):
            if change == 'owner':
                app.validator.valid = False
            else:
                hosted[1](people_write_enabled=False)
            return super().read(length)
    assert invoke(app, '/api/people', body, owner, people['csrf'], **{'wsgi.input': ChangingBody(json.dumps(body).encode())})[0] == 403
    assert not app.app.store.people()


def test_fixed_config_change_requires_restart_and_mail_checks_at_send(hosted):
    app = enabled(hosted)
    hosted[1](mail_enabled=False)
    with pytest.raises(Exception, match='paused'):
        app.app.mail.transport.send(object())
    assert not app.app.mail.transport.transport.messages
    config = json.loads(app.config_path.read_text())
    config['owner_identity'] = 'changed-owner'
    app.config_path.write_text(json.dumps(config))
    assert invoke(app, '/guest')[0] == 503


def test_registration_pause_after_body_prevents_challenge_and_mail(hosted):
    app = enabled(hosted)
    _, headers, result = invoke(app, '/api/access')
    cookie, csrf = headers['Set-Cookie'].split(';')[0], result['csrf']
    _, headers, result = invoke(app, '/api/invite', {'invitation': app.app.store.invitation()['token']}, cookie, csrf)
    cookie, csrf = headers['Set-Cookie'].split(';')[0], result['csrf']
    body = {'name': 'Guest', 'email': 'guest@example.invalid'}
    class ChangingBody(BytesIO):
        def read(self, length):
            hosted[1](mail_enabled=False)
            return super().read(length)
    assert invoke(app, '/api/register', body, cookie, csrf, **{'wsgi.input': ChangingBody(json.dumps(body).encode())})[0] == 403
    with app.app.store._transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM challenges').fetchone()[0] == 0
    assert not app.app.mail.transport.transport.messages


def test_pause_before_send_keeps_unsent_code_recoverable(hosted):
    app = enabled(hosted)
    store = app.app.store
    raw = store.browser(store.invitation()['token'])
    store.start(raw, 'Guest', 'guest@example.invalid')
    hosted[1](mail_enabled=False)
    from boomer_switch.website_mail import MailUnavailable
    with pytest.raises(MailUnavailable, match='paused'):
        app.app.mail.deliver()
    assert not app.app.mail.transport.transport.messages
    with store._transaction() as db:
        assert db.execute('SELECT code FROM mailbox').fetchone()[0]
        assert db.execute('SELECT COUNT(*) FROM mail_delivery').fetchone()[0] == 0
    hosted[1](mail_enabled=True)
    assert app.app.mail.deliver() == 1
    assert app.app.mail.deliver() == 0


def test_invalid_private_config_before_send_is_definitely_unsent(hosted):
    app = enabled(hosted)
    store = app.app.store
    raw = store.browser(store.invitation()['token'])
    store.start(raw, 'Guest', 'guest@example.invalid')
    saved = app.config_path.read_text()
    app.config_path.write_text('{')
    from boomer_switch.website_mail import MailUnavailable
    with pytest.raises(MailUnavailable, match='paused'):
        app.app.mail.deliver()
    assert not app.app.mail.transport.transport.messages
    app.config_path.write_text(saved)
    assert app.app.mail.deliver() == 1


def test_paused_unsent_owner_notice_is_delivered_once_after_resume(hosted):
    app = enabled(hosted)
    store = app.app.store
    raw = store.browser(store.invitation()['token'])
    store.start(raw, 'Guest', 'guest@example.invalid')
    store.verify(raw, store.mailbox('guest@example.invalid')['messages'][-1]['code'])
    hosted[1](mail_enabled=False)
    from boomer_switch.website_mail import MailUnavailable
    with pytest.raises(MailUnavailable, match='paused'):
        app.app.mail.deliver()
    assert not app.app.mail.transport.transport.messages
    hosted[1](mail_enabled=True)
    assert app.app.mail.deliver() == 1
    assert not app.app.mail.transport.transport.messages
    assert len(app.notice_log.read_text().splitlines()) == 1
    assert app.app.mail.deliver() == 0


@pytest.mark.parametrize('script,expected', [
    ("import sys; sys.stdout.buffer.write(b'MATCH' if sys.stdin.buffer.read()==b'synthetic' else b'DENY')", True),
    ("print('MATCH')", False),
    ("import sys,time; sys.stdout.write('MATCH'); sys.stdout.flush(); time.sleep(0.05); sys.stdout.write('x')", False),
    ("print('x'*100000)", False),
    ("import time; time.sleep(10)", False),
])
def test_private_validator_bounded_subprocess(script, expected):
    validator = PHPValidator([sys.executable, '-c', script], 'owner_cookie')
    assert validator.check({'HTTP_COOKIE': 'owner_cookie=synthetic'}) is expected
    assert not validator.check({'HTTP_COOKIE': 'unrelated=synthetic'})


def test_validator_parent_exit_with_inherited_pipe_is_bounded():
    script = ("import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(4)']); "
              "sys.stdout.buffer.write(b'MATCH'); sys.stdout.buffer.flush()")
    validator = PHPValidator([sys.executable, '-c', script], 'owner_cookie')
    started = time.monotonic()
    assert not validator.check({'HTTP_COOKIE': 'owner_cookie=synthetic'})
    assert time.monotonic()-started < 3.5


def test_hosting_package_inputs_are_complete():
    import tomllib
    config = tomllib.loads(Path('pyproject.toml').read_text())
    inputs = config['tool']['setuptools']['data-files']['share/boomer-switch/hosting']
    assert {'hosting/passenger_wsgi.py', 'hosting/passenger-config.example.json', 'hosting/index.php'} <= set(inputs)
    assert all(Path(item).is_file() for item in inputs)


def test_fleet_catalog_has_bounded_poll_only_admission(hosted):
    from boomer_switch.configuration import POLL_BYTES
    app = enabled(hosted)
    lights = [{"light_id": f"sample-{i}", "name": "\U0001f4a1" * 120, "room": "\U0001f4a1" * 120} for i in range(32)]
    body = {"agent_id": str(uuid.uuid4()), "sequence": 1, "request_id": str(uuid.uuid4()),
            "lights": lights, "catalog_revision": "c" * 64}
    assert 4096 < len(json.dumps(body)) < POLL_BYTES
    status, _, result = invoke(app, "/home/poll", body, HTTP_AUTHORIZATION="Bearer " + "k" * 40)
    assert status == 200 and result["operation"] is None
    status, _, _ = invoke(app, "/api/register", body)
    assert status == 413
    status, _, _ = invoke(app, "/home/poll", body, CONTENT_LENGTH=str(POLL_BYTES + 1))
    assert status == 413


def test_owner_replaces_invitation_once_and_prints_both_designs(hosted, tmp_path):
    from pypdf import PdfWriter, PdfReader
    from boomer_switch.registration import AccessDenied

    app = enabled(hosted)
    guest, guest_csrf = verified(app)
    store = app.app.store
    _, headers, people = invoke(app, '/api/people', cookie='EXISTING_OWNER_COOKIE=synthetic')
    owner = 'EXISTING_OWNER_COOKIE=synthetic; ' + headers['Set-Cookie'].split(';')[0]
    person = people['people'][0]
    assert invoke(app, '/api/people', {'action': 'approve', 'email': person['email'], 'revision': person['revision']}, owner, people['csrf'])[0] == 200
    old = store.invitation()
    unverified = store.browser(old['token'])
    store.start(unverified, 'Another sample', 'another@example.invalid')
    saved_people = store.people()
    messages = len(app.app.mail.transport.transport.messages)
    template = tmp_path / 'blank.pdf'
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(612, 792)
    writer.write(template)
    original = template.read_bytes()
    app.app.people.invitation_cards.template = template
    status, headers, invitation = invoke(app, '/api/invitation', cookie=owner)
    assert status == 200 and headers['Cache-Control'] == 'no-store'
    assert invitation['pdf_available'] is True
    body = {'revision': invitation['revision'], 'confirmation': 'replace'}
    for invalid in ({'revision': None, 'confirmation': 'replace'}, {'confirmation': 'replace'},
                    {'revision': invitation['revision'], 'confirmation': 'yes'}):
        assert invoke(app, '/api/invitation', invalid, owner, invitation['csrf'])[0] == 400
    assert invoke(app, '/api/invitation', body, owner, 'wrong')[0] == 403
    assert invoke(app, '/api/invitation', body, owner, invitation['csrf'], HTTP_ORIGIN='https://other.invalid')[0] == 403
    assert store.invitation() == old
    status, _, current = invoke(app, '/api/invitation', body, owner, invitation['csrf'])
    assert status == 200 and current['revision'] != invitation['revision']
    new = store.invitation()
    assert invoke(app, '/api/invitation', body, owner, invitation['csrf'])[0] == 409
    assert store.invitation() == new
    status, redirect, _ = invoke(app, '/s/' + old['alias'])
    assert status == 303 and redirect['Location'].endswith('/guest#invitation=')
    assert invoke(app, '/api/invite', {'invitation': old['token']}, guest, guest_csrf)[0] == 403
    status, redirect, _ = invoke(app, '/s/' + new['alias'])
    assert status == 303 and redirect['Location'].endswith('/guest#invitation=' + new['token'])
    with pytest.raises(AccessDenied):
        store.resolve_alias(old['alias'])
    with pytest.raises(AccessDenied):
        store.start(unverified, 'Another sample', 'another@example.invalid')
    assert invoke(app, '/api/access', cookie=guest)[2]['status'] == 'approved'
    assert store.people() == saved_people
    assert len(app.app.mail.transport.transport.messages) == messages
    status, _, card = invoke(app, '/invitation-card', cookie=owner)
    assert status == 200 and b'<svg' in card and new['alias'].encode() in card
    assert old['alias'].encode() not in card
    status, headers, pdf = invoke(app, '/invitation.pdf', cookie=owner)
    assert status == 200 and headers['Content-Type'] == 'application/pdf' and headers['Cache-Control'] == 'no-store'
    reader = PdfReader(BytesIO(pdf))
    assert len(reader.pages) == 2
    for page in reader.pages:
        text = ''.join(page.extract_text().split())
        assert 'https://example.invalid/house-lights/s/' + new['alias'] in text
    assert template.read_bytes() == original


def test_concurrent_owner_confirmations_commit_one_invitation(hosted):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    app = enabled(hosted)
    _, headers, state = invoke(app, '/api/invitation', cookie='EXISTING_OWNER_COOKIE=synthetic')
    owner = 'EXISTING_OWNER_COOKIE=synthetic; ' + headers['Set-Cookie'].split(';')[0]
    body = {'revision': state['revision'], 'confirmation': 'replace'}
    barrier = Barrier(2)
    original_prepare = app.app.people.invitation_cards.prepare
    def prepare(candidate):
        original_prepare(candidate)
        barrier.wait(timeout=5)
    app.app.people.invitation_cards.prepare = prepare
    def replace():
        return invoke(app, '/api/invitation', body, owner, state['csrf'])[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: replace(), range(2)))
    assert sorted(statuses) == [200, 409]
    assert app.app.store.invitation_revision() != state['revision']


def test_invitation_routes_require_current_owner_and_write_switch(hosted):
    app, switches = hosted
    enabled(hosted)
    original = app.app.store.invitation()
    for path in ('/api/invitation', '/invitation-card', '/invitation.pdf'):
        assert invoke(app, path)[0] == 403
        assert invoke(app, path, HTTP_REMOTE_USER='CONFIGURED_OWNER_IDENTITY')[0] == 403
        assert invoke(app, path, cookie='EXISTING_OWNER_COOKIE=synthetic', HTTP_X_HL_APACHE_PEER='')[0] == 403
    _, headers, state = invoke(app, '/api/invitation', cookie='EXISTING_OWNER_COOKIE=synthetic')
    owner = 'EXISTING_OWNER_COOKIE=synthetic; ' + headers['Set-Cookie'].split(';')[0]
    body = {'revision': state['revision'], 'confirmation': 'replace'}
    switches(people_write_enabled=False)
    assert invoke(app, '/api/invitation', body, owner, state['csrf'])[0] == 403
    switches(people_write_enabled=True)
    class PausedBody(BytesIO):
        def read(self, *args):
            switches(people_write_enabled=False)
            return super().read(*args)
    assert invoke(app, '/api/invitation', body, owner, state['csrf'], **{'wsgi.input': PausedBody(json.dumps(body).encode())})[0] == 403
    assert app.app.store.invitation() == original
    switches(people_write_enabled=True)
    def pause_after_render(candidate):
        switches(people_write_enabled=False)
    app.app.people.invitation_cards.prepare = pause_after_render
    assert invoke(app, '/api/invitation', body, owner, state['csrf'])[0] == 403
    assert app.app.store.invitation() == original


def test_bad_template_cannot_retire_current_invitation(hosted, tmp_path):
    app = enabled(hosted)
    _, headers, state = invoke(app, '/api/invitation', cookie='EXISTING_OWNER_COOKIE=synthetic')
    owner = 'EXISTING_OWNER_COOKIE=synthetic; ' + headers['Set-Cookie'].split(';')[0]
    original = app.app.store.invitation()
    app.app.people.invitation_cards.template = tmp_path / 'missing-private.pdf'
    assert invoke(app, '/api/invitation', {'revision': state['revision'], 'confirmation': 'replace'}, owner, state['csrf'])[0] == 400
    assert app.app.store.invitation() == original
