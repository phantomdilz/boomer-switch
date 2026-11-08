"""Run one bounded PHP-to-CGI request without a persistent Python service."""

from io import BytesIO
import json
import os
from pathlib import Path
import sys

from .registration import RegistrationStore
from .home_rendezvous import HomeRendezvous
from .website import WebsiteApplication
from .website_mail import WebsiteMail, SMTPTransport, SendmailTransport
from .owner_notifications import OwnerNotifications
from .mail_signing import configured_signer


def create_application(config):
    state = Path(config["state_directory"])
    if not state.is_absolute():
        raise ValueError("Configure an absolute private state directory.")
    store = RegistrationStore(state, mode="production")
    mail_config = config["mail"]
    signer = configured_signer(mail_config, state)
    if mail_config["transport"] == "smtp":
        transport = SMTPTransport(mail_config["host"], mail_config["port"], mail_config.get("username"), mail_config.get("password"), implicit_tls=mail_config.get("implicit_tls", False), signer=signer)
    elif mail_config["transport"] == "sendmail":
        transport = SendmailTransport(mail_config["executable"], signer=signer)
    else:
        raise ValueError("Configure the website mail transport.")
    owner = config.get('owner_notifications')
    notices = None
    if owner is not None:
        if not isinstance(owner, dict) or set(owner) != {'command', 'decision_key'} or owner['decision_key'] == config['service_key']:
            raise ValueError("Configure a separate private owner-action capability.")
        notices = OwnerNotifications(store, owner['command'], owner['decision_key'], config['origin']+config['mount']+'/people')
    mail = WebsiteMail(store, transport, mail_config["sender"], owner_notifications=notices)
    rendezvous = HomeRendezvous(store, config["service_key"], idle_poll_seconds=config.get("idle_poll_seconds", 15))
    return WebsiteApplication(store, mail, origin=config["origin"], mount=config["mount"],
                              owner_identity=config["owner_identity"], grant_app=None, rendezvous=rendezvous, owner_actions=notices)


def main():
    try:
        config = json.loads(Path(os.environ["BOOMER_PRIVATE_CONFIG"]).read_text())
        app = create_application(config)
        length = int(os.environ.get("CONTENT_LENGTH") or 0)
        if not 0 <= length <= 4096:
            raise ValueError
        environ = {key: os.environ[key] for key in ("REQUEST_METHOD", "PATH_INFO", "SCRIPT_NAME", "CONTENT_TYPE", "CONTENT_LENGTH", "REMOTE_USER", "HTTP_COOKIE", "HTTP_ORIGIN", "HTTP_X_CSRF_TOKEN", "HTTP_AUTHORIZATION", "REMOTE_ADDR") if key in os.environ}
        environ["wsgi.url_scheme"] = "https" if os.environ.get("BOOMER_HTTPS") == "1" else "http"
        environ["boomer.direct_peer"] = os.environ.get("BOOMER_DIRECT_SOURCE") == "1"
        environ["wsgi.input"] = BytesIO(sys.stdin.buffer.read(length))
        response = []
        def start_response(status, headers, exc_info=None):
            response.append((status, headers))
        body = b"".join(app(environ, start_response))
        status, headers = response[0]
        prefix = "Status: " + status + "\r\n" + "".join(name+": "+value+"\r\n" for name,value in headers) + "\r\n"
        sys.stdout.buffer.write(prefix.encode("ascii")+body)
    except Exception:
        # Never emit private configuration, message contents or provider errors.
        sys.stdout.buffer.write(b'Status: 503 Service Unavailable\r\nContent-Type: application/json\r\nCache-Control: no-store\r\n\r\n{"error":"Website service unavailable."}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
