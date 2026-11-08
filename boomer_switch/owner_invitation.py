"""Render owner-only invitation cards from the canonical current invitation."""

from html import escape
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from .website_card import render_hosted_card


class OwnerInvitationCards:
    def __init__(self, store, base_url, template=None):
        self.store, self.base_url = store, base_url
        self.template = Path(template) if template is not None else None
        if self.template is not None and not self.template.is_absolute():
            raise ValueError("Configure an absolute private invitation template path.")

    def urls(self, invitation=None):
        invitation = self.store.invitation() if invitation is None else invitation
        return {"verified": True,
                "invite_url": self.base_url + "/guest#" + urlencode({"invitation": invitation["token"]}),
                "short_url": self.base_url + "/s/" + invitation["alias"]}

    def html(self):
        import segno

        urls = self.urls()
        svg = BytesIO()
        segno.make(urls["invite_url"], micro=False, error="m").save(svg, kind="svg", scale=6)
        artwork = '<p><a href="invitation.pdf">Download the two-design PDF</a></p>' if self.template else ""
        return ('''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>House Lights invitation</title><link rel="stylesheet" href="style.css"></head>
<body><main class="invitation-card"><h1>House Lights</h1>
<p>Scan this code or type the address below. Verify your email to request access.</p>
<div class="invitation-qr">''' + svg.getvalue().decode() + '''</div>
<p class="invitation-address">''' + escape(urls["short_url"]) + '''</p>
<div class="print-help"><p>Use your browser's Print command to print this card.</p>''' + artwork + '''
<p>This card is current when opened. After replacement, open the card again.</p>
<a href="people">Return to People</a></div></main></body></html>''').encode()

    def prepare(self, invitation):
        import segno

        urls = self.urls(invitation)
        segno.make(urls["invite_url"], micro=False, error="m")
        if self.template is not None:
            self.pdf(urls)

    def pdf(self, urls=None):
        if self.template is None:
            raise ValueError("Configure the private two-design invitation template.")
        with TemporaryDirectory(prefix="house-lights-card-") as directory:
            output = Path(directory) / "invitation.pdf"
            try:
                render_hosted_card(self.template, output, self.urls() if urls is None else urls)
            except Exception:
                raise ValueError("The invitation template is unavailable. Keep the current card and check the private template.") from None
            return output.read_bytes()
