"""Place sample invitation values on an explicitly supplied private PDF template."""

from io import BytesIO
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


def protect_template(template: Path, *outputs: Path):
    template = Path(template)
    for destination in outputs:
        destination = Path(destination)
        if template.resolve() == destination.resolve() or (destination.exists() and os.path.samefile(template, destination)):
            raise ValueError("Choose outputs separate from the blank master.")


def render_invitation_pdf(template: Path, output: Path, invite_url: str, short_url: str, *, production=False) -> Path:
    """Render both retained US-Letter designs without changing the blank master."""
    import segno
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen.canvas import Canvas

    template, output = Path(template), Path(output)
    protect_template(template, output)
    for address in (invite_url, short_url):
        if not isinstance(address, str) or not address.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in address):
            raise ValueError("Use a printable ASCII invitation address.")
        parsed = urlsplit(address)
        if production and (parsed.scheme != "https" or parsed.query):
            raise ValueError("Use a verified HTTPS production invitation address.")
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Use an explicit invitation address without credentials.")
    if template.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Use a private template smaller than 20 MiB.")
    reader = PdfReader(BytesIO(template.read_bytes()))
    if reader.is_encrypted or len(reader.pages) != 2 or reader.get_fields():
        raise ValueError("Use the blank two-page static template.")
    for page in reader.pages:
        if tuple(float(value) for value in page.mediabox) != (0, 0, 612, 792) or page.rotation != 0 or tuple(page.cropbox) != tuple(page.mediabox):
            raise ValueError("Use the unrotated US-Letter template without a crop.")
        if any(annotation.get_object().get("/Subtype") == "/Widget" for annotation in page.get("/Annots", [])):
            raise ValueError("Use a static template without form widgets.")

    # Slot coordinates describe the two layouts. The private artwork stays external.
    layouts = (
        {"qr": (195, 291, 216), "area": (150, 164, 312, 40), "url": (152, 183.5, 166.5, 308, 12, 14), "label": (460, 198, 7), "footer": 39},
        {"qr": (150.6, 249, 302.4), "area": (88, 70, 437, 57), "url": (90.4, 101, 78, 431.2, 14, 18), "label": (521.6, 119, 9.5), "footer": 20},
    )
    prefix, separator, alias = short_url.rpartition("/")
    if not separator or not alias:
        raise ValueError("Use a short address with a final alias.")
    lines = (prefix + separator, alias)
    matrix = tuple(segno.make(invite_url, micro=False, error="m").matrix)
    writer = PdfWriter()
    for index, (page, layout) in enumerate(zip(reader.pages, layouts)):
        stream = BytesIO()
        canvas = Canvas(stream, pagesize=(612, 792), pageCompression=1)
        x, y, side = layout["qr"]
        canvas.setFillColorRGB(1, 1, 1)
        canvas.rect(x, y, side, side, stroke=0, fill=1)
        unit = side / (len(matrix) + 8)
        canvas.setFillColorRGB(0, 0, 0)
        for row, values in enumerate(matrix):
            for column, dark in enumerate(values):
                if dark:
                    canvas.rect(x + (column + 4) * unit, y + (len(matrix) + 3 - row) * unit,
                                unit, unit, stroke=0, fill=1)
        canvas.setFillColorRGB(1, 0.956863, 0.894118)
        canvas.rect(*layout["area"], stroke=0, fill=1)
        x, first, second, width, prefix_size, alias_size = layout["url"]
        font = "Helvetica-Bold"
        canvas.setFillColorRGB(0.082353, 0.372549, 0.360784)
        for text, baseline, size in zip(lines, (first, second), (prefix_size, alias_size)):
            if stringWidth(text, font, size) > width:
                raise ValueError("Use a shorter typed address. The card must stay legible.")
            canvas.setFont(font, size)
            canvas.drawString(x, baseline, text)
        right, baseline, size = layout["label"]
        canvas.setFont("Helvetica-Bold", size)
        canvas.drawString(x, baseline, "LINK / SHORTCUT - TYPE AS ONE ADDRESS")
        canvas.drawRightString(right, baseline, "HOUSE LIGHTS" if production else "SAMPLE - LOCAL DEMO")
        if index == 0:
            canvas.setFillColorRGB(1, 1, 1)
            canvas.rect(125, 82, 370, 18, stroke=0, fill=1)
            canvas.setFillColorRGB(0.25, 0.35, 0.34)
            canvas.setFont("Helvetica", 9.5)
            canvas.drawCentredString(306, 89, "Verify your email to request access." if production else "Sample only. This is not a live invitation.")
        baseline = layout["footer"]
        canvas.setFillColorRGB(1, 1, 1)
        canvas.rect(160, baseline - 5, 300, 20, stroke=0, fill=1)
        canvas.setFillColorRGB(0.25, 0.35, 0.34)
        canvas.setFont("Helvetica", 8)
        canvas.drawCentredString(306, baseline, f"US LETTER | {'HOUSE LIGHTS INVITATION' if production else 'SAMPLE INVITATION'} | {index + 1} OF 2")
        canvas.save()
        target = writer.add_page(page)
        target.merge_page(PdfReader(BytesIO(stream.getvalue())).pages[0])
    writer.add_metadata({"/Title": "House Lights invitation" if production else "Sample House Lights invitation",
                         "/Subject": "Private invitation" if production else "Synthetic local demo only"})
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".card-", suffix=".pdf", delete=False) as file:
            temporary = Path(file.name)
            writer.write(file)
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output.resolve()
