"""One isolated Firefox acceptance for room-grouped light controls."""

import argparse
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from demos.mobile_layout_acceptance import geometry
from playwright.sync_api import expect, sync_playwright

WEB = ROOT / 'boomer_switch/web'
ORIGIN = 'https://multi-light.invalid'
CATALOG = [
    {'light_id':'desk','name':'Sample reading lamp with a descriptive name','room':'Sample study'},
    {'light_id':'window','name':'Sample window lamp','room':'Sample study'},
    {'light_id':'hall','name':'Sample hall lamp','room':'Sample hall'},
]


class Fixture:
    def __init__(self):
        self.status = 'invited'
        self.home = 'ready'
        self.legacy = False
        self.states = {'desk':'off','window':'off','hall':'off'}
        self.offline = {'window'}
        self.unknown = set()
        self.operations = {}
        self.posts = []
        self.hold = False
        self.freeze_auto = False
        self.age_offset = 0
        self.reports = {}
        self.confirmed = {}
        self.attempts = {}
        self.auto_reads = []
        self.phase = 0

    def now(self):
        return time.monotonic()+self.age_offset

    def sample(self,target):
        state = self.states[target]
        availability = 'online'
        if target in self.offline:
            state,availability='unavailable','unavailable'
        elif target in self.unknown:
            state,availability='unavailable','unknown'
        self.reports[target]={'state':state,'availability':availability,'at':self.now()}
        if availability=='online':self.confirmed[target]=self.reports[target]

    def access(self):
        result = {'status':self.status,'csrf':'synthetic-csrf'}
        if self.status == 'approved':
            result.update(home_status=self.home,home_ready=self.home=='ready')
            if self.home == 'ready':
                if not self.legacy: result['direct_commands'] = True
                if self.legacy: result['light'] = CATALOG[0]
                else:
                    result.update(lights=CATALOG,automatic_updates=True,state_generation=1,state_time=self.now())
                    result['states']=[]
                    for light in CATALOG:
                        report=self.reports.get(light['light_id']);good=self.confirmed.get(light['light_id']);age=None if report is None else max(0,round((self.now()-report['at'])*1000))
                        result['states'].append({'light_id':light['light_id'],'state':report['state'] if report else 'unavailable',
                            'availability':'stale' if report and report['availability']=='online' and age>=45000 else report['availability'] if report else 'unchecked',
                            'age_ms':age,'sample':report['at'] if report else None,
                            'last_confirmed':{'state':good['state'],'age_ms':max(0,round((self.now()-good['at'])*1000))} if good else None})
        return result

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.scheme+'://'+parsed.netloc != ORIGIN:
            route.abort(); return
        name = parsed.path.rsplit('/',1)[-1]
        if parsed.path == '/house-lights/guest':
            route.fulfill(path=str(WEB/'website.html'),content_type='text/html'); return
        if name in ('website.js','entry.js','style.css'):
            route.fulfill(path=str(WEB/name),content_type='text/css' if name.endswith('.css') else 'text/javascript'); return
        if request.method == 'POST' and request.headers.get('x-csrf-token') != 'synthetic-csrf':
            route.fulfill(status=403,json={'error':'Refresh access.'}); return
        if name == 'access': route.fulfill(json=self.access()); return
        if name == 'register': self.status='verify'; route.fulfill(json=self.access()); return
        if name == 'verify': self.status='pending'; route.fulfill(json=self.access()); return
        if name == 'lights' and request.method == 'POST':
            body = request.post_data_json
            if body.get('background') is True:
                if self.status!='approved' or self.home!='ready' or set(body)!={'operation_id','action','background','preferred'} or body['action']!='read':
                    route.fulfill(status=403,json={'error':'Access unavailable.'});return
                self.posts.append(dict(body))
                if not self.freeze_auto and not any(not op.get('_finished') for op in self.operations.values()) and (not self.auto_reads or self.now()-self.auto_reads[-1]['at']>=2):
                    eligible=sorted((light['light_id'] for light in CATALOG if self.now()-self.attempts.get(light['light_id'],float('-inf'))>=10),key=lambda target:(self.attempts.get(target,float('-inf')),target))
                    preferred=[target for target in eligible if target in body['preferred']] if self.phase!=2 else []
                    if eligible:
                        target=(preferred or eligible)[0];self.sample(target);self.attempts[target]=self.now();self.auto_reads.append({'target':target,'at':self.now()});self.phase=(self.phase+1)%3
                route.fulfill(status=202,json=self.access());return
            expected = {'operation_id','action'} if self.legacy else {'operation_id','action','light_id'}
            target = body.get('light_id','desk' if self.legacy else None)
            if set(body)!=expected or target not in self.states or body.get('action') not in ('read','on','off'):
                route.fulfill(status=400,json={'error':'Use a valid target.'}); return
            if self.status!='approved' or self.home!='ready':
                route.fulfill(status=403,json={'error':'Access unavailable.'}); return
            self.posts.append(dict(body))
            self.attempts[target]=self.now()
            self.operations[body['operation_id']] = body|{'target':target}
            route.fulfill(status=202,json={'operation_id':body['operation_id'],'status':'queued'}); return
        if '/api/operations/' in parsed.path:
            operation = self.operations.get(name)
            if not operation: route.fulfill(status=404,json={'error':'Result unavailable.'}); return
            if self.status!='approved' or self.home!='ready':
                route.fulfill(status=403,json={'error':'Access unavailable.'}); return
            target = operation['target']
            if self.hold:
                result = {'status':'queued','state':'unavailable','availability':'unknown','light_id':target}
            elif target in self.offline:
                result = {'status':'not_started','state':'unavailable','availability':'unavailable','light_id':target}
            elif target in self.unknown:
                result = {'status':'unknown','state':'unavailable','availability':'unknown','light_id':target}
            else:
                if operation['action']!='read': self.states[target]=operation['action']
                result = {'status':'confirmed','state':self.states[target],'availability':'online','light_id':target}
            if result['status']!='queued':
                operation['_finished']=True;self.sample(target)
            result['operation_id'] = operation['operation_id']
            if self.legacy: result.pop('light_id',None);result.pop('availability',None)
            route.fulfill(json=result); return
        route.fulfill(status=404,json={'error':'Unavailable.'})


def run(output):
    output.mkdir(parents=True,exist_ok=False)
    checks=[]
    with sync_playwright() as playwright:
        browser=playwright.firefox.launch()
        try:
            for width in (320,375):
                fixture=Fixture();context=browser.new_context(viewport={'width':width,'height':740})
                context.route('**/*',fixture.route)
                page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(ORIGIN+'/house-lights/guest')
                page.locator('#name').fill('Sample guest');page.locator('#email').fill('guest@example.invalid')
                page.locator('#send-code').click();expect(page.locator('#code-help')).to_contain_text('10 minutes')
                page.locator('#code').fill('123456');page.locator('#verify-code').click()
                expect(page.locator('#status')).to_have_text('Waiting for approval')
                fixture.status='approved'
                cards=page.locator('[data-light-id]')
                expect(cards).to_have_count(3,timeout=10000)
                expect(page.locator('#rooms > .room')).to_have_count(2)
                picker = page.get_by_label('Jump to room')
                expect(picker.locator('option')).to_have_text(['Choose a room', 'Sample hall', 'Sample study'])
                picker.select_option(label='Sample study')
                expect(page.get_by_role('heading', name='Sample study')).to_be_focused()
                desk=page.locator('[data-light-id="desk"]');hall=page.locator('[data-light-id="hall"]');window=page.locator('[data-light-id="window"]')
                expect(desk.locator('[data-role="state"]')).to_have_text('Off',timeout=15000)
                expect(hall.locator('[data-role="state"]')).to_have_text('Off',timeout=15000)
                expect(window.locator('[data-role="state"]')).to_have_text('Unavailable',timeout=15000)
                expect(page.get_by_role('heading',name='Sample study')).to_be_focused()
                assert fixture.auto_reads and all(after['at']-before['at']>=2 for before,after in zip(fixture.auto_reads,fixture.auto_reads[1:]))
                foreground=lambda:[p for p in fixture.posts if not p.get('background')]
                desk.locator('[data-action="on"]').click()
                expect(desk.locator('[data-role="state"]')).to_have_text('On',timeout=10000)
                assert [(p['action'],p['light_id']) for p in foreground()]==[('on','desk')]
                expect(window.locator('[data-action="on"]')).to_be_disabled()
                assert hall.get_attribute('data-availability')!='unavailable', 'One failed device must not mark its neighbor unavailable.'
                fixture.states['hall']='on'
                expect(hall.locator('[data-role="state"]')).to_have_text('On',timeout=15000)
                fixture.unknown.add('hall');hall.locator('[data-action="read"]').click()
                expect(hall.locator('[data-role="state"]')).to_have_text('Unknown',timeout=10000)
                expect(hall.locator('[data-action="off"]')).to_be_disabled()
                fixture.unknown.clear();hall.locator('[data-action="read"]').click()
                expect(hall.locator('[data-role="state"]')).to_have_text('On',timeout=10000)
                fixture.freeze_auto=True;fixture.age_offset=50
                expect(desk.locator('[data-role="state"]')).to_have_attribute('data-state','stale',timeout=10000)
                expect(desk.locator('[data-role="state"]')).to_have_text('On')
                expect(desk.locator('[data-role="detail"]')).to_contain_text('Stale')
                desk.locator('[data-action="read"]').click()
                expect(desk.locator('[data-role="state"]')).to_have_attribute('data-state','on',timeout=10000)
                geometry(page);page.screenshot(path=str(output/f'multi-{width}.png'),full_page=True)
                page.evaluate("document.documentElement.style.fontSize='200%'")
                geometry(page);page.screenshot(path=str(output/f'multi-large-text-{width}.png'),full_page=True)
                page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'))")
                expect(page.locator('#controls')).to_be_hidden();page.wait_for_timeout(300)
                before=len(fixture.auto_reads);page.wait_for_timeout(2500);assert len(fixture.auto_reads)==before
                page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'))")
                expect(page.locator('#controls')).to_be_visible(timeout=10000)
                # The pending UI request is cleared when source access disappears.
                fixture.hold=True;desk.locator('[data-action="on"]').click()
                expect(desk.locator('[data-role="state"]')).to_have_text('Checking')
                before=len(foreground());fixture.home='source_mismatch'
                expect(page.locator('#controls')).to_be_hidden(timeout=10000)
                expect(page.locator('#connection')).to_have_attribute('role','alert')
                expect(page.locator('#rooms')).to_have_text('')
                fixture.hold=False;page.wait_for_timeout(1500);assert len(foreground())==before
                fixture.status='denied'
                expect(page.locator('#status')).to_contain_text('Access unavailable',timeout=10000)
                expect(page.locator('#controls')).to_be_hidden()
                # The legacy singleton response is intentionally still accepted.
                fixture.status='approved';fixture.home='ready';fixture.legacy=True;fixture.offline.clear();fixture.unknown.clear()
                page.reload();expect(page.locator('[data-light-id]')).to_have_count(1,timeout=10000)
                before=len(fixture.posts);page.locator('#refresh-light').click()
                expect(page.locator('#light-state')).to_have_text('On',timeout=10000)
                page.locator('#light-off').click();expect(page.locator('#light-state')).to_have_text('Off',timeout=10000)
                assert all('light_id' not in p for p in fixture.posts[before:])
                geometry(page);assert not errors, errors
                checks.append({'width':width,'grouped_lights':3,'independent_failure':True,'explicit_targeting':True,
                               'automatic_external_updates':True,'global_read_cap':True,'last_state_age_and_stale':True,
                               'hidden_stop':True,'room_focus_preserved':True,'legacy_singleton':True,'large_text':True})
                context.close()
        finally: browser.close()
    print(json.dumps({'ok':True,'checks':checks}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'.test-runs/state-updates')
    args=parser.parse_args();run(args.output)
