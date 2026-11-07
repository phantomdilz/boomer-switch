"""Render a private production card from operator-verified existing URLs."""

import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .invitation_pdf import render_invitation_pdf, protect_template


def render_hosted_card(template, output, verified):
    if not isinstance(verified, dict) or set(verified) != {"verified", "invite_url", "short_url"} or verified["verified"] is not True:
        raise ValueError("Supply the operator-verified production URL record.")
    invite, short = verified["invite_url"], verified["short_url"]
    for address, pattern in ((invite, r".*/invite/[A-Za-z0-9_-]{32,128}"), (short, r".*/s/[a-z0-9]{1,64}")):
        if not isinstance(address, str):
            raise ValueError("Use verified production invitation URLs.")
        target = urlsplit(address)
        if target.scheme != "https" or not target.netloc or target.username or target.query or target.fragment or not re.fullmatch(pattern, target.path):
            raise ValueError("Use verified production invitation URLs.")
    if urlsplit(invite).netloc != urlsplit(short).netloc or invite.rsplit('/invite/', 1)[0] != short.rsplit('/s/', 1)[0]:
        raise ValueError("Use the same verified website namespace for both URLs.")
    return render_invitation_pdf(template, output, invite, short, production=True)


def main():
    parser = argparse.ArgumentParser(description="Create a private hosted invitation PDF.")
    parser.add_argument("--verified-urls", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    protect_template(options.verified_urls, options.output)
    render_hosted_card(options.template, options.output, json.loads(options.verified_urls.read_text()))
    print("Invitation PDF created.")


if __name__ == "__main__":
    main()
