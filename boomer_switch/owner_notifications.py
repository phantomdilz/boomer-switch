"""Send owner notices and apply narrowly scoped, revision-bound decisions."""

import hmac
import base64
import hashlib
import json
import re
import secrets

from .configuration import service_key
from .private_process import exact_private_reply, private_command
from .registration import AccessDenied, StalePerson


OWNER_DECISION_PATH = "/owner/decision"


class OwnerNotifications:
    """Keep callbacks and People changes in the canonical permission transaction."""

    def __init__(self, store, command, decision_key, people_url):
        if decision_key == 'REPLACE_WITH_PRIVATE_OWNER_DECISION_KEY':
            raise ValueError('Configure a private owner-action key.')
        self.store, self.command = store, private_command(command)
        self.key, self.people_url = service_key(decision_key), people_url
        with store._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS owner_notice_actions (
                notice INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE, email TEXT NOT NULL,
                revision INTEGER NOT NULL, decision TEXT)""")

    def prepare(self, db, row):
        notice = db.execute("SELECT * FROM owner_notice_actions WHERE notice=?", (row['id'],)).fetchone()
        if notice is None:
            token = secrets.token_urlsafe(24)
            db.execute("INSERT INTO owner_notice_actions VALUES (?,?,?,?,NULL)",
                       (row['id'], token, row['email'], int(row['reference'])))
        else:
            token = notice['token']
        return {'name': row['name'], 'email': row['email'], 'people_url': self.people_url,
                'approve': self.callback('a', token), 'deny': self.callback('d', token)}

    def callback(self, action, token):
        data = 'hl:'+action+':'+token
        signature = base64.urlsafe_b64encode(hmac.new(self.key.encode(), data.encode(), hashlib.sha256).digest()[:12]).decode()
        return data+':'+signature

    def send(self, payload):
        if not exact_private_reply(self.command, json.dumps(payload).encode(), b'SENT', timeout=5):
            raise RuntimeError("Owner notification is unconfirmed. Review People.")

    def authorize(self, environ):
        if (environ.get('wsgi.url_scheme') != 'https' or environ.get('HTTP_ORIGIN')
                or not hmac.compare_digest(environ.get('HTTP_AUTHORIZATION', '').encode(), ('Bearer '+self.key).encode())):
            raise AccessDenied("Owner action is unavailable.")

    def decide(self, environ, body):
        self.authorize(environ)
        if not isinstance(body, dict) or set(body) != {'callback_data'} or not isinstance(body['callback_data'], str):
            raise ValueError("Use a valid owner action.")
        match = re.fullmatch(r'hl:([ad]):([A-Za-z0-9_-]{32}):([A-Za-z0-9_-]{16})', body['callback_data'])
        if match is None:
            raise ValueError("Use a valid owner action.")
        if not hmac.compare_digest(self.callback(match[1], match[2]), body['callback_data']):
            raise AccessDenied("Owner action is unavailable.")
        action = 'approve' if match[1] == 'a' else 'deny'
        with self.store._transaction() as db:
            notice = db.execute("SELECT * FROM owner_notice_actions WHERE token=?", (match[2],)).fetchone()
            if notice is None:
                raise StalePerson("This request changed. Open People.")
            if notice['decision'] is not None:
                if notice['decision'] != action:
                    raise StalePerson("This request was already handled. Open People.")
                return {'result': 'already_handled'}
            self.store._change_person(db, action, notice['email'], notice['revision'])
            db.execute("UPDATE owner_notice_actions SET decision=? WHERE notice=?", (action, notice['notice']))
        return {'result': 'approved' if action == 'approve' else 'denied'}
