"""Generate VAT_Bot_Tutorial.pdf from screenshots.

How to use:
  1. Drop screenshots into tmp/tutorial_screenshots/ with these filenames:
        01_search.png     (searching for @tashkent_vat_bot in Telegram)
        02_welcome.png    (the /start welcome message)
        03_setname.png    (/setname conversation flow)
        04_receipt.png    (uploading a receipt + Receipt added confirmation)
        05_export_vat.png (VAT_Refund.xlsx opened in Excel)
        06_export_pdf.png (Receipts.pdf cover page)
     Missing files are OK — a placeholder is shown and the guide still builds.
  2. Run:  .venv/bin/python scripts/build_tutorial.py
  3. Open VAT_Bot_Tutorial.pdf in the project root.
"""
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCREENSHOTS_DIR = PROJECT_ROOT / "tmp" / "tutorial_screenshots"
OUTPUT_PDF = PROJECT_ROOT / "VAT_Bot_Tutorial.pdf"

NAVY = colors.HexColor("#0B2A4A")
ACCENT = colors.HexColor("#B22234")
MUTED = colors.HexColor("#555555")
SOFT_BG = colors.HexColor("#F4F6FA")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Title"],
            fontName="Helvetica-Bold", fontSize=26, leading=32,
            textColor=NAVY, alignment=TA_CENTER, spaceAfter=12,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"],
            fontName="Helvetica", fontSize=14, leading=18,
            textColor=MUTED, alignment=TA_CENTER, spaceAfter=24,
        ),
        "h1": ParagraphStyle(
            "h1", parent=base["Heading1"],
            fontName="Helvetica-Bold", fontSize=18, leading=22,
            textColor=NAVY, spaceAfter=10, spaceBefore=4,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"],
            fontName="Helvetica-Bold", fontSize=13, leading=16,
            textColor=ACCENT, spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "body", parent=base["BodyText"],
            fontName="Helvetica", fontSize=11, leading=16,
            textColor=colors.black, alignment=TA_LEFT, spaceAfter=8,
        ),
        "caption": ParagraphStyle(
            "caption", parent=base["Italic"],
            fontName="Helvetica-Oblique", fontSize=9.5, leading=12,
            textColor=MUTED, alignment=TA_CENTER, spaceAfter=14,
        ),
        "footer": ParagraphStyle(
            "footer", parent=base["Normal"],
            fontName="Helvetica", fontSize=9, leading=11,
            textColor=MUTED, alignment=TA_CENTER,
        ),
    }


def _screenshot(filename: str, caption: str, max_w=5.6 * inch, max_h=6.0 * inch):
    """Return a flowable for the screenshot, falling back to a placeholder box."""
    styles = _styles()
    path = SCREENSHOTS_DIR / filename
    if path.exists():
        img = Image(str(path))
        iw, ih = img.drawWidth, img.drawHeight
        scale = min(max_w / iw, max_h / ih, 1.0)
        img.drawWidth = iw * scale
        img.drawHeight = ih * scale
        img.hAlign = "CENTER"
        return [img, Spacer(1, 4), Paragraph(caption, styles["caption"])]
    placeholder = Table(
        [[Paragraph(f"<i>[ screenshot missing: {filename} ]</i>", styles["body"])]],
        colWidths=[max_w], rowHeights=[2.2 * inch],
    )
    placeholder.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SOFT_BG),
        ("BOX", (0, 0), (-1, -1), 1, MUTED),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    return [placeholder, Spacer(1, 4), Paragraph(caption, styles["caption"])]


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(MUTED)
    canvas.drawCentredString(
        LETTER[0] / 2, 0.5 * inch,
        f"Tashkent Embassy VAT Refund V.3.0  —  page {doc.page}",
    )
    canvas.restoreState()


def build():
    s = _styles()
    story = []

    # --- Cover page -------------------------------------------------------
    cover_path = SCREENSHOTS_DIR / "cover.png"
    if cover_path.exists():
        hero = Image(str(cover_path))
        max_side = 4.2 * inch
        scale = min(max_side / hero.drawWidth, max_side / hero.drawHeight, 1.0)
        hero.drawWidth *= scale
        hero.drawHeight *= scale
        hero.hAlign = "CENTER"
        story.append(Spacer(1, 0.15 * inch))
        story.append(hero)
        story.append(Spacer(1, 0.15 * inch))
    else:
        story.append(Spacer(1, 1.2 * inch))

    cover_title = ParagraphStyle(
        "cover_title", parent=s["title"], fontSize=22, leading=26, spaceAfter=6,
    )
    cover_subtitle = ParagraphStyle(
        "cover_subtitle", parent=s["subtitle"], fontSize=12, leading=15, spaceAfter=14,
    )
    story.append(Paragraph("Tashkent Embassy VAT Refund Bot", cover_title))
    story.append(Paragraph("User Guide &mdash; Version 3.0", cover_subtitle))

    cover = Table(
        [[Paragraph(
            "<b>What this bot does</b><br/><br/>"
            "Take a photo of a Uzbek receipt. The bot scans the QR code, "
            "looks up the VAT on <b>ofd.soliq.uz</b>, and stores it. When "
            "you&rsquo;re ready to file your quarterly refund, it hands you "
            "a completed Excel file and a PDF of all receipts &mdash; in a "
            "couple of taps.",
            s["body"],
        )]],
        colWidths=[5.6 * inch],
    )
    cover.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SOFT_BG),
        ("BOX", (0, 0), (-1, -1), 0.75, NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(cover)
    story.append(Spacer(1, 0.12 * inch))
    story.append(Paragraph(
        "Free &amp; open source &mdash; MIT License",
        s["caption"],
    ))
    story.append(PageBreak())

    # --- Step 1: Find the bot --------------------------------------------
    story.append(Paragraph("Step 1 &mdash; Find the bot", s["h1"]))
    story.append(Paragraph(
        "Open Telegram and search for <b>@tashkent_vat_bot</b>. "
        "Tap the result to open the chat, then press <b>START</b> at the bottom.",
        s["body"],
    ))
    story.extend(_screenshot("01_search.png", "Searching for @tashkent_vat_bot in Telegram."))
    story.append(PageBreak())

    # --- Step 2: Welcome message -----------------------------------------
    story.append(Paragraph("Step 2 &mdash; The welcome message", s["h1"]))
    story.append(Paragraph(
        "After you press START, the bot greets you and lists every command. "
        "You&rsquo;ll only use three regularly: <b>/setname</b>, "
        "<b>/export_vat</b>, and <b>/export_pdf</b>. "
        "Everything else is a safety net.",
        s["body"],
    ))
    story.extend(_screenshot("02_welcome.png", "The V.3.0 welcome message with the full command list."))
    story.append(PageBreak())

    # --- Step 3: Set your name -------------------------------------------
    story.append(Paragraph("Step 3 &mdash; Tell the bot your name", s["h1"]))
    story.append(Paragraph(
        "Tap <b>/setname</b>. The bot will ask <i>&ldquo;Please enter your full "
        "name.&rdquo;</i> Type your name the way you want it to appear on the "
        "VAT refund Excel file &mdash; for example <b>Shirali</b> or "
        "<b>John Smith</b>. The bot saves it and uses it on every future export.",
        s["body"],
    ))
    story.append(Paragraph(
        "You only have to do this once. You can change it later by running "
        "<b>/setname</b> again.",
        s["body"],
    ))
    story.extend(_screenshot("03_setname.png", "Setting your name &mdash; just tap /setname and type it in."))
    story.append(PageBreak())

    # --- Step 4: Submit a receipt ----------------------------------------
    story.append(Paragraph("Step 4 &mdash; Submit a receipt", s["h1"]))
    story.append(Paragraph(
        "Tap the <b>paperclip</b> icon and choose <b>File</b> (not Photo) &mdash; "
        "sending as a file keeps the image at full resolution, which gives "
        "the best QR-reading results. Pick the receipt photo and send it.",
        s["body"],
    ))
    story.append(Paragraph(
        "The bot scans the QR code, fetches the VAT amount from "
        "<b>ofd.soliq.uz</b>, and replies with a confirmation showing the "
        "vendor, VAT, and total. That&rsquo;s it &mdash; the receipt is "
        "stored. Repeat for every receipt you want to include in the refund.",
        s["body"],
    ))
    story.extend(_screenshot("04_receipt.png", "A successful receipt upload with vendor, VAT, and total."))
    story.append(PageBreak())

    # --- Step 5: Export VAT xlsx -----------------------------------------
    story.append(Paragraph("Step 5 &mdash; Download the VAT Refund Excel", s["h1"]))
    story.append(Paragraph(
        "When you&rsquo;re ready to file, run <b>/export_vat</b>. The bot "
        "returns <b>VAT_Refund.xlsx</b> &mdash; already filled in with your "
        "name, every receipt, and the totals. Open it in Excel, review it, "
        "and submit it exactly as-is to the embassy.",
        s["body"],
    ))
    story.extend(_screenshot("05_export_vat.png", "VAT_Refund.xlsx &mdash; ready to submit, your name already in place."))
    story.append(PageBreak())

    # --- Step 6: Export receipts pdf -------------------------------------
    story.append(Paragraph("Step 6 &mdash; Download the receipts PDF", s["h1"]))
    story.append(Paragraph(
        "Run <b>/export_pdf</b> to get <b>Receipts.pdf</b> &mdash; a single "
        "document with a cover page, a summary table, and every receipt "
        "image you&rsquo;ve uploaded. Attach this alongside the Excel file "
        "when you submit your refund.",
        s["body"],
    ))
    story.extend(_screenshot("06_export_pdf.png", "Receipts.pdf &mdash; cover page, summary, and all receipt images."))
    story.append(PageBreak())

    # --- Tips -------------------------------------------------------------
    story.append(Paragraph("Tips &amp; Troubleshooting", s["h1"]))

    story.append(Paragraph("QR code won&rsquo;t read?", s["h2"]))
    story.append(Paragraph(
        "Retake the photo in good light, hold the phone steady, and make "
        "sure the QR square is fully in the frame and not wrinkled. Send "
        "the image as a <b>File</b> (paperclip &rarr; File), not as a Photo. "
        "If it still fails, open your phone&rsquo;s camera on the receipt, "
        "copy the soliq.uz link the camera detects, and paste that link to "
        "the bot &mdash; that works too.",
        s["body"],
    ))

    story.append(Paragraph("Uploaded the wrong receipt?", s["h2"]))
    story.append(Paragraph(
        "If the bot is still asking you to confirm, run "
        "<b>/cancel_pending</b>. If it&rsquo;s already saved, just keep "
        "going &mdash; duplicates are blocked automatically (same receipt "
        "number won&rsquo;t be stored twice).",
        s["body"],
    ))

    story.append(Paragraph("See what you&rsquo;ve stored", s["h2"]))
    story.append(Paragraph(
        "<b>/list</b> shows every receipt the bot is holding for you, "
        "with vendor, VAT, and date. Quick way to check your running total.",
        s["body"],
    ))

    story.append(Paragraph("Start over", s["h2"]))
    story.append(Paragraph(
        "<b>/reset</b> deletes everything &mdash; every receipt and every "
        "image. Use it after you&rsquo;ve submitted your quarterly refund "
        "and want a clean slate for next quarter.",
        s["body"],
    ))

    story.append(Paragraph("Need help?", s["h2"]))
    story.append(Paragraph(
        "Message <b>Shirali DT</b> at the embassy. The bot is free and "
        "open source under the MIT License &mdash; anyone at any U.S. "
        "embassy is welcome to use or adapt it.",
        s["body"],
    ))

    # --- Build ------------------------------------------------------------
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(OUTPUT_PDF),
        pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title="Tashkent Embassy VAT Refund Bot &mdash; User Guide",
        author="Shirali DT",
    )
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    print(f"Wrote {OUTPUT_PDF.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    build()
