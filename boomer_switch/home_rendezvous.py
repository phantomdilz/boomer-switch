"""Authorize same-origin guest operations through a bounded outbound home agent."""

import hmac
import ipaddress
import json
import re
import uuid

from .configuration import service_key as validate_service_key, light_metadata, light_catalog
from .registration import AccessDenied, _digest


class HomeUnavailable(RuntimeError):
    pass


class OperationConflict(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Use a canonical operation identifier.")
    return value


class HomeRendezvous:
    FRESH = 6
    TTL = 8

    def __init__(self, store, service_key, *, idle_poll_seconds=15):
        validate_service_key(service_key)
        if type(idle_poll_seconds) is not int or not 15 <= idle_poll_seconds <= 60:
            raise ValueError("Set the idle poll interval from 15 through 60 seconds.")
        self.store, self.key, self.idle = store, service_key, idle_poll_seconds
        with store._transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS home_connection (
                    id INTEGER PRIMARY KEY CHECK(id=1), agent TEXT NOT NULL, sequence INTEGER NOT NULL,
                    request_id TEXT NOT NULL, peer TEXT NOT NULL, generation INTEGER NOT NULL,
                    seen REAL NOT NULL, last_operation TEXT, valid INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL DEFAULT 0, light TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS home_operations (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, peer TEXT NOT NULL,
                    generation INTEGER NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, poll_sequence INTEGER,
                    outcome TEXT, completed REAL, invalidated INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS home_demand (
                    session TEXT PRIMARY KEY, peer TEXT NOT NULL, seen REAL NOT NULL);
            ''')
            db.execute("BEGIN IMMEDIATE")
            for name, declaration in (("valid","INTEGER NOT NULL DEFAULT 1"),("active","INTEGER NOT NULL DEFAULT 0"),("light","TEXT NOT NULL DEFAULT '{}'")):
                if name not in {row['name'] for row in db.execute('PRAGMA table_info(home_connection)')}:
                    db.execute(f'ALTER TABLE home_connection ADD COLUMN {name} {declaration}')
            if 'invalidated' not in {row['name'] for row in db.execute('PRAGMA table_info(home_operations)')}:
                db.execute('ALTER TABLE home_operations ADD COLUMN invalidated INTEGER NOT NULL DEFAULT 0')
            bind_legacy = "light_id" not in {row["name"] for row in db.execute("PRAGMA table_info(home_operations)")}
            for name, declaration in (("light_id", "TEXT"), ("targeted", "INTEGER NOT NULL DEFAULT 0"), ("target_lease", "INTEGER NOT NULL DEFAULT 0")):
                if name not in {row['name'] for row in db.execute('PRAGMA table_info(home_operations)')}:
                    db.execute(f'ALTER TABLE home_operations ADD COLUMN {name} {declaration}')
            if 'catalog_revision' not in {row['name'] for row in db.execute('PRAGMA table_info(home_connection)')}:
                db.execute("ALTER TABLE home_connection ADD COLUMN catalog_revision TEXT NOT NULL DEFAULT ''")
            old = db.execute('SELECT light,generation FROM home_connection WHERE id=1').fetchone() if bind_legacy else None
            if old:
                metadata = json.loads(old['light'])
                if isinstance(metadata, dict) and metadata.get('light_id'):
                    db.execute('UPDATE home_operations SET light_id=? WHERE light_id IS NULL AND generation=?', (metadata['light_id'], old['generation']))
            db.execute('CREATE INDEX IF NOT EXISTS home_queue ON home_operations(status,generation,invalidated,expires,created)')
            db.execute('CREATE INDEX IF NOT EXISTS home_recent ON home_operations(generation,created DESC)')
            db.execute('CREATE INDEX IF NOT EXISTS home_session ON home_operations(session,invalidated,status)')
            db.execute("CREATE INDEX IF NOT EXISTS home_read_expiry ON home_operations(expires) WHERE action='read'")
            db.execute("CREATE INDEX IF NOT EXISTS home_compaction ON home_operations(expires) WHERE action!='read' AND session!=''")

    @staticmethod
    def peer(environ):
        forbidden = ('HTTP_FORWARDED', 'HTTP_X_REAL_IP', 'HTTP_CLIENT_IP')
        if environ.get('boomer.direct_peer') is not True or environ.get('wsgi.url_scheme') != 'https' or any(
                key in forbidden or key.startswith(('HTTP_X_FORWARDED', 'HTTP_CF_', 'HTTP_TRUE_CLIENT')) for key in environ):
            raise AccessDenied("Home network access is unavailable.")
        try:
            address = ipaddress.ip_address(environ.get('REMOTE_ADDR', ''))
        except ValueError:
            raise AccessDenied("Home network access is unavailable.") from None
        if address.version != 4 or not address.is_global or address.is_multicast or address.is_unspecified:
            raise AccessDenied("This pilot requires the verified IPv4 website origin.")
        return str(address)

    def _approved(self, db, session_id):
        session = db.execute('SELECT * FROM sessions WHERE id=? AND verified=1 AND expires>?', (session_id, self.store.clock())).fetchone()
        guest = db.execute('SELECT * FROM guests WHERE email=?', (session['email'],)).fetchone() if session else None
        return self.store._active(guest) and bool(guest['email_verified'])

    def _current(self, db, peer):
        current = db.execute('SELECT * FROM home_connection WHERE id=1').fetchone()
        if current is not None and current['peer'] != peer:
            raise AccessDenied("Connect through the home internet connection.")
        if current is None or not current['valid'] or not current['active'] or not 0 <= self.store.clock()-current['seen'] < self.FRESH:
            raise HomeUnavailable("Waiting for the home connection. This can take up to 60 seconds.")
        return current

    def _observe_guest(self, db, raw, peer):
        identity = _digest(raw)
        db.execute("UPDATE home_operations SET invalidated=1 WHERE session=? AND invalidated=0 AND status IN ('queued','leased','authorized') AND (? IS NULL OR peer!=?)", (identity,peer,peer))
        if peer is not None and self._approved(db,identity):
            db.execute('DELETE FROM home_demand WHERE seen<=?', (self.store.clock()-10,))
            db.execute('INSERT OR REPLACE INTO home_demand VALUES (?,?,?)', (identity,peer,self.store.clock()))

    def _guest_peer(self, raw, environ):
        try:
            peer = self.peer(environ)
        except AccessDenied:
            with self.store._transaction() as db:
                self._observe_guest(db,raw,None)
            raise
        with self.store._transaction() as db:
            self._observe_guest(db,raw,peer)
        return peer

    def access(self, raw, environ):
        return self.access_status(raw, environ) == "ready"

    def access_status(self, raw, environ):
        try:
            peer = self._guest_peer(raw, environ)
        except AccessDenied:
            return "unavailable"
        with self.store._transaction() as db:
            if not self._approved(db, _digest(raw)):
                return "unavailable"
            current = db.execute('SELECT * FROM home_connection WHERE id=1').fetchone()
            if current is None or not current['valid']:
                return "unavailable"
            age = self.store.clock() - current['seen']
            # This observation explains access. It grants no device authority.
            observation_limit = self.FRESH if current["active"] else self.idle + self.FRESH
            if current['peer'] != peer and 0 <= age < observation_limit:
                return "source_mismatch"
            if current['peer'] == peer and current['active'] and 0 <= age < self.FRESH:
                return "ready"
            return "unavailable"

    @staticmethod
    def _catalog(current):
        value = json.loads(current['light'])
        return value if isinstance(value, list) else [value]

    def display_lights(self, raw, environ):
        peer = self._guest_peer(raw, environ)
        with self.store._transaction() as db:
            return self._catalog(self._guest(db, raw, peer))

    def display(self, raw, environ):
        values = self.display_lights(raw, environ)
        if len(values) != 1:
            raise HomeUnavailable("Use the selected light catalog.")
        return values[0]

    def _guest(self, db, raw, peer):
        self._observe_guest(db,raw,peer)
        if not self._approved(db, _digest(raw)):
            raise AccessDenied("Current website permission is unavailable.")
        return self._current(db, peer)

    def _compact(self, db):
        cutoff = self.store.clock()-60
        db.execute("DELETE FROM home_operations WHERE action='read' AND expires<?", (cutoff,))
        db.execute("UPDATE home_operations SET peer='',session='' WHERE action!='read' AND session!='' AND expires<? AND (status='completed' OR (status='queued' AND poll_sequence IS NULL))", (cutoff,))

    def enqueue(self, raw, environ, body):
        if not isinstance(body, dict) or set(body) not in ({'operation_id','action'}, {'operation_id','action','light_id'}) or body['action'] not in ('read','on','off'):
            raise ValueError
        operation_id, peer = identifier(body['operation_id']), self._guest_peer(raw,environ)
        now = self.store.clock()
        with self.store._transaction() as db:
            current = self._guest(db, raw, peer)
            catalog = self._catalog(current)
            target = body.get('light_id')
            if 'light_id' not in body and len(catalog) == 1:
                target = catalog[0]['light_id']
            if not isinstance(target, str) or target not in {light['light_id'] for light in catalog}:
                raise AccessDenied("Select an allowed light.")
            self._compact(db)
            previous = db.execute('SELECT * FROM home_operations WHERE id=?', (operation_id,)).fetchone()
            if previous:
                if previous['session'] != _digest(raw) or previous['action'] != body['action'] or previous['generation'] != current['generation'] or previous['light_id'] != target:
                    raise OperationConflict("This operation cannot be reused.")
                return self._projection(previous)
            if db.execute("SELECT 1 FROM home_operations WHERE expires>? AND generation=? AND invalidated=0 AND status IN ('queued','leased','authorized') LIMIT 1", (now,current['generation'])).fetchone():
                raise OperationConflict("Wait for the current operation to finish.")
            if body['action'] != 'read':
                recent = db.execute("SELECT outcome FROM home_operations WHERE generation=? AND light_id=? AND created>? AND invalidated=0 AND outcome IS NOT NULL ORDER BY created DESC LIMIT 1", (current['generation'],target,now-self.FRESH)).fetchone()
                if recent is None or json.loads(recent['outcome'])['status'] != 'confirmed':
                    raise HomeUnavailable("Refresh the confirmed state before another command.")
            # Expired operations remain receipts. They never return to the queue.
            db.execute('INSERT INTO home_operations(id,session,peer,generation,action,status,created,expires,poll_sequence,outcome,completed,light_id,targeted) VALUES (?,?,?,?,?,?,?, ?,NULL,NULL,NULL,?,?)',
                       (operation_id,_digest(raw),peer,current['generation'],body['action'],'queued',now,now+self.TTL,target,int('light_id' in body)))
            return self._projection(db.execute('SELECT * FROM home_operations WHERE id=?', (operation_id,)).fetchone())

    def operation(self, raw, environ, operation_id):
        operation_id, peer = identifier(operation_id), self._guest_peer(raw,environ)
        with self.store._transaction() as db:
            current = self._guest(db, raw, peer)
            row = db.execute('SELECT * FROM home_operations WHERE id=? AND session=?', (operation_id,_digest(raw))).fetchone()
            if row is None or row['generation'] != current['generation']:
                raise AccessDenied("Operation unavailable. Refresh the current state.")
            return self._projection(row)

    def _projection(self, row):
        status, state = row['status'], 'unavailable'
        if row['invalidated']:
            status = 'unknown' if row['status']=='authorized' or row['outcome'] else 'not_started'
        elif row['outcome'] and self.store.clock()-row['created'] >= self.FRESH:
            status = 'unknown'
        elif row['outcome']:
            result = json.loads(row['outcome'])
            status, state = result['status'], result['state']
        elif row['expires'] <= self.store.clock():
            status = 'unknown' if status == 'authorized' else 'not_started'
        result = {'operation_id':row['id'],'status':status,'state':state,'expires':row['expires']}
        if row['targeted']:
            result.update(light_id=row['light_id'], availability='online' if status == 'confirmed' else 'unknown' if status == 'unknown' else 'unavailable')
        return result

    def _observe_agent(self, agent, peer, *, poll_sequence=None, exact=False):
        with self.store._transaction() as db:
            current = db.execute('SELECT * FROM home_connection WHERE id=1').fetchone()
            if current and poll_sequence is not None and (poll_sequence<current['sequence'] or (exact and poll_sequence!=current['sequence'])):
                return
            if current and current['agent']==agent and (peer is None or current['peer']!=peer):
                if current['valid'] or (peer is not None and current['peer']!=peer):
                    db.execute('UPDATE home_connection SET peer=COALESCE(?,peer),generation=generation+1,valid=0,active=0,last_operation=NULL WHERE id=1', (peer,))

    def machine(self, environ, path, body):
        authorization = environ.get('HTTP_AUTHORIZATION', '')
        if not isinstance(authorization,str) or not hmac.compare_digest(authorization.encode(), ('Bearer '+self.key).encode()):
            raise AccessDenied("Home authentication required.")
        if not isinstance(body, dict):
            raise ValueError
        expected = {'agent_id','sequence','request_id'}
        if path == '/home/poll':
            expected |= {'lights', 'catalog_revision'} if 'lights' in body else {'light'}
        if path in ('/home/authorize','/home/result'):
            expected |= {'operation_id','generation'}
        if path == '/home/result':
            expected.add('outcome')
        if path in ('/home/result', '/home/authorize') and 'light_id' in body:
            expected.add('light_id')
        if path not in ('/home/poll','/home/authorize','/home/result') or not isinstance(body,dict) or set(body) != expected:
            raise ValueError
        identifier(body['agent_id']); identifier(body['request_id'])
        if type(body['sequence']) is not int or not 1 <= body['sequence'] <= 2**63-1:
            raise ValueError
        if path != '/home/poll':
            identifier(body['operation_id'])
            if type(body['generation']) is not int or body['generation'] < 1:
                raise ValueError
        try:
            peer = self.peer(environ)
        except AccessDenied:
            self._observe_agent(body['agent_id'],None,poll_sequence=body['sequence'] if path=='/home/poll' else None)
            raise
        if path != '/home/poll':
            self._observe_agent(body['agent_id'],peer)
        else:
            self._observe_agent(body['agent_id'],peer,poll_sequence=body['sequence'],exact=True)
        with self.store._transaction() as db:
            if path == '/home/poll':
                return self._poll(db, peer, body)
            current = db.execute('SELECT * FROM home_connection WHERE id=1').fetchone()
            row = db.execute('SELECT * FROM home_operations WHERE id=?', (body['operation_id'],)).fetchone()
            if current is None or current['agent'] != body['agent_id'] or row is None or row['poll_sequence'] != body['sequence'] or row['generation'] != body['generation']:
                raise OperationConflict("The home assignment no longer matches.")
            if 'light_id' in body and body['light_id'] != row['light_id']:
                raise OperationConflict("The operation belongs to another light.")
            if 'light_id' not in body and row['target_lease']:
                raise OperationConflict("Include the exact selected light.")
            if path == '/home/result':
                return self._result(db, row, body)
            allowed = (not row['invalidated'] and current['valid'] and current['active'] and row['status'] in ('leased','authorized') and row['expires'] > self.store.clock() and
                       current['generation'] == row['generation'] and current['sequence'] == row['poll_sequence'] and current['peer'] == peer and
                       0 <= self.store.clock()-current['seen'] < self.FRESH and self._approved(db,row['session']))
            if allowed:
                db.execute("UPDATE home_operations SET status='authorized' WHERE id=?", (row['id'],))
            return {'allowed':bool(allowed),'request_id':body['request_id'],'operation_id':row['id'],
                    'generation':row['generation'],'expires':min(row['expires'],current['seen']+self.FRESH,self.store.clock()+2)}

    def _poll(self, db, peer, body):
        now = self.store.clock()
        fleet = 'lights' in body
        lights = light_catalog(body['lights']) if fleet else [light_metadata(body['light'])]
        revision = body.get('catalog_revision', '')
        if fleet and (not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{64}', revision)):
            raise ValueError
        encoded = json.dumps(lights if fleet else lights[0], sort_keys=True)
        self._compact(db)
        current = db.execute('SELECT * FROM home_connection WHERE id=1').fetchone()
        if current and (current['agent'] != body['agent_id'] or body['sequence'] < current['sequence']):
            raise OperationConflict("The home poll is stale or belongs to another agent.")
        if current and now < current['seen']:
            raise HomeUnavailable("The home connection clock is unavailable.")
        retry = current and body['sequence'] == current['sequence']
        if retry:
            if current['request_id'] != body['request_id'] or current['peer'] != peer or current['seen']+self.FRESH <= now or not current['valid'] or current['light'] != encoded or current['catalog_revision'] != revision:
                raise OperationConflict("The home poll cannot be replayed.")
            generation = current['generation']
            operation = db.execute('SELECT * FROM home_operations WHERE id=?', (current['last_operation'],)).fetchone()
        else:
            generation = current['generation'] + int(current['peer'] != peer or (current['valid'] and current['seen']+self.FRESH <= now) or current['light'] != encoded or current['catalog_revision'] != revision) if current else 1
            db.execute('INSERT OR REPLACE INTO home_connection(id,agent,sequence,request_id,peer,generation,seen,last_operation,valid,active,light,catalog_revision) VALUES (1,?,?,?,?,?,?,NULL,1,0,?,?)',
                       (body['agent_id'],body['sequence'],body['request_id'],peer,generation,now,encoded,revision))
            operation = db.execute("SELECT * FROM home_operations WHERE status='queued' AND invalidated=0 AND generation=? AND peer=? AND expires>? ORDER BY created LIMIT 1", (generation,peer,now)).fetchone()
            if operation and not self._approved(db,operation['session']):
                db.execute("UPDATE home_operations SET status='denied' WHERE id=?", (operation['id'],))
                operation = None
            if operation:
                db.execute("UPDATE home_operations SET status='leased',poll_sequence=?,target_lease=? WHERE id=?", (body['sequence'],int(fleet),operation['id']))
                db.execute('UPDATE home_connection SET last_operation=? WHERE id=1', (operation['id'],))
        if operation and (operation['invalidated'] or operation['expires'] <= now or operation['status'] not in ('queued','leased')):
            operation = None
        demand = db.execute('SELECT 1 FROM home_demand WHERE peer=? AND seen>? LIMIT 1', (peer,now-10)).fetchone()
        if not retry:
            db.execute('UPDATE home_connection SET active=? WHERE id=1', (int(bool(demand)),))
        assignment = None if operation is None else {'operation_id':operation['id'],'action':operation['action'],
            'session_binding':operation['session'],'generation':generation,'expires':operation['expires']}
        if assignment is not None and fleet:
            assignment['light_id'] = operation['light_id']
        return {'request_id':body['request_id'],'generation':generation,'expires':current['seen']+self.FRESH if retry else now+self.FRESH,
                'poll_after':1 if demand or assignment else self.idle,'operation':assignment}

    def _result(self, db, row, body):
        outcome = body['outcome']
        if not isinstance(outcome,dict) or set(outcome) != {'status','state'} or outcome['status'] not in ('confirmed','unknown','denied','not_started') or outcome['state'] not in ('on','off','unavailable'):
            raise ValueError
        if (outcome['status']=='confirmed') != (outcome['state'] in ('on','off')):
            raise ValueError
        encoded = json.dumps(outcome,sort_keys=True)
        if row['outcome'] is not None and row['outcome'] != encoded:
            raise OperationConflict("The operation already has another result.")
        if row['outcome'] is None:
            if outcome['status']=='confirmed' and row['status'] != 'authorized':
                raise OperationConflict("The operation never received dispatch authority.")
            db.execute("UPDATE home_operations SET outcome=?,status='completed',completed=? WHERE id=?", (encoded,self.store.clock(),row['id']))
        return {'accepted':True,'request_id':body['request_id'],'operation_id':row['id']}
