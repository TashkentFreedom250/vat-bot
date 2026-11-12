"""
Recognising soliq.uz receipt links.

Kept free of heavy imports (no config, no OpenCV, no httpx) so the QR
decoder, the text handler and the scraper can all share ONE definition
of "this is a soliq.uz link".

Why this is strict: the bot loads the link in a headless browser and
records whatever VAT amount the page's API call returns as "Verified by
soliq.uz". A substring test ("soliq.uz" appears somewhere in the text)
would also accept https://example.com/soliq.uz — a page anyone could
host, returning any VAT amount they like. So the HOST must be soliq.uz
or one of its subdomains, nothing else.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
# A link typed or shared without its scheme: "ofd.soliq.uz/check?t=..."
_BARE_RE = re.compile(r"(?<![\w./-])((?:[\w-]+\.)*soliq\.uz/[^\s<>\"']+)", re.IGNORECASE)
_TRAILING_JUNK = ".,;:!?)]}>'\""


def is_soliq_host(host: Optional[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    return host == "soliq.uz" or host.endswith(".soliq.uz")


def is_soliq_url(url: str) -> bool:
    """True only for an http(s) URL whose host is soliq.uz or a subdomain."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    return parsed.scheme.lower() in ("http", "https") and is_soliq_host(parsed.hostname)


def find_soliq_url(text: str) -> Optional[str]:
    """Pull the first soliq.uz link out of free text.

    Handles the things people actually send: the bare link, the link with
    a sentence around it ("here it is https://ofd.soliq.uz/... thanks"),
    and a link with no https:// in front. Returns a clean absolute URL,
    or None.
    """
    if not text:
        return None
    for match in _URL_RE.finditer(text):
        candidate = match.group(0).rstrip(_TRAILING_JUNK)
        if is_soliq_url(candidate):
            return candidate
    for match in _BARE_RE.finditer(text):
        candidate = "https://" + match.group(1).rstrip(_TRAILING_JUNK)
        if is_soliq_url(candidate):
            return candidate
    return None
