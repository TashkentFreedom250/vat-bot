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
    if not qr_url:
        return None

    try:
        qs = parse_qs(urlparse(qr_url).query)
    except Exception:
        qs = {}

    terminal = (qs.get("t") or [None])[0]
    receipt = (qs.get("r") or [None])[0]
    date_param = (qs.get("c") or [None])[0]
    amount_param = (qs.get("s") or [None])[0]
    fallback_date = _date_from_qr_param(qr_url)

    # Build a deduplicated list of URLs to try.
    # We only add the /api/check variant if it differs from qr_url.
    urls_to_try: list[str] = [qr_url]
    if terminal and receipt:
        alt = (
            f"https://ofd.soliq.uz/api/check"
            f"?t={terminal}&r={receipt}&c={date_param}&s={amount_param}"
        )
        if alt != qr_url:
            urls_to_try.append(alt)

    async with httpx.AsyncClient(
        timeout=config.SOLIQ_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "uz,en;q=0.9,ru;q=0.8"},
        follow_redirects=True,
    ) as client:
        for url in urls_to_try:
            try:
                resp = await client.get(url)
            except Exception as exc:
                logger.warning("soliq.uz fetch error for %s: %r", url, exc)
                continue

            if resp.status_code != 200:
                logger.warning("soliq.uz returned HTTP %s for %s", resp.status_code, url)
                continue

            content_type = resp.headers.get("content-type", "")
            logger.info(
                "soliq.uz %s → %s, content-type=%s, len=%s",
                url, resp.status_code, content_type, len(resp.text),
            )

            # Try JSON first (some terminals return application/json)
            if "application/json" in content_type:
                try:
                    data = resp.json()
                    normalized = _normalize_json(data, terminal or "", receipt or "")
                    if normalized:
                        return normalized
                except Exception as exc:
                    logger.warning("JSON parse failed: %s", exc)

            # Fall back to HTML scraping
            try:
                result = _parse_html(resp.text, qr_url, fallback_date=fallback_date)
                if result:
                    return result
            except Exception as exc:
                logger.warning("HTML parse error for %s: %s", url, exc)

    logger.warning("fetch_receipt_data: all attempts failed for %s", qr_url)
    return None


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

        if not vendor and not total:
            return None

        return {
            "vendor": str(vendor).strip() or "Unknown vendor",
            "date": date_str,
            "receipt_number": str(receipt),
            "vat_amount": _to_float(vat),
            "total_amount": _to_float(total),
            "raw": {"source": "json", "terminal": terminal},
        }
    except Exception:
        return None


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

    # If we got nothing usable, give up
    if vat is None and total is None and not vendor:
        return None

    return {
        "vendor": vendor or "Unknown vendor",
        "date": date_str,
        "receipt_number": receipt_no,
        "vat_amount": vat or 0.0,
        "total_amount": total or 0.0,
        "raw": {"source": "html"},
    }


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
