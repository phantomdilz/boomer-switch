from io import BytesIO
import json
import hashlib
from pathlib import Path
import re
import runpy
import subprocess
import sys

import pytest

from boomer_switch.website_passenger import PassengerApplication


ROOT = Path(__file__).resolve().parents[1]
KEY = 'd'*40


def invoke(app, path, body=None, cookie='', csrf='', **extra):
    data = json.dumps(body).encode() if body is not None else b''
    env = {'PATH_INFO': path, 'SCRIPT_NAME': '/house-lights', 'REQUEST_METHOD': 'POST' if body is not None else 'GET',
           'wsgi.url_scheme': 'https', 'HTTP_HOST': 'site.invalid', 'REMOTE_ADDR': 'localhost',
           'HTTP_X_HL_APACHE_PEER': 'localhost', 'CONTENT_TYPE': 'application/json', 'CONTENT_LENGTH': str(len(data)),
           'wsgi.input': BytesIO(data), 'HTTP_ORIGIN': 'https://site.invalid', 'HTTP_COOKIE': cookie,
           'HTTP_X_CSRF_TOKEN': csrf}
    # The synthetic transport supplies equal valid address metadata; no sockets are opened.
    import ipaddress
    env['REMOTE_ADDR'] = env['HTTP_X_HL_APACHE_PEER'] = str(ipaddress.IPv4Address(134744072))
    env.update(extra)
    result = {}
    def start(status, headers, exc_info=None):
        result.update(status=int(status.split()[0]), headers=dict(headers))
    payload = b''.join(app(env, start))
    return result['status'], result['headers'], json.loads(payload)


@pytest.fixture
def integration(tmp_path, monkeypatch, php_runtime):
    class Mail:
        def __init__(self, path): self.messages = []
        def send(self, message): self.messages.append(message)
    monkeypatch.setattr('boomer_switch.website_cgi.SendmailTransport', Mail)
    capture = tmp_path / 'telegram.jsonl'
    module = tmp_path / 'owner_telegram.php'
    module.write_bytes((ROOT/'hosting/owner_telegram.php').read_bytes())
    cfg = {'allowed_id': 1, 'token': '1:'+'synthetic'*4}
    settings = {'enabled': True, 'owner_chat_id': 1, 'decision_key': KEY,
                'decision_url': 'https://site.invalid/house-lights/owner/decision',
                'people_url': 'https://site.invalid/house-lights/people'}
    def php_json(path, value):
        path.write_text('<?php return json_decode('+json.dumps(json.dumps(value))+', true);', encoding='utf-8')
    bot_cfg, service_cfg = tmp_path/'bot.php', tmp_path/'service.php'
    php_json(bot_cfg, cfg); php_json(service_cfg, settings)
    cli = (ROOT/'hosting/owner_telegram_cli.php').read_text().replace('hl_notice($cfg, $notice);', 'hl_notice($cfg, $notice, "synthetic_post");')
    capture_php = json.dumps(str(capture))
    cli += '\nfunction synthetic_post($url,$body,$headers) { file_put_contents('+capture_php+',json_encode(["url"=>$url,"body"=>$body])."\\n",FILE_APPEND);return ["ok"=>true,"result"=>["message_id"=>1,"chat"=>["id"=>1,"type"=>"private"]]]; }\n'
    notice_cli = tmp_path/'notice.php'
    notice_cli.write_text(cli, encoding='utf-8')
    config = json.loads((ROOT/'hosting/passenger-config.example.json').read_text())
    config.update(state_directory=str(tmp_path/'state'), origin='https://site.invalid', service_key='k'*40,
                  owner_notifications={'command':[str(php_runtime),'-n','-f',str(notice_cli),'--',str(bot_cfg),str(service_cfg)],'decision_key':KEY})
    config['passenger'].update({key:True for key in ('registration_enabled','mail_enabled','people_write_enabled','pilot_enabled','apache_peer_verified')})
    config['passenger']['owner_validator_command'] = [sys.executable]
    config['mail']['executable'] = sys.executable
    path = tmp_path/'config.local.json'; path.write_text(json.dumps(config))
    app = PassengerApplication(path)
    return app, path, capture, php_runtime, cfg, settings, tmp_path


def pending(integration):
    app, path, capture, *_ = integration
    _, headers, access = invoke(app, '/api/access')
    cookie, csrf = headers['Set-Cookie'].split(';')[0], access['csrf']
    assert invoke(app, '/api/register', {'name':'Guest','email':'unknown@example.invalid'}, cookie, csrf)[0] == 200
    assert not app.app.mail.transport.transport.messages and not capture.exists()
    _, headers, access = invoke(app, '/api/invite', {'invitation':app.app.store.invitation()['token']}, cookie, csrf)
    cookie, csrf = headers['Set-Cookie'].split(';')[0], access['csrf']
    assert invoke(app, '/api/register', {'name':'Guest','email':'guest@example.invalid'}, cookie, csrf)[0] == 200
    emails = app.app.mail.transport.transport.messages
    assert len(emails) == 1 and str(emails[0]['To']) == 'guest@example.invalid' and not capture.exists()
    code = re.search(r'\b[0-9]{6}\b', emails[0].get_content()).group()
    assert invoke(app, '/api/verify', {'code':code}, cookie, csrf)[2]['status'] == 'pending'
    assert len(emails) == 1
    messages = [json.loads(line) for line in capture.read_text().splitlines()]
    assert len(messages) == 1
    message = messages[0]['body']
    assert message['chat_id'] == 1 and message['reply_markup']['inline_keyboard'][1][0]['url'].endswith('/people')
    assert app.app.mail.deliver() == 0
    return cookie, csrf, message


@pytest.mark.parametrize('action,expected', [('a','approved'), ('d','denied')])
def test_production_guest_email_to_native_php_notice_and_canonical_decision(integration, action, expected):
    app, path, capture, *_ = integration
    cookie, csrf, notice = pending(integration)
    buttons = notice['reply_markup']['inline_keyboard'][0]
    callback = next(button['callback_data'] for button in buttons if button['callback_data'].startswith('hl:'+action+':'))
    body = {'callback_data':callback}
    assert len(callback.encode()) <= 64 and 'guest@' not in callback
    assert invoke(app, '/owner/decision', body, cookie, csrf)[0] == 403
    assert invoke(app, '/owner/decision', body, HTTP_ORIGIN='', HTTP_AUTHORIZATION='Bearer '+'k'*40)[0] == 403
    endpoint = lambda: invoke(app, '/owner/decision', body, HTTP_ORIGIN='', HTTP_AUTHORIZATION='Bearer '+KEY)
    assert endpoint()[2]['result'] == expected
    revision = app.app.store.people()[0]['revision']
    # A new application instance uses the persisted receipt, not process memory.
    app = PassengerApplication(path)
    assert endpoint()[2]['result'] == 'already_handled'
    assert app.app.store.people()[0]['revision'] == revision
    altered = callback[:3]+('d' if action=='a' else 'a')+callback[4:]
    assert invoke(app, '/owner/decision', {'callback_data':altered}, HTTP_ORIGIN='', HTTP_AUTHORIZATION='Bearer '+KEY)[0] == 403
    other = next(button['callback_data'] for button in buttons if button['callback_data'] != callback)
    assert invoke(app, '/owner/decision', {'callback_data':other}, HTTP_ORIGIN='', HTTP_AUTHORIZATION='Bearer '+KEY)[0] == 409
    if action == 'a':
        assert invoke(app, '/api/access', cookie=cookie)[2]['status'] == 'approved'
        app.app.store.operator('revoke', 'guest@example.invalid')
        assert endpoint()[2]['result'] == 'already_handled'
        assert app.app.store.people()[0]['status'] == 'revoked'


def test_pending_action_has_no_time_expiry_but_stale_revision_and_pause_deny(integration):
    app, path, *_ = integration
    _, _, notice = pending(integration)
    callback = notice['reply_markup']['inline_keyboard'][0][0]['callback_data']
    class PausingBody(BytesIO):
        def read(self, length):
            config=json.loads(path.read_text());config['passenger']['people_write_enabled']=False;path.write_text(json.dumps(config))
            return super().read(length)
    assert invoke(app, '/owner/decision', {'callback_data':callback}, HTTP_ORIGIN='', HTTP_AUTHORIZATION='Bearer '+KEY,
                  **{'wsgi.input':PausingBody(json.dumps({'callback_data':callback}).encode())})[0] == 403
    assert app.app.store.people()[0]['status']=='pending'
    config=json.loads(path.read_text());config['passenger']['people_write_enabled']=True;path.write_text(json.dumps(config))
    app.app.store.clock = lambda: 10**10
    assert invoke(app, '/owner/decision', {'callback_data':callback}, HTTP_ORIGIN='', HTTP_AUTHORIZATION='Bearer '+KEY)[2]['result'] == 'approved'
    app.app.store.operator('revoke', 'guest@example.invalid')
    fresh = {'callback_data':notice['reply_markup']['inline_keyboard'][0][1]['callback_data']}
    assert invoke(app, '/owner/decision', fresh, HTTP_ORIGIN='', HTTP_AUTHORIZATION='Bearer '+KEY)[0] == 409
    config=json.loads(path.read_text());config['passenger']['people_write_enabled']=False;path.write_text(json.dumps(config))
    assert invoke(app, '/owner/decision', {'callback_data':callback}, HTTP_ORIGIN='', HTTP_AUTHORIZATION='Bearer '+KEY)[0] == 403


def test_people_change_supersedes_pending_button_and_decision_rolls_back_as_one_unit(integration):
    app, _, _, *_ = integration
    _,_,notice=pending(integration)
    callback=notice['reply_markup']['inline_keyboard'][0][0]['callback_data']
    with app.app.store._transaction() as db:
        db.execute("CREATE TRIGGER fail_receipt BEFORE UPDATE ON owner_notice_actions BEGIN SELECT RAISE(ABORT,'synthetic interruption'); END")
    assert invoke(app,'/owner/decision',{'callback_data':callback},HTTP_ORIGIN='',HTTP_AUTHORIZATION='Bearer '+KEY)[0]==503
    assert app.app.store.people()[0]['status']=='pending'
    with app.app.store._transaction() as db:
        assert db.execute('SELECT decision FROM owner_notice_actions').fetchone()[0] is None
        db.execute('DROP TRIGGER fail_receipt')
    person=app.app.store.people()[0]
    app.app.store.change_person('deny',person['email'],person['revision'])
    assert invoke(app,'/owner/decision',{'callback_data':callback},HTTP_ORIGIN='',HTTP_AUTHORIZATION='Bearer '+KEY)[0]==409
    assert app.app.store.people()[0]['status']=='denied'


def test_ambiguous_notice_failure_keeps_pending_and_never_retries_or_emails_owner(integration, monkeypatch):
    app, _, capture, *_ = integration
    owner = app.app.mail.owner_notifications.transport
    attempts=[]
    def lost(payload): attempts.append(payload); raise TimeoutError('private provider detail')
    monkeypatch.setattr(owner, 'send', lost)
    # Reuse the ordinary flow through email proof, then inspect its canonical outcome.
    store=app.app.store;raw=store.browser(store.invitation()['token'])
    store.start(raw,'Guest','guest@example.invalid');app.app.mail.deliver()
    code=re.search(r'\b[0-9]{6}\b',app.app.mail.transport.transport.messages[0].get_content()).group()
    store.verify(raw,code)
    assert app.app.mail.deliver()==0 and store.status(raw)=='pending'
    assert app.app.mail.deliver()==0 and len(attempts)==1 and not capture.exists()
    assert len(app.app.mail.transport.transport.messages)==1
    with store._transaction() as db:
        assert db.execute("SELECT state FROM mail_delivery ORDER BY id DESC LIMIT 1").fetchone()[0]=='unknown'


def php_run(php, tmp_path, body):
    script=tmp_path/'script.php';script.write_text(body,encoding='utf-8')
    result=subprocess.run([str(php),'-n',str(script)],capture_output=True,timeout=10)
    assert result.returncode==0, result.stderr.decode(errors='replace')
    return json.loads(result.stdout)


def test_native_php_dispatch_private_owner_help_and_old_handler_preservation(integration):
    app, _, _, php, cfg, settings, tmp = integration
    _,_,notice=pending(integration)
    callback=notice['reply_markup']['inline_keyboard'][0][0]['callback_data']
    prefix='<?php require '+json.dumps(str(ROOT/'hosting/owner_telegram.php'))+'; $cfg=json_decode('+json.dumps(json.dumps(cfg|{'house_lights':settings}))+',true);'
    script=prefix+'''
    $calls=[]; $reply='already_handled';
    $post=static function($url,$body,$headers) use (&$calls,&$reply){ $calls[]=['url'=>$url,'body'=>$body,'headers'=>$headers];return str_ends_with($url,'/owner/decision')?['result'=>$reply]:['ok'=>true];};
    $base=['from'=>['id'=>1],'chat'=>['id'=>1,'type'=>'private']];
    $cq=['id'=>'synthetic','data'=>CALLBACK,'from'=>['id'=>1],'message'=>['message_id'=>1,'chat'=>$base['chat']]];
    $outs=[];
    foreach ([['from'=>['id'=>2]], ['message'=>['message_id'=>1,'chat'=>['id'=>2,'type'=>'private']]], ['message'=>['message_id'=>1,'chat'=>['id'=>1,'type'=>'group']]]] as $bad) {
      $outs[]=hl_dispatch(['callback_query'=>array_replace($cq,$bad)],$cfg,$post);
    }
    $none=count($calls)===0;
    $old=hl_dispatch(['callback_query'=>array_replace($cq,['data'=>'mr'])],$cfg,$post);
    hl_dispatch(['callback_query'=>$cq],$cfg,$post);
    $honest=$calls[1]['body']['text'];
    $help=hl_help($cfg,$base);
    $groupHelp=hl_help($cfg,array_replace($base,['chat'=>['id'=>1,'type'=>'group']]));
    echo json_encode(['rejected_without_calls'=>$none,'old_callback_unhandled'=>$old===false,'honest'=>$honest,'help'=>$help,'group_help'=>$groupHelp,'calls'=>$calls]);
    '''.replace('CALLBACK',json.dumps(callback))
    result=php_run(php,tmp,script)
    assert result['rejected_without_calls'] and result['old_callback_unhandled']
    assert result['honest']=='Already handled. Open People for current access.'
    assert settings['people_url'] in result['help'] and result['group_help']==''
    first=result['calls'][0]
    assert first['url']==settings['decision_url'] and first['body']=={'callback_data':callback}
    assert first['headers']==['Authorization: Bearer '+KEY]


def test_patch_preserves_existing_help_and_other_commands_and_lints(php_runtime,tmp_path):
    patch=runpy.run_path(str(ROOT/'hosting/prepare_owner_bot.py'))['patch_webhook']
    source="""<?php $cfg=[];
if ($_SERVER['REQUEST_METHOD'] !== 'POST' || !hash_equals($cfg['secret'], $_SERVER['HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN'] ?? '')) { http_response_code(403); exit; }
$u=[];
if ($cq = $u['callback_query'] ?? null) { existing_callback(); }
if ($cmd==='/start' || $cmd==='/help') reply('Existing help');
elseif ($cmd==='/rss') existing_rss();
elseif ($cmd==='/recent') existing_recent();
"""
    updated=patch(source,bridge_path='/private/owner_telegram.php',settings_path='/private/house-lights.php',expected_sha256=hashlib.sha256(source.encode()).hexdigest())
    assert "reply('Existing help' ." in updated and "elseif ($cmd==='/rss') existing_rss();" in updated
    assert updated.index('hash_equals')<updated.index('hl_dispatch')<updated.index('existing_callback')
    output=tmp_path/'webhook.php';output.write_text(updated)
    assert subprocess.run([str(php_runtime),'-n','-l',str(output)],capture_output=True,timeout=5).returncode==0


def test_patched_webhook_rejects_wrong_and_missing_secret_before_dispatch(integration):
    app, _, _, php, cfg, settings, tmp = integration
    _,_,notice=pending(integration)
    cq={'id':'synthetic','data':notice['reply_markup']['inline_keyboard'][0][0]['callback_data'],
        'from':{'id':1},'message':{'message_id':1,'chat':{'id':1,'type':'private'}}}
    cfg=cfg|{'secret':'synthetic-secret'}
    calls=tmp/'calls.jsonl'
    source='<?php $cfg=json_decode('+json.dumps(json.dumps(cfg))+',true);\n'+"""
if ($_SERVER['REQUEST_METHOD'] !== 'POST' || !hash_equals($cfg['secret'], $_SERVER['HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN'] ?? '')) { http_response_code(403); exit; }
"""+'$u=json_decode('+json.dumps(json.dumps({'callback_query':cq}))+',true);\n'+"""
if ($cq = $u['callback_query'] ?? null) { throw new Exception('Old handler must not consume House Lights.'); }
if ($cmd==='/start' || $cmd==='/help') reply('Existing help');
"""
    source+='function synthetic_post($url,$body,$headers) {file_put_contents('+json.dumps(str(calls))+',"called\\n",FILE_APPEND);return str_ends_with($url,"/owner/decision")?["result"=>"approved"]:["ok"=>true];}'
    patch=runpy.run_path(str(ROOT/'hosting/prepare_owner_bot.py'))['patch_webhook']
    updated=patch(source,bridge_path=str(tmp/'owner_telegram.php'),settings_path=str(tmp/'service.php'),expected_sha256=hashlib.sha256(source.encode()).hexdigest())
    updated=updated.replace('hl_dispatch($u, $cfg)', 'hl_dispatch($u, $cfg, "synthetic_post")')
    for secret in ('', 'wrong', 'synthetic-secret'):
        prefix='<?php $_SERVER["REQUEST_METHOD"]="POST";$_SERVER["HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"]='+json.dumps(secret)+'; ?>'
        script=tmp/'guard.php';script.write_text(prefix+updated,encoding='utf-8')
        process=subprocess.run([str(php),'-n',str(script)],capture_output=True,timeout=5)
        assert process.returncode==0,process.stderr.decode(errors='replace')
        assert calls.exists() is (secret=='synthetic-secret')
    assert len(calls.read_text().splitlines())==3


def test_native_php_relay_uses_canonical_application_and_replay_receipt(integration):
    app, config, _, php, cfg, settings, tmp = integration
    cookie,_,notice=pending(integration)
    shim=tmp/'decision.py'
    shim.write_text('''import sys,json
from io import BytesIO
sys.path.insert(0,ROOT)
from boomer_switch.website_passenger import load_application
request=json.load(sys.stdin);data=json.dumps(request['body']).encode()
env={'PATH_INFO':'/owner/decision','SCRIPT_NAME':'/house-lights','REQUEST_METHOD':'POST','wsgi.url_scheme':'https',
     'HTTP_HOST':'site.invalid','REMOTE_ADDR':'localhost','HTTP_X_HL_APACHE_PEER':'localhost',
     'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(data)),'wsgi.input':BytesIO(data),
     'HTTP_AUTHORIZATION':request['headers'][0].split(': ',1)[1]}
import ipaddress
env['REMOTE_ADDR']=env['HTTP_X_HL_APACHE_PEER']=str(ipaddress.IPv4Address(134744072))
response=[]
body=b''.join(load_application(sys.argv[1])(env,lambda status,headers:response.append(status)))
assert response==['200 OK']
sys.stdout.buffer.write(body)
'''.replace('ROOT',repr(str(ROOT))),encoding='utf-8')
    callback={'id':'synthetic','data':notice['reply_markup']['inline_keyboard'][0][0]['callback_data'],
              'from':{'id':1},'message':{'message_id':1,'chat':{'id':1,'type':'private'}}}
    script='<?php require '+json.dumps(str(ROOT/'hosting/owner_telegram.php'))+'; $cfg=json_decode('+json.dumps(json.dumps(cfg|{'house_lights':settings}))+',true);'
    script+='''
    $calls=[];
    $post=static function($url,$body,$headers) use (&$calls) {
      if (str_ends_with($url,'/owner/decision')) {
        $process=proc_open(COMMAND,[0=>['pipe','r'],1=>['pipe','w'],2=>['pipe','w']],$pipes);
        fwrite($pipes[0],json_encode(['body'=>$body,'headers'=>$headers]));fclose($pipes[0]);
        $result=stream_get_contents($pipes[1]);fclose($pipes[1]);$err=stream_get_contents($pipes[2]);fclose($pipes[2]);
        if(proc_close($process)!==0)throw new Exception('Synthetic canonical request failed.');
        return json_decode($result,true);
      }
      $calls[]=$body;return ['ok'=>true];
    };
    hl_dispatch(json_decode(UPDATE,true),$cfg,$post);echo json_encode($calls);
    '''.replace('COMMAND','json_decode('+json.dumps(json.dumps([sys.executable,str(shim),str(config)]))+',true)').replace('UPDATE',json.dumps(json.dumps({'callback_query':callback})))
    first=php_run(php,tmp,script)
    assert first[0]['text']=='Request approved.'
    assert invoke(app,'/api/access',cookie=cookie)[2]['status']=='approved'
    app.app.store.operator('revoke','guest@example.invalid')
    second=php_run(php,tmp,script)
    assert second[0]['text']=='Already handled. Open People for current access.'
    assert app.app.store.people()[0]['status']=='revoked'


def test_wrong_positive_notice_chat_is_rejected_before_any_send(integration):
    _,_,_,php,cfg,settings,tmp=integration
    cfg=cfg|{'house_lights':settings|{'owner_chat_id':2}}
    script='<?php require '+json.dumps(str(ROOT/'hosting/owner_telegram.php'))+'; $cfg=json_decode('+json.dumps(json.dumps(cfg))+',true);'
    script+='''$calls=0;try{hl_notice($cfg,[],function()use(&$calls){$calls++;return ['ok'=>true];});}catch(Throwable $e){}echo json_encode(['calls'=>$calls]);'''
    assert php_run(php,tmp,script)['calls']==0
