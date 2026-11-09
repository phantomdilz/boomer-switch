"""Keep the command journal and route this module to the outbound home runner."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3


class DuplicateCommand(ValueError):
    """This operation ID already has a recorded submission."""


class CommandJournal:
    """Reserve operation IDs durably before a possible physical effect."""

    def __init__(self, path, device_id, *, allow_fleet=False):
        self.path = Path(path)
        self.identity = hashlib.sha256(device_id.encode()).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if not allow_fleet and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_targets'").fetchone():
                    raise ValueError("Use the multi-light runner for this journal.")
                db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,status TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS pilot_identity (id INTEGER PRIMARY KEY CHECK(id=1),binding TEXT NOT NULL)")
                bound = db.execute("SELECT binding FROM pilot_identity WHERE id=1").fetchone()
                if bound is None:
                    if db.execute("SELECT 1 FROM commands LIMIT 1").fetchone():
                        raise ValueError("Preserve and reconcile the existing unbound command journal before use.")
                    db.execute("INSERT INTO pilot_identity VALUES (1,?)", (self.identity,))
                elif bound[0] != self.identity:
                    raise ValueError("This journal belongs to another physical pilot device.")
        finally:
            db.close()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=1)

    def reserve(self, identity, light_id, action, request_id):
        fingerprint = hashlib.sha256(json.dumps([identity, light_id, action]).encode()).hexdigest()
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM commands WHERE status IN ('started','dispatched','unknown') LIMIT 1").fetchone():
                    raise DuplicateCommand("Read confirmed state before another command.")
                db.execute("INSERT INTO commands VALUES (?,?,'reserved')", (request_id, fingerprint))
        except sqlite3.IntegrityError:
            raise DuplicateCommand("Command already submitted. Read state before a new command.") from None
        finally:
            db.close()

    def dispatched(self, request_id):
        db = self._connect()
        try:
            with db:
                changed = db.execute("UPDATE commands SET status='dispatched' WHERE id=? AND status='reserved'", (request_id,)).rowcount
                if changed != 1:
                    raise DuplicateCommand("Command already submitted.")
        finally:
            db.close()

    def reconcile(self):
        db = self._connect()
        try:
            with db:
                db.execute("UPDATE commands SET status='reconciled' WHERE status IN ('started','dispatched','unknown')")
        finally:
            db.close()


@contextmanager
def runtime_lock(database):
    """Allow one normal pilot process to own a command journal."""
    path = Path(str(database) + ".lock")
    with path.open("a+b") as file:
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Another pilot process owns this command journal.") from None
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def main():
    from .home_runner import main as outbound_main

    outbound_main()


if __name__ == "__main__":
    main()
