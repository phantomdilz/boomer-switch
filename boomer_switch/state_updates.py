"""Share bounded automatic reads and device reports through the operation ledger."""

import json
import math


class StateUpdates:
    INTERVAL = 2
    TARGET_INTERVAL = 10
    FRESH = 45
    CLIENT_INTERVAL_MS = 2000
    CONFIRMED_SQL = "outcome IN ('{\"state\": \"off\", \"status\": \"confirmed\"}','{\"state\": \"on\", \"status\": \"confirmed\"}')"

    @staticmethod
    def install(db):
        if 'background' not in {row['name'] for row in db.execute('PRAGMA table_info(home_operations)')}:
            db.execute('ALTER TABLE home_operations ADD COLUMN background INTEGER NOT NULL DEFAULT 0')
        db.execute('CREATE INDEX IF NOT EXISTS home_background ON home_operations(background,created DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS home_reads ON home_operations(generation,action,created DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS home_reports ON home_operations(generation,light_id,created DESC,id DESC) WHERE invalidated=0 AND outcome IS NOT NULL')
        db.execute('CREATE INDEX IF NOT EXISTS home_unconfirmed ON home_operations(generation,light_id,created DESC,id DESC) WHERE invalidated=0 AND outcome IS NULL')
        db.execute('CREATE INDEX IF NOT EXISTS home_confirmations ON home_operations(generation,light_id,created DESC,id DESC) WHERE invalidated=0 AND '+StateUpdates.CONFIRMED_SQL)
        db.execute("INSERT OR IGNORE INTO settings VALUES ('automatic_phase','0')")

    @classmethod
    def rows(cls, db, generation, catalog):
        fields = 'id,light_id,created,outcome,action,status,expires'
        rows = {}
        # Bounded indexed seeks per selected target keep old command truth visible
        # without scanning the permanent mutation receipts on every refresh.
        for light in catalog:
            for predicate in ('outcome IS NOT NULL', cls.CONFIRMED_SQL, 'outcome IS NULL', "outcome IS NULL AND action!='read'"):
                row = db.execute('SELECT '+fields+' FROM home_operations WHERE generation=? AND light_id=? AND invalidated=0 AND '+predicate+' ORDER BY created DESC,id DESC LIMIT 1',
                                 (generation,light['light_id'])).fetchone()
                if row is not None:
                    rows[row['id']] = row
        return sorted(rows.values(), key=lambda row: (row['created'], row['id']), reverse=True)

    @classmethod
    def snapshots(cls, db, current, catalog, now):
        latest, confirmed = {}, {}
        for row in cls.rows(db, current['generation'], catalog):
            target = row['light_id']
            if target is None:
                continue
            if row['outcome'] is None and row['expires'] > now and (row['action']=='read' or row['status']=='queued'):
                continue
            outcome = json.loads(row['outcome']) if row['outcome'] else {'status':'not_started' if row['status']=='queued' else 'unknown','state':'unavailable'}
            latest.setdefault(target, (row, outcome))
            if outcome['status'] == 'confirmed' and outcome['state'] in ('on', 'off'):
                confirmed.setdefault(target, (row, outcome))

        def age(row):
            value = now-row['created']
            return math.ceil(value*1000) if math.isfinite(value) and value >= 0 else None

        result = []
        for light in catalog:
            target = light['light_id']
            item = {'light_id': target, 'state': 'unavailable', 'availability': 'unchecked',
                    'age_ms': None, 'sample': None, 'last_confirmed': None}
            if target in confirmed:
                row, outcome = confirmed[target]
                item['last_confirmed'] = {'state': outcome['state'], 'age_ms': age(row)}
            if target in latest:
                row, outcome = latest[target]
                elapsed = age(row)
                item.update(age_ms=elapsed, sample=row['created'])
                if elapsed is None:
                    item['availability'] = 'unknown'
                elif outcome['status'] == 'confirmed' and outcome['state'] in ('on', 'off'):
                    item.update(state=outcome['state'], availability='online' if elapsed < cls.FRESH*1000 else 'stale')
                else:
                    item['availability'] = 'unavailable' if outcome['status'] in ('denied', 'not_started') else 'unknown'
            result.append(item)
        return result

    @classmethod
    def target(cls, db, current, catalog, preferred, now):
        last = db.execute('SELECT MAX(created) FROM home_operations WHERE background=1').fetchone()[0]
        if last is not None and now-last < cls.INTERVAL:
            return None
        attempts = {row['light_id']: row['attempt'] for row in db.execute(
            "SELECT light_id,MAX(created) AS attempt FROM (SELECT light_id,created FROM home_operations WHERE generation=? AND created>=? UNION ALL SELECT light_id,created FROM home_operations WHERE generation=? AND action='read' AND created<?) WHERE light_id IS NOT NULL GROUP BY light_id",
            (current['generation'],now-60,current['generation'],now-60))}
        eligible = [item['light_id'] for item in catalog
                    if item['light_id'] not in attempts or now-attempts[item['light_id']] >= cls.TARGET_INTERVAL]
        if not eligible:
            return None
        eligible.sort(key=lambda target: (attempts.get(target, float('-inf')), target))
        phase = int(db.execute("SELECT value FROM settings WHERE key='automatic_phase'").fetchone()[0])
        if phase not in (0, 1, 2):
            raise ValueError('Automatic read scheduling is unavailable.')
        # Every third admission serves the oldest target across the full catalog.
        candidates = [target for target in eligible if target in preferred] if phase != 2 else []
        chosen = (candidates or eligible)[0]
        db.execute("UPDATE settings SET value=? WHERE key='automatic_phase'", (str((phase+1) % 3),))
        return chosen

    @staticmethod
    def read_keepers(db, current):
        if current is None:
            return set()
        attempted, confirmed, keep = set(), set(), set()
        rows = db.execute("SELECT id,light_id,outcome FROM home_operations WHERE generation=? AND action='read' AND invalidated=0 AND light_id IS NOT NULL ORDER BY created DESC,id DESC", (current['generation'],))
        for row in rows:
            target = row['light_id']
            if target not in attempted:
                keep.add(row['id']); attempted.add(target)
            if target not in confirmed and row['outcome'] and json.loads(row['outcome'])['status'] == 'confirmed':
                keep.add(row['id']); confirmed.add(target)
        return keep
