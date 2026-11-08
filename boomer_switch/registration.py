"""Persist invitation, verification, and permission decisions for the local demo."""

from contextlib import contextmanager
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import time


class AccessDenied(ValueError):
    """The request cannot establish current access."""


class InvalidInvitation(AccessDenied):
    """The invitation is absent, malformed, or retired."""


class StalePerson(ValueError):
    """The owner action does not match the current person record."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class RegistrationStore:
    """Keep demo registration and fake mail in one SQLite transaction owner."""

    CODE_SECONDS = 300
    WINDOW_SECONDS = 900
    RESEND_SECONDS = 30
    MAX_SENDS = 3
    MAX_ATTEMPTS = 5
    MAX_TOTAL_SENDS = 20
    SESSION_SECONDS = 86400

    def __init__(self, directory: Path, *, clock=time.time, mode="demo"):
        if mode not in ("demo", "production"):
            raise ValueError("Use an explicit supported store mode.")
        existed = (directory / "registration.sqlite").exists()
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "registration.sqlite"
        self.clock = clock
        with self._transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS guests (
                    email TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                    expires REAL, revision INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, invitation TEXT, email TEXT,
                    verified INTEGER NOT NULL DEFAULT 0, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS challenges (
                    session TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL,
                    digest TEXT NOT NULL, expires REAL NOT NULL, invitation TEXT);
                CREATE TABLE IF NOT EXISTS limits (
                    email TEXT PRIMARY KEY, started REAL NOT NULL, last_send REAL NOT NULL,
                    sends INTEGER NOT NULL, attempts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS mailbox (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, email TEXT NOT NULL,
                    code TEXT, name TEXT, reference TEXT, created_at REAL NOT NULL,
                    UNIQUE(kind, email, reference));
                CREATE TABLE IF NOT EXISTS budget (
                    id INTEGER PRIMARY KEY CHECK(id=1), started REAL NOT NULL, sends INTEGER NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            marker = db.execute("SELECT value FROM settings WHERE key='store_mode'").fetchone()
            if marker is None:
                if existed and mode == "production":
                    raise ValueError("Use a fresh production state directory. Existing demo state cannot become production state.")
                db.execute("INSERT INTO settings VALUES ('store_mode', ?)", (mode,))
            elif marker[0] != mode:
                raise ValueError("The state directory belongs to another environment.")
            self.mode = mode
            columns = {row["name"] for row in db.execute("PRAGMA table_info(guests)")}
            if "email_verified" not in columns:
                # Existing guest rows came only from successful inbox verification.
                db.execute("ALTER TABLE guests ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 1")
            for key, value in (("invitation", secrets.token_urlsafe(32)),
                               ("alias", self._alias()), ("key", secrets.token_hex(32))):
                db.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (key, value))

    @staticmethod
    def _alias():
        from mnemonic import Mnemonic

        words = Mnemonic("english").wordlist
        return " ".join(secrets.choice(words) for _ in range(4))

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except AccessDenied:
            # Failed guesses must keep their consumed attempt count.
            db.commit()
            raise
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _setting(db, key):
        return db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0]

    def invitation(self):
        with self._transaction() as db:
            words = self._setting(db, "alias")
            return {"token": self._setting(db, "invitation"), "alias": words.replace(" ", ""), "words": words}

    def rotate(self):
        with self._transaction() as db:
            db.execute("UPDATE settings SET value=? WHERE key='invitation'", (secrets.token_urlsafe(32),))
            db.execute("UPDATE settings SET value=? WHERE key='alias'", (self._alias(),))

    def resolve_alias(self, alias):
        with self._transaction() as db:
            if not re.fullmatch(r"[a-z0-9]{1,64}", alias) or not hmac.compare_digest(alias, self._setting(db, "alias").replace(" ", "")):
                raise InvalidInvitation("Invitation unavailable.")
            return self._setting(db, "invitation")

    def browser(self, invitation=None, *, existing=None):
        raw = secrets.token_urlsafe(32)
        with self._transaction() as db:
            if invitation is not None and (not isinstance(invitation, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", invitation)
                    or not hmac.compare_digest(invitation, self._setting(db, "invitation"))):
                raise InvalidInvitation("Invitation unavailable.")
            previous = db.execute("SELECT * FROM sessions WHERE id=? AND expires>?", (_digest(existing or ""), self.clock())).fetchone()
            if invitation is not None and previous is not None and previous["verified"]:
                guest = db.execute("SELECT * FROM guests WHERE email=?", (previous["email"],)).fetchone()
                if self._active(guest) or (guest is not None and guest["status"] == "pending"):
                    return existing
            if previous is not None and not previous["verified"] and previous["invitation"] == invitation:
                return existing
            if self.mode == "production":
                db.execute("DELETE FROM challenges WHERE session IN (SELECT id FROM sessions WHERE expires<=?)", (self.clock(),))
                db.execute("DELETE FROM sessions WHERE expires<=?", (self.clock(),))
                if db.execute("SELECT COUNT(*) FROM sessions WHERE verified=0").fetchone()[0] >= 1000:
                    raise AccessDenied("Registration is busy. Try again later.")
            db.execute("INSERT INTO sessions(id, invitation, expires) VALUES (?, ?, ?)",
                       (_digest(raw), invitation, self.clock() + self.SESSION_SECONDS))
        return raw

    def reject_browser(self, raw):
        with self._transaction() as db:
            identity = _digest(raw or "")
            current = db.execute("SELECT verified FROM sessions WHERE id=? AND expires>?", (identity, self.clock())).fetchone()
            if current is not None and current["verified"]:
                return False
            db.execute("DELETE FROM challenges WHERE session=?", (identity,))
            db.execute("DELETE FROM sessions WHERE id=?", (identity,))
            return True

    def _session(self, db, raw):
        session = db.execute("SELECT * FROM sessions WHERE id=? AND expires>?", (_digest(raw or ""), self.clock())).fetchone()
        if session is None:
            raise AccessDenied("Open the invitation or verify your email again.")
        return session

    def _active(self, guest):
        return guest is not None and guest["status"] == "approved" and (guest["expires"] is None or guest["expires"] > self.clock())

    def status(self, raw):
        try:
            with self._transaction() as db:
                session = self._session(db, raw)
                if session["verified"]:
                    guest = db.execute("SELECT * FROM guests WHERE email=?", (session["email"],)).fetchone()
                    if self._active(guest):
                        return "approved"
                    return "pending" if guest is not None and guest["status"] == "pending" else "denied"
                if db.execute("SELECT 1 FROM challenges WHERE session=?", (session["id"],)).fetchone():
                    return "verify"
                return "invited" if session["invitation"] == self._setting(db, "invitation") else "anonymous"
        except AccessDenied:
            return "anonymous"

    def require_access(self, raw):
        if self.status(raw) != "approved":
            raise AccessDenied("Verify your email and wait for owner approval.")

    def _code_digest(self, db, session, code):
        return hmac.new(self._setting(db, "key").encode(), f"{session}:{code}".encode(), hashlib.sha256).hexdigest()

    def _issue(self, db, session, name, email):
        guest = db.execute("SELECT * FROM guests WHERE email=?", (email,)).fetchone()
        invitation = None if self._active(guest) else session["invitation"]
        eligible = self._active(guest) or invitation == self._setting(db, "invitation")
        if not eligible and self.mode != "production":
            raise AccessDenied("Use a current invitation before registration.")
        if not eligible:
            invitation = None
        now = self.clock()
        budget = db.execute("SELECT * FROM budget WHERE id=1").fetchone()
        if budget is None or now - budget["started"] >= self.WINDOW_SECONDS:
            db.execute("INSERT OR REPLACE INTO budget VALUES (1, ?, 0)", (now,))
            budget = db.execute("SELECT * FROM budget WHERE id=1").fetchone()
        if budget["sends"] >= self.MAX_TOTAL_SENDS:
            raise AccessDenied("Registration is busy. Try again later.")
        limit = db.execute("SELECT * FROM limits WHERE email=?", (email,)).fetchone()
        if limit is None or now - limit["started"] >= self.WINDOW_SECONDS:
            db.execute("INSERT OR REPLACE INTO limits VALUES (?, ?, ?, 0, 0)", (email, now, now - self.RESEND_SECONDS))
            limit = db.execute("SELECT * FROM limits WHERE email=?", (email,)).fetchone()
        if limit["sends"] >= self.MAX_SENDS or limit["attempts"] >= self.MAX_ATTEMPTS or now - limit["last_send"] < self.RESEND_SECONDS:
            raise AccessDenied("Wait before you request another code.")
        code = f"{secrets.randbelow(1000000):06d}"
        db.execute("UPDATE limits SET sends=sends+1, last_send=? WHERE email=?", (now, email))
        db.execute("UPDATE budget SET sends=sends+1 WHERE id=1")
        db.execute("INSERT OR REPLACE INTO challenges VALUES (?, ?, ?, ?, ?, ?)",
                   (session["id"], email, name, self._code_digest(db, session["id"], code), now + self.CODE_SECONDS, invitation))
        if eligible:
            db.execute("INSERT INTO mailbox(kind,email,code,created_at) VALUES ('code',?,?,?)", (email, code, now))

    def start(self, raw, name, email):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise AccessDenied("Enter a name of at most 80 characters.")
        email = self._email(email)
        with self._transaction() as db:
            session = self._session(db, raw)
            if session["verified"]:
                raise AccessDenied("This browser already verified its email.")
            self._issue(db, session, name.strip(), email)

    @staticmethod
    def _email(email):
        if not isinstance(email, str) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+", email) or len(email) > 254:
            raise AccessDenied("Enter a valid email address.")
        return email.lower()

    def resend(self, raw):
        with self._transaction() as db:
            session = self._session(db, raw)
            challenge = db.execute("SELECT * FROM challenges WHERE session=?", (session["id"],)).fetchone()
            if challenge is None:
                raise AccessDenied("Start email verification first.")
            self._issue(db, session, challenge["name"], challenge["email"])

    def verify(self, raw, code):
        with self._transaction() as db:
            session = self._session(db, raw)
            challenge = db.execute("SELECT * FROM challenges WHERE session=?", (session["id"],)).fetchone()
            if challenge is None or challenge["expires"] <= self.clock():
                raise AccessDenied("Request a new verification code.")
            if challenge["invitation"] is not None and challenge["invitation"] != self._setting(db, "invitation"):
                raise AccessDenied("Use a current invitation before registration.")
            if challenge["invitation"] is None:
                current_guest = db.execute("SELECT * FROM guests WHERE email=?", (challenge["email"],)).fetchone()
                if not self._active(current_guest):
                    if self.mode == "production":
                        limit = db.execute("SELECT * FROM limits WHERE email=?", (challenge["email"],)).fetchone()
                        if limit["attempts"] >= self.MAX_ATTEMPTS:
                            raise AccessDenied("Wait before you try another code.")
                        db.execute("UPDATE limits SET attempts=attempts+1 WHERE email=?", (challenge["email"],))
                        raise AccessDenied("Enter the current verification code.")
                    raise AccessDenied("Current permission is unavailable.")
            limit = db.execute("SELECT * FROM limits WHERE email=?", (challenge["email"],)).fetchone()
            if limit["attempts"] >= self.MAX_ATTEMPTS:
                raise AccessDenied("Wait before you try another code.")
            db.execute("UPDATE limits SET attempts=attempts+1 WHERE email=?", (challenge["email"],))
            if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) or not hmac.compare_digest(challenge["digest"], self._code_digest(db, session["id"], code)):
                raise AccessDenied("Enter the current verification code.")
            guest = db.execute("SELECT * FROM guests WHERE email=?", (challenge["email"],)).fetchone()
            if guest is None:
                db.execute("INSERT INTO guests(email,name,status) VALUES (?,?,'pending')", (challenge["email"], challenge["name"]))
            elif not self._active(guest) and guest["status"] != "pending":
                db.execute("DELETE FROM sessions WHERE email=?", (challenge["email"],))
                db.execute("DELETE FROM challenges WHERE email=?", (challenge["email"],))
                db.execute("UPDATE guests SET name=?,status='pending',expires=NULL,email_verified=1,revision=revision+1 WHERE email=?", (challenge["name"], challenge["email"]))
            elif not guest["email_verified"]:
                db.execute("UPDATE guests SET name=?,email_verified=1,revision=revision+1 WHERE email=?", (challenge["name"], challenge["email"]))
            guest = db.execute("SELECT * FROM guests WHERE email=?", (challenge["email"],)).fetchone()
            if guest["status"] == "pending":
                db.execute("INSERT OR IGNORE INTO mailbox(kind,email,name,reference,created_at) VALUES ('owner_notice',?,?,?,?)",
                           (guest["email"], guest["name"], str(guest["revision"]), self.clock()))
            db.execute("UPDATE sessions SET email=?,verified=1,invitation=NULL WHERE id=?", (challenge["email"], session["id"]))
            db.execute("DELETE FROM challenges WHERE session=?", (session["id"],))

    def operator(self, action, email, *, expires=None):
        self._expiration(expires)
        with self._transaction() as db:
            guest = db.execute("SELECT * FROM guests WHERE email=?", (email.lower(),)).fetchone()
            if guest is None:
                raise AccessDenied("Verify the sample guest first.")
            if action == "approve":
                if guest["status"] == "approved" and not self._active(guest):
                    db.execute("DELETE FROM sessions WHERE email=?", (email.lower(),))
                    db.execute("DELETE FROM challenges WHERE email=?", (email.lower(),))
                db.execute("UPDATE guests SET status='approved', expires=?, revision=revision+1 WHERE email=?", (expires, email.lower()))
            elif action == "revoke":
                db.execute("UPDATE guests SET status='revoked', revision=revision+1 WHERE email=?", (email.lower(),))
                db.execute("DELETE FROM sessions WHERE email=?", (email.lower(),))
                db.execute("DELETE FROM challenges WHERE email=?", (email.lower(),))
            elif action == "expire":
                db.execute("UPDATE guests SET expires=?, revision=revision+1 WHERE email=?", (self.clock() - 1, email.lower()))
                db.execute("DELETE FROM sessions WHERE email=?", (email.lower(),))
                db.execute("DELETE FROM challenges WHERE email=?", (email.lower(),))
            else:
                raise ValueError("Use a supported demo operator action.")

    def people(self):
        with self._transaction() as db:
            return [dict(row) | {"email_verified": bool(row["email_verified"])}
                    for row in db.execute("SELECT email,name,status,expires,revision,email_verified FROM guests ORDER BY email")]

    @staticmethod
    def _expiration(expires):
        # Bound Unix timestamps before float conversion, including oversized JSON integers.
        if expires is not None and (type(expires) not in (int, float) or not -62135596800 <= expires <= 253402300799):
            raise ValueError("Use a finite end date within years 1 through 9999.")

    def change_person(self, action, email, revision, *, expires=None):
        """Commit one versioned owner decision without replay or stale overwrite."""
        email = self._email(email)
        if action not in ("preapprove", "approve", "deny", "remove", "expiry"):
            raise ValueError("Use a supported People action.")
        self._expiration(expires)
        if action != "expiry" and expires is not None:
            raise ValueError("Use the end-date action to change a deadline.")
        with self._transaction() as db:
            current = db.execute("SELECT * FROM guests WHERE email=?", (email,)).fetchone()
            if current is None:
                if action != "preapprove" or revision is not None:
                    raise StalePerson("Refresh People before this action.")
                db.execute("INSERT INTO guests(email,name,status,email_verified) VALUES (?,'','approved',0)", (email,))
                return
            if type(revision) is not int or revision != current["revision"]:
                raise StalePerson("This person changed. Refresh People before this action.")
            if action == "preapprove":
                if current["status"] not in ("denied", "revoked"):
                    raise StalePerson("This person already has a current request or approval.")
                db.execute("UPDATE guests SET status='approved',expires=NULL,revision=revision+1 WHERE email=?", (email,))
            elif action == "approve" and current["status"] == "pending" and current["email_verified"]:
                db.execute("UPDATE guests SET status='approved',expires=NULL,revision=revision+1 WHERE email=?", (email,))
            elif action in ("deny", "remove") and current["status"] == ("pending" if action == "deny" else "approved"):
                db.execute("UPDATE guests SET status=?,revision=revision+1 WHERE email=?", ("denied" if action == "deny" else "revoked", email))
                db.execute("DELETE FROM sessions WHERE email=?", (email,))
                db.execute("DELETE FROM challenges WHERE email=?", (email,))
            elif action == "expiry" and current["status"] == "approved":
                if not self._active(current) or (expires is not None and expires <= self.clock()):
                    db.execute("DELETE FROM sessions WHERE email=?", (email,))
                    db.execute("DELETE FROM challenges WHERE email=?", (email,))
                db.execute("UPDATE guests SET expires=?,revision=revision+1 WHERE email=?", (expires, email))
            else:
                raise StalePerson("This action no longer matches the person's status.")

    def mailbox(self, email):
        with self._transaction() as db:
            return {"messages": [
                {key: row[key] for key in ("kind", "email", "code", "name", "created_at") if row[key] is not None}
                for row in db.execute("SELECT * FROM mailbox WHERE email=? ORDER BY id", (email.lower(),))
            ]}
