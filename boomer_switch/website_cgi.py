"""Run one bounded PHP-to-CGI request without a persistent Python service."""

from io import BytesIO
import json
import os
from pathlib import Path
import sys

from .registration import RegistrationStore
from .website import WebsiteApplication
from .website_handoff import WebsiteHandoff
from .website_mail import WebsiteMail, SMTPTransport, SendmailTransport


def create_application(config, *, grant_app=None):
    state = Path(config["state_directory"])
    if not state.is_absolute():
        raise ValueError("Configure an absolute private state directory.")
    store = RegistrationStore(state, mode="production")
    mail_config = config["mail"]
    if mail_config["transport"] == "smtp":
        transport = SMTPTransport(mail_config["host"], mail_config["port"], mail_config.get("username"), mail_config.get("password"), implicit_tls=mail_config.get("implicit_tls", False))
    elif mail_config["transport"] == "sendmail":
        transport = SendmailTransport(mail_config["executable"])
    else:
        raise ValueError("Configure the website mail transport.")
    mail = WebsiteMail(store, transport, mail_config["sender"], mail_config["owner"])
    handoff = WebsiteHandoff(store, config["service_key"], config["home_callback"], config["home_entry"])
    return WebsiteApplication(store, mail, origin=config["origin"], mount=config["mount"],
                              owner_identity=config["owner_identity"], grant_app=grant_app, handoff=handoff)


def main():
    try:
        config = json.loads(Path(os.environ["BOOMER_PRIVATE_CONFIG"]).read_text())
        from .website_grants import create_website_grant_application
        app = create_application(config)
        app.grant_app = create_website_grant_application(app.store.path, config["service_key"])
        length = int(os.environ.get("CONTENT_LENGTH") or 0)
        if not 0 <= length <= 4096:
            raise ValueError
        environ = {key: os.environ[key] for key in ("REQUEST_METHOD", "PATH_INFO", "SCRIPT_NAME", "CONTENT_TYPE", "CONTENT_LENGTH", "REMOTE_USER", "HTTP_COOKIE", "HTTP_ORIGIN", "HTTP_X_CSRF_TOKEN", "HTTP_AUTHORIZATION") if key in os.environ}
        environ["wsgi.url_scheme"] = "https" if os.environ.get("BOOMER_HTTPS") == "1" else "http"
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
