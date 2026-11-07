import hashlib
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from pypdf import PdfReader, PdfWriter
import pytest
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from boomer_switch.invitation_pdf import render_invitation_pdf
from boomer_switch.registration import RegistrationStore
from boomer_switch.registration_demo import printable_card


INVITE = "http://localhost:12345/invite/SYNTHETIC_INVITATION"
SHORT = "http://localhost:12345/s/samplewordssamplewordssamplewords"


def synthetic_template(path, *, pages=2, size=(612, 792)):
    canvas = Canvas(str(path), pagesize=size)
    canvas.setAuthor("SYNTHETIC_TEMPLATE_AUTHOR")
    for index in range(pages):
        canvas.setFont("Helvetica-Bold", 18)
        canvas.drawString(80, 740, f"Synthetic layout {index + 1}")
        canvas.setFont("Helvetica", 12)
        canvas.drawString(240, 390, "QR PLACEHOLDER")
        canvas.showPage()
    canvas.save()
    return path


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.smoke
def test_normal_card_flow_adds_two_page_pdf_without_changing_master(tmp_path):
    master = synthetic_template(tmp_path / "private.pdf")
    before = checksum(master)
    state = tmp_path / "state"
    store = RegistrationStore(state)
    result = printable_card(store, state, "http://localhost:12345", pdf_template=master)
    assert Path(result["printable_card"]).exists()
    assert Path(result["printable_pdf"]) == state / "card.pdf"
    reader = PdfReader(result["printable_pdf"])
    assert len(reader.pages) == 2
    for index, page in enumerate(reader.pages):
        assert tuple(page.mediabox) == (0, 0, 612, 792)
        text = page.extract_text()
        assert result["short_url"] in "".join(text.split())
        assert f"Synthetic layout {index + 1}" in text
        assert "SAMPLE - LOCAL DEMO" in text
    assert reader.metadata.get("/Author") is None
    assert reader.metadata["/Title"] == "Sample House Lights invitation"
    assert checksum(master) == before


def test_every_generated_alias_can_fit_the_compact_address_at_readable_size(tmp_path):
    from mnemonic import Mnemonic

    word = max(Mnemonic("english").wordlist, key=lambda item: stringWidth(item, "Helvetica-Bold", 14))
    short = "http://localhost:65535/s/" + word * 4
    master = synthetic_template(tmp_path / "private.pdf")
    output = render_invitation_pdf(master, tmp_path / "sample.pdf", INVITE, short)
    for page in PdfReader(output).pages:
        assert short in "".join(page.extract_text().split())
    sizes = []
    PdfReader(output).pages[0].extract_text(visitor_text=lambda text, _cm, _tm, _font, size: sizes.append((text, size)))
    assert any(word * 4 in text and size >= 14 for text, size in sizes)


def test_master_and_existing_good_output_survive_a_rejected_long_address(tmp_path):
    master = synthetic_template(tmp_path / "private.pdf")
    output = tmp_path / "sample.pdf"
    render_invitation_pdf(master, output, INVITE, SHORT)
    before = checksum(master), checksum(output)
    with pytest.raises(ValueError, match="shorter"):
        render_invitation_pdf(master, output, INVITE, "https://example.invalid/" + "w" * 500)
    assert (checksum(master), checksum(output)) == before
    assert not list(tmp_path.glob(".card-*"))


@pytest.mark.parametrize("kind", ["same-path", "hard-link"])
def test_renderer_refuses_master_output_aliases(tmp_path, kind):
    master = synthetic_template(tmp_path / "private.pdf")
    output = master if kind == "same-path" else tmp_path / "alias.pdf"
    if kind == "hard-link":
        os.link(master, output)
    before = checksum(master)
    with pytest.raises(ValueError, match="separate"):
        render_invitation_pdf(master, output, INVITE, SHORT)
    assert checksum(master) == before


@pytest.mark.parametrize("kind", ["same-html-name", "html-hard-link", "pdf-hard-link"])
def test_actual_cli_never_truncates_the_master_via_either_output(tmp_path, kind):
    state = tmp_path / "state"
    state.mkdir()
    master = synthetic_template(state / "card.html" if kind == "same-html-name" else tmp_path / "private.pdf")
    if kind == "html-hard-link":
        os.link(master, state / "card.html")
    elif kind == "pdf-hard-link":
        os.link(master, state / "card.pdf")
    before = checksum(master)
    root = Path(__file__).parents[1]
    result = subprocess.run([sys.executable, "-m", "boomer_switch.registration_demo", "operator", "--state-dir", str(state),
        "card", "--base-url", "http://localhost:12345", "--pdf-template", str(master)],
        cwd=root, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert checksum(master) == before
    assert result.stdout == ""


def test_html_publication_does_not_follow_an_existing_output_link(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    keep = tmp_path / "keep.txt"
    keep.write_text("SYNTHETIC_KEEP", encoding="utf-8")
    os.link(keep, state / "card.html")
    result = printable_card(RegistrationStore(state), state, "http://localhost:12345")
    assert keep.read_text(encoding="utf-8") == "SYNTHETIC_KEEP"
    assert "House Lights" in (state / "card.html").read_text(encoding="utf-8")
    assert Path(result["printable_card"]) == state / "card.html"


def test_html_symlink_is_replaced_before_the_returned_card_path_is_resolved(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    keep = tmp_path / "keep.txt"
    keep.write_text("SYNTHETIC_KEEP", encoding="utf-8")
    try:
        (state / "card.html").symlink_to(keep)
    except OSError as error:
        if getattr(error, "winerror", None) in (5, 1314) or error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            pytest.skip("This host does not permit file symlinks.")
        raise
    result = printable_card(RegistrationStore(state), state, "http://localhost:12345")
    assert Path(result["printable_card"]) == state / "card.html"
    assert "House Lights" in Path(result["printable_card"]).read_text(encoding="utf-8")
    assert keep.read_text(encoding="utf-8") == "SYNTHETIC_KEEP"
    assert not (state / "card.html").is_symlink()


def test_returned_html_path_uses_new_file_after_leaf_resolution_changes(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    card = state / "card.html"
    card.write_bytes(b"SYNTHETIC_LINK_TARGET")
    keep = tmp_path / "keep.txt"
    keep.write_text("SYNTHETIC_KEEP", encoding="utf-8")
    original_resolve = Path.resolve

    def resolve_leaf(path, *args, **kwargs):
        if path == card and path.read_bytes() == b"SYNTHETIC_LINK_TARGET":
            return original_resolve(keep)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_leaf)
    assert card.resolve() == original_resolve(keep)
    result = printable_card(RegistrationStore(state), state, "http://localhost:12345")
    assert Path(result["printable_card"]) == original_resolve(card)
    assert "House Lights" in card.read_text(encoding="utf-8")
    assert keep.read_text(encoding="utf-8") == "SYNTHETIC_KEEP"


@pytest.mark.parametrize("kind", ["one-page", "wrong-size", "rotated", "encrypted"])
def test_wrong_private_template_profile_is_rejected(tmp_path, kind):
    master = synthetic_template(tmp_path / "private.pdf", pages=1 if kind == "one-page" else 2,
                                size=(600, 792) if kind == "wrong-size" else (612, 792))
    if kind in ("rotated", "encrypted"):
        writer = PdfWriter(clone_from=master)
        if kind == "rotated":
            writer.pages[0].rotate(90)
        else:
            writer.encrypt("SYNTHETIC_PASSWORD")
        writer.write(master)
    with pytest.raises(ValueError):
        render_invitation_pdf(master, tmp_path / "output.pdf", INVITE, SHORT)


def test_cli_pdf_qr_and_printed_address_match_on_both_designs(tmp_path):
    decoder = pytest.importorskip("zxingcpp", reason="Install the demo extra for independent QR decoding.")
    image = pytest.importorskip("PIL.Image")
    renderer = shutil.which("pdftoppm")
    if renderer is None:
        pytest.skip("Poppler is required for the rendered QR acceptance check.")
    master = synthetic_template(tmp_path / "private.pdf")
    state = tmp_path / "state"
    root = Path(__file__).parents[1]
    result = subprocess.run([sys.executable, "-m", "boomer_switch.registration_demo", "operator", "--state-dir", str(state),
        "card", "--base-url", "http://localhost:12345", "--pdf-template", str(master)],
        cwd=root, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    card = json.loads(result.stdout)
    rendered = subprocess.run([renderer, "-r", "144", "-png", card["printable_pdf"], str(tmp_path / "page")],
                              capture_output=True, timeout=15)
    assert rendered.returncode == 0
    pages = sorted(tmp_path.glob("page-*.png"))
    assert len(pages) == 2
    for page in pages:
        with image.open(page) as bitmap:
            codes = decoder.read_barcodes(bitmap)
        assert len(codes) == 1
        assert codes[0].text == card["invite_url"]


def test_production_card_uses_verified_existing_urls_without_sample_labels_or_master_change(tmp_path):
    from boomer_switch.website_card import render_hosted_card
    master = synthetic_template(tmp_path / 'private.pdf')
    before = checksum(master)
    urls = {'verified':True, 'invite_url':'https://site.invalid/lights/invite/'+'x'*43,
            'short_url':'https://site.invalid/lights/s/fourwords'}
    output = render_hosted_card(master, tmp_path / 'private-card.pdf', urls)
    reader = PdfReader(output)
    assert checksum(master) == before and len(reader.pages) == 2
    for page in reader.pages:
        text = page.extract_text()
        assert 'SAMPLE' not in text and 'not a live invitation' not in text
        assert urls['short_url'] in ''.join(text.split())
        assert 'HOUSE LIGHTS INVITATION' in text
    assert reader.metadata['/Title'] == 'House Lights invitation'
    assert reader.metadata.get('/Author') is None
    with pytest.raises(ValueError):
        render_hosted_card(master, tmp_path / 'rejected.pdf', urls | {'verified':False})
    with pytest.raises(ValueError):
        render_hosted_card(master, tmp_path / 'rejected.pdf', urls | {'short_url':'https://other.invalid/lights/s/alias'})
    with pytest.raises(ValueError):
        render_hosted_card(master, master, urls)


def test_card_cli_does_not_overwrite_verified_url_record(tmp_path):
    master = synthetic_template(tmp_path / 'master.pdf')
    record = tmp_path / 'config.local.json'
    record.write_text(json.dumps({'verified':True,'invite_url':'https://site.invalid/lights/invite/'+'x'*43,
                                  'short_url':'https://site.invalid/lights/s/alias'}))
    before = checksum(record)
    result = subprocess.run([sys.executable,'-m','boomer_switch.website_card','--verified-urls',str(record),
                             '--template',str(master),'--output',str(record)], capture_output=True, timeout=10)
    assert result.returncode != 0 and checksum(record) == before
