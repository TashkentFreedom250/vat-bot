"""
Render a synthetic receipt-style PNG for online purchases.

Online purchases don't have a physical receipt to photograph, but the
PDF export still wants a visual record per receipt. This module turns a
soliq.uz scrape result + the original QR URL into a clean PNG: header,
structured fields, and the QR code re-rendered from the URL so finance
can verify it independently.
"""
from __future__ import annotations

from io import BytesIO

import qrcode
from PIL import Image, ImageDraw, ImageFont


_WIDTH = 1000
_MARGIN = 40
_BG = (255, 255, 255)
_TEXT = (32, 32, 32)
_MUTED = (110, 110, 110)
_ACCENT = (0, 80, 160)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Pick a system font that ships with macOS. Falls back to PIL default if
    the font isn't found (older macOS or sandboxed environments)."""
    candidates = (
        ("/System/Library/Fonts/HelveticaNeue.ttc", bold),
        ("/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf", bold),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", bold),
    )
    for path, _ in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _qr_image(url: str, box_size: int = 8) -> Image.Image:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def render_online_receipt_png(data: dict, qr_url: str) -> bytes:
    """Render the snapshot as PNG bytes.

    `data` is the dict returned by soliq.fetch_receipt_data.
    `qr_url` is the original soliq.uz URL the user pasted.
    """
    title = "ONLINE PURCHASE — soliq.uz Verified"
    vendor = data.get("vendor") or "—"
    date = data.get("date") or "—"
    receipt_no = data.get("receipt_number") or "—"
    total = float(data.get("total_amount") or 0)
    vat = float(data.get("vat_amount") or 0)

    title_font = _font(30, bold=True)
    label_font = _font(20, bold=True)
    value_font = _font(20)
    small_font = _font(14)
    big_value_font = _font(26, bold=True)

    qr_img = _qr_image(qr_url, box_size=8)
    qr_w, qr_h = qr_img.size

    # Compose lines and lay them out top-down. Width is fixed; height grows
    # to fit. Reserve enough room for the QR on the right.
    height = 720
    img = Image.new("RGB", (_WIDTH, height), _BG)
    draw = ImageDraw.Draw(img)

    # Header bar
    draw.rectangle([(0, 0), (_WIDTH, 70)], fill=_ACCENT)
    draw.text((_MARGIN, 20), title, font=title_font, fill=(255, 255, 255))

    # Body
    y = 110
    label_x = _MARGIN
    value_x = _MARGIN + 220
    line_h = 42

    for label, value in (
        ("Vendor", vendor),
        ("Date", date),
        ("Receipt #", receipt_no),
    ):
        draw.text((label_x, y), label, font=label_font, fill=_MUTED)
        # Truncate vendor if absurdly long.
        v = value if len(str(value)) <= 60 else (str(value)[:57] + "...")
        draw.text((value_x, y), str(v), font=value_font, fill=_TEXT)
        y += line_h

    y += 20
    # Money block — bigger so it stands out for finance review
    draw.line([(label_x, y - 5), (_WIDTH - _MARGIN, y - 5)], fill=_MUTED, width=1)
    y += 15
    draw.text((label_x, y), "Total", font=label_font, fill=_MUTED)
    draw.text((value_x, y), f"{total:,.2f} UZS", font=big_value_font, fill=_TEXT)
    y += line_h + 10
    draw.text((label_x, y), "VAT", font=label_font, fill=_MUTED)
    draw.text((value_x, y), f"{vat:,.2f} UZS", font=big_value_font, fill=_ACCENT)
    y += line_h + 30

    # QR code on the right of the body, vertically anchored at top
    qr_x = _WIDTH - _MARGIN - qr_w
    qr_y = 110
    img.paste(qr_img, (qr_x, qr_y))
    qr_caption_y = qr_y + qr_h + 6
    draw.text(
        (qr_x, qr_caption_y),
        "Scan to verify on soliq.uz",
        font=small_font,
        fill=_MUTED,
    )

    # Footer with the URL (wrapped if needed)
    footer_y = max(y, qr_caption_y + 30)
    draw.line(
        [(label_x, footer_y), (_WIDTH - _MARGIN, footer_y)],
        fill=_MUTED, width=1,
    )
    footer_y += 12
    draw.text(
        (label_x, footer_y),
        "Source URL:",
        font=small_font,
        fill=_MUTED,
    )
    footer_y += 20
    # URL can be long — break into chunks of ~110 chars
    chunk = 110
    for i in range(0, len(qr_url), chunk):
        draw.text(
            (label_x, footer_y),
            qr_url[i : i + chunk],
            font=small_font,
            fill=_TEXT,
        )
        footer_y += 18

    # Crop to actual content height
    final_h = min(height, footer_y + _MARGIN)
    img = img.crop((0, 0, _WIDTH, final_h))

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
