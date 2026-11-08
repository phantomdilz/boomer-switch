"""One isolated Firefox acceptance for room-grouped light controls."""

import argparse
import json
from pathlib import Path
import sys
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

    def access(self):
        result = {'status':self.status,'csrf':'synthetic-csrf'}
        if self.status == 'approved':
            result.update(home_status=self.home,home_ready=self.home=='ready')
            if self.home == 'ready':
                if self.legacy: result['light'] = CATALOG[0]
                else: result['lights'] = CATALOG
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
            expected = {'operation_id','action'} if self.legacy else {'operation_id','action','light_id'}
            target = body.get('light_id','desk' if self.legacy else None)
            if set(body)!=expected or target not in self.states or body.get('action') not in ('read','on','off'):
                route.fulfill(status=400,json={'error':'Use a valid target.'}); return
            if self.status!='approved' or self.home!='ready':
                route.fulfill(status=403,json={'error':'Access unavailable.'}); return
            self.posts.append(dict(body))
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
                desk=page.locator('[data-light-id="desk"]');hall=page.locator('[data-light-id="hall"]');window=page.locator('[data-light-id="window"]')
                expect(desk.locator('[data-role="state"]')).to_have_text('Not checked')
                page.wait_for_timeout(3500)
                assert fixture.posts==[], 'Approved access must not fan out device reads.'
                desk.locator('[data-action="on"]').click()
                expect(desk.locator('[data-role="state"]')).to_have_text('On',timeout=10000)
                assert [(p['action'],p['light_id']) for p in fixture.posts]==[('read','desk'),('on','desk')]
                hall.locator('[data-action="read"]').click()
                expect(hall.locator('[data-role="state"]')).to_have_text('Off',timeout=10000)
                window.locator('[data-action="read"]').click()
                expect(window.locator('[data-role="state"]')).to_have_text('Unavailable',timeout=10000)
                expect(window.locator('[data-action="on"]')).to_be_disabled()
                assert hall.get_attribute('data-availability')!='unavailable', 'One failed device must not mark its neighbor unavailable.'
                before=len(fixture.posts);page.wait_for_timeout(3500);assert len(fixture.posts)==before
                fixture.states['hall']='on';hall.locator('[data-action="read"]').click()
                expect(hall.locator('[data-role="state"]')).to_have_text('On',timeout=10000)
                fixture.unknown.add('hall');hall.locator('[data-action="read"]').click()
                expect(hall.locator('[data-role="state"]')).to_have_text('Unknown',timeout=10000)
                expect(hall.locator('[data-action="off"]')).to_be_disabled()
                geometry(page);page.screenshot(path=str(output/f'multi-{width}.png'),full_page=True)
                page.evaluate("document.documentElement.style.fontSize='200%'")
                geometry(page);page.screenshot(path=str(output/f'multi-large-text-{width}.png'),full_page=True)
                # A stale-read follow-up is cancelled when source access disappears.
                fixture.hold=True;desk.locator('[data-action="on"]').click()
                expect(desk.locator('[data-role="state"]')).to_have_text('Checking')
                before=len(fixture.posts);fixture.home='source_mismatch'
                expect(page.locator('#controls')).to_be_hidden(timeout=10000)
                expect(page.locator('#connection')).to_have_attribute('role','alert')
                expect(page.locator('#rooms')).to_have_text('')
                fixture.hold=False;page.wait_for_timeout(1500);assert len(fixture.posts)==before
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
                               'no_background_device_reads':True,'legacy_singleton':True,'large_text':True})
                context.close()
        finally: browser.close()
    print(json.dumps({'ok':True,'checks':checks}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'.test-runs/pj84-multi-light')
    args=parser.parse_args();run(args.output)
