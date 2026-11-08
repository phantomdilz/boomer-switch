"""Keep target identity and command reconciliation in the outbound journal."""

import hashlib
import json
import sqlite3

from .home_runner import OutboundJournal
from .pilot import DuplicateCommand


class FleetJournal(OutboundJournal):
    fleet = True

    def __init__(self, path, lights, website_url, *, clock):
        lights = tuple(lights)
        super().__init__(path, lights[0].device_id, website_url, clock=clock)
        self.targets = {light.light_id: hashlib.sha256(light.device_id.encode()).hexdigest() for light in lights}
        with self._transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            first = not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_targets'").fetchone()
            db.execute("CREATE TABLE IF NOT EXISTS fleet_targets(light_id TEXT PRIMARY KEY,binding TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS command_targets(operation_id TEXT PRIMARY KEY,light_id TEXT NOT NULL,binding TEXT NOT NULL)")
            for light_id, binding in self.targets.items():
                previous = db.execute("SELECT binding FROM fleet_targets WHERE light_id=?", (light_id,)).fetchone()
                if previous and previous[0] != binding:
                    raise ValueError("Preserve the physical binding of each saved light ID.")
                db.execute("INSERT OR IGNORE INTO fleet_targets VALUES (?,?)", (light_id, binding))
            if first:
                # Legacy marks belong only to the journal's original physical device.
                db.execute("INSERT INTO command_targets SELECT id,?,? FROM commands", (lights[0].light_id, self.identity))
        metadata = [(light.light_id, light.name, light.room, self.targets[light.light_id]) for light in lights]
        self.catalog_revision = hashlib.sha256(json.dumps(sorted(metadata)).encode()).hexdigest()

    def reserve_target(self, identity, light_id, action, request_id):
        binding = self.targets[light_id]
        fingerprint = hashlib.sha256(json.dumps([identity, light_id, action]).encode()).hexdigest()
        try:
            with self._transaction() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM commands c JOIN command_targets t ON t.operation_id=c.id WHERE t.binding=? AND c.status IN ('started','dispatched','unknown') LIMIT 1", (binding,)).fetchone():
                    raise DuplicateCommand("Read confirmed state for this light before another command.")
                db.execute("INSERT INTO commands VALUES (?,?,'reserved')", (request_id, fingerprint))
                db.execute("INSERT INTO command_targets VALUES (?,?,?)", (request_id, light_id, binding))
        except sqlite3.IntegrityError:
            raise DuplicateCommand("Command already submitted. Read state before a new command.") from None

    def reconcile_target(self, light_id):
        with self._transaction() as db:
            db.execute("UPDATE commands SET status='reconciled' WHERE status IN ('started','dispatched','unknown') AND id IN (SELECT operation_id FROM command_targets WHERE binding=?)", (self.targets[light_id],))

    def reserve(self, *args):
        raise ValueError("Reserve the exact selected light.")

    def reconcile(self):
        raise ValueError("Reconcile only the confirmed light.")
