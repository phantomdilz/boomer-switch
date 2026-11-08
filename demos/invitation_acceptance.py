"""Exercise owner invitation replacement with isolated hosted fixtures in Firefox."""

import argparse
import base64
from http.cookies import SimpleCookie
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image
from pypdf import PdfReader, PdfWriter
from boomer_switch.registration import RegistrationStore
from boomer_switch.website import WebsiteApplication
from boomer_switch.website_mail import WebsiteMail
from demos.mobile_layout_acceptance import geometry
import zxingcpp

ORIGIN = 'https://invitation.invalid'
MOUNT = '/house-lights'


class Inbox:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


class Fixture:
    def __init__(self, directory, template):
        self.store = RegistrationStore(directory, mode='production')
        self.inbox = Inbox()
        self.mail = WebsiteMail(self.store, self.inbox, 'sender@example.invalid')
        self.app = WebsiteApplication(self.store, self.mail, origin=ORIGIN, mount=MOUNT,
                                     owner_identity='sample-owner', invitation_template=template)
        self.replacements = 0

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.scheme + '://' + parsed.netloc != ORIGIN or not parsed.path.startswith(MOUNT + '/'):
            route.abort()
            return
        body = (request.post_data or '').encode()
        cookies = SimpleCookie(request.headers.get('cookie', ''))
        path = parsed.path[len(MOUNT):]
        env = {'PATH_INFO': path, 'SCRIPT_NAME': MOUNT, 'REQUEST_METHOD': request.method,
               'wsgi.url_scheme': 'https', 'wsgi.input': BytesIO(body), 'CONTENT_LENGTH': str(len(body)),
               'CONTENT_TYPE': request.headers.get('content-type', ''),
               'HTTP_COOKIE': request.headers.get('cookie', ''), 'HTTP_ORIGIN': request.headers.get('origin', ''),
               'HTTP_X_CSRF_TOKEN': request.headers.get('x-csrf-token', '')}
        if cookies.get('fixture_owner') and cookies['fixture_owner'].value == 'synthetic':
            env['REMOTE_USER'] = 'sample-owner'
        response = {}
        def start(status, headers, exc_info=None):
            response.update(status=int(status.split()[0]), headers=dict(headers))
        payload = b''.join(self.app(env, start))
        if path == '/api/invitation' and request.method == 'POST' and response['status'] == 200:
            self.replacements += 1
        route.fulfill(status=response['status'], headers=response['headers'], body=payload)

    def approved_browser(self):
        raw = self.store.browser(self.store.invitation()['token'])
        self.store.start(raw, 'Sample guest', 'guest@example.invalid')
        self.mail.deliver()
        code = re.search(r'\b\d{6}\b', self.inbox.messages[-1].get_content()).group()
        self.store.verify(raw, code)
        person = self.store.people()[0]
        self.store.change_person('approve', person['email'], person['revision'])
        return raw


def blank_template(directory):
    path = directory / 'synthetic-master.pdf'
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(612, 792)
    writer.write(path)
    return path


def check_pdf(raw, directory, urls):
    path = directory / 'synthetic-invitation.pdf'
    path.write_bytes(raw)
    reader = PdfReader(BytesIO(raw))
    assert len(reader.pages) == 2
    for index, page in enumerate(reader.pages, 1):
        assert urls['short_url'] in ''.join(page.extract_text().split())
        output = directory / f'design-{index}'
        result = subprocess.run(['pdftoppm', '-png', '-r', '144', '-f', str(index), '-l', str(index),
                                 '-singlefile', str(path), str(output)], capture_output=True, timeout=30)
        assert result.returncode == 0, 'Synthetic PDF rendering failed.'
        with Image.open(output.with_suffix('.png')) as image:
            codes = zxingcpp.read_barcodes(image)
        assert len(codes) == 1 and codes[0].text == urls['invite_url'], 'Rendered QR does not match current invitation.'


def run(output, template=None, check_card=False):
    output.mkdir(parents=True, exist_ok=False)
    if check_card:
        with TemporaryDirectory(prefix='state-', dir=output) as temporary:
            root = Path(temporary)
            fixture = Fixture(root / 'state', template or blank_template(root))
            cards = fixture.app.people.invitation_cards
            before = cards.template.read_bytes()
            check_pdf(cards.pdf(), output, cards.urls())
            assert cards.template.read_bytes() == before
        print(json.dumps({'ok': True, 'designs': 2, 'decoded_qr': 2, 'master_unchanged': True, 'browser': False}))
        return
    from playwright.sync_api import expect, sync_playwright
    with TemporaryDirectory(prefix='state-', dir=output) as temporary, sync_playwright() as playwright:
        root = Path(temporary)
        master = template or blank_template(root)
        original = master.read_bytes()
        browser = playwright.firefox.launch()
        try:
            for width in (320, 375):
                fixture = Fixture(root / str(width), master)
                approved = fixture.approved_browser()
                before = fixture.store.invitation()
                mail_count = len(fixture.inbox.messages)
                guest_context = browser.new_context()
                guest_context.route('**/*', fixture.route)
                guest_context.add_cookies([{'name': 'bs_session', 'value': approved, 'url': ORIGIN + MOUNT,
                                            'httpOnly': True, 'secure': True}])
                bookmark = guest_context.new_page()
                bookmark.goto(ORIGIN + MOUNT + '/guest')
                expect(bookmark.locator('#status')).to_have_text('Access approved')
                context = browser.new_context(viewport={'width': width, 'height': 740})
                context.route('**/*', fixture.route)
                context.add_cookies([{'name': 'fixture_owner', 'value': 'synthetic', 'url': ORIGIN, 'httpOnly': True, 'secure': True}])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(ORIGIN + MOUNT + '/people')
                expect(page.locator('#invitation')).to_be_visible()
                expect(page.locator('#invitation-pdf')).to_be_visible()
                with page.expect_popup() as popup:
                    page.locator('#print-invitation').click()
                card = popup.value
                expect(card.locator('.invitation-qr svg')).to_be_visible()
                assert fixture.store.invitation() == before and fixture.replacements == 0
                card.close()
                page.locator('#replace-invitation').click()
                expect(page.locator('#replace-warning')).to_contain_text('old QR code and typed address will stop working')
                page.locator('#cancel-replace').click()
                assert fixture.store.invitation() == before and fixture.replacements == 0
                stale = page.evaluate("fetch('api/invitation').then(r=>r.json())")
                page.locator('#replace-invitation').click()
                page.locator('#confirm-replace').click()
                expect(page.locator('#invitation-status')).to_have_text('Invitation replaced. Print the new card.')
                current = fixture.store.invitation()
                assert current != before and fixture.replacements == 1
                replay = page.evaluate("async body => (await fetch('api/invitation',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':body.csrf},body:JSON.stringify({revision:body.revision,confirmation:'replace'})})).status", stale)
                assert replay == 409 and fixture.replacements == 1
                with page.expect_popup() as popup:
                    page.locator('#print-invitation').click()
                card = popup.value
                expect(card.locator('.invitation-address')).to_contain_text(current['alias'])
                png = card.locator('.invitation-qr svg').screenshot()
                with Image.open(BytesIO(png)) as image:
                    codes = zxingcpp.read_barcodes(image)
                urls = fixture.app.people.invitation_cards.urls()
                assert len(codes) == 1 and codes[0].text == urls['invite_url']
                card.close()
                pdf = page.evaluate("async () => {const r=await fetch('invitation.pdf');if(!r.ok)throw Error('PDF unavailable');const a=new Uint8Array(await r.arrayBuffer());return btoa(Array.from(a,x=>String.fromCharCode(x)).join(''));}")
                design_dir = output / str(width)
                design_dir.mkdir()
                check_pdf(base64.b64decode(pdf), design_dir, urls)
                assert fixture.store.status(approved) == 'approved'
                assert len(fixture.inbox.messages) == mail_count
                bookmark.reload()
                expect(bookmark.locator('#status')).to_have_text('Access approved')
                assert next(c['value'] for c in guest_context.cookies() if c['name'] == 'bs_session') == approved
                guest_context.close()
                geometry(page)
                page.screenshot(path=str(output / f'people-{width}.png'), full_page=True)
                page.evaluate("document.documentElement.style.fontSize='200%'")
                geometry(page)
                page.screenshot(path=str(output / f'people-large-{width}.png'), full_page=True)
                assert not errors
                context.close()
                anonymous = browser.new_context()
                anonymous.route('**/*', fixture.route)
                stranger = anonymous.new_page()
                for endpoint in ('api/invitation', 'invitation-card', 'invitation.pdf'):
                    assert stranger.goto(ORIGIN + MOUNT + '/' + endpoint).status == 403
                stranger.goto(urls['invite_url'])
                expect(stranger.locator('#send-code')).to_be_enabled()
                stranger.locator('#name').fill('New sample guest')
                stranger.locator('#email').fill('new-guest@example.invalid')
                stranger.locator('#send-code').click()
                expect(stranger.locator('#verify-code')).to_be_visible()
                assert len(fixture.inbox.messages) == mail_count + 1
                anonymous.close()
            assert master.read_bytes() == original
        finally:
            browser.close()
    print(json.dumps({'ok': True, 'widths': [320, 375], 'designs': 2, 'replacement_once': True,
                      'approved_bookmark_preserved': True, 'no_extra_mail': True, 'master_unchanged': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / '.test-runs/pj87-invitation')
    parser.add_argument('--template', type=Path)
    parser.add_argument('--check-card', action='store_true')
    options = parser.parse_args()
    run(options.output, options.template.resolve() if options.template else None, options.check_card)
