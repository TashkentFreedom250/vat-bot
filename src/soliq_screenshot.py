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


# API endpoint soliq's SPA calls at runtime — we intercept the response
# here to recover receipt data instead of parsing HTML that no longer
# exists (their site switched to a React SPA in June 2026; the /check
# URL now returns just an empty <div id="root"></div>).
_API_URL_FRAGMENT = "new-ofd.soliq.uz/api/payment"


async def capture(url: str) -> tuple[Optional[bytes], Optional[dict]]:
    """Render the URL, intercept soliq's JSON API response, and take a
    JPEG screenshot of the rendered receipt block. Returns
    (png_bytes or None, api_data or None). Both are best-effort — a
    missing screenshot falls back to the data renderer, and missing
    API data is handled by the caller (which used to rely on HTML
    scraping and no longer works).
    """
    if _browser is None:
        await startup()
        if _browser is None:
            return None, None

    api_data: dict[str, dict] = {}

    try:
        async with _lock:
            context = await _browser.new_context(
                viewport={"width": _VIEWPORT_W, "height": _VIEWPORT_H},
                device_scale_factor=_DEVICE_SCALE,
                ignore_https_errors=True,
            )
            page = await context.new_page()

            # Intercept the SPA's XHR. Playwright's response event fires
            # once the browser gets a full body — perfect timing to
            # cache the JSON without slowing anything down.
            async def _capture_response(resp):
                if _API_URL_FRAGMENT not in resp.url:
                    return
                try:
                    if resp.status == 200:
                        payload = await resp.json()
                        if isinstance(payload, dict):
                            api_data["value"] = payload
                except Exception:
                    logger.debug("Failed to read /api/payment body", exc_info=True)

            page.on("response", lambda r: asyncio.create_task(_capture_response(r)))

            try:
                # networkidle so the SPA's XHR completes before we
                # screenshot. domcontentloaded fires before the API
                # call and would give us a blank React shell.
                await page.goto(url, wait_until="networkidle",
                                timeout=_NAV_TIMEOUT_MS)
                # Small buffer in case the response handler is still
                # awaiting body() when goto returns.
                await page.wait_for_timeout(400)

                # Find the receipt content's bounding box for a tight
                # screenshot. The SPA renders inside a #root element;
                # we look for the biggest child inside root that
                # contains the totals row and clip to its bounds. If
                # that fails, we take the full page (the whole SPA
                # viewport is receipt content — no header banner).
                bounds = await page.evaluate("""
                    () => {
                        const root = document.querySelector("#root");
                        if (!root) return { top: 0, bottom: 0 };
                        const rootRect = root.getBoundingClientRect();
                        // Look for the totals row (Jami / Umumiy / Итого)
                        // among leaf-ish elements. Same approach we used
                        // for the old server-rendered layout.
                        const labels = ["jami to", "umumiy qqs", "umumiy q",
                                        "итого", "total", "vat"];
                        const all = Array.from(root.querySelectorAll("*"));
                        let bottom = 0;
                        for (const el of all) {
                            const direct = (el.textContent || "").trim().toLowerCase();
                            if (!direct) continue;
                            if (!labels.some(l => direct.startsWith(l))) continue;
                            if (el.childElementCount > 8) continue;
                            const rect = el.getBoundingClientRect();
                            const elBottom = rect.bottom + window.scrollY;
                            if (elBottom > bottom) bottom = elBottom;
                        }
                        return {
                            top: rootRect.top + window.scrollY,
                            bottom: bottom,
                        };
                    }
                """)

                screenshot_kwargs = {
                    "type": "jpeg",
                    "quality": 85,
                    "full_page": True,
                }
                top = max(0, int(bounds.get("top") or 0))
                bottom = int(bounds.get("bottom") or 0)
                if bottom > top + 200:
                    screenshot_kwargs["clip"] = {
                        "x": 0,
                        "y": top,
                        "width": _VIEWPORT_W,
                        "height": (bottom - top) + 25,
                    }

                png = await page.screenshot(**screenshot_kwargs)
                return png, api_data.get("value")
            finally:
                await context.close()
    except Exception:
        logger.exception("soliq page capture failed for %s", url)
        return None, api_data.get("value")
