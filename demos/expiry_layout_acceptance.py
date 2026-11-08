"""Check native People dates, including saved values, with synthetic owner fixtures."""

import argparse
from http.cookies import SimpleCookie
from io import BytesIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from boomer_switch.people import PeopleApplication, RemoteUserOwnerAuth
from boomer_switch.registration import RegistrationStore
from demos.mobile_layout_acceptance import geometry

ORIGIN = 'https://expiry.invalid'
EXISTING = 'existing@example.invalid'
ADDED = 'added@example.invalid'


class Fixture:
    def __init__(self, directory):
        self.store = RegistrationStore(directory)
        browser = self.store.browser(self.store.invitation()['token'])
        self.store.start(browser, 'Existing sample guest', EXISTING)
        code = next(m['code'] for m in self.store.mailbox(EXISTING)['messages'] if m['kind'] == 'code')
        self.store.verify(browser, code)
        self.store.operator('approve', EXISTING)
        self.app = RemoteUserOwnerAuth(PeopleApplication(self.store), 'sample-owner')
        self.posts = []

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        if url.scheme + '://' + url.netloc != ORIGIN:
            route.abort()
            return
        body = (request.post_data or '').encode()
        env = {'PATH_INFO': url.path, 'REQUEST_METHOD': request.method, 'wsgi.url_scheme': 'https',
               'wsgi.input': BytesIO(body), 'CONTENT_LENGTH': str(len(body)),
               'CONTENT_TYPE': request.headers.get('content-type', ''),
               'HTTP_COOKIE': request.headers.get('cookie', ''),
               'HTTP_X_CSRF_TOKEN': request.headers.get('x-csrf-token', '')}
        cookie = SimpleCookie(env['HTTP_COOKIE'])
        if cookie.get('fixture_owner') and cookie['fixture_owner'].value == 'synthetic':
            env['REMOTE_USER'] = 'sample-owner'
        response = {}
        def start(status, headers, exc_info=None):
            response.update(status=int(status.split()[0]), headers=dict(headers))
        raw = b''.join(self.app(env, start))
        if url.path == '/api/people' and request.method == 'POST' and response['status'] == 200:
            self.posts.append(json.loads(body))
        route.fulfill(status=response['status'], headers=response['headers'], body=raw)


def capture_date(page, field, output, label, width, scale):
    assert field.get_attribute('type') == 'date', 'The native date input must remain.'
    measurements = field.evaluate('''e => {
      const s=getComputedStyle(e),r=e.getBoundingClientRect();
      return {font:parseFloat(s.fontSize),width:r.width,scroll:e.scrollWidth,client:e.clientWidth,
              left:r.left,right:r.right,padding:parseFloat(s.paddingLeft)+parseFloat(s.paddingRight)};
    }''')
    assert measurements['font'] >= 16 * scale / 100, 'Date text must retain the requested size.'
    assert measurements['scroll'] <= measurements['client'] + 1, 'Date input content overflows.'
    assert measurements['left'] >= 0 and measurements['right'] <= width + 1
    geometry(page)
    field.screenshot(path=str(output / f'{label}-{width}-{scale}.png'))
    return measurements


def submit_change(page, control):
    with page.expect_response(lambda response: response.url == ORIGIN + '/api/people'
                              and response.request.method == 'POST') as accepted:
        control.click()
    assert accepted.value.status == 200, 'The owner change must succeed before its stored state is checked.'


def run(output):
    from playwright.sync_api import expect, sync_playwright

    output.mkdir(parents=True, exist_ok=False)
    evidence = []
    with TemporaryDirectory(prefix='expiry-state-', dir=output) as temporary, sync_playwright() as playwright:
        browser = playwright.firefox.launch()
        try:
            for width in (320, 375):
                for scale in (100, 200):
                    fixture = Fixture(Path(temporary) / f'{width}-{scale}')
                    context = browser.new_context(viewport={'width': width, 'height': 740}, timezone_id='America/New_York')
                    context.route('**/*', fixture.route)
                    context.add_cookies([{'name': 'fixture_owner', 'value': 'synthetic', 'url': ORIGIN,
                                         'httpOnly': True, 'secure': True}])
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(ORIGIN + '/people')
                    expect(page.locator('#people-status')).to_have_text('People ready.')
                    page.evaluate('scale => document.documentElement.style.fontSize=scale+"%"', scale)
                    page.locator('#add-email').fill(ADDED)
                    submit_change(page, page.locator('#add-person'))
                    expect(page.locator('[data-email="' + ADDED + '"]')).to_be_visible()
                    expected_expiry = page.evaluate('new Date(2032,0,1).getTime()/1000')
                    fields = []
                    for email, label in ((EXISTING, 'existing'), (ADDED, 'added')):
                        row = page.locator('[data-email="' + email + '"]')
                        date = row.locator('[data-end-date]')
                        expect(date).to_have_value('')
                        empty = capture_date(page, date, output, label + '-empty', width, scale)
                        date.fill('2031-12-31')
                        expect(date).to_have_value('2031-12-31')
                        filled = capture_date(page, date, output, label + '-filled', width, scale)
                        submit_change(page, row.locator('[data-action="save-expiry"]'))
                        expect(page.locator('#people-status')).to_have_text('People updated.')
                        expect(date).to_have_value('2031-12-31')
                        current = next(p for p in fixture.store.people() if p['email'] == email)
                        assert current['expires'] == expected_expiry, 'The last day must remain inclusive in local time.'
                        capture_date(page, date, output, label + '-saved', width, scale)
                        submit_change(page, row.locator('[data-action="clear-expiry"]'))
                        expect(date).to_have_value('')
                        current = next(p for p in fixture.store.people() if p['email'] == email)
                        assert current['expires'] is None
                        assert current['email_verified'] is (email == EXISTING), 'Date changes must not supply inbox proof.'
                        fields.append({'person': label, 'empty': empty, 'filled': filled, 'save_clear': True})
                    assert [p['action'] for p in fixture.posts] == ['preapprove', 'expiry', 'expiry', 'expiry', 'expiry']
                    assert [p.get('expires') for p in fixture.posts[1:]] == [expected_expiry, None, expected_expiry, None]
                    geometry(page)
                    page.screenshot(path=str(output / f'people-cleared-{width}-{scale}.png'), full_page=True)
                    assert not errors
                    evidence.append({'width': width, 'scale': scale, 'fields': fields})
                    context.close()
        finally:
            browser.close()
    (output / 'measurements.json').write_text(json.dumps(evidence, indent=2))
    print(json.dumps({'ok': True, 'widths': [320, 375], 'text_scales': [100, 200],
                      'native_picker_retained': True, 'empty_filled_saved_crops': True,
                      'existing_and_preapproved_save_clear': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / '.test-runs/pj91-expiry')
    run(parser.parse_args().output)
