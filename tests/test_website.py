from io import BytesIO
from http.cookies import SimpleCookie
import json
import uuid

import pytest

from boomer_switch.registration import RegistrationStore
from boomer_switch.website import WebsiteApplication
from boomer_switch.website_handoff import WebsiteHandoff
from boomer_switch.website_mail import WebsiteMail, MailUnavailable


class FakeTransport:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    def send(self, message):
        self.messages.append(message)
        if self.fail:
            raise TimeoutError("secret provider detail")


def invoke(app, path, method='GET', body=None, cookie='', csrf='', **extra):
    data = json.dumps(body).encode() if body is not None else b''
    env = {'PATH_INFO': path, 'REQUEST_METHOD': method, 'wsgi.url_scheme': 'https',
           'CONTENT_TYPE': 'application/json', 'CONTENT_LENGTH': str(len(data)),
           'wsgi.input': BytesIO(data), 'HTTP_COOKIE': cookie, 'HTTP_ORIGIN': 'https://site.invalid',
           'HTTP_X_CSRF_TOKEN': csrf}
    env.update(extra)
    out = {}
    def start(status, headers, exc_info=None):
        out.update(status=int(status.split()[0]), headers=headers)
    payload = b''.join(app(env, start))
    return out['status'], dict(out['headers']), payload


@pytest.fixture
def hosted(tmp_path):
    store = RegistrationStore(tmp_path)
    transport = FakeTransport()
    mail = WebsiteMail(store, transport, 'sender@example.invalid', 'owner@example.invalid')
    handoff = WebsiteHandoff(store, 'k'*40, 'https://home.invalid/callback')
    app = WebsiteApplication(store, mail, origin='https://site.invalid', mount='/house-lights', owner_identity='owner', handoff=handoff)
    return app, store, transport


def guest(hosted):
    app, store, transport = hosted
    status, headers, _ = invoke(app, '/invite/'+store.invitation()['token'])
    assert status == 303 and headers['Location'] == '/house-lights/guest'
    cookie = headers['Set-Cookie'].split(';')[0]
    csrf = json.loads(invoke(app, '/api/access', cookie=cookie)[2])['csrf']
    return cookie, csrf


def verify(hosted):
    app, store, transport = hosted
    cookie, csrf = guest(hosted)
    assert invoke(app, '/api/register', 'POST', {'name': 'Guest', 'email': 'guest@example.invalid'}, cookie, csrf)[0] == 200
    import re
    code = re.search(r'\b[0-9]{6}\b', transport.messages[-1].get_content()).group()
    assert invoke(app, '/api/verify', 'POST', {'code': code}, cookie, csrf)[0] == 200
    return cookie, csrf


def test_real_flow_sends_guest_and_owner_via_transport_without_production_mailbox(hosted):
    app, store, transport = hosted
    cookie, _ = verify(hosted)
    assert [str(message['To']) for message in transport.messages] == ['guest@example.invalid', 'owner@example.invalid']
    assert json.loads(invoke(app, '/api/access', cookie=cookie)[2])['status'] == 'pending'
    assert store.mailbox('guest@example.invalid')['messages'][0].get('code') is None
    for route in ('/api/mailbox', '/app.js', '/api/lights', '/api/people', '/people', '/people.js'):
        assert invoke(app, route)[0] in (403, 404)
    assert app.mail.deliver() == 0


@pytest.mark.parametrize('extra', [{'wsgi.url_scheme': 'http'}, {'HTTP_ORIGIN': 'https://attacker.invalid'}, {'HTTP_X_CSRF_TOKEN': 'wrong'}])
def test_register_rejects_tls_origin_and_csrf_without_mail(hosted, extra):
    app, _, transport = hosted
    cookie, csrf = guest(hosted)
    assert invoke(app, '/api/register', 'POST', {'name':'Guest','email':'guest@example.invalid'}, cookie, csrf, **extra)[0] == 403
    assert not transport.messages


def test_ambiguous_delivery_is_claimed_once_and_redacted(hosted):
    app, store, transport = hosted
    transport.fail = True
    cookie, csrf = guest(hosted)
    result = invoke(app, '/api/register', 'POST', {'name':'Guest','email':'guest@example.invalid'}, cookie, csrf)
    assert result[0] == 503 and b'secret provider' not in result[2]
    assert app.mail.deliver() == 0 and len(transport.messages) == 1
    assert invoke(app, '/api/resend', 'POST', {}, cookie, csrf)[0] == 403
    with store._transaction() as db:
        assert db.execute('SELECT state FROM mail_delivery').fetchone()[0] == 'unknown'
        assert db.execute('SELECT code FROM mailbox').fetchone()[0] is None


def test_private_owner_auth_persists_across_wsgi_instances_and_requires_current_host_identity(hosted):
    app, store, _ = hosted
    status, headers, body = invoke(app, '/api/people', REMOTE_USER='owner')
    assert status == 200
    cookie = headers['Set-Cookie'].split(';')[0]
    csrf = json.loads(body)['csrf']
    other = WebsiteApplication(store, app.mail, origin=app.origin, mount=app.mount, owner_identity='owner')
    body = {'action':'preapprove','email':'guest@example.invalid','revision':None}
    assert invoke(other, '/api/people', 'POST', body, cookie, csrf, HTTP_REMOTE_USER='owner')[0] == 403
    assert invoke(other, '/api/people', 'POST', body, cookie, csrf, REMOTE_USER='owner')[0] == 200


def test_handoff_is_browser_challenge_bound_machine_only_current_and_idempotent(hosted):
    app, store, _ = hosted
    cookie, csrf = verify(hosted)
    store.operator('approve', 'guest@example.invalid')
    body = {'challenge':'c'*43,'state':'s'*43}
    result = invoke(app, '/api/handoff', 'POST', body, cookie, csrf)
    assert result[0] == 200
    from urllib.parse import urlsplit, parse_qs
    redirect = json.loads(result[2])['redirect']
    assert redirect.startswith('https://home.invalid/callback?')
    code = parse_qs(urlsplit(redirect).query)['code'][0]
    exchange = {'code':code,'challenge':'c'*43,'request_id':str(uuid.uuid4())}
    assert invoke(app, '/exchange', 'POST', exchange)[0] == 403
    auth = {'HTTP_AUTHORIZATION':'Bearer '+'k'*40}
    first = invoke(app, '/exchange', 'POST', exchange, **auth)
    assert first[0] == 200
    assert invoke(app, '/exchange', 'POST', exchange, **auth)[2] == first[2]
    assert invoke(app, '/exchange', 'POST', exchange | {'challenge':'x'*43}, **auth)[0] == 403
    assert invoke(app, '/exchange', 'POST', exchange | {'request_id':str(uuid.uuid4())}, **auth)[0] == 403
    store.operator('revoke', 'guest@example.invalid')
    assert invoke(app, '/exchange', 'POST', exchange, **auth)[0] == 403


def test_smtp_uses_certificate_verified_tls_before_login_and_closes_on_send(monkeypatch):
    from boomer_switch import website_mail
    from email.message import EmailMessage
    calls = []
    class SMTP:
        def __init__(self, host, port, timeout):
            calls.append(('connect', timeout))
        def starttls(self, context):
            import ssl
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            calls.append('tls')
        def login(self, username, password):
            calls.append('login')
        def send_message(self, message):
            calls.append('send')
        def close(self):
            calls.append('close')
    monkeypatch.setattr(website_mail.smtplib, 'SMTP', SMTP)
    website_mail.SMTPTransport('smtp.invalid', 587, 'user', 'secret').send(EmailMessage())
    assert calls == [('connect', 5), 'tls', 'login', 'send', 'close']


def test_sendmail_has_absolute_fixed_command_timeout_and_no_shell(monkeypatch):
    from boomer_switch import website_mail
    from email.message import EmailMessage
    from pathlib import Path
    calls = []
    monkeypatch.setattr(website_mail.subprocess, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
    executable = str(Path('sendmail').resolve())
    website_mail.SendmailTransport(executable).send(EmailMessage())
    args, kwargs = calls[0]
    assert args[0] == [executable, '-i', '-t']
    assert kwargs['timeout'] == 5 and kwargs['check'] is True
    assert 'shell' not in kwargs
    with pytest.raises(ValueError):
        website_mail.SendmailTransport('sendmail')
