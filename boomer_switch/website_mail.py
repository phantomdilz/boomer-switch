"""Deliver committed registration mail through the configured website transport."""

from email.message import EmailMessage
import smtplib
import ssl
import subprocess
import secrets


class WebsiteMail:
    """Claim each message once and retain ambiguous outcomes without automatic retry."""

    def __init__(self, store, transport, sender, owner):
        self.store, self.transport = store, transport
        self.sender, self.owner = store._email(sender), store._email(owner)
        with store._transaction() as db:
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
                db.execute("INSERT INTO mail_delivery VALUES (?, 'unknown')", (row["id"],))
                message = EmailMessage()
                message["From"] = self.sender
                message["To"] = row["email"] if row["kind"] == "code" else self.owner
                message["Message-ID"] = f"<house-lights-{row['id']}.{self.store._setting(db, 'mail_identity')}@localhost>"
                if row["kind"] == "code":
                    message["Subject"] = "House Lights verification code"
                    message.set_content(f"Your verification code is {row['code']}. It expires in 5 minutes.\n")
                else:
                    message["Subject"] = "House Lights access request"
                    message.set_content(f"A verified guest requests access. Open the private People page.\nName: {row['name']}\nEmail: {row['email']}\n")
                # OTP plaintext leaves durable storage before the external attempt.
                db.execute("UPDATE mailbox SET code=NULL WHERE id=?", (row["id"],))
            try:
                self.transport.send(message)
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
            client.send_message(message)
        finally:
            client.close()


class SendmailTransport:
    def __init__(self, executable):
        from pathlib import Path
        if not Path(executable).is_absolute():
            raise ValueError("Configure an absolute sendmail path.")
        self.executable = executable

    def send(self, message):
        subprocess.run([self.executable, "-i", "-t"], input=message.as_bytes(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=True)
