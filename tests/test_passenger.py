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
        def __init__(self, executable):
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
