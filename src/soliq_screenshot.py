"""
Capture a pixel-perfect screenshot of a soliq.uz receipt page.

Used at receipt-save time so /export_pdf can embed an exact visual
reproduction of the soliq.uz page in the receipt package — what the
user sees on the website is exactly what the tax office sees in the
PDF.

Implementation: a single long-running headless Chromium instance held
in module-level state. Spinning up a fresh browser per receipt would
add ~2s to every save; reusing a warm context keeps it under 500ms
per page.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("vat_bot.soliq_shot")

_VIEWPORT_W = 800
_VIEWPORT_H = 1200
_DEVICE_SCALE = 2  # retina-style oversampling — keeps text crisp once scaled
_NAV_TIMEOUT_MS = 25_000

# Cropping: soliq.uz pages render a Google Maps section at the bottom
# that's not useful in a tax record and bloats the screenshot. We trim
# it by waiting for the page to render then asking Chromium for the
# bounding box of the document EXCLUDING the map iframe.
_TRIM_SELECTOR = "iframe, .map, #map, .gmap"

_pw = None
_browser = None
_lock = asyncio.Lock()


async def startup() -> None:
    """Launch the shared headless browser. Called once from the bot's
    application startup hook. Safe to call repeatedly — re-entrant."""
    global _pw, _browser
    if _browser is not None:
        return
    try:
        from playwright.async_api import async_playwright
        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--hide-scrollbars"],
        )
        logger.info("Playwright Chromium launched for soliq screenshots.")
    except Exception:
        logger.exception("Failed to launch Playwright Chromium; "
                         "PDF screenshots will be unavailable.")
        _pw = None
        _browser = None


async def shutdown() -> None:
    """Tear down the shared browser. Called from bot shutdown hooks."""
    global _pw, _browser
    try:
        if _browser is not None:
            await _browser.close()
    except Exception:
        logger.debug("Browser close raised (already gone?)", exc_info=True)
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:
        logger.debug("Playwright stop raised", exc_info=True)
    _pw = None
    _browser = None


async def capture(url: str) -> Optional[bytes]:
    """Render the URL and return a JPEG screenshot of the receipt block.

    Returns None on any failure — the caller treats a missing screenshot
    as "fall back to the soliq-data renderer" rather than failing the
    whole save flow. We never want a screenshot hiccup to drop a
    receipt the user took the trouble to photograph.
    """
    if _browser is None:
        # Lazy startup — the bot may not have called startup() (e.g. CLI
        # usage). Best effort: try to launch now.
        await startup()
        if _browser is None:
            return None

    try:
        async with _lock:
            context = await _browser.new_context(
                viewport={"width": _VIEWPORT_W, "height": _VIEWPORT_H},
                device_scale_factor=_DEVICE_SCALE,
                ignore_https_errors=True,
            )
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=_NAV_TIMEOUT_MS)
                # Wait for the receipt table to render — the page is
                # hydrated by JS after DOMContentLoaded.
                try:
                    await page.wait_for_selector("table", timeout=8000)
                except Exception:
                    pass

                # Find the receipt content's bounding box. We clip from
                # the top of the .ticket-wrap div (skipping the SOLIQ
                # mobile-app banner that wastes the top ~110 px) down
                # to the bottom of the last totals row (skipping the
                # Yandex map injected at the bottom of ticket-wrap).
                bounds = await page.evaluate("""
                    () => {
                        const wrap = document.querySelector(".ticket-wrap")
                                 || document.querySelector("main")
                                 || document.body;
                        const wrapTop = wrap
                            ? (wrap.getBoundingClientRect().top + window.scrollY)
                            : 0;
                        const labels = ["jami to", "umumiy qqs", "umumiy q", "итого"];
                        const all = Array.from(document.querySelectorAll("body *"));
                        let bottom = 0;
                        for (const el of all) {
                            const direct = (el.textContent || "").trim().toLowerCase();
                            if (!direct) continue;
                            if (!labels.some(l => direct.startsWith(l))) continue;
                            // Skip wrapping containers — we want the row.
                            if (el.childElementCount > 8) continue;
                            const rect = el.getBoundingClientRect();
                            const elBottom = rect.bottom + window.scrollY;
                            if (elBottom > bottom) bottom = elBottom;
                        }
                        return { top: wrapTop, bottom: bottom };
                    }
                """)

                # full_page=True is required for clip to reach below the
                # viewport fold. Without it Playwright clamps clip to
                # viewport height and the totals row gets chopped.
                screenshot_kwargs = {
                    "type": "jpeg",
                    "quality": 85,
                    "full_page": True,
                }
                top = max(0, int(bounds.get("top") or 0))
                bottom = int(bounds.get("bottom") or 0)
                if bottom > top + 200:
                    # Pad 25 px below the totals row so sub-pixel rounding
                    # doesn't shave the bottom border off.
                    screenshot_kwargs["clip"] = {
                        "x": 0,
                        "y": top,
                        "width": _VIEWPORT_W,
                        "height": (bottom - top) + 25,
                    }

                png = await page.screenshot(**screenshot_kwargs)
                return png
            finally:
                await context.close()
    except Exception:
        logger.exception("soliq screenshot capture failed for %s", url)
        return None
