"""
Fetch verified receipt data from soliq.uz (Uzbekistan tax authority).

QR codes on Uzbek fiscal receipts encode a URL like:
  https://ofd.soliq.uz/epul/?t=TERMINAL_ID&r=RECEIPT_NUMBER&c=DATE&s=AMOUNT

The page can be fetched as HTML, or in many cases there is a JSON endpoint
under ofd.soliq.uz/check that returns the structured data. We try both.

Returns a normalized dict:
  {
    "vendor": str,
    "date": "YYYY-MM-DD" (str),
    "receipt_number": str,
    "vat_amount": float,        # UZS
    "total_amount": float,      # UZS
    "raw": <debug info>
  }
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from . import config

logger = logging.getLogger("vat_bot.soliq")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


async def fetch_receipt_data(qr_url: str) -> Optional[dict]:
    """Fetch and parse a soliq.uz receipt page from a QR URL."""
    result, _ = await fetch_receipt_with_diag(qr_url)
    return result


async def fetch_receipt_with_diag(qr_url: str) -> tuple[Optional[dict], str]:
    """Like fetch_receipt_data but also returns a human-readable failure reason."""
    if not qr_url:
        return None, "Empty QR URL."

    try:
        qs = parse_qs(urlparse(qr_url).query)
    except Exception:
        qs = {}

    terminal = (qs.get("t") or [None])[0]
    receipt = (qs.get("r") or [None])[0]
    date_param = (qs.get("c") or [None])[0] or ""
    amount_param = (qs.get("s") or [None])[0] or ""
    fallback_date = _date_from_qr_param(qr_url)

    # Try the QR URL as-is, then common endpoint variants. soliq.uz has
    # shipped several paths over the years; receipts in the wild carry any
    # of /check, /epul/, or the bare root form.
    urls_to_try: list[str] = [qr_url]
    if terminal and receipt:
        qs_str = f"t={terminal}&r={receipt}&c={date_param}&s={amount_param}"
        for variant in (
            f"https://ofd.soliq.uz/api/check?{qs_str}",
            f"https://ofd.soliq.uz/check?{qs_str}",
            f"https://ofd.soliq.uz/epul/?{qs_str}",
        ):
            if variant not in urls_to_try:
                urls_to_try.append(variant)

    network_errors = 0
    bad_statuses: list[int] = []
    parsed_but_empty = 0

    client_kwargs = dict(
        timeout=config.SOLIQ_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "uz,en;q=0.9,ru;q=0.8"},
        follow_redirects=True,
    )
    if config.SOLIQ_PROXY:
        client_kwargs["proxy"] = config.SOLIQ_PROXY

    async with httpx.AsyncClient(**client_kwargs) as client:
        for url in urls_to_try:
            try:
                resp = await client.get(url)
            except Exception as exc:
                network_errors += 1
                logger.warning("soliq.uz fetch error for %s: %r", url, exc)
                continue

            if resp.status_code != 200:
                bad_statuses.append(resp.status_code)
                logger.warning("soliq.uz returned HTTP %s for %s", resp.status_code, url)
                continue

            content_type = resp.headers.get("content-type", "")
            body = resp.text
            logger.info(
                "soliq.uz %s → %s, content-type=%s, len=%s",
                url, resp.status_code, content_type, len(body),
            )

            # Sniff JSON by content-type OR by body shape (some endpoints
            # return JSON with text/html on this host).
            looks_json = "application/json" in content_type or body.lstrip().startswith(("{", "["))
            if looks_json:
                try:
                    data = resp.json()
                except Exception:
                    try:
                        import json as _json
                        data = _json.loads(body)
                    except Exception as exc:
                        logger.warning("JSON parse failed for %s: %s", url, exc)
                        data = None
                if data is not None:
                    normalized = _normalize_json(data, terminal or "", receipt or "")
                    if normalized:
                        return normalized, "ok"

            try:
                parsed = _parse_html(body, qr_url, fallback_date=fallback_date)
                if parsed:
                    return parsed, "ok"
                parsed_but_empty += 1
            except Exception as exc:
                logger.warning("HTML parse error for %s: %s", url, exc)

    logger.warning("fetch_receipt_data: all attempts failed for %s", qr_url)
    if network_errors and not bad_statuses and not parsed_but_empty:
        diag = (
            "I couldn't reach ofd.soliq.uz from this computer — check the "
            "internet connection. (This machine must be on an Uzbekistan ISP.)"
        )
    elif bad_statuses and not parsed_but_empty:
        diag = f"soliq.uz returned HTTP {bad_statuses[0]} for every attempt."
    elif parsed_but_empty:
        diag = (
            "soliq.uz loaded the page but I couldn't read VAT/total from it. "
            "The page layout may have changed."
        )
    else:
        diag = "All soliq.uz attempts failed for an unknown reason."
    return None, diag


def _normalize_json(data: dict, terminal: str, receipt: str) -> Optional[dict]:
    """Best-effort normalization of soliq JSON response."""
    try:
        # Responses vary; common keys observed on ofd.soliq.uz
        vendor = (
            data.get("companyName")
            or data.get("sellerName")
            or data.get("vendor")
            or (data.get("seller") or {}).get("name")
            or ""
        )
        total = (
            data.get("totalPrice")
            or data.get("totalAmount")
            or data.get("total")
            or 0
        )
        vat = (
            data.get("totalVAT")
            or data.get("vatAmount")
            or data.get("vat")
            or _sum_vat_from_items(data.get("items") or [])
        )
        receipt_dt = (
            data.get("receiptDateTime")
            or data.get("dateTime")
            or data.get("date")
        )
        date_str = _parse_date(receipt_dt)
        time_str = _parse_time(receipt_dt)

        seller = data.get("seller") or {}
        meta = {
            "address": data.get("sellerAddress") or seller.get("address") or "",
            "stir": (
                data.get("companyTin")
                or data.get("sellerTin")
                or data.get("tin")
                or seller.get("tin")
                or ""
            ),
            "terminal": terminal or data.get("terminalId") or "",
            "time": time_str,
            "cashier": data.get("cashierName") or seller.get("cashier") or "",
        }
        items = _items_from_json(data)

        if not vendor and not total:
            return None

        return {
            "vendor": str(vendor).strip() or "Unknown vendor",
            "date": date_str,
            "receipt_number": str(receipt),
            "vat_amount": _to_float(vat),
            "total_amount": _to_float(total),
            "items": items,
            "meta": {k: str(v) for k, v in meta.items() if v},
            "raw": {"source": "json", "terminal": terminal},
        }
    except Exception:
        return None


def _items_from_json(data: dict) -> list[dict]:
    """Normalize the items array out of a soliq JSON payload. Keys vary by
    endpoint, so try the common spellings and skip anything that's not
    obviously a line item."""
    raw_items = data.get("items") or data.get("receiptItems") or []
    if not isinstance(raw_items, list):
        return []
    out: list[dict] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        name = (
            it.get("name")
            or it.get("productName")
            or it.get("title")
            or ""
        )
        qty = _to_float(it.get("count") or it.get("quantity") or it.get("amount") or 0)
        price = _to_float(it.get("price") or it.get("unitPrice") or 0)
        line_total = _to_float(it.get("totalPrice") or it.get("sum") or it.get("total") or 0)
        vat = _to_float(it.get("vatSum") or it.get("vatAmount") or it.get("vat") or 0)
        if not name and qty == 0 and price == 0 and line_total == 0:
            continue
        out.append({
            "name": str(name).strip(),
            "qty": qty,
            "price": price,
            "total": line_total or (qty * price),
            "vat": vat,
        })
    return out


def _sum_vat_from_items(items: list) -> float:
    total = 0.0
    for it in items:
        v = it.get("vat") or it.get("vatSum") or it.get("vatAmount") or 0
        total += _to_float(v)
    return total


def _date_from_qr_param(qr_url: str) -> str:
    """Extract date from the QR URL's c= param (format: YYYYMMDDHHMMSS)."""
    try:
        qs = parse_qs(urlparse(qr_url).query)
        c = (qs.get("c") or [None])[0]
        if c and len(c) >= 8:
            return datetime.strptime(c[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        pass
    return ""


def _parse_html(html: str, qr_url: str, fallback_date: str = "") -> Optional[dict]:
    """Scrape the soliq.uz receipt page as HTML (fallback)."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)

    vendor = _extract_vendor(soup, text)
    vat = _extract_vat_from_table(soup)
    if vat is None:
        vat = _extract_vat(text)
    total = _extract_total_from_table(soup)
    if total is None:
        total = _extract_total(text)
    date_str = _extract_date(text) or fallback_date
    receipt_no = _extract_receipt_number(text, qr_url)
    items = _extract_items_from_html(soup)
    meta = _extract_meta_from_html(soup, text)

    # If we got nothing usable, give up
    if vat is None and total is None and not vendor:
        return None

    return {
        "vendor": vendor or "Unknown vendor",
        "date": date_str,
        "receipt_number": receipt_no,
        "vat_amount": vat or 0.0,
        "total_amount": total or 0.0,
        "items": items,
        "meta": meta,
        "raw": {"source": "html"},
    }


# Header labels that indicate a row is the items-table header (Uzbek/Russian).
_ITEM_HEADER_HINTS = (
    "tovar nomi", "tovar", "nomi", "mahsulot", "tovarlar",
    "наименование", "tobap", "наим",
)
_QTY_HINTS = ("soni", "miqdor", "miqdori", "qty", "кол", "количество")
_PRICE_HINTS = ("narx", "narxi", "unit", "цена", "narh")
_VAT_HINTS = ("qqs", "ндс", "vat")
_TOTAL_HINTS = ("jami", "summa", "сумма", "итого", "total")


def _extract_items_from_html(soup: BeautifulSoup) -> list[dict]:
    """Best-effort scrape of the line-items table on a soliq.uz receipt
    page. Soliq's HTML varies by terminal vendor, so we look for any
    table whose header row mentions a 'product name' label and parse the
    rows that follow until totals/labels break the run.

    Returns a list of {name, qty, price, total, vat}. Empty when no
    items table can be identified — the caller treats this as "header
    only" and the PDF renders without an item table."""
    for table in soup.select("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_cells = [
            _normalize_text(td.get_text(" ", strip=True)).lower()
            for td in rows[0].find_all(["td", "th"])
        ]
        if not header_cells or not any(
            any(hint in cell for hint in _ITEM_HEADER_HINTS)
            for cell in header_cells
        ):
            continue
        col_map = _map_item_columns(header_cells)
        if col_map.get("name") is None:
            continue
        items: list[dict] = []
        for tr in rows[1:]:
            cells = [
                _normalize_text(td.get_text(" ", strip=True))
                for td in tr.find_all(["td", "th"])
            ]
            if not cells or len(cells) < 2:
                continue
            name = cells[col_map["name"]] if col_map["name"] < len(cells) else ""
            # Stop when we hit a totals/label row.
            lowered = name.lower()
            if any(hint in lowered for hint in ("jami", "umumiy", "итого", "to'lov")):
                break
            qty = _to_float(cells[col_map["qty"]]) if col_map.get("qty") is not None and col_map["qty"] < len(cells) else 0.0
            price = _to_float(cells[col_map["price"]]) if col_map.get("price") is not None and col_map["price"] < len(cells) else 0.0
            line_total = _to_float(cells[col_map["total"]]) if col_map.get("total") is not None and col_map["total"] < len(cells) else 0.0
            vat = _to_float(cells[col_map["vat"]]) if col_map.get("vat") is not None and col_map["vat"] < len(cells) else 0.0
            if not name and not line_total and not price:
                continue
            items.append({
                "name": name,
                "qty": qty,
                "price": price,
                "total": line_total or (qty * price),
                "vat": vat,
            })
        if items:
            return items
    return []


def _map_item_columns(header_cells: list[str]) -> dict:
    """Given a header row's cell texts (already lower-cased), figure out
    which column index holds the name, qty, price, vat, and total."""
    mapping: dict = {"name": None, "qty": None, "price": None, "total": None, "vat": None}
    for idx, cell in enumerate(header_cells):
        if mapping["name"] is None and any(hint in cell for hint in _ITEM_HEADER_HINTS):
            mapping["name"] = idx
            continue
        if mapping["qty"] is None and any(hint in cell for hint in _QTY_HINTS):
            mapping["qty"] = idx
            continue
        if mapping["vat"] is None and any(hint in cell for hint in _VAT_HINTS):
            mapping["vat"] = idx
            continue
        if mapping["price"] is None and any(hint in cell for hint in _PRICE_HINTS):
            mapping["price"] = idx
            continue
        if mapping["total"] is None and any(hint in cell for hint in _TOTAL_HINTS):
            mapping["total"] = idx
            continue
    return mapping


_STIR_RE = re.compile(r"\b(\d{9})\b")
_TIME_RE = re.compile(r"\b(\d{2}:\d{2}(?::\d{2})?)\b")


def _extract_meta_from_html(soup: BeautifulSoup, text: str) -> dict:
    """Pull supplementary metadata (address, STIR/INN, time, cashier,
    terminal) out of a soliq.uz receipt page. All fields are optional —
    missing keys are simply omitted from the dict."""
    meta: dict = {}
    address = _extract_labeled_text(soup, ("manzil", "yur. manzil", "адрес"))
    if address:
        meta["address"] = address
    stir = _extract_labeled_text(soup, ("stir", "инн", "tin"))
    if stir:
        match = _STIR_RE.search(stir)
        meta["stir"] = match.group(1) if match else stir.strip()
    time_match = _TIME_RE.search(text)
    if time_match:
        meta["time"] = time_match.group(1)
    cashier = _extract_labeled_text(soup, ("kassir", "кассир", "cashier"))
    if cashier:
        meta["cashier"] = cashier
    terminal = _extract_labeled_text(soup, ("terminal", "терминал"))
    if terminal:
        meta["terminal"] = terminal
    return meta


def _extract_labeled_text(soup: BeautifulSoup, labels: tuple[str, ...]) -> str:
    """Find a table row whose left cell mentions a label and return the
    right cell's text."""
    for tr in soup.select("tr"):
        cells = [_normalize_text(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if len(cells) < 2:
            continue
        for idx, cell in enumerate(cells):
            lowered = cell.lower()
            if not any(label in lowered for label in labels):
                continue
            for value in cells[idx + 1:]:
                value = value.strip(" :")
                if value and value.lower() != cell.lower():
                    return value
    return ""


# --- Extraction helpers --------------------------------------------------

_TERMINAL_ID_RE = re.compile(r"^[A-Z]{1,4}\d{8,}$")
_ADDRESS_SPLIT_RE = re.compile(
    r"\b("
    r"Toshkent|Andijon|Buxoro|Farg(?:'|o)?ona|Jizzax|Namangan|Navoiy|"
    r"Qashqadaryo|Qoraqalpog'?iston|Samarqand|Sirdaryo|Surxondaryo|Xorazm|Urganch|Nukus|"
    r"shahri|sh\.|tumani|tum\.|MFY|mavze|ko['’`]chasi|uy\b|kv\b"
    r")\b",
    flags=re.IGNORECASE,
)
_VENDOR_LABEL_BLOCKLIST = (
    "savdo cheki",
    "sotuv",
    "chek raqami",
    "onlayn nkm nomi",
    "qqs",
    "naqd pul",
    "bank kartalari",
    "jami to",
    "umumiy qqs",
    "sn :",
)


def _normalize_text(text: str) -> str:
    return " ".join(str(text).replace("\xa0", " ").split())


def _clean_vendor_candidate(text: str) -> str:
    text = _normalize_text(text)
    text = re.sub(r"\b\d{9,}\b.*$", "", text).strip()
    m = _ADDRESS_SPLIT_RE.search(text)
    if m:
        text = text[:m.start()].strip(" ,.-")
    return text.strip(" ,.-")


def _is_vendor_candidate(text: str) -> bool:
    text = _normalize_text(text)
    if not text or len(text) < 3 or len(text) > 200:
        return False
    lowered = text.lower()
    if any(label in lowered for label in _VENDOR_LABEL_BLOCKLIST):
        return False
    if text.isdigit() or _TERMINAL_ID_RE.match(text):
        return False
    if re.fullmatch(r"[\d\s:.,/%-]+", text):
        return False
    return True


def _extract_vendor_from_table(soup: BeautifulSoup) -> str:
    for tr in soup.select("tr"):
        cells = [_normalize_text(td.get_text(" ", strip=True)) for td in tr.select("td,th")]
        cells = [cell for cell in cells if cell]
        if not cells:
            continue
        for cell in cells:
            if _is_vendor_candidate(cell):
                cleaned = _clean_vendor_candidate(cell)
                if _is_vendor_candidate(cleaned):
                    return cleaned
    return ""


def _extract_vendor(soup: BeautifulSoup, text: str) -> str:
    # soliq.uz puts the company name in a bold <h3> — check this first
    # (it's the second line: "Savdo cheki/Sotuv" is the first h3, company is the second)
    h3_tags = soup.find_all("h3")
    for h3 in h3_tags:
        raw = h3.get_text(strip=True)
        name = _clean_vendor_candidate(raw)
        if _is_vendor_candidate(name) and len(name) < 120:
            return name

    # Try common class names used on soliq pages
    for selector in [".company-name", ".seller-name", ".title", "h1", "h2"]:
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            name = _clean_vendor_candidate(el.get_text(strip=True))
            if _is_vendor_candidate(name) and len(name) < 120:
                return name

    table_vendor = _extract_vendor_from_table(soup)
    if table_vendor:
        return table_vendor
    # Heuristic: first non-empty line that isn't a URL or just a number
    for line in text.splitlines():
        line = _clean_vendor_candidate(line.strip())
        if _is_vendor_candidate(line) and not line.startswith("http") and len(line) <= 120:
            return line
    return ""


_MONEY_RE = r"([\d\s\u00a0]+(?:[.,]\d+)?)"


def _extract_labeled_amount_from_table(soup: BeautifulSoup, labels: tuple[str, ...]) -> Optional[float]:
    for tr in soup.select("tr"):
        cells = [_normalize_text(td.get_text(" ", strip=True)) for td in tr.select("td,th")]
        cells = [cell for cell in cells if cell]
        if not cells:
            continue
        for idx, cell in enumerate(cells):
            lowered = cell.lower().strip()
            if len(lowered) > 80:
                continue
            if not any(label in lowered for label in labels):
                continue
            # Soliq tables sometimes flatten multiple labeled values into one row.
            # In those cases, the value we want is the first numeric cell after
            # the matching label, not the last numeric cell in the row.
            for value_cell in cells[idx + 1:]:
                if re.search(r"\d", value_cell):
                    return _to_float(value_cell)
    return None


def _extract_vat_from_table(soup: BeautifulSoup) -> Optional[float]:
    # Prefer the "Umumiy QQS qiymati" (total VAT) row over per-item rows.
    total = _extract_labeled_amount_from_table(soup, ("umumiy qqs qiymati",))
    if total is not None:
        return total
    return _extract_labeled_amount_from_table(soup, ("qqs qiymati", "ндс", "vat"))


def _extract_vat(text: str) -> Optional[float]:
    # QQS (Uzbek), НДС (Russian), VAT (English)
    patterns = [
        rf"(?:Jami\s+)?QQS[^\d-]*{_MONEY_RE}",
        rf"НДС[^\d-]*{_MONEY_RE}",
        rf"VAT[^\d-]*{_MONEY_RE}",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            return _to_float(m.group(1))
    return None


def _extract_total(text: str) -> Optional[float]:
    patterns = [
        rf"To'lov\s+uchun\s+jami[^\d-]*{_MONEY_RE}",
        rf"Jami[^\d-]*{_MONEY_RE}\s*(?:UZS|so'm|сум)",
        rf"Итого[^\d-]*{_MONEY_RE}",
        rf"Total[^\d-]*{_MONEY_RE}",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            return _to_float(m.group(1))
    return None


def _extract_total_from_table(soup: BeautifulSoup) -> Optional[float]:
    return _extract_labeled_amount_from_table(
        soup,
        ("jami to`lov", "jami to'lov", "to'lov uchun jami", "итого", "total"),
    )


def _extract_date(text: str) -> str:
    # dd-mm-yyyy or dd.mm.yyyy or yyyy-mm-dd
    m = re.search(r"(\d{2})[-./](\d{2})[-./](\d{4})", text)
    if m:
        d, mo, y = m.groups()
        try:
            return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = re.search(r"(\d{4})[-./](\d{2})[-./](\d{2})", text)
    if m:
        y, mo, d = m.groups()
        try:
            return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def _extract_receipt_number(text: str, qr_url: str) -> str:
    # Prefer the `r` parameter from the QR URL
    try:
        qs = parse_qs(urlparse(qr_url).query)
        r = (qs.get("r") or [None])[0]
        if r:
            return str(r)
    except Exception:
        pass
    m = re.search(r"(?:Chek|Чек|Receipt)[^\d]*(\d{6,})", text, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    return ""


def _parse_date(val) -> str:
    if not val:
        return ""
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(val / 1000 if val > 1e12 else val).strftime("%Y-%m-%d")
        except Exception:
            return ""
    s = str(val)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s[: len(fmt) + 5], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last resort: look for a date inside
    return _extract_date(s)


def _parse_time(val) -> str:
    """Extract HH:MM (or HH:MM:SS) from a soliq datetime string. Returns
    empty string if nothing time-shaped is found."""
    if not val:
        return ""
    s = str(val)
    match = _TIME_RE.search(s)
    return match.group(1) if match else ""


def _to_float(x) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace("\u00a0", " ").replace(" ", "")
    # Strip anything non-numeric except common separators and '-'
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return 0.0

    if "," in s and "." in s:
        last_comma = s.rfind(",")
        last_dot = s.rfind(".")
        decimal_sep = "," if last_comma > last_dot else "."
        thousands_sep = "." if decimal_sep == "," else ","
        s = s.replace(thousands_sep, "")
        if decimal_sep == ",":
            s = s.replace(",", ".")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            s = ".".join(parts)
        else:
            s = "".join(parts)
    elif "." in s:
        parts = s.split(".")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            s = ".".join(parts)
        else:
            s = "".join(parts)

    try:
        return float(s)
    except ValueError:
        return 0.0
