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

import re
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from . import config

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


async def fetch_receipt_data(qr_url: str) -> Optional[dict]:
    """Fetch and parse a soliq.uz receipt page from a QR URL."""
    if not qr_url:
        return None

    async with httpx.AsyncClient(
        timeout=config.SOLIQ_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "uz,en;q=0.9,ru;q=0.8"},
        follow_redirects=True,
    ) as client:
        # 1) Try the JSON API first (ofd.soliq.uz exposes one for some terminals)
        data = await _try_json_api(client, qr_url)
        if data:
            return data

        # 2) Fall back to HTML scraping of the consumer receipt page
        try:
            resp = await client.get(qr_url)
            resp.raise_for_status()
        except httpx.HTTPError:
            return None

        fallback_date = _date_from_qr_param(qr_url)
        return _parse_html(resp.text, qr_url, fallback_date=fallback_date)


async def _try_json_api(client: httpx.AsyncClient, qr_url: str) -> Optional[dict]:
    """Try known soliq.uz JSON endpoints based on the QR URL parameters."""
    try:
        parsed = urlparse(qr_url)
        qs = parse_qs(parsed.query)
    except Exception:
        return None

    terminal = (qs.get("t") or [None])[0]
    receipt = (qs.get("r") or [None])[0]
    date = (qs.get("c") or [None])[0]
    amount = (qs.get("s") or [None])[0]

    if not (terminal and receipt):
        return None

    # Known endpoint format
    endpoints = [
        f"https://ofd.soliq.uz/check?t={terminal}&r={receipt}&c={date}&s={amount}",
        f"https://ofd.soliq.uz/api/check?t={terminal}&r={receipt}&c={date}&s={amount}",
    ]
    for url in endpoints:
        try:
            resp = await client.get(url, headers={"Accept": "application/json"})
            if resp.status_code == 200 and "application/json" in resp.headers.get("content-type", ""):
                data = resp.json()
                normalized = _normalize_json(data, terminal, receipt)
                if normalized:
                    return normalized
        except Exception:
            continue
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
    vat = _extract_vat(text)
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

def _extract_vendor(soup: BeautifulSoup, text: str) -> str:
    # Try common class names used on soliq pages
    for selector in [".company-name", ".seller-name", "h1", "h2", ".title"]:
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            name = el.get_text(strip=True)
            if len(name) < 120:
                return name
    # Heuristic: first non-empty line that isn't a URL or just a number
    for line in text.splitlines():
        line = line.strip()
        if 3 <= len(line) <= 120 and not line.startswith("http") and not line.isdigit():
            return line
    return ""


_MONEY_RE = r"([\d\s\u00a0]+(?:[.,]\d+)?)"


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
    s = str(x).replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    # Strip anything non-numeric except '.' and '-'
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return 0.0
