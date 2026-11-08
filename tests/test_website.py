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
    store = RegistrationStore(tmp_path, mode="production")
    transport = FakeTransport()
    mail = WebsiteMail(store, transport, 'sender@example.invalid', 'owner@example.invalid')
    handoff = WebsiteHandoff(store, 'k'*40, 'https://home.invalid/callback')
    app = WebsiteApplication(store, mail, origin='https://site.invalid', mount='/house-lights', owner_identity='owner', handoff=handoff)
    return app, store, transport


def guest(hosted):
    app, store, transport = hosted
    status, headers, _ = invoke(app, '/invite/'+store.invitation()['token'])
    assert status == 303 and headers['Location'].startswith('/house-lights/guest#invitation=')
    assert 'Set-Cookie' not in headers
    _, bootstrap, payload = invoke(app, '/api/access')
    cookie = bootstrap['Set-Cookie'].split(';')[0]
    csrf = json.loads(payload)['csrf']
    _, headers, payload = invoke(app, '/api/invite', 'POST', {'invitation':store.invitation()['token']}, cookie, csrf)
    cookie = headers.get('Set-Cookie', cookie).split(';')[0]
    csrf = json.loads(payload)['csrf']
    return cookie, csrf


def test_invitation_preview_is_stateless_and_bootstrap_acceptance_is_idempotent(hosted):
    app, store, transport = hosted
    token = store.invitation()['token']
    for path in ('/guest', '/invite/'+token, '/s/'+store.invitation()['alias']):
        status, headers, _ = invoke(app, path, HTTP_SEC_FETCH_SITE='cross-site')
        assert status in (200, 303) and 'Set-Cookie' not in headers
        assert headers['Referrer-Policy'] == 'no-referrer'
    with store._transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
    cookie, csrf = guest(hosted)
    status, headers, payload = invoke(app, '/api/invite', 'POST', {'invitation':token}, cookie, csrf)
    assert status == 200 and 'Set-Cookie' not in headers and json.loads(payload)['status'] == 'invited'
    assert not transport.messages
    assert invoke(app, '/api/register', 'POST', {'name':'Guest','email':'guest@example.invalid'}, cookie, csrf)[0] == 200
    before = len(transport.messages)
    assert json.loads(invoke(app, '/api/invite', 'POST', {'invitation':token}, cookie, csrf)[2])['status'] == 'verify'
    assert invoke(app, '/api/register', 'POST', {'name':'Guest','email':'guest@example.invalid'}, cookie, csrf)[0] == 403
    assert len(transport.messages) == before == 1


@pytest.mark.parametrize('invalid', [None, True, '', 'retired', '\u2603'])
def test_invalid_invitation_acknowledgement_never_grants_mail_and_preserves_approved(hosted, invalid):
    app, store, transport = hosted
    cookie, csrf = guest(hosted)
    status, headers, payload = invoke(app, '/api/invite', 'POST', {'invitation':invalid}, cookie, csrf)
    assert status == 403 and json.loads(payload)['code'] == 'invitation_unavailable'
    assert 'Max-Age=0' in headers['Set-Cookie']
    assert invoke(app, '/api/register', 'POST', {'name':'Guest','email':'guest@example.invalid'}, cookie, csrf)[0] == 403
    assert not transport.messages
    cookie, csrf = verify(hosted)
    store.operator('approve', 'guest@example.invalid')
    before = len(transport.messages)
    status, headers, _ = invoke(app, '/api/invite', 'POST', {'invitation':invalid}, cookie, csrf)
    assert status == 403 and 'Set-Cookie' not in headers
    assert json.loads(invoke(app, '/api/access', cookie=cookie)[2])['status'] == 'approved'
    assert len(transport.messages) == before


def test_shared_browser_invitation_state_machine_without_a_browser():
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for the optional pure JavaScript state-machine checks.')
    root = Path(__file__).parents[1]
    result = subprocess.run([node, str(root/'tests/entry_cases.cjs'), str(root/'boomer_switch/web/entry.js')],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


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
        def send_message(self, message, **envelope):
            assert envelope == {"from_addr": "sender@example.invalid", "to_addrs": ["guest@example.invalid"]}
            calls.append('send')
        def close(self):
            calls.append('close')
    monkeypatch.setattr(website_mail.smtplib, 'SMTP', SMTP)
    message = EmailMessage()
    message['From'], message['To'] = 'sender@example.invalid', 'guest@example.invalid'
    website_mail.SMTPTransport('smtp.invalid', 587, 'user', 'secret').send(message)
    assert calls == [('connect', 5), 'tls', 'login', 'send', 'close']


def test_sendmail_has_absolute_fixed_command_timeout_and_no_shell(monkeypatch):
    from boomer_switch import website_mail
    from email.message import EmailMessage
    from pathlib import Path
    calls = []
    monkeypatch.setattr(website_mail.subprocess, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
    executable = str(Path('sendmail').resolve())
    message = EmailMessage()
    message['From'], message['To'] = 'sender@example.invalid', 'guest@example.invalid'
    website_mail.SendmailTransport(executable).send(message)
    args, kwargs = calls[0]
    assert args[0] == [executable, '-i', '-f', 'sender@example.invalid', '--', 'guest@example.invalid']
    assert kwargs['timeout'] == 5 and kwargs['check'] is True
    assert 'shell' not in kwargs
    with pytest.raises(ValueError):
        website_mail.SendmailTransport('sendmail')


@pytest.mark.parametrize('email', ['postmaster,guest@example.invalid', 'Guest <guest@example.invalid>', 'guest@example.invalid;other', 'guest(comment)@example.invalid', 'guest@example.invalid\r\nBcc: extra@example.invalid'])
def test_single_mailbox_validation_rejects_header_recipient_expansion(hosted, email):
    app, _, transport = hosted
    cookie, csrf = guest(hosted)
    assert invoke(app, '/api/register', 'POST', {'name':'Guest','email':email}, cookie, csrf)[0] == 403
    assert not transport.messages
    with pytest.raises(ValueError):
        WebsiteMail(app.store, transport, email, 'owner@example.invalid')
    with pytest.raises(ValueError):
        WebsiteMail(app.store, transport, 'sender@example.invalid', email)


def test_cross_site_guest_bootstrap_does_not_replace_approved_strict_cookie(hosted):
    app, store, _ = hosted
    cookie, _ = verify(hosted)
    store.operator('approve', 'guest@example.invalid')
    status, headers, _ = invoke(app, '/guest', HTTP_SEC_FETCH_SITE='cross-site')
    assert status == 200 and 'Set-Cookie' not in headers
    status, headers, payload = invoke(app, '/api/access', cookie=cookie)
    assert status == 200 and json.loads(payload)['status'] == 'approved'
    assert 'Set-Cookie' not in headers
    status, headers, payload = invoke(app, '/api/access')
    assert status == 200 and 'Set-Cookie' in headers
    assert json.loads(payload)['status'] == 'anonymous'


def test_live_transport_refuses_existing_demo_mail(tmp_path):
    store = RegistrationStore(tmp_path)
    raw = store.browser(store.invitation()['token'])
    store.start(raw, 'Demo', 'demo@example.invalid')
    transport = FakeTransport()
    with pytest.raises(ValueError, match='Historical demo mail'):
        WebsiteMail(store, transport, 'sender@example.invalid', 'owner@example.invalid')
    assert not transport.messages


def test_expired_code_is_discarded_without_external_send(hosted):
    app, store, transport = hosted
    raw = store.browser(store.invitation()['token'])
    store.start(raw, 'Guest', 'guest@example.invalid')
    now = store.clock()
    store.clock = lambda: now + 601
    assert app.mail.deliver() == 0 and not transport.messages
    with store._transaction() as db:
        assert db.execute('SELECT state FROM mail_delivery').fetchone()[0] == 'discarded'
        assert db.execute('SELECT code FROM mailbox').fetchone()[0] is None


def test_anonymous_bootstrap_reuses_prunes_and_bounds_public_state(hosted):
    app, store, _ = hosted
    _, headers, _ = invoke(app, '/api/access')
    cookie = headers['Set-Cookie'].split(';')[0]
    assert 'Set-Cookie' not in invoke(app, '/api/access', cookie=cookie)[1]
    with store._transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1
        db.executemany('INSERT INTO sessions(id,expires) VALUES (?,?)', [(str(i), store.clock()+100) for i in range(999)])
    assert invoke(app, '/api/access')[0] == 403
    now = store.clock()
    store.clock = lambda: now + 86401
    assert invoke(app, '/api/access')[0] == 200
    with store._transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1


def test_production_store_rejects_demo_preapproval_and_demo_rejects_live_state(tmp_path):
    demo = RegistrationStore(tmp_path / 'demo')
    demo.change_person('preapprove', 'guest@example.invalid', None)
    with pytest.raises(ValueError):
        RegistrationStore(tmp_path / 'demo', mode='production')
    live = RegistrationStore(tmp_path / 'live', mode='production')
    with pytest.raises(ValueError):
        RegistrationStore(tmp_path / 'live')
    assert live.people() == []


def test_valid_card_cross_site_return_preserves_approved_session(hosted):
    app, store, _ = hosted
    cookie, csrf = verify(hosted)
    store.operator('approve', 'guest@example.invalid')
    status, headers, _ = invoke(app, '/invite/'+store.invitation()['token'], HTTP_SEC_FETCH_SITE='cross-site')
    assert status == 303 and 'Set-Cookie' not in headers
    result = invoke(app, '/api/invite', 'POST', {'invitation':store.invitation()['token']}, cookie, csrf)
    assert result[0] == 200 and 'Set-Cookie' not in result[1]
    assert json.loads(result[2])['status'] == 'approved'


def test_public_recovery_does_not_disclose_approval_membership(hosted):
    app, store, transport = hosted
    store.change_person('preapprove', 'approved@example.invalid', None)
    outcomes = []
    for email in ('approved@example.invalid', 'unknown@example.invalid'):
        _, headers, payload = invoke(app, '/api/access')
        cookie = headers['Set-Cookie'].split(';')[0]
        csrf = json.loads(payload)['csrf']
        start = invoke(app, '/api/register', 'POST', {'name':'Guest','email':email}, cookie, csrf)
        wrong = invoke(app, '/api/verify', 'POST', {'code':'wrong'}, cookie, csrf)
        resend = invoke(app, '/api/resend', 'POST', {}, cookie, csrf)
        outcomes.append((start[0],json.loads(start[2])['status'],wrong[0],wrong[2],resend[0],resend[2]))
    assert outcomes[0] == outcomes[1]
    assert len(transport.messages) == 1 and transport.messages[0]['To'] == 'approved@example.invalid'


def test_actual_cgi_entry_uses_fake_mail_and_current_source_rendezvous(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    import re
    from urllib.parse import urlsplit, parse_qs
    from boomer_switch.registration import RegistrationStore
    config = {'state_directory':str(tmp_path / 'state'), 'origin':'https://site.invalid', 'mount':'/house-lights',
              'owner_identity':'owner', 'service_key':'k'*40, 'home_callback':'https://home.invalid/auth/callback', 'home_entry':'https://home.invalid/auth/start',
              'mail':{'transport':'smtp','host':'smtp.invalid','port':587,'sender':'sender@example.invalid','owner':'owner@example.invalid'}}
    private = tmp_path / 'config.local.json'
    private.write_text(json.dumps(config))
    inbox = tmp_path / 'fake-inbox.jsonl'
    script = "import runpy,os,json; from pathlib import Path; from boomer_switch.website_mail import SMTPTransport; SMTPTransport.send=lambda self,message: Path(os.environ['FAKE_INBOX']).open('a').write(json.dumps({'to':str(message['To']),'body':message.get_content()})+'\\n'); runpy.run_module('boomer_switch.website_cgi',run_name='__main__')"
    source_a = str(__import__('ipaddress').IPv4Address(134744072))
    source_b = str(__import__('ipaddress').ip_address(source_a)+1)
    def cgi(path, method='GET', body=None, cookie='', csrf='', owner=False, auth='', source=None):
        data = json.dumps(body).encode() if body is not None else b''
        env = dict(os.environ, BOOMER_PRIVATE_CONFIG=str(private), BOOMER_HTTPS='1', FAKE_INBOX=str(inbox),
                   BOOMER_DIRECT_SOURCE='1', REMOTE_ADDR=source or source_a,
                   REQUEST_METHOD=method, PATH_INFO=path, SCRIPT_NAME='/house-lights', CONTENT_TYPE='application/json',
                   CONTENT_LENGTH=str(len(data)), HTTP_COOKIE=cookie, HTTP_ORIGIN='https://site.invalid',
                   HTTP_X_CSRF_TOKEN=csrf, HTTP_AUTHORIZATION=auth)
        env.pop('REMOTE_USER', None)
        if owner:
            env['REMOTE_USER'] = 'owner'
        response = subprocess.run([sys.executable,'-c',script], input=data, capture_output=True, env=env, timeout=10, check=True)
        head, payload = response.stdout.split(b'\r\n\r\n',1)
        lines = head.decode().split('\r\n')
        headers = dict(line.split(': ',1) for line in lines)
        return int(headers['Status'].split()[0]), headers, json.loads(payload)
    _, headers, access = cgi('/api/access')
    cookie, csrf = headers['Set-Cookie'].split(';')[0], access['csrf']
    store = RegistrationStore(Path(config['state_directory']), mode='production')
    _, headers, access = cgi('/api/invite','POST',{'invitation':store.invitation()['token']},cookie,csrf)
    cookie, csrf = headers['Set-Cookie'].split(';')[0], access['csrf']
    assert cgi('/api/register','POST',{'name':'Guest','email':'guest@example.invalid'},cookie,csrf)[0] == 200
    delivered = json.loads(inbox.read_text().splitlines()[0])
    code = re.search(r'\b[0-9]{6}\b',delivered['body']).group()
    assert cgi('/api/verify','POST',{'code':code},cookie,csrf)[2]['status'] == 'pending'
    _, owner_headers, people = cgi('/api/people', owner=True)
    owner_cookie = owner_headers['Set-Cookie'].split(';')[0]
    person = people['people'][0]
    assert cgi('/api/people','POST',{'action':'approve','email':person['email'],'revision':person['revision']},owner_cookie,people['csrf'],True)[0] == 200
    agent = str(uuid.uuid4())
    def home(path, sequence, assignment=None, source=None, outcome=None):
        body = {'agent_id':agent,'sequence':sequence,'request_id':str(uuid.uuid4())}
        if path=='poll':
            body['light']={'light_id':'selected-lamp','name':'Selected lamp','room':'Guest room'}
        if assignment:
            body |= {'operation_id':assignment['operation_id'],'generation':assignment['generation']}
        if outcome:
            body['outcome'] = outcome
        return cgi('/home/'+path,'POST',body,auth='Bearer '+'k'*40,source=source)
    assert cgi('/api/access',cookie=cookie)[2]['home_ready'] is False
    assert home('poll',1)[0] == 200
    assert cgi('/api/access',cookie=cookie)[2]['home_ready'] is True
    operation_id = str(uuid.uuid4())
    assert cgi('/api/lights','POST',{'operation_id':operation_id,'action':'read'},cookie,csrf)[0] == 202
    assignment = home('poll',2)[2]['operation']
    assert home('authorize',2,assignment)[2]['allowed'] is True
    assert home('result',2,assignment,outcome={'status':'confirmed','state':'off'})[2]['accepted'] is True
    assert cgi('/api/operations/'+operation_id,cookie=cookie)[2]['state']=='off'
    assert cgi('/api/access',cookie=cookie,source=source_b)[2]['home_ready'] is False
    assert home('poll',3,source=source_b)[0]==200
    assert cgi('/api/operations/'+operation_id,cookie=cookie)[0]==403
    access = cgi('/api/access',cookie=cookie,source=source_b)[2]
    assert access['status']=='approved' and access['home_ready'] is True
    new_id = str(uuid.uuid4())
    assert cgi('/api/lights','POST',{'operation_id':new_id,'action':'read'},cookie,csrf,source=source_b)[0]==202
    assignment = home('poll',4,source=source_b)[2]['operation']
    store.operator('revoke','guest@example.invalid')
    assert home('authorize',4,assignment,source_b)[2]['allowed'] is False
    assert cgi('/grant','POST',{},auth='Bearer '+'k'*40)[0]==404
    assert cgi('/exchange','POST',{},auth='Bearer '+'k'*40)[0]==404
    assert [json.loads(line)['to'] for line in inbox.read_text().splitlines()] == ['guest@example.invalid']


def test_card_origin_approved_guest_receives_only_fixed_home_entry(hosted):
    app, store, _ = hosted
    cookie, _ = verify(hosted)
    store.operator('approve', 'guest@example.invalid')
    result = json.loads(invoke(app, '/api/access', cookie=cookie)[2])
    assert result['home_entry'] == 'https://home.invalid/start'
    assert 'home_entry' not in json.loads(invoke(app, '/api/access')[2])
    assert invoke(app, '/api/handoff', 'POST', {'challenge':'c'*43,'state':'s'*43,'callback':'https://other.invalid/'},cookie,result['csrf'])[0] == 400


def test_native_php_bridge_runs_real_cgi_and_keeps_site_owner_cookie_private(tmp_path, php_runtime):
    import os
    from pathlib import Path
    import socket
    import subprocess
    import sys
    import time
    from http.client import HTTPConnection
    php = php_runtime
    root = Path(__file__).resolve().parents[1]
    release = tmp_path / 'release'
    package = release / 'boomer_switch'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('__path__.append('+repr(str(root / 'boomer_switch'))+')')
    inbox, capture = tmp_path / 'fake-inbox.jsonl', tmp_path / 'captured-env.jsonl'
    wrapper = '''import importlib.util,json,os
from pathlib import Path
from boomer_switch.website_mail import SMTPTransport
config=json.loads(Path(os.environ['BOOMER_PRIVATE_CONFIG']).read_text())
with Path(config['capture']).open('a') as target:
 target.write(json.dumps({'cookie':os.environ.get('HTTP_COOKIE'),'owner':os.environ.get('REMOTE_USER'),'peer':os.environ.get('REMOTE_ADDR'),'direct':os.environ.get('BOOMER_DIRECT_SOURCE')})+'\\n')
def fake_send(self,message):
 with Path(config['fake_inbox']).open('a') as target:
  target.write(json.dumps({'to':str(message['To']),'body':message.get_content()})+'\\n')
SMTPTransport.send=fake_send
spec=importlib.util.spec_from_file_location('boomer_switch._actual_cgi',config['cgi_module'])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
raise SystemExit(module.main())
'''
    (package / 'website_cgi.py').write_text(wrapper)
    config = {'state_directory':str(tmp_path / 'state'), 'origin':'https://site.invalid','mount':'/lights','owner_identity':'owner',
              'service_key':'k'*40,'home_callback':'https://home.invalid/auth/callback','home_entry':'https://home.invalid/auth/start',
              'mail':{'transport':'smtp','host':'smtp.invalid','port':587,'sender':'sender@example.invalid','owner':'owner@example.invalid'},
              'fake_inbox':str(inbox),'capture':str(capture),'cgi_module':str(root / 'boomer_switch/website_cgi.py')}
    private = tmp_path / 'config.local.json'
    private.write_text(json.dumps(config))
    def php_string(value):
        return "'"+str(value).replace('\\','\\\\').replace("'","\\'")+"'"
    bridge_config = tmp_path / 'config.local.php'
    bridge_config.write_text("<?php return ["+','.join(php_string(key)+'=>'+php_string(value) for key,value in {
        'python':sys.executable,'release_directory':str(release),'python_config':str(private),'mount':'/lights','owner_identity':'owner'}.items())+
        ",'direct_source'=>true,'owner_authenticated'=>static fn()=>($_COOKIE['site_owner']??'')==='PRIVATE_OWNER_SENTINEL'];")
    router = tmp_path / 'router.php'
    router.write_text("<?php $_SERVER['HTTPS']='on'; require "+php_string(root / 'hosting/index.php')+";")
    with socket.socket() as listener:
        listener.bind(('localhost',0))
        port = listener.getsockname()[1]
    env = dict(os.environ, BOOMER_BRIDGE_CONFIG=str(bridge_config))
    process = subprocess.Popen([str(php),'-n','-S',f'localhost:{port}',str(router)],cwd=tmp_path,env=env,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    def http(path, body=None, cookie='', csrf='', extra=None):
        connection = HTTPConnection('localhost',port,timeout=5)
        headers = {'Cookie':cookie,'Origin':'https://site.invalid','X-CSRF-Token':csrf,'Content-Type':'application/json'}
        headers.update(extra or {})
        try:
            connection.request('POST' if body is not None else 'GET','/lights'+path,json.dumps(body) if body is not None else None,headers)
            response = connection.getresponse()
            return response.status,dict(response.getheaders()),json.loads(response.read())
        finally:
            connection.close()
    try:
        for _ in range(100):
            try:
                status,headers,access = http('/api/access')
                break
            except (OSError, ValueError):
                if process.poll() is not None:
                    raise AssertionError('The synthetic PHP host exited.')
                time.sleep(.05)
        else:
            raise AssertionError('The synthetic PHP host did not start.')
        assert status == 200
        cookie,csrf = headers['Set-Cookie'].split(';')[0],access['csrf']
        store = RegistrationStore(Path(config['state_directory']),mode='production')
        status,headers,access = http('/api/invite',{'invitation':store.invitation()['token']},cookie,csrf)
        assert status == 200
        cookie,csrf = headers['Set-Cookie'].split(';')[0],access['csrf']
        assert http('/api/register',{'name':'Guest','email':'guest@example.invalid'},cookie,csrf)[0] == 200
        assert json.loads(inbox.read_text().splitlines()[0])['to'] == 'guest@example.invalid'
        assert http('/api/people',cookie=cookie+'; site_owner=PRIVATE_OWNER_SENTINEL; unrelated=PRIVATE_OTHER_SENTINEL')[0] == 200
        assert http('/api/people',cookie=cookie+'; bs_owner=malformed; unrelated=PRIVATE_OTHER_SENTINEL')[0] == 403
        captured = capture.read_text()
        assert 'PRIVATE_OWNER_SENTINEL' not in captured and 'PRIVATE_OTHER_SENTINEL' not in captured and 'malformed' not in captured
        assert any(json.loads(line)['owner']=='owner' for line in captured.splitlines())
        import ipaddress
        normal = json.loads(captured.splitlines()[0])
        assert normal['direct']=='1' and ipaddress.ip_address(normal['peer']).is_loopback
        assert http('/api/access',cookie=cookie,extra={'X-Forwarded-For':'forged-source'})[0]==200
        rejected = json.loads(capture.read_text().splitlines()[-1])
        assert rejected['direct']=='0' and rejected['peer']==normal['peer']
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_hosted_fixture_idle_bare_tcp_does_not_block_https_or_shutdown(tmp_path):
    import importlib
    from http.client import HTTPSConnection
    import socket
    import ssl
    from threading import Event, Thread
    pytest.importorskip('playwright.sync_api', reason='This fixture check requires the optional demo dependencies.')
    pytest.importorskip('PIL.Image', reason='This fixture check requires the optional demo dependencies.')
    pytest.importorskip('zxingcpp', reason='This fixture check requires the optional demo dependencies.')
    demo = importlib.import_module('demos.hosted_website_acceptance')
    accepted = Event()
    class ProbeServer(demo.FixtureServer):
        def get_request(self):
            result = super().get_request()
            accepted.set()
            return result
    cert, key = demo.make_certificate(tmp_path)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    def app(environ, start):
        start('200 OK', [('Content-Type','text/plain'),('Content-Length','5')])
        return [b'ready']
    server = demo.make_server(demo.LOOPBACK,0,app,server_class=ProbeServer,handler_class=demo.QuietHandler)
    server.enable_tls(tls)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    idle = socket.create_connection((demo.LOOPBACK,server.server_port),timeout=2)
    connection = HTTPSConnection('localhost',server.server_port,timeout=2,context=ssl.create_default_context(cafile=str(cert)))
    cleanup = Thread(target=lambda: (server.shutdown(), server.server_close()), daemon=True)
    try:
        assert accepted.wait(2), 'The fixture blocked before it accepted bare TCP.'
        connection.request('GET','/')
        response = connection.getresponse()
        assert response.status == 200 and response.read() == b'ready'
        # Keep bare TCP open while the server stops. No ClientHello is sent.
        cleanup.start()
        cleanup.join(timeout=2)
        assert not cleanup.is_alive(), 'The fixture shutdown waited for a TLS handshake.'
        worker.join(timeout=2)
        assert not worker.is_alive()
    finally:
        connection.close()
        idle.close()
        if cleanup.ident is None:
            cleanup.start()
        cleanup.join(timeout=3)
        worker.join(timeout=3)


@pytest.mark.parametrize("condition, expected", [
    ("home", "ready"), ("different_source", "source_mismatch"),
    ("offline", "unavailable"), ("unknown_source", "unavailable"),
])
def test_approved_refresh_distinguishes_known_mismatch_from_unavailable(hosted, condition, expected):
    from boomer_switch.home_rendezvous import HomeRendezvous
    from test_rendezvous import A, CELL, LIGHT, KEY, AGENT, env

    app, store, _ = hosted
    cookie, _ = verify(hosted)
    store.operator("approve", "guest@example.invalid")
    app.rendezvous = HomeRendezvous(store, KEY)
    now = store.clock()
    store.clock = lambda: now
    app.rendezvous.access(cookie.split("=", 1)[1], env())
    app.rendezvous.machine(env(), "/home/poll", {
        "agent_id": AGENT, "sequence": 1, "request_id": str(uuid.uuid4()), "light": LIGHT})
    if condition == "offline":
        store.clock = lambda: now + app.rendezvous.idle + app.rendezvous.FRESH
    source = CELL if condition in ("different_source", "offline") else A
    peer = env(source)
    peer.pop("HTTP_AUTHORIZATION")
    if condition == "unknown_source":
        peer.pop("boomer.direct_peer")
    status, _, payload = invoke(app, "/api/access", cookie=cookie, **peer)
    result = json.loads(payload)
    assert status == 200 and result["status"] == "approved"
    assert result["home_status"] == expected
    assert result["home_ready"] is (expected == "ready")
    assert ("light" in result) is (expected == "ready")
    assert A not in payload.decode() and CELL not in payload.decode()
    store.operator("revoke", "guest@example.invalid")
    result = json.loads(invoke(app, "/api/access", cookie=cookie, **peer)[2])
    assert "home_status" not in result and "light" not in result


def test_verification_mail_states_ten_minute_lifetime(hosted):
    app, store, transport = hosted
    raw = store.browser(store.invitation()["token"])
    store.start(raw, "Guest", "guest@example.invalid")
    assert app.mail.deliver() == 1
    assert "expires in 10 minutes" in transport.messages[-1].get_content()
