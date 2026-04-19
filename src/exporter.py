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
    """
    Fill the VAT_Refund template with the user's receipts and save to output_path.
    """
    shutil.copy(config.TEMPLATE_PATH, output_path)
    wb = load_workbook(output_path)

    receipts = await db.list_receipts(telegram_id)
    grand_total = 0.0

    # Chunk receipts across sheets (30 per sheet), write data + per-sheet totals
    for idx, sheet_name in enumerate(SHEETS_ORDER):
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        chunk = receipts[idx * ROWS_PER_SHEET : (idx + 1) * ROWS_PER_SHEET]
        if not chunk and idx > 0:
            continue

        # Write employee name directly (D5 is the top-left of the D5:E5 merged cell)
        ws.cell(row=NAME_ROW, column=NAME_COL, value=employee_name)

        # Write receipt rows
        sheet_total = 0.0
        for i, r in enumerate(chunk):
            row = DATA_START_ROW + i
            ws.cell(row=row, column=2, value=r.get("date", ""))
            ws.cell(row=row, column=3, value=_display_vendor(r))
            ws.cell(row=row, column=4, value=r.get("receipt_number", ""))
            vat = float(r.get("vat_amount") or 0)
            ws.cell(row=row, column=5, value=vat)
            sheet_total += vat

        # Write per-sheet total as a value (E40), replacing the formula
        ws.cell(row=PER_SHEET_TOTAL_ROW, column=PER_SHEET_TOTAL_COL, value=sheet_total)
        grand_total += sheet_total

    # Write grand total as a value to D6 in Table sheet (D6:E6 merged cell)
    if "Table" in wb.sheetnames:
        wb["Table"].cell(row=GRAND_TOTAL_ROW, column=GRAND_TOTAL_COL, value=grand_total)

    wb.save(output_path)
    return output_path


async def build_pdf(telegram_id: int, employee_name: str, output_path: Path) -> Path:
    """
    Generate a PDF with one receipt image per page, plus a header summary.
    """
    receipts = await db.list_receipts(telegram_id)

    c = canvas.Canvas(str(output_path), pagesize=A4)
    width, height = A4
    margin = 15 * mm

    # --- Cover / summary page ---
    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin, height - margin - 10 * mm, "VAT Refund – Receipt Package")
    c.setFont("Helvetica", 11)
    c.drawString(margin, height - margin - 18 * mm, f"Employee: {employee_name or '—'}")
    c.drawString(margin, height - margin - 24 * mm, f"Total receipts: {len(receipts)}")
    total_vat = sum(float(r.get("vat_amount") or 0) for r in receipts)
    c.drawString(margin, height - margin - 30 * mm, f"Total VAT (UZS): {total_vat:,.2f}")

    # Summary table
    y = height - margin - 45 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, y, "#")
    c.drawString(margin + 10 * mm, y, "Date")
    c.drawString(margin + 35 * mm, y, "Vendor")
    c.drawString(margin + 100 * mm, y, "Receipt #")
    c.drawString(margin + 150 * mm, y, "VAT (UZS)")
    y -= 5 * mm
    c.setFont("Helvetica", 9)
    for i, r in enumerate(receipts, 1):
        if y < margin + 15 * mm:
            c.showPage()
            y = height - margin
            c.setFont("Helvetica", 9)
        c.drawString(margin, y, str(i))
        c.drawString(margin + 10 * mm, y, str(r.get("date", "") or ""))
        vendor = _display_vendor(r)[:32]
        c.drawString(margin + 35 * mm, y, vendor)
        c.drawString(margin + 100 * mm, y, str(r.get("receipt_number", "") or "")[:18])
        vat = float(r.get("vat_amount") or 0)
        c.drawRightString(margin + 185 * mm, y, f"{vat:,.2f}")
        y -= 5 * mm

    # --- One page per receipt image ---
    for i, r in enumerate(receipts, 1):
        file_id = r.get("image_file_id")
        if not file_id:
            continue
        try:
            img_bytes = await db.get_image(file_id)
        except Exception:
            continue

        c.showPage()
        c.setFont("Helvetica-Bold", 12)
        c.drawString(margin, height - margin, f"Receipt #{i}")
        c.setFont("Helvetica", 10)
        c.drawString(margin, height - margin - 6 * mm,
                     f"{r.get('date','')}  |  {_display_vendor(r)}  |  VAT: {float(r.get('vat_amount') or 0):,.2f} UZS")

        # Fit image to page while preserving aspect ratio
        try:
            pil = Image.open(BytesIO(img_bytes))
            iw, ih = pil.size
            max_w = width - 2 * margin
            max_h = height - 2 * margin - 15 * mm
            ratio = min(max_w / iw, max_h / ih)
            dw, dh = iw * ratio, ih * ratio
            # ReportLab needs a file path or ImageReader
            from reportlab.lib.utils import ImageReader
            img_reader = ImageReader(BytesIO(img_bytes))
            c.drawImage(img_reader, margin, margin,
                        width=dw, height=dh, preserveAspectRatio=True, mask="auto")
        except Exception:
            c.setFont("Helvetica-Oblique", 10)
            c.drawString(margin, height / 2, "[Image could not be rendered]")

    c.save()
    return output_path
