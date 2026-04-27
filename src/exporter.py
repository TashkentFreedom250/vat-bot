"""
Export receipts to the official VAT Refund XLSX template
and to a PDF containing all receipt images.
"""
from __future__ import annotations

import shutil
from io import BytesIO
from pathlib import Path
from typing import Iterable

from openpyxl import load_workbook
from openpyxl.styles import Font
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from PIL import Image

from . import config, db

# XLSX layout (inspected from the template):
# Columns: A=#, B=Date, C=Vendor, D=Receipt#, E=VAT Amount
# D5:E5 merged  = employee name field (Table sheet) / =Table!D5 formula (continuation sheets)
# D6:E6 merged  = grand total (Table sheet only)
# E40            = per-sheet SUM (each sheet)
SHEETS_ORDER = ["Table", "Continuation", "Continuation2", "Continuation3"]
ROWS_PER_SHEET = 30
DATA_START_ROW = 9   # First data row (A9 = 1)
NAME_ROW = 5         # Row of the employee name merged cell D5:E5
NAME_COL = 4         # Column D
GRAND_TOTAL_ROW = 6  # Row of grand total merged cell D6:E6
GRAND_TOTAL_COL = 4  # Column D
PER_SHEET_TOTAL_ROW = 40  # Row with per-sheet SUM
PER_SHEET_TOTAL_COL = 5   # Column E


def _display_vendor(doc: dict) -> str:
    return doc.get("display_vendor") or doc.get("printed_vendor") or doc.get("vendor") or ""


def _find_data_start(ws) -> int:
    """Find the row where numbered data starts (cell A = 1)."""
    for row in range(1, 50):
        val = ws.cell(row=row, column=1).value
        if val == 1 or val == "1":
            return row
    return DATA_START_ROW


def _find_name_cell(ws) -> tuple[int, int] | None:
    """Find the 'Employee Name' label and return the cell to the right of it."""
    for row in range(1, 10):
        for col in range(1, 10):
            val = ws.cell(row=row, column=col).value
            if val and isinstance(val, str) and "Employee Name" in val:
                return (row, col + 1)
    return None


async def build_xlsx(telegram_id: int, employee_name: str, output_path: Path) -> Path:
    """Fill the VAT_Refund template with all of the user's receipts."""
    receipts = await db.list_receipts(telegram_id)
    return build_xlsx_from_receipts(receipts, employee_name, output_path)


def build_xlsx_from_receipts(
    receipts: list[dict], employee_name: str, output_path: Path
) -> Path:
    """Render a pre-filtered list of receipts into a fresh copy of the template.
    Used by /export_vat to produce one workbook per calendar year."""
    shutil.copy(config.TEMPLATE_PATH, output_path)
    wb = load_workbook(output_path)

    cumulative_total = 0.0

    # Walk every sheet in the template — even empty ones get the employee
    # name so continuations stay identified. Per-sheet totals accumulate
    # (E40 on each sheet = running total through that sheet) so the user
    # can see the refund amount grow as they flip across tabs.
    for idx, sheet_name in enumerate(SHEETS_ORDER):
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        chunk = receipts[idx * ROWS_PER_SHEET : (idx + 1) * ROWS_PER_SHEET]

        # Employee name on every continuation tab (D5 is the TL of D5:E5).
        ws.cell(row=NAME_ROW, column=NAME_COL, value=employee_name)

        # Write receipt rows. Column A (sequential #) is written explicitly
        # because the template's pre-filled numbers display with a broken
        # number format on some Excel versions.
        sheet_total = 0.0
        for i, r in enumerate(chunk):
            row = DATA_START_ROW + i
            seq = idx * ROWS_PER_SHEET + i + 1
            num_cell = ws.cell(row=row, column=1, value=seq)
            num_cell.number_format = "General"
            # Manual entries: bold the row number so the finance office can
            # spot rows that were typed in (and could contain typos) rather
            # than fetched and verified from soliq.uz.
            if r.get("manual"):
                existing = num_cell.font
                num_cell.font = Font(
                    name=existing.name, size=existing.size,
                    color=existing.color, family=existing.family,
                    bold=True,
                )
            ws.cell(row=row, column=2, value=r.get("date", ""))
            ws.cell(row=row, column=3, value=_display_vendor(r))
            ws.cell(row=row, column=4, value=r.get("receipt_number", ""))
            vat = float(r.get("vat_amount") or 0)
            ws.cell(row=row, column=5, value=vat)
            sheet_total += vat

        # Blank leftover rows in this sheet's data block so stale template
        # numbers (1-30) don't show through when the chunk is short/empty.
        for i in range(len(chunk), ROWS_PER_SHEET):
            row = DATA_START_ROW + i
            for col in range(1, 6):
                ws.cell(row=row, column=col).value = None

        # Running total through this sheet (E40), replacing the SUM formula.
        cumulative_total += sheet_total
        ws.cell(row=PER_SHEET_TOTAL_ROW, column=PER_SHEET_TOTAL_COL, value=cumulative_total)

    # Grand total on Table!D6 (D6:E6 merged cell).
    if "Table" in wb.sheetnames:
        wb["Table"].cell(row=GRAND_TOTAL_ROW, column=GRAND_TOTAL_COL, value=cumulative_total)

    wb.save(output_path)
    return output_path


_PDF_DPI = 180  # sharp enough to read receipt text; keeps files small
_PDF_JPEG_QUALITY = 78


def _draw_pil(c, pil_img: "Image.Image", box_x: float, box_y: float,
              box_w: float, box_h: float) -> None:
    """Draw a PIL image scaled to fit and centered inside the given box.

    Downscales and JPEG-encodes so PDFs stay under Telegram's 50 MB cap —
    receipts packed with full-res PNG pages balloon fast.
    """
    from reportlab.lib.utils import ImageReader
    iw, ih = pil_img.size
    if iw == 0 or ih == 0:
        return
    ratio = min(box_w / iw, box_h / ih)
    dw, dh = iw * ratio, ih * ratio
    dx = box_x + (box_w - dw) / 2
    dy = box_y + (box_h - dh) / 2

    # Downscale to the printed size at _PDF_DPI. reportlab draws in points
    # (72/inch), so target-pixel = drawn_points * DPI / 72.
    target_w = max(1, int(dw * _PDF_DPI / 72))
    if target_w < iw:
        target_h = max(1, int(ih * target_w / iw))
        pil_img = pil_img.resize((target_w, target_h), Image.LANCZOS)

    buf = BytesIO()
    pil_img.save(buf, format="JPEG", quality=_PDF_JPEG_QUALITY, optimize=True)
    buf.seek(0)
    c.drawImage(ImageReader(buf), dx, dy, width=dw, height=dh)


def _strip_coverage(n_cols: int, iw: int, ih: int,
                    box_w: float, box_h: float, gap: float) -> float:
    """Total drawn area if the image is sliced into n equal-height strips side by side."""
    if n_cols < 1:
        return 0.0
    col_w = (box_w - (n_cols - 1) * gap) / n_cols
    if col_w <= 0:
        return 0.0
    strip_h = ih / n_cols
    ratio = min(col_w / iw, box_h / strip_h)
    return n_cols * (iw * ratio) * (strip_h * ratio)


def _draw_receipt_images(
    c,
    img_bytes: bytes,
    qr_bytes: "bytes | None",
    x: float,
    y: float,
    max_w: float,
    max_h: float,
) -> None:
    """
    Render receipt image(s) so they fill as much of the box as possible.

    • QR close-up present: receipt 2/3 width, QR 1/3 width, each centered.
    • No QR close-up: try 1, 2, and 3 vertical-strip layouts; pick whichever
      covers the largest area of the page box.
    """
    gap = 3 * mm
    pil_main = Image.open(BytesIO(img_bytes)).convert("RGB")
    iw, ih = pil_main.size

    if qr_bytes:
        receipt_w = (max_w - gap) * 2 / 3
        qr_w = max_w - gap - receipt_w
        _draw_pil(c, pil_main, x, y, receipt_w, max_h)
        pil_qr = Image.open(BytesIO(qr_bytes)).convert("RGB")
        _draw_pil(c, pil_qr, x + receipt_w + gap, y, qr_w, max_h)
        return

    best_n = max(
        (1, 2, 3),
        key=lambda n: _strip_coverage(n, iw, ih, max_w, max_h, gap),
    )

    if best_n == 1:
        _draw_pil(c, pil_main, x, y, max_w, max_h)
        return

    col_w = (max_w - (best_n - 1) * gap) / best_n
    strip_h = ih // best_n
    for i in range(best_n):
        top = i * strip_h
        bot = ih if i == best_n - 1 else (i + 1) * strip_h
        strip = pil_main.crop((0, top, iw, bot))
        _draw_pil(c, strip, x + i * (col_w + gap), y, col_w, max_h)


# Cap per-PDF receipt count to stay safely under Telegram's 50 MB upload
# limit even when users have lots of long receipts.
RECEIPTS_PER_PDF = 30


async def build_pdf(telegram_id: int, employee_name: str, output_path: Path) -> Path:
    """Back-compat single-file PDF builder. Prefer build_pdfs for large sets."""
    paths = await build_pdfs(telegram_id, employee_name, output_path.parent, output_path.stem)
    # When only one file was produced, rename it to match the requested name.
    if len(paths) == 1 and paths[0] != output_path:
        paths[0].rename(output_path)
        return output_path
    return paths[0]


async def build_pdfs(
    telegram_id: int,
    employee_name: str,
    output_dir: Path,
    name_prefix: str = "Receipts",
) -> list[Path]:
    """Generate one or more PDFs (split every RECEIPTS_PER_PDF receipts)."""
    receipts = await db.list_receipts(telegram_id)
    return await build_pdfs_from_receipts(
        receipts, employee_name, output_dir, name_prefix
    )


async def build_pdfs_from_receipts(
    receipts: list[dict],
    employee_name: str,
    output_dir: Path,
    name_prefix: str = "Receipts",
) -> list[Path]:
    """PDF builder for a pre-filtered receipt set (e.g. one calendar year)."""
    if not receipts:
        return []

    chunks = [
        receipts[i : i + RECEIPTS_PER_PDF]
        for i in range(0, len(receipts), RECEIPTS_PER_PDF)
    ]
    total_vat_all = sum(float(r.get("vat_amount") or 0) for r in receipts)
    total_parts = len(chunks)
    out_paths: list[Path] = []

    for part_idx, chunk in enumerate(chunks, 1):
        global_offset = (part_idx - 1) * RECEIPTS_PER_PDF
        suffix = f"_part{part_idx}_of_{total_parts}" if total_parts > 1 else ""
        out_path = output_dir / f"{name_prefix}{suffix}.pdf"
        await _write_pdf_chunk(
            chunk=chunk,
            employee_name=employee_name,
            part_idx=part_idx,
            total_parts=total_parts,
            global_offset=global_offset,
            grand_total_vat=total_vat_all,
            total_receipts=len(receipts),
            output_path=out_path,
        )
        out_paths.append(out_path)

    return out_paths


async def _write_pdf_chunk(
    *,
    chunk: list[dict],
    employee_name: str,
    part_idx: int,
    total_parts: int,
    global_offset: int,
    grand_total_vat: float,
    total_receipts: int,
    output_path: Path,
) -> None:
    c = canvas.Canvas(str(output_path), pagesize=A4)
    width, height = A4
    margin = 15 * mm

    # --- Cover / summary page ---
    title = "Tashkent Embassy VAT Refund V1 - Receipt Package"
    if total_parts > 1:
        title += f"  (Part {part_idx} of {total_parts})"
    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin, height - margin - 10 * mm, title)
    c.setFont("Helvetica", 11)
    c.drawString(margin, height - margin - 18 * mm, f"Employee: {employee_name or '—'}")
    c.drawString(
        margin, height - margin - 24 * mm,
        f"Receipts in this file: {len(chunk)} (#{global_offset + 1}–#{global_offset + len(chunk)} of {total_receipts})",
    )
    chunk_total = sum(float(r.get("vat_amount") or 0) for r in chunk)
    c.drawString(margin, height - margin - 30 * mm, f"VAT in this file (UZS): {chunk_total:,.2f}")
    if total_parts > 1:
        c.drawString(margin, height - margin - 36 * mm, f"Grand total VAT across all parts: {grand_total_vat:,.2f} UZS")
        summary_y = height - margin - 51 * mm
    else:
        summary_y = height - margin - 45 * mm

    # Summary table for this chunk
    y = summary_y
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, y, "#")
    c.drawString(margin + 10 * mm, y, "Date")
    c.drawString(margin + 35 * mm, y, "Vendor")
    c.drawString(margin + 100 * mm, y, "Receipt #")
    c.drawRightString(margin + 185 * mm, y, "VAT (UZS)")
    y -= 2 * mm
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.setLineWidth(0.3)
    c.line(margin, y, width - margin, y)
    y -= 5 * mm
    c.setFont("Helvetica", 9)
    for i, r in enumerate(chunk, 1):
        if y < margin + 15 * mm:
            c.showPage()
            y = height - margin
            c.setFont("Helvetica", 9)
        c.drawString(margin, y, str(global_offset + i))
        c.drawString(margin + 10 * mm, y, str(r.get("date", "") or ""))
        vendor = _display_vendor(r)[:32]
        c.drawString(margin + 35 * mm, y, vendor)
        c.drawString(margin + 100 * mm, y, str(r.get("receipt_number", "") or "")[:18])
        vat = float(r.get("vat_amount") or 0)
        c.drawRightString(margin + 185 * mm, y, f"{vat:,.2f}")
        y -= 5 * mm
    y += 2 * mm
    c.line(margin, y, width - margin, y)
    y -= 6 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin + 100 * mm, y, "SUBTOTAL" if total_parts > 1 else "TOTAL")
    c.drawRightString(margin + 185 * mm, y, f"{chunk_total:,.2f}")

    # --- One page per receipt image ---
    for i, r in enumerate(chunk, 1):
        global_i = global_offset + i
        file_id = r.get("image_file_id")
        if not file_id:
            continue
        try:
            img_bytes = await db.get_image(file_id)
        except Exception:
            continue

        qr_bytes = None
        if r.get("qr_image_file_id"):
            try:
                qr_bytes = await db.get_image(r["qr_image_file_id"])
            except Exception:
                pass

        c.showPage()
        c.setFont("Helvetica-Bold", 12)
        c.drawString(margin, height - margin, f"Receipt #{global_i}")
        c.setFont("Helvetica", 10)
        c.drawString(
            margin, height - margin - 6 * mm,
            f"{r.get('date','') or '—'}  •  {_display_vendor(r) or '—'}  •  "
            f"VAT: {float(r.get('vat_amount') or 0):,.2f} UZS",
        )

        img_box_y = margin + 6 * mm
        img_box_h = height - margin - 12 * mm - img_box_y
        img_box_w = width - 2 * margin

        try:
            _draw_receipt_images(c, img_bytes, qr_bytes, margin, img_box_y, img_box_w, img_box_h)
        except Exception:
            c.setFont("Helvetica-Oblique", 10)
            c.drawString(margin, height / 2, "[Image could not be rendered]")

        c.setFont("Helvetica", 8)
        c.setFillGray(0.5)
        c.drawRightString(width - margin, margin / 2, f"Receipt {global_i} of {total_receipts}")
        c.setFillGray(0)

    c.save()
