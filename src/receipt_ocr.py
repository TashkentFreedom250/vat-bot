"""
OCR helpers for receipt previews and printed merchant names.

This module is intentionally best-effort:
  - When QR verification succeeds, OCR only supplements the printed vendor name.
  - When QR verification fails, OCR provides a preview but does not verify tax data.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from difflib import SequenceMatcher
from threading import Lock
from typing import Optional

import cv2
import numpy as np

from .receipt_image import _to_cv

logger = logging.getLogger("vat_bot.ocr")

_ENGINE = None
_ENGINE_LOCK = Lock()

_LABEL_WORDS = (
    "manzil",
    "yur",
    "stir",
    "jshshir",
    "sana",
    "vaqt",
    "chek",
    "savdo",
    "raqami",
    "terminal",
    "terminal s/r",
    "naqd",
    "naqdsiz",
    "kart",
    "rrn",
    "qqs",
    "mxik",
    "shtrix",
    "fiskal",
    "versiya",
    "to'lov",
    "jami",
)
_COMPANY_HINTS = (
    "mchj",
    "llc",
    "ooo",
    "ao",
    "xk",
    "xususiy",
    "family",
    "market",
    "shop",
    "store",
    "cafe",
    "restaurant",
    "bar",
    "hotel",
)
_DATE_RE = re.compile(r"(\d{2})[-./](\d{2})[-./](\d{4})(?:\s*\d{2}:\d{2}(?::\d{2})?)?")
_MONEY_RE = re.compile(r"(-?[\d\s]+(?:[.,]\d{2})|[\d\s]+(?:[.,]\d{3})+(?:[.,]\d{2})?)")
_RECEIPT_NO_RE = re.compile(r"\b\d{3,18}\b")
_PAYMENT_SLIP_HINTS = (
    "оплата",
    "omnara",
    "ornata",
    "somnara",
    "odobreno",
    "одобрено",
    "ono6peho",
    "ono6peno",
    "komissiya",
    "комиссия",
    "komnccua",
    "код ответа",
    "kod otveta",
    "kodotbeta",
    "aip:1800",
    "tvr:",
    "tsi:",
    "mastercard",
    "visa",
    "mtogo",
    "итого",
)
_FISCAL_RECEIPT_HINTS = (
    "savdo",
    "qqs",
    "mxik",
    "shtrix",
    "keshbek",
    "fiskal inzo",
    "savdo raqami",
    "chek",
    "naqd",
    "naqdsiz",
)


def extract_printed_vendor(image_bytes: bytes) -> Optional[str]:
    img = _downscale_for_ocr(_to_cv(image_bytes))
    if img.size == 0:
        return None

    for crop_ratio in (0.25, 0.32):
        top = img[: max(1, int(img.shape[0] * crop_ratio)), :]
        lines = _ocr_lines(top)
        vendor = _extract_vendor_from_lines(lines, image_height=top.shape[0])
        if vendor:
            return vendor
    return None


def extract_receipt_preview(image_bytes: bytes) -> dict:
    img = _downscale_for_ocr(_to_cv(image_bytes))
    if img.size == 0:
        return {}

    lines = _ocr_lines(img)
    if not lines:
        return {}

    vat_amount = _extract_vat_amount_from_lines(lines)
    vat_rate = _extract_vat_rate_from_lines(lines)
    preview = {
        "vendor": _extract_vendor_from_lines(lines, image_height=img.shape[0]),
        "date": _extract_date_from_lines(lines),
        "receipt_number": _extract_receipt_number_from_lines(lines),
        "total_amount": _extract_total_from_lines(lines),
        "vat_amount": vat_amount,
        "vat_rate": vat_rate,
        "vat_hint": _format_vat_hint(vat_amount, vat_rate),
    }
    return {key: value for key, value in preview.items() if value not in (None, "", [])}


def classify_document(image_bytes: bytes) -> dict:
    img = _downscale_for_ocr(_to_cv(image_bytes))
    if img.size == 0:
        return {"kind": "unknown"}

    lines = _ocr_lines(img)
    if not lines:
        return {"kind": "unknown"}

    return _classify_lines(lines, image_height=img.shape[0])


def _get_engine():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE

    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        # RapidOCR emits "The text detection result is empty" at WARNING any
        # time it scans a region without legible text — completely benign, but
        # launchd captures it on stderr and the noise drowns out real errors.
        # Pin the loggers (and the engine's own log_level) to ERROR so only
        # actual problems leak out.
        try:
            logging.getLogger("RapidOCR").setLevel(logging.ERROR)
            logging.getLogger("rapidocr").setLevel(logging.ERROR)
            from rapidocr import RapidOCR
        except Exception:
            logger.exception("RapidOCR is unavailable.")
            return None

        try:
            _ENGINE = RapidOCR(params={"Global.log_level": "ERROR"})
            logging.getLogger("RapidOCR").setLevel(logging.ERROR)
            logging.getLogger("rapidocr").setLevel(logging.ERROR)
        except Exception:
            logger.exception("Failed to initialize RapidOCR.")
            return None
        return _ENGINE


def _downscale_for_ocr(img: np.ndarray, max_dim: int = 2200) -> np.ndarray:
    h, w = img.shape[:2]
    current_max = max(h, w)
    if current_max <= max_dim:
        return img
    scale = max_dim / current_max
    return cv2.resize(
        img,
        (max(1, int(w * scale)), max(1, int(h * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _ocr_lines(img: np.ndarray) -> list[dict]:
    engine = _get_engine()
    if engine is None:
        return []

    variants = [img]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    sharp = cv2.addWeighted(gray, 1.6, cv2.GaussianBlur(gray, (0, 0), 2), -0.6, 0)
    variants.append(sharp)

    seen: set[tuple[str, int, int]] = set()
    found: list[dict] = []
    for variant in variants:
        with _ENGINE_LOCK:
            try:
                result = engine(variant)
            except Exception:
                logger.exception("RapidOCR inference failed.")
                continue
        if not result:
            continue

        boxes = getattr(result, "boxes", None)
        txts = getattr(result, "txts", None)
        scores = getattr(result, "scores", None)
        if boxes is None or txts is None or scores is None:
            continue
        for box, text, score in zip(boxes, txts, scores):
            cleaned = _clean_text(text)
            if not cleaned or float(score or 0) < 0.5:
                continue
            box_arr = np.array(box, dtype=float)
            top = float(np.min(box_arr[:, 1]))
            left = float(np.min(box_arr[:, 0]))
            key = (_fingerprint(cleaned), int(round(top / 6)), int(round(left / 6)))
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "text": cleaned,
                    "score": float(score),
                    "top": top,
                    "left": left,
                }
            )

    found.sort(key=lambda item: (item["top"], item["left"]))
    return found


def _clean_text(text: str) -> str:
    text = " ".join(str(text).replace("\xa0", " ").split())
    text = (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("`", "'")
        .replace("’", "'")
        .replace("‘", "'")
    )
    text = re.sub(r"\bMCH[\]\|1I!]\b", "MCHJ", text, flags=re.IGNORECASE)
    text = re.sub(r'"\s*MCHJ', '" MCHJ', text, flags=re.IGNORECASE)
    text = re.sub(r'(?<=\w)"(?=MCHJ)', '" ', text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,.-")
    return text


def _fingerprint(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _looks_like_company(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in _COMPANY_HINTS) or '"' in text


def _looks_like_vendor(text: str) -> bool:
    lowered = text.lower()
    if not text or len(text) < 3 or len(text) > 80:
        return False
    if any(word in lowered for word in _LABEL_WORDS):
        return False
    if re.search(r"\d{4,}", text):
        return False
    if re.fullmatch(r"[\d\s:.,/%-]+", text):
        return False
    return True


def _extract_vendor_from_lines(lines: list[dict], image_height: int) -> Optional[str]:
    candidates = []
    for idx, line in enumerate(lines):
        text = line["text"]
        if not _looks_like_vendor(text):
            continue
        top_ratio = line["top"] / max(image_height, 1)
        company_like = _looks_like_company(text)
        if top_ratio > 0.45 and not company_like:
            continue
        score = line["score"] + (0.18 if company_like else 0.0) + (0.06 if top_ratio <= 0.2 else 0.0)
        candidates.append(
            {
                "text": text,
                "score": line["score"],
                "rank": score,
                "company_like": company_like,
                "order": idx,
            }
        )

    if not candidates:
        return None

    company_candidates = [item for item in candidates if item["company_like"]]
    if company_candidates:
        return _select_company_candidate(company_candidates)

    best = max(candidates, key=lambda item: (item["rank"], -item["order"]))
    return best["text"]


def _select_company_candidate(candidates: list[dict]) -> str:
    ranked = sorted(candidates, key=lambda item: (item["rank"], item["order"]), reverse=True)
    best_rank = ranked[0]["rank"]
    close = [item for item in candidates if item["rank"] >= best_rank - 0.05]
    for item in reversed(close):
        if any(
            SequenceMatcher(None, item["text"].lower(), other["text"].lower()).ratio() >= 0.88
            for other in close
            if other is not item
        ):
            return item["text"]
    return ranked[0]["text"]


def _extract_date_from_lines(lines: list[dict]) -> str:
    for line in lines:
        match = _DATE_RE.search(line["text"])
        if not match:
            continue
        day, month, year = match.groups()
        try:
            return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _extract_receipt_number_from_lines(lines: list[dict]) -> str:
    labels = ("chek", "receipt")
    for idx, line in enumerate(lines):
        lowered = line["text"].lower()
        if not any(label in lowered for label in labels):
            continue
        same_line = _RECEIPT_NO_RE.findall(line["text"])
        if same_line:
            return same_line[-1]
        for other in lines[max(0, idx - 1) : idx]:
            values = _RECEIPT_NO_RE.findall(other["text"])
            if values:
                return values[-1]
        for other in lines[idx + 1 : idx + 3]:
            values = _RECEIPT_NO_RE.findall(other["text"])
            if values:
                return values[-1]
    return ""


def _extract_total_from_lines(lines: list[dict]) -> Optional[float]:
    for label in ("to'lov uchun jami", "tolov uchun jami", "jami", "total", "итого"):
        amount = _extract_labeled_amount(lines, label)
        if amount not in (None, 0.0):
            return amount
    return None


def _extract_vat_amount_from_lines(lines: list[dict]) -> Optional[float]:
    for label in ("umumiy qqs qiymati", "qqs qiymati", "vat", "ндс"):
        amount = _extract_labeled_amount(lines, label)
        if amount is not None:
            return amount
    for line in lines:
        lowered = line["text"].lower()
        if "qqs" in lowered and "siz" in lowered:
            return 0.0
    return None


def _extract_vat_rate_from_lines(lines: list[dict]) -> Optional[str]:
    for idx, line in enumerate(lines):
        lowered = line["text"].lower()
        if "qqs" not in lowered and "vat" not in lowered:
            continue
        match = re.search(r"(\d{1,2})\s*%", line["text"])
        if match:
            return f"{match.group(1)}%"
        if "siz" in lowered or "s1z" in lowered:
            return "0%"
        for other in lines[idx + 1 : idx + 3]:
            match = re.search(r"(\d{1,2})\s*%", other["text"])
            if match:
                return f"{match.group(1)}%"
    return None


def _extract_labeled_amount(lines: list[dict], label: str) -> Optional[float]:
    label = label.lower()
    for idx, line in enumerate(lines):
        lowered = line["text"].lower()
        if label not in lowered:
            continue
        inline = _extract_money(line["text"])
        if inline is not None:
            return inline
        nearby = []
        for other in lines[max(0, idx - 4) : idx + 5]:
            if other is line or abs(other["top"] - line["top"]) > 250:
                continue
            amount = _extract_money(other["text"])
            if amount is None:
                continue
            nearby.append(
                (
                    other["score"],
                    -abs(other["top"] - line["top"]),
                    other["left"],
                    amount,
                )
            )
        if nearby:
            return max(nearby)[-1]
    return None


def _extract_money(text: str) -> Optional[float]:
    matches = _MONEY_RE.findall(text)
    if not matches:
        return None
    for candidate in reversed(matches):
        amount = _to_float(candidate)
        if amount is not None:
            return amount
    return None


def _to_float(value: str) -> Optional[float]:
    if value is None:
        return None
    cleaned = re.sub(r"[^\d,.\-]", "", str(value).replace("\u00a0", " ").replace(" ", ""))
    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        cleaned = cleaned.replace(thousands_sep, "")
        if decimal_sep == ",":
            cleaned = cleaned.replace(",", ".")
    elif "," in cleaned:
        parts = cleaned.split(",")
        cleaned = ".".join(parts) if len(parts) == 2 and len(parts[1]) in (1, 2) else "".join(parts)
    elif "." in cleaned:
        parts = cleaned.split(".")
        cleaned = ".".join(parts) if len(parts) == 2 and len(parts[1]) in (1, 2) else "".join(parts)

    try:
        return float(cleaned)
    except ValueError:
        return None


def _format_vat_hint(vat_amount: Optional[float], vat_rate: Optional[str]) -> str:
    if vat_rate:
        return vat_rate
    if vat_amount is None:
        return ""
    return f"{vat_amount:,.2f} UZS"


def _normalize_for_match(text: str) -> str:
    text = _clean_text(text).lower()
    return re.sub(r"[^a-z0-9а-яё%' ]+", " ", text)


def _classify_lines(lines: list[dict], image_height: int) -> dict:
    joined = " ".join(_normalize_for_match(line["text"]) for line in lines)
    payment_hits = sum(1 for hint in _PAYMENT_SLIP_HINTS if hint in joined)
    fiscal_hits = sum(1 for hint in _FISCAL_RECEIPT_HINTS if hint in joined)
    vendor = _extract_vendor_from_lines(lines, image_height=image_height)
    card_slip_pattern = (
        any(hint in joined for hint in ("aip:1800", "tvr:", "tsi"))
        and any(hint in joined for hint in ("mastercard", "visa"))
        and any(hint in joined for hint in ("ono6peho", "ono6peno", "mtogo", "komnccua", "omnara", "ornata", "somnara"))
    )

    if (payment_hits >= 2 and fiscal_hits == 0) or (card_slip_pattern and fiscal_hits == 0):
        return {
            "kind": "payment_slip",
            "vendor": vendor or "",
            "payment_hits": payment_hits,
            "fiscal_hits": fiscal_hits,
        }

    if fiscal_hits >= 2:
        return {
            "kind": "fiscal_receipt",
            "vendor": vendor or "",
            "payment_hits": payment_hits,
            "fiscal_hits": fiscal_hits,
        }

    return {
        "kind": "unknown",
        "vendor": vendor or "",
        "payment_hits": payment_hits,
        "fiscal_hits": fiscal_hits,
    }
