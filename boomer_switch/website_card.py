"""Render a private production card from operator-verified existing URLs."""

import argparse
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .invitation_pdf import render_invitation_pdf, protect_template


def render_hosted_card(template, output, verified):
    if not isinstance(verified, dict) or set(verified) != {"verified", "invite_url", "short_url"} or verified["verified"] is not True:
        raise ValueError("Supply the operator-verified production URL record.")
    invite, short = verified["invite_url"], verified["short_url"]
    targets = []
    for address in (invite, short):
        if not isinstance(address, str):
            raise ValueError("Use verified production invitation URLs.")
        target = urlsplit(address)
        if (not address.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in address)
                or target.scheme != "https" or not target.hostname or target.username is not None or target.password is not None or target.query):
            raise ValueError("Use verified production invitation URLs.")
        if target.port == 0:
            raise ValueError("Use a reachable HTTPS origin.")
        targets.append(target)
    invitation, redirect = targets
    namespace = r"((?:/[A-Za-z0-9_-]+)*)"
    legacy = re.fullmatch(namespace + r"/invite/([A-Za-z0-9_-]{32,128})", invitation.path)
    canonical = re.fullmatch(namespace + r"/guest", invitation.path)
    if legacy and not invitation.fragment:
        base, token = legacy.groups()
    elif canonical:
        fragment = parse_qsl(invitation.fragment, strict_parsing=True, keep_blank_values=True)
        if len(fragment) != 1 or fragment[0][0] != "invitation" or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", fragment[0][1]):
            raise ValueError("Use one invitation fragment.")
        base, token = canonical[1], fragment[0][1]
    else:
        raise ValueError("Use a current invitation URL.")
    short_path = re.fullmatch(namespace + r"/s/[a-z0-9]{1,64}", redirect.path)
    if invitation.netloc != redirect.netloc or not short_path or short_path[1] != base or redirect.fragment:
        raise ValueError("Use the same verified website namespace for both URLs.")
    invite = urlunsplit(("https", invitation.netloc, base + "/guest", "", urlencode({"invitation": token})))
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
