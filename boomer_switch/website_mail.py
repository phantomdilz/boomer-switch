"""Deliver committed registration mail through the configured website transport."""

from email.message import EmailMessage
import smtplib
import ssl
import subprocess
import secrets
import hmac

from .registration import RegistrationStore


class WebsiteMail:
    """Claim each message once and retain ambiguous outcomes without automatic retry."""

    def __init__(self, store, transport, sender, owner):
        self.store, self.transport = store, transport
        self.sender, self.owner = store._email(sender), store._email(owner)
        with store._transaction() as db:
            environment = db.execute("SELECT value FROM settings WHERE key='store_mode'").fetchone()
            if environment is None or environment[0] != 'production':
                raise ValueError("Historical demo mail cannot become live mail. Use production state.")
            marker = db.execute("SELECT value FROM settings WHERE key='mail_mode'").fetchone()
            if marker is None:
                if db.execute("SELECT 1 FROM mailbox LIMIT 1").fetchone():
                    raise ValueError("Use a fresh production state directory. Historical demo mail cannot become live mail.")
                db.execute("INSERT INTO settings VALUES ('mail_mode', 'production')")
            elif marker[0] != 'production':
                raise ValueError("Use a production mail store.")
            db.execute("CREATE TABLE IF NOT EXISTS mail_delivery (id INTEGER PRIMARY KEY, state TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('mail_identity', ?)", (secrets.token_hex(16),))

    def deliver(self, maximum=4):
        if type(maximum) is not int or not 1 <= maximum <= 4:
            raise ValueError("Use a mail batch from 1 through 4.")
        delivered = 0
        for _ in range(maximum):
            with self.store._transaction() as db:
                row = db.execute("SELECT m.* FROM mailbox m LEFT JOIN mail_delivery d ON d.id=m.id WHERE d.id IS NULL ORDER BY m.id LIMIT 1").fetchone()
                if row is None:
                    break
                if row["kind"] == "code":
                    candidates = db.execute("SELECT * FROM challenges WHERE email=? AND expires>?", (row["email"], self.store.clock())).fetchall()
                    current = row["code"] is not None and any(hmac.compare_digest(item["digest"], self.store._code_digest(db, item["session"], row["code"])) for item in candidates)
                else:
                    person = db.execute("SELECT * FROM guests WHERE email=? AND status='pending'", (row["email"],)).fetchone()
                    current = person is not None and str(person["revision"]) == row["reference"]
                if not current:
                    db.execute("INSERT INTO mail_delivery VALUES (?, 'discarded')", (row["id"],))
                    db.execute("UPDATE mailbox SET code=NULL WHERE id=?", (row["id"],))
                    continue
                db.execute("INSERT INTO mail_delivery VALUES (?, 'unknown')", (row["id"],))
                message = EmailMessage()
                message["From"] = self.sender
                message["To"] = row["email"] if row["kind"] == "code" else self.owner
                message["Message-ID"] = f"<house-lights-{row['id']}.{self.store._setting(db, 'mail_identity')}@localhost>"
                if row["kind"] == "code":
                    message["Subject"] = "House Lights verification code"
                    message.set_content(f"Your verification code is {row['code']}. It expires in {self.store.CODE_SECONDS // 60} minutes.\n")
                else:
                    message["Subject"] = "House Lights access request"
                    message.set_content(f"A verified guest requests access. Open the private People page.\nName: {row['name']}\nEmail: {row['email']}\n")
                # OTP plaintext leaves durable storage before the external attempt.
                db.execute("UPDATE mailbox SET code=NULL WHERE id=?", (row["id"],))
            try:
                self.transport.send(message)
            except MailPaused:
                # No provider attempt occurred. Keep the unsent message recoverable.
                with self.store._transaction() as db:
                    db.execute("DELETE FROM mail_delivery WHERE id=? AND state='unknown'", (row['id'],))
                    db.execute("UPDATE mailbox SET code=? WHERE id=?", (row['code'], row['id']))
                raise MailUnavailable("Website mail is paused. Try again after service resumes.") from None
            except Exception:
                # The provider can accept a message before the connection fails.
                # Keep the claim and never retry this operation automatically.
                raise MailUnavailable("Mail delivery is unconfirmed. Wait before requesting a new code.") from None
            with self.store._transaction() as db:
                db.execute("UPDATE mail_delivery SET state='sent' WHERE id=?", (row["id"],))
            delivered += 1
        return delivered


class MailUnavailable(RuntimeError):
    """Mail delivery requires an operator check or a new bounded request."""


class MailPaused(RuntimeError):
    """The transport refused before any provider attempt."""


class SMTPTransport:
    def __init__(self, host, port, username, password, *, implicit_tls=False):
        if not isinstance(host, str) or not host or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Configure a valid SMTP endpoint.")
        self.host, self.port = host, port
        self.username, self.password, self.implicit_tls = username, password, implicit_tls

    def send(self, message):
        context = ssl.create_default_context()
        client = smtplib.SMTP_SSL(self.host, self.port, timeout=5, context=context) if self.implicit_tls else smtplib.SMTP(self.host, self.port, timeout=5)
        try:
            if not self.implicit_tls:
                client.starttls(context=context)
            if self.username:
                client.login(self.username, self.password)
            client.send_message(message, from_addr=RegistrationStore._email(str(message["From"])),
                                to_addrs=[RegistrationStore._email(str(message["To"]))])
        finally:
            client.close()


class SendmailTransport:
    def __init__(self, executable):
        from pathlib import Path
        if not Path(executable).is_absolute():
            raise ValueError("Configure an absolute sendmail path.")
        self.executable = executable

    def send(self, message):
        sender = RegistrationStore._email(str(message["From"]))
        recipient = RegistrationStore._email(str(message["To"]))
        subprocess.run([self.executable, "-i", "-f", sender, "--", recipient], input=message.as_bytes(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=True)
