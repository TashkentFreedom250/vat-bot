"""
Read a soliq.uz receipt page with a headless browser.

One trip gives us both things we need from the tax authority:

  * the receipt DATA — soliq.uz is a JavaScript app (since June 2026);
    the numbers only exist in the answer to the page's own call to
    new-ofd.soliq.uz/api/payment, which we intercept;
  * a SCREENSHOT of the rendered receipt, embedded in /export_pdf.

How it stays fast (measured Nov 2026: a healthy lookup is well under a
second, but the old code made a user wait 18-31 s on every hiccup):

  * One long-lived Chromium AND one long-lived browser context, so the
    site's scripts, fonts and open connections are reused between
    receipts instead of being fetched from scratch every time.
  * Up to _MAX_CONCURRENT_PAGES receipts are read at the same time. A
    single global lock used to make a burst of photos queue up.
  * We react to the API answer the instant it lands instead of waiting
    for the whole page (the embedded map can stall "load" forever).
  * A stuck connection is abandoned after a few seconds and retried.
    Healthy navigations commit in ~0.1 s, so 8 s of silence means that
    connection is dead, not slow.
  * When the API itself answers "no such receipt", we stop immediately
    and hand the caller that answer, instead of waiting out a timeout.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from .soliq_url import is_soliq_url

logger = logging.getLogger("vat_bot.soliq_shot")

_VIEWPORT_W = 800
_VIEWPORT_H = 1200
_DEVICE_SCALE = 2  # retina-style oversampling — keeps text crisp once scaled

# Per-attempt navigation timeouts. Three quick tries beat one long one:
# soliq's failures are per-connection (the very next try usually works).
_NAV_TIMEOUTS_MS = (8_000, 8_000, 12_000)
# After the page has committed: how long to wait for the payment API.
_API_WAIT_S = 10.0
# If the page never even ISSUES the API request this long after
# navigating, the link is not a receipt page — don't wait the full time.
_API_REQUEST_GRACE_S = 5.0
# Cap on waiting for React to paint the data before the screenshot.
_RENDER_WAIT_MS = 3_000
_MAX_CONCURRENT_PAGES = 3
# Recycle the shared context now and then so weeks of uptime can't
# accumulate cookies/cache/service-worker state without bound.
_CONTEXT_MAX_USES = 200

# The call soliq's page makes for the receipt data. Matched on the exact
# host + path prefix (not a substring) so nothing but soliq's own API
# can ever supply the numbers we record as "verified".
_API_HOST = "new-ofd.soliq.uz"
_API_PATH_PREFIX = "/api/payment"

_pw = None
_browser = None
_context = None
_context_uses = 0
_active_pages = 0
# Created lazily inside the running loop — see _primitives().
_sem: Optional[asyncio.Semaphore] = None
_state_lock: Optional[asyncio.Lock] = None
_owner_loop: Optional[asyncio.AbstractEventLoop] = None


@dataclass
class Capture:
    """Outcome of one receipt-page read."""

    screenshot: Optional[bytes] = None   # JPEG bytes
    api_data: Optional[dict] = None      # the payment API's JSON (HTTP 200)
    api_status: Optional[int] = None     # None = the API never answered
    api_message: str = ""                # soliq's own error text, if any
    error: str = ""                      # why the page could not be read
    fatal: bool = False                  # error that a retry cannot fix
    attempts: int = 0
    seconds: float = 0.0

    @property
    def definitive(self) -> bool:
        """True when soliq gave a real answer that a retry won't change
        (e.g. 404 'receipt not found', 400 'bad fiscal sign')."""
        if self.fatal:
            return True
        return (
            self.api_status is not None
            and 400 <= self.api_status < 500
            and self.api_status not in (408, 425, 429)
        )


def _primitives() -> tuple[asyncio.Semaphore, asyncio.Lock]:
    """Semaphore + state lock for the CURRENT event loop.

    bot.main() rebuilds the application on a brand-new event loop after
    a polling failure. Anything created on the old loop (asyncio
    primitives, the Playwright connection) is unusable on the new one,
    so when the loop changes we drop it all and start clean. The old
    Chromium exits by itself once its driver pipe closes."""
    global _sem, _state_lock, _owner_loop
    global _pw, _browser, _context, _context_uses, _active_pages
    loop = asyncio.get_running_loop()
    if _owner_loop is not loop:
        if _browser is not None:
            logger.warning("Event loop changed — discarding the old browser handle.")
        _pw = None
        _browser = None
        _context = None
        _context_uses = 0
        _active_pages = 0
        _sem = asyncio.Semaphore(_MAX_CONCURRENT_PAGES)
        _state_lock = asyncio.Lock()
        _owner_loop = loop
    return _sem, _state_lock


async def _teardown() -> None:
    """Close context, browser and driver. Never raises."""
    global _pw, _browser, _context, _context_uses
    for label, obj, method in (
        ("context", _context, "close"),
        ("browser", _browser, "close"),
        ("playwright", _pw, "stop"),
    ):
        if obj is None:
            continue
        try:
            await getattr(obj, method)()
        except Exception:
            logger.debug("%s %s raised (already gone?)", label, method, exc_info=True)
    _pw = None
    _browser = None
    _context = None
    _context_uses = 0


async def _acquire_context():
    """Return the shared browser context, launching or re-launching
    Chromium when needed. Raises if the browser cannot be started.
    Every successful call must be paired with _release_context()."""
    global _pw, _browser, _context, _context_uses, _active_pages
    _, state_lock = _primitives()
    async with state_lock:
        if _browser is not None and not _browser.is_connected():
            # Chromium crashed or was killed. Before this check existed
            # a dead browser meant every receipt failed until someone
            # restarted the bot.
            logger.warning("Headless Chromium is gone — relaunching it.")
            await _teardown()

        if _browser is None:
            from playwright.async_api import async_playwright

            try:
                _pw = await async_playwright().start()
                _browser = await _pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-gpu", "--hide-scrollbars"],
                )
            except Exception:
                await _teardown()
                raise
            logger.info("Playwright Chromium launched for soliq screenshots.")

        if (
            _context is not None
            and _context_uses >= _CONTEXT_MAX_USES
            and _active_pages == 0
        ):
            try:
                await _context.close()
            except Exception:
                logger.debug("Context close raised during recycle", exc_info=True)
            _context = None

        if _context is None:
            _context = await _browser.new_context(
                viewport={"width": _VIEWPORT_W, "height": _VIEWPORT_H},
                device_scale_factor=_DEVICE_SCALE,
                ignore_https_errors=True,
            )
            _context_uses = 0

        _context_uses += 1
        _active_pages += 1
        return _context


def _release_context() -> None:
    global _active_pages
    _active_pages = max(0, _active_pages - 1)


async def startup() -> None:
    """Launch the shared headless browser. Called once from the bot's
    startup hook so the first receipt doesn't pay the ~2 s cold start.
    Safe to call repeatedly. Never raises — if Chromium can't start now,
    the next receipt tries again."""
    try:
        await _acquire_context()
        _release_context()
    except Exception:
        logger.exception(
            "Failed to launch Playwright Chromium; soliq.uz lookups will "
            "keep retrying the launch on each receipt."
        )


async def shutdown() -> None:
    """Tear down the shared browser. Called from bot shutdown hooks."""
    global _sem, _state_lock, _owner_loop, _active_pages
    await _teardown()
    _sem = None
    _state_lock = None
    _owner_loop = None
    _active_pages = 0


def _short_error(exc: BaseException) -> str:
    """First line of a Playwright error, without the multi-line call log."""
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:200]


# True once the receipt has actually been painted: the totals row is on
# the page and nothing is still loading. Replaces a fixed sleep.
_RENDERED_JS = """
() => {
    const root = document.querySelector("#root");
    if (!root) return false;
    const text = (root.innerText || "").toLowerCase();
    if (!/jami to|umumiy q|итого|total/.test(text)) return false;
    if (document.fonts && document.fonts.status === "loading") return false;
    return Array.from(document.images).every(img => img.complete);
}
"""

# Bounding box of the receipt block, top of #root down to the totals
# row. Everything below it (the map widget) is useless in a tax record.
_BOUNDS_JS = """
() => {
    const root = document.querySelector("#root");
    if (!root) return { top: 0, bottom: 0 };
    const rootRect = root.getBoundingClientRect();
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
"""


async def _take_screenshot(page) -> Optional[bytes]:
    """JPEG of the rendered receipt block, or None. Never raises — a
    missing screenshot only means the PDF page falls back to the clean
    data view; it must not cost the user their receipt."""
    try:
        try:
            await page.wait_for_function(_RENDERED_JS, timeout=_RENDER_WAIT_MS)
        except Exception:
            logger.debug("Receipt paint wait timed out; capturing anyway.")
        # One short beat for layout to settle after the last paint.
        await page.wait_for_timeout(150)

        bounds = await page.evaluate(_BOUNDS_JS)
        kwargs = {"type": "jpeg", "quality": 85, "full_page": True}
        top = max(0, int(bounds.get("top") or 0))
        bottom = int(bounds.get("bottom") or 0)
        if bottom > top + 200:
            kwargs["clip"] = {
                "x": 0,
                "y": top,
                "width": _VIEWPORT_W,
                "height": (bottom - top) + 25,
            }
        return await page.screenshot(**kwargs)
    except Exception as exc:
        logger.warning("soliq screenshot failed (%s)", _short_error(exc))
        return None


async def _fetch_once(url: str, want_screenshot: bool, nav_timeout_ms: int) -> Capture:
    result = Capture()
    context = await _acquire_context()
    page = None
    try:
        page = await context.new_page()
        loop = asyncio.get_running_loop()
        api_done: asyncio.Future = loop.create_future()
        api_requested = False
        readers: list[asyncio.Task] = []

        def _is_api(target_url: str) -> bool:
            try:
                parsed = urlparse(target_url)
            except ValueError:
                return False
            return (
                (parsed.hostname or "").lower() == _API_HOST
                and parsed.path.startswith(_API_PATH_PREFIX)
            )

        def _on_request(req) -> None:
            nonlocal api_requested
            if _is_api(req.url):
                api_requested = True

        async def _read_api(resp) -> None:
            status = resp.status
            payload = None
            try:
                payload = await resp.json()
            except Exception:
                logger.debug("Could not parse /api/payment body", exc_info=True)
            if not api_done.done():
                api_done.set_result((status, payload))

        def _on_response(resp) -> None:
            if api_done.done() or not _is_api(resp.url):
                return
            if resp.request.method == "OPTIONS":
                return  # CORS preflight, not the data
            readers.append(asyncio.create_task(_read_api(resp)))

        page.on("request", _on_request)
        page.on("response", _on_response)

        # "commit" = the server answered. We deliberately do not wait
        # for "load": the page's embedded map keeps the load event
        # pending for a long time on bad days, and the only thing we
        # need is the API answer intercepted above.
        try:
            await page.goto(url, wait_until="commit", timeout=nav_timeout_ms)
        except Exception as exc:
            result.error = _short_error(exc)
            return result

        if not is_soliq_url(page.url):
            result.error = "the link redirected away from soliq.uz"
            result.fatal = True
            return result

        committed = time.monotonic()
        while not api_done.done():
            waited = time.monotonic() - committed
            if waited >= _API_WAIT_S:
                break
            if not api_requested and waited >= _API_REQUEST_GRACE_S:
                break
            await asyncio.wait({api_done}, timeout=0.25)

        if not api_done.done():
            api_done.cancel()
            if api_requested:
                result.error = "soliq.uz opened but never returned the receipt data"
            else:
                result.error = "soliq.uz opened but this link is not a receipt page"
                result.fatal = True
            return result

        status, payload = api_done.result()
        result.api_status = status
        if status == 200 and isinstance(payload, dict):
            result.api_data = payload
        else:
            if isinstance(payload, dict):
                result.api_message = str(payload.get("message") or "")[:200]
            return result

        if want_screenshot:
            result.screenshot = await _take_screenshot(page)
        return result
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                logger.debug("page.close raised", exc_info=True)
        _release_context()


async def fetch_page(url: str, *, want_screenshot: bool = True) -> Capture:
    """Read one soliq.uz receipt page. Never raises.

    Returns a Capture: `.api_data` holds the receipt JSON on success;
    otherwise `.api_status`/`.api_message` carry soliq's own answer, or
    `.error` says why the page could not be read at all.
    """
    started = time.monotonic()
    if not is_soliq_url(url):
        return Capture(error="not a soliq.uz link", fatal=True)

    sem, _ = _primitives()
    result = Capture()
    attempts = 0
    async with sem:
        for nav_timeout_ms in _NAV_TIMEOUTS_MS:
            attempts += 1
            try:
                result = await _fetch_once(url, want_screenshot, nav_timeout_ms)
            except Exception as exc:
                # Browser launch failure or a crash mid-read.
                logger.warning(
                    "soliq read crashed on attempt %s: %s", attempts, _short_error(exc)
                )
                result = Capture(error=_short_error(exc))
            if result.api_data is not None or result.definitive:
                break
            if attempts < len(_NAV_TIMEOUTS_MS):
                logger.warning(
                    "soliq read attempt %s failed (%s) — retrying. %s",
                    attempts,
                    result.error or f"API HTTP {result.api_status}",
                    url,
                )

    result.attempts = attempts
    result.seconds = time.monotonic() - started
    if result.api_data is not None:
        logger.info(
            "soliq read ok in %.2fs (attempt %s, screenshot=%s)",
            result.seconds, attempts,
            "yes" if result.screenshot else ("no" if want_screenshot else "skipped"),
        )
    elif result.definitive:
        logger.info(
            "soliq gave a final 'no' in %.2fs (%s) for %s",
            result.seconds,
            result.error or f"HTTP {result.api_status}: {result.api_message or '-'}",
            url,
        )
    else:
        logger.error(
            "soliq page capture failed for %s after %s attempt(s) in %.1fs: %s",
            url, attempts, result.seconds,
            result.error or f"API HTTP {result.api_status}",
        )
    return result


async def capture(url: str) -> tuple[Optional[bytes], Optional[dict]]:
    """Back-compat wrapper: (screenshot_jpeg or None, api_data or None)."""
    result = await fetch_page(url, want_screenshot=True)
    return result.screenshot, result.api_data
