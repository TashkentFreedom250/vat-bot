"""
Telegram bot entry point.

Adding a receipt: send a photo (or a soliq.uz link as text).

Commands:
  /start            - welcome + register / request access
  /setname          - set employee name (used in exports)
  /manual           - type a receipt in by hand (QR unreadable)
  /online_purchase  - add a receipt from a soliq.uz link, no photo
  /list             - show all stored receipts
  /export_vat       - get the filled VAT_Refund.xlsx
  /export_pdf       - get the PDF receipt package
  /reset            - delete all your receipts (asks first)
  /help             - show help
Hidden: /whoami, /heartcheck and /report (admins), /pending /approve
/deny (DT approvers).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import time as dt_time
from functools import partial
from html import escape
import logging
import logging.handlers
import os
import signal
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import (
    config,
    db,
    exporter,
    maintenance,
    notice,
    online_snapshot,
    receipt_image,
    receipt_ocr,
    soliq,
    soliq_screenshot,
)
from telegram.request import HTTPXRequest

from .tg_request import ResilientRequest


def _configure_logging() -> None:
    """Log to a rotating daily file the bot manages itself.

    When running under launchd (VAT_BOT_LAUNCHD=1) we skip the console
    handler — stdout/stderr go to launchd's crash-only capture file, and
    everything normal lives in logs/bot.log which rotates itself nightly.
    """
    log_dir = config.BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    if not os.environ.get("VAT_BOT_LAUNCHD"):
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / "bot.log",
        when="midnight",
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


_configure_logging()
logger = logging.getLogger("vat_bot")
SAVE_DEBUG_IMAGES = os.getenv("SAVE_DEBUG_IMAGES", "").lower() in {"1", "true", "yes"}

# Wall-clock start of this bot process — used to report uptime in the
# hidden /heartcheck admin command. Set at import (i.e. process start).
_PROCESS_START_TS = time.time()

# Reference to the running Application's Bot, set in _post_init. Lets
# deep helpers (e.g. the double-claim rejection path in
# _save_verified_receipt, which has no ctx) proactively DM the admins.
_APP_BOT = None


async def _notify_admins(text: str) -> None:
    """Best-effort Telegram DM to every ADMIN_TELEGRAM_IDS entry. Never
    raises — an unreachable admin must not break receipt processing."""
    if _APP_BOT is None:
        logger.warning("Admin notify skipped (bot not started yet): %s", text)
        return
    for admin_id in config.ADMIN_TELEGRAM_IDS:
        try:
            await _APP_BOT.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            logger.warning("Could not DM admin %s", admin_id, exc_info=True)

# Image processing (OpenCV, zxing, PIL) releases the GIL — use all cores
_executor = ThreadPoolExecutor(
    max_workers=(os.cpu_count() or 4) * 2,
    thread_name_prefix="vat-worker",
)


async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.error import Conflict, NetworkError, RetryAfter, TimedOut
    err = ctx.error
    # Transient connectivity hiccups (DNS blips, Mac WiFi sleep, brief
    # api.telegram.org reachability gaps) are already retried internally by
    # python-telegram-bot's polling loop and self-heal within seconds. Logging
    # the full traceback at ERROR makes routine network noise look like a
    # crash in /heartcheck — record a one-line WARNING instead.
    if isinstance(err, (NetworkError, TimedOut)):
        if update is None:
            # From the polling loop, which simply polls again.
            logger.warning("Transient network error (auto-retried): %s", err)
        else:
            # From a handler: an answer to a user did not get through,
            # even after ResilientRequest's connection retries. Say so
            # honestly — this used to be logged as "auto-retried" too,
            # which hid lost replies.
            user = getattr(update, "effective_user", None)
            logger.warning(
                "Network error while answering user %s — a reply may not "
                "have been delivered: %s",
                getattr(user, "id", "?"), err,
            )
        return
    # Conflict means two bot instances briefly polled at the same time
    # (e.g. during a launchd restart while a manual run was still alive).
    # The duplicate dies within seconds and the survivor keeps polling, so
    # this is recovery noise, not an outage.
    if isinstance(err, Conflict):
        logger.warning("Polling conflict (duplicate instance, auto-resolved): %s", err)
        return
    # RetryAfter = Telegram per-bot flood control. Happens during photo
    # bursts. PTB raises it on whatever Telegram call hit the limit; the
    # call sites that matter (send_chat_action / typing keepalive) wrap
    # in try/except so this is never user-facing — log as WARNING.
    if isinstance(err, RetryAfter):
        logger.warning("Telegram flood control: %s", err)
        return
    logger.error("Unhandled exception while processing an update.", exc_info=err)


def _display_vendor(doc: dict) -> str:
    return doc.get("display_vendor") or doc.get("printed_vendor") or doc.get("vendor") or ""


def _qr_failure_message(*, retry: bool) -> str:
    """User-facing message when we couldn't decode a QR. `retry` = a previous
    photo for this same receipt also failed.

    Important: we deliberately do NOT suggest "send a closer photo" here,
    even though that used to be option 1. The previous flow tried to merge
    a new photo's decoded QR into the previously-saved pending image, which
    silently corrupted the image-to-data mapping when users actually sent a
    different receipt by mistake. Recovery paths are now text-only and
    unambiguous: paste the URL, /manual, or /cancel_pending.
    """
    lead = (
        "Sorry, I still couldn't read the QR code on this receipt."
        if retry
        else "Sorry, I couldn't read the QR code from this receipt."
    )
    return (
        f"{lead}\n\n"
        "👉 <b>Best way to finish this entry:</b> paste the soliq.uz "
        "link.\n\n"
        "How: open your phone's camera, point it at the QR, "
        "long-press the yellow banner → Share → pick this chat. "
        "I'll fetch verified data from soliq.uz and finish the entry "
        "with the photo you already sent.\n\n"
        "<i>Last resort:</i> /manual to type it in by hand (only if "
        "you really can't get the link). Or /cancel_pending to discard "
        "and start fresh.\n\n"
        f"Need help? Contact {config.SUPPORT_CONTACT}."
    )


def _format_ocr_preview(preview: dict, intro: str = "I couldn't verify the QR code, but OCR read this from the receipt:") -> str:
    lines = [
        intro,
        "",
    ]
    if preview.get("vendor"):
        lines.append(f"Vendor: {preview['vendor']}")
    if preview.get("date"):
        lines.append(f"Date: {preview['date']}")
    if preview.get("receipt_number"):
        lines.append(f"Receipt #: {preview['receipt_number']}")
    if preview.get("total_amount") is not None:
        lines.append(f"Total: {preview['total_amount']:,.2f} UZS")
    if preview.get("vat_hint"):
        lines.append(f"QQS: {preview['vat_hint']}")
    lines.extend(
        [
            "",
            "This preview is not verified and was not saved.",
            "Please resend the image as a file for a verified import.",
        ]
    )
    return "\n".join(lines)


async def _safe_telegram_call(coro_factory, what: str = "Telegram call"):
    """Run a Telegram coroutine and never let a transient error propagate.

    Used so cosmetic UI calls (status messages, typing pings) can't abort
    the actual receipt save when Telegram is rate-limiting us. Tries
    once, sleeps + retries on RetryAfter (capped), and swallows
    NetworkError / TimedOut. Returns the call's return value on success
    or None on any failure. The caller is responsible for treating None
    as "no message handle" (e.g. skipping subsequent edits)."""
    from telegram.error import NetworkError, RetryAfter, TimedOut
    try:
        return await coro_factory()
    except RetryAfter as e:
        wait = min(getattr(e, "retry_after", 5), 15)
        logger.warning("RetryAfter on %s (waiting %ss): %s", what, wait, e)
        try:
            await asyncio.sleep(wait + 0.5)
            return await coro_factory()
        except Exception:
            logger.warning(
                "%s still failed after RetryAfter retry — receipt save "
                "still proceeded.", what, exc_info=True,
            )
            return None
    except (NetworkError, TimedOut) as e:
        logger.warning("Transient %s error: %s", what, e)
        return None
    except Exception:
        logger.warning("%s failed (non-fatal)", what, exc_info=True)
        return None


async def _safe_reply(msg, text: str, **kwargs):
    """msg.reply_text with retry-on-RetryAfter; returns the new Message or
    None. None means subsequent edits should be skipped — the receipt
    save still goes through."""
    return await _safe_telegram_call(
        lambda: msg.reply_text(text, **kwargs), "reply_text"
    )


async def _safe_edit(status_msg, text: str, **kwargs) -> None:
    """Edit a status message. No-op if status_msg is None (which happens
    when the initial reply failed under flood control). Never raises."""
    if status_msg is None:
        return
    await _safe_telegram_call(
        lambda: status_msg.edit_text(text, **kwargs), "status edit"
    )


async def _keep_typing(
    bot, chat_id: int, interval: float = 4.0, action: str = ChatAction.TYPING
) -> None:
    """Keep Telegram's "is typing..." indicator alive while a slow
    operation runs. Telegram drops the chat action after ~5 seconds, so
    we re-fire it on a short loop until cancelled. Used during photo
    processing because OCR + multi-soliq lookups can take 5-15 seconds
    on long receipts and the user starts wondering if the bot is hung.
    Exports pass ChatAction.UPLOAD_DOCUMENT ("sending a file...").
    """
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id, action)
            except Exception:
                # Don't let a transient send_chat_action failure kill the
                # processing loop. Worst case the typing indicator just
                # disappears for a few seconds.
                logger.debug("Typing keep-alive ping failed (non-fatal)", exc_info=True)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        # Normal shutdown when the parent finishes the request.
        pass


async def _stop_keepalive(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _decode_qr(
    loop: asyncio.AbstractEventLoop, image_bytes: bytes, on_crop=None
) -> tuple[str | None, str]:
    """Find the soliq.uz QR link in a photo. Returns (url or None, how).

    The search itself lives in receipt_image.find_qr_url: a quick pass
    that settles nine photos in ten in ~0.1 s, then (only if needed) a
    time-boxed parallel deep search. It used to take 20-30 s to give up
    on an unreadable photo; now it is capped at about 10 s.
    """
    started = time.monotonic()
    try:
        url, how = await loop.run_in_executor(
            None, partial(receipt_image.find_qr_url, image_bytes, on_crop=on_crop)
        )
    except Exception:
        logger.exception("QR decode failed")
        url, how = None, "error"
    if url:
        logger.info("QR found: %s", url)
    logger.info("QR search: %s in %.2fs", how, time.monotonic() - started)
    return url, how


def _crash_text(what: str) -> str:
    """What the user sees when something unexpected broke on our side.
    Before this existed an unexpected error left the "processing..."
    message sitting there forever with no answer at all."""
    return (
        f"Sorry — something went wrong on my side while handling {what}. "
        "Nothing was saved. Please send it again; if it keeps happening, "
        f"contact {config.SUPPORT_CONTACT}."
    )


@dataclass
class SaveOutcome:
    """Result of trying to turn a soliq.uz link into a saved receipt.

    `resolved` answers "is the question of THIS receipt settled?" — True
    when it was saved, is a duplicate, or has no VAT. False when soliq
    could not be asked (down, receipt not in their system yet, broken
    link): the user may want to retry with the same photo, so callers
    keep any pending photo around instead of throwing it away."""

    message: str
    saved: bool = False
    resolved: bool = True


def _format_saved_on(doc: dict) -> str:
    when = doc.get("created_at")
    try:
        return f" — saved on {when:%d %b %Y}"
    except Exception:
        return ""


def _soliq_failure_text(result: "soliq.FetchResult", *, pasted_link: bool) -> str:
    """Explain a failed soliq.uz lookup in terms of what the user should
    do next. The old single message ("could not fetch data ... page
    layout may have changed") was shown for everything, including the
    very common case of a receipt that simply isn't in the tax system
    yet."""
    resend = "paste the link again" if pasted_link else "send the photo again"
    if result.reason == "not_found":
        body = (
            "⏳ <b>soliq.uz doesn't have this receipt yet.</b>\n\n"
            "New receipts can take up to 48 hours to reach the tax "
            "system. Nothing was saved — please "
            f"{resend} later (tomorrow is a safe bet)."
        )
    elif result.reason == "unreachable":
        body = (
            "⚠️ <b>soliq.uz isn't responding right now</b>, so I couldn't "
            "verify this receipt.\n\n"
            "That is a problem on the tax website's side, not with your "
            f"receipt. Nothing was saved — please {resend} in a few minutes."
        )
    elif result.reason == "rejected":
        body = (
            "soliq.uz did not accept this link — its security code doesn't "
            "match, which usually means the link was cut off or misread.\n\n"
            + (
                "Please copy the complete link and paste it again."
                if pasted_link
                else "Please send a sharper photo of the QR code, or paste "
                "the soliq.uz link (camera → point at the QR → share the link here)."
            )
        )
    elif result.reason == "bad_link":
        body = (
            "That doesn't look like a soliq.uz receipt link.\n\n"
            "A receipt link starts with <code>https://ofd.soliq.uz/</code> "
            "and is what your phone's camera shows when you point it at "
            "the QR code on a receipt."
        )
    else:
        body = (
            "I reached soliq.uz but couldn't read the VAT amount from its "
            "answer — the website may have changed. Nothing was saved."
        )
    return f"{body}\n\nNeed help? Contact {escape(config.SUPPORT_CONTACT)}."


async def _duplicate_outcome(
    uid: int, qr_url: str, *, soliq_fid: str | None = None
) -> SaveOutcome | None:
    """Answer 'already saved' / 'already claimed' without storing
    anything. Called twice per receipt:

      * before the soliq.uz lookup, with the id read off the link — a
        re-sent receipt is recognised in milliseconds, no lookup needed;
      * after it, with the id soliq itself reported (`soliq_fid`) — so a
        link whose parameters were altered is still matched to the
        receipt it really is.

    db.save_receipt enforces the same rule again at insert time (with a
    unique index behind it). This is the fast path, not the guarantee."""
    link_fid = db.fiscal_id_from_url(qr_url)
    try:
        existing = await db.find_receipt_by_fiscal_id(link_fid, soliq_fid)
    except Exception:
        logger.exception("Duplicate pre-check failed; continuing to full save")
        return None
    if not existing:
        return None
    if existing.get("telegram_id") == uid:
        return SaveOutcome(
            f"This receipt (#{escape(str(existing.get('receipt_number') or '?'))}) "
            f"is already in your records{_format_saved_on(existing)}."
        )
    fid = soliq_fid or link_fid or "-"
    altered = bool(soliq_fid and link_fid and soliq_fid != link_fid)
    logger.warning(
        "Double-claim blocked: fiscal receipt %s already claimed by user %s; "
        "rejected for user %s%s", fid, existing.get("telegram_id"), uid,
        " (submitted link did not match soliq's record)" if altered else "",
    )
    await _notify_admins(
        "⚠️ <b>Double-claim blocked</b>\n\n"
        f"Fiscal receipt: <code>{escape(fid)}</code>\n"
        f"Vendor: {escape(_display_vendor(existing) or '-')}\n"
        f"VAT: {float(existing.get('vat_amount') or 0):,.2f} UZS\n"
        f"Already claimed by user <code>{existing.get('telegram_id')}</code>\n"
        f"Rejected attempt by user <code>{uid}</code>"
        + (
            "\n\n<b>Note:</b> the submitted link's details did not match "
            "soliq's own record of this receipt (the link had been altered)."
            if altered else ""
        )
    )
    return SaveOutcome(_double_claim_text())


_DOUBLE_CLAIM_TEXT = (
    "⛔ This receipt has already been claimed by another employee.\n\n"
    "Each fiscal receipt can only be refunded once, so it was NOT "
    "added to your records.\n\n"
    "If you believe this is a mistake, contact {support}."
)


def _double_claim_text() -> str:
    return _DOUBLE_CLAIM_TEXT.format(support=escape(config.SUPPORT_CONTACT))


async def _save_verified_receipt(
    *,
    uid: int,
    source_image_bytes: bytes | None,
    qr_url: str,
    loop: asyncio.AbstractEventLoop,
    qr_image_bytes: bytes | None = None,
    prefetched: "soliq.FetchResult | None" = None,
    pasted_link: bool = False,
    online_purchase: bool = False,
) -> SaveOutcome:
    """Verify a receipt on soliq.uz and store it. The one place where a
    soliq.uz link becomes a saved receipt — photos, pasted links and
    /online_purchase all end up here. The returned message is Telegram
    HTML.

    `source_image_bytes` is the user's photo. For an online purchase
    there is none (pass None with online_purchase=True) and a clean
    snapshot is rendered from the verified data instead, so the entry
    still has a visual record.

    `prefetched` lets a caller that already looked the link up (the OCR
    fallback validates its guesses against soliq) hand the answer over
    instead of paying for a second trip."""
    started = time.monotonic()

    duplicate = await _duplicate_outcome(uid, qr_url)
    if duplicate is not None:
        return duplicate

    # One browser trip returns the verified data AND the page screenshot.
    result = prefetched or await soliq.fetch_receipt(qr_url, want_screenshot=True)
    data = result.data
    if not data:
        return SaveOutcome(
            _soliq_failure_text(result, pasted_link=pasted_link), resolved=False
        )

    if not data.get("vat_amount"):
        vendor = data.get("vendor", "") or data.get("receipt_number", "this receipt")
        return SaveOutcome(
            f"This receipt ({escape(str(vendor))}) has 0 VAT — nothing to refund.\n"
            "It will not be added to your records."
        )

    # Second duplicate check, now with the identity soliq reported. Only
    # differs from the first when the link's parameters don't match
    # soliq's record of the receipt.
    soliq_fid = data.get("fiscal_id")
    if soliq_fid and soliq_fid != db.fiscal_id_from_url(qr_url):
        logger.warning(
            "Link parameters differ from soliq's record for user %s: %s", uid, qr_url
        )
        duplicate = await _duplicate_outcome(uid, qr_url, soliq_fid=soliq_fid)
        if duplicate is not None:
            return duplicate

    # soliq.uz names the seller on every receipt, so reading the vendor
    # off the photo with OCR is only worth it when soliq's name is
    # missing. (It used to run on EVERY photo — about a second of work,
    # and across 2,900 saved receipts its result was never once used.)
    vendor = data.get("vendor", "")
    printed_vendor = ""
    if source_image_bytes and (not vendor or vendor == "Unknown vendor"):
        try:
            printed_vendor = await loop.run_in_executor(
                None, receipt_ocr.extract_printed_vendor, source_image_bytes
            ) or ""
        except Exception:
            logger.exception("Printed vendor OCR failed")
    display_vendor = (
        printed_vendor if (printed_vendor and vendor in ("", "Unknown vendor")) else vendor
    )

    if online_purchase and not source_image_bytes:
        try:
            source_image_bytes = await loop.run_in_executor(
                None, online_snapshot.render_online_receipt_png, data, qr_url
            )
        except Exception:
            # Save the receipt without an image rather than dropping it.
            logger.exception("Online snapshot render failed")

    # Store the images side by side rather than one after another.
    async def _store(image: bytes | None, filename: str, **kwargs) -> str | None:
        if not image:
            return None
        try:
            return await db.save_image(uid, image, filename, **kwargs)
        except Exception:
            logger.exception("Failed to save %s", filename)
            return None

    file_id, qr_file_id, screenshot_file_id = await asyncio.gather(
        _store(
            source_image_bytes,
            f"online_{uid}.png" if online_purchase else f"receipt_{uid}.png",
        ),
        _store(qr_image_bytes, f"qr_{uid}.png"),
        _store(result.screenshot, f"soliq_{uid}.jpg", reencode=False),
    )
    stored_images = [file_id, qr_file_id, screenshot_file_id]
    if file_id is None and not online_purchase:
        # The user's photo is the one thing we must not lose silently.
        await db.delete_images(stored_images)
        raise RuntimeError("could not store the receipt photo")

    receipt_doc = {
        "telegram_id": uid,
        "image_file_id": file_id,
        "qr_image_file_id": qr_file_id,
        "soliq_screenshot_file_id": screenshot_file_id,
        "date": data.get("date", ""),
        "vendor": data.get("vendor", ""),
        "printed_vendor": printed_vendor or "",
        "display_vendor": display_vendor,
        "receipt_number": data.get("receipt_number", ""),
        "vat_amount": data.get("vat_amount", 0.0),
        "total_amount": data.get("total_amount", 0.0),
        "soliq_url": qr_url,
        "raw_qr": qr_url,
        "soliq_items": data.get("items") or [],
        "soliq_meta": data.get("meta") or {},
    }
    if online_purchase:
        receipt_doc["online_purchase"] = True
    if soliq_fid:
        receipt_doc["fiscal_id"] = soliq_fid
    try:
        inserted = await db.save_receipt(receipt_doc)
    except db.DuplicateClaimError as e:
        # Only reachable when two people submit the same receipt in the
        # same instant (the checks above catch every other case).
        await db.delete_images(stored_images)
        logger.warning("Double-claim blocked: %s", e)
        await _notify_admins(
            "⚠️ <b>Double-claim blocked</b>\n\n"
            f"Fiscal receipt: <code>{escape(e.fiscal_id)}</code>\n"
            f"Vendor: {escape(display_vendor or '-')}\n"
            f"VAT: {data.get('vat_amount', 0):,.2f} UZS\n"
            f"Already claimed by user <code>{e.existing_telegram_id}</code>\n"
            f"Rejected attempt by user <code>{e.new_telegram_id}</code>"
        )
        return SaveOutcome(_double_claim_text())
    except Exception:
        await db.delete_images(stored_images)
        raise
    if inserted is None:
        await db.delete_images(stored_images)
        return SaveOutcome(
            f"This receipt (#{escape(str(data.get('receipt_number')))}) "
            "is already in your records."
        )

    count, total_vat = await db.receipt_totals(uid)
    logger.info(
        "Receipt saved for user %s in %.2fs (%s; soliq lookup %s)",
        uid, time.monotonic() - started,
        "online purchase" if online_purchase else "photo",
        "reused" if prefetched else "1 trip",
    )
    headline = "✅ Online purchase saved!" if online_purchase else "✅ Saved!"
    return SaveOutcome(
        f"{headline} That's receipt #{count} for you.\n\n"
        f"Vendor: {escape(display_vendor or '-')}\n"
        f"Date: {escape(str(data.get('date') or '-'))}\n"
        f"Receipt #: {escape(str(data.get('receipt_number') or '-'))}\n"
        f"Total: {data.get('total_amount', 0):,.2f} UZS\n"
        f"VAT: {data.get('vat_amount', 0):,.2f} UZS\n\n"
        f"Verified by soliq.uz · your VAT total so far: <b>{total_vat:,.2f} UZS</b>",
        saved=True,
    )


# ---------- Access control ----------

def _is_approver(uid: int) -> bool:
    return uid in config.APPROVER_TELEGRAM_IDS


async def _require_approved(update: Update) -> bool:
    """Gate ordinary commands and message handlers. Returns True if the
    caller is approved. Otherwise replies with the right message and
    returns False so the caller can early-exit.

    DT approvers (APPROVER_TELEGRAM_IDS) bypass the gate entirely: the
    whitelist is more authoritative than the DB. On first interaction we
    lazily create their user row as approved so /list, /export_vat etc.
    have something to attach to."""
    uid = update.effective_user.id
    user = update.effective_user
    if _is_approver(uid):
        await db.ensure_approver_user(
            uid, user.full_name or user.username or str(uid)
        )
        await _show_notice_once(update)
        return True
    status = await db.get_user_status(uid)
    if status == "approved":
        await _show_notice_once(update)
        return True
    if update.message is None:
        return False
    if status == "pending":
        await update.message.reply_text(
            "Your access request is being reviewed by DT. "
            "You will get a message here when approved."
        )
    elif status == "denied":
        await update.message.reply_text(
            "Your access request was not approved. "
            f"Please contact {config.SUPPORT_CONTACT} directly if this is a mistake."
        )
    else:
        # Unknown user who skipped /start — point them at it.
        await update.message.reply_text(
            "This bot is restricted to approved users. "
            "Tap /start to request access from DT."
        )
    return False


# Users known (in this process) to have been shown the licence notice —
# spares a database write on every message. The database is the record
# that survives restarts; this is only a shortcut in front of it.
_notice_seen: set[int] = set()


async def _show_notice_once(update: Update) -> None:
    """Show the open-source licence / no-warranty notice the first time
    an approved user uses the bot. Once per user, ever: the database
    remembers (users.notice_shown_at). Never raises and never blocks
    the command the user actually sent."""
    uid = update.effective_user.id
    if uid in _notice_seen or update.message is None:
        return
    # Mark before the first await: five photos sent as an album arrive
    # as five simultaneous updates and must not produce five notices.
    _notice_seen.add(uid)
    try:
        if not await db.claim_notice(uid, notice.NOTICE_VERSION):
            return  # shown on an earlier day
        sent = await _safe_reply(
            update.message, notice.notice_html(),
            parse_mode="HTML", disable_web_page_preview=True,
        )
        if sent is None:
            # Telegram did not take it — try again on their next message.
            await db.unclaim_notice(uid)
            _notice_seen.discard(uid)
        else:
            logger.info("Licence notice shown to user %s.", uid)
    except Exception:
        logger.exception("Could not show the licence notice to user %s", uid)
        _notice_seen.discard(uid)


async def _send_approval_request(ctx: ContextTypes.DEFAULT_TYPE, requester) -> None:
    """Notify every DT approver that a new user is waiting. Each approver
    gets the same message + Approve/Deny inline buttons; whoever taps first
    wins, and the buttons no-op for everyone else (the user's status will
    no longer be 'pending')."""
    if not config.APPROVER_TELEGRAM_IDS:
        logger.warning(
            "New user %s requested access but APPROVER_TELEGRAM_IDS is empty — "
            "nobody will be notified.", requester.id,
        )
        return
    name = escape(requester.full_name or "(no name)")
    handle = f"@{escape(requester.username)}" if requester.username else "<i>(no username)</i>"
    text = (
        "<b>🔐 New access request</b>\n\n"
        f"Name: <b>{name}</b>\n"
        f"Telegram: {handle}\n"
        f"User ID: <code>{requester.id}</code>\n\n"
        "Approve to let them use the VAT bot."
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"access:approve:{requester.id}"),
        InlineKeyboardButton("❌ Deny", callback_data=f"access:deny:{requester.id}"),
    ]])
    for approver_id in config.APPROVER_TELEGRAM_IDS:
        try:
            await ctx.bot.send_message(approver_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.exception("Failed to notify approver %s", approver_id)


async def access_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the inline Approve/Deny buttons sent to DT approvers."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("access:"):
        return
    clicker_id = query.from_user.id
    if not _is_approver(clicker_id):
        await query.answer("You are not authorized to approve users.", show_alert=True)
        return

    try:
        _, action, target_str = query.data.split(":", 2)
        target_id = int(target_str)
    except ValueError:
        await query.answer("Bad request data.", show_alert=True)
        return

    current = await db.get_user_status(target_id)
    if current != "pending":
        await query.answer(
            f"Already handled — current status: {current or 'unknown'}.",
            show_alert=True,
        )
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    new_status = "approved" if action == "approve" else "denied"
    await db.set_user_status(target_id, new_status, clicker_id)

    # Update the approver-facing message so other approvers see it's handled.
    decided_by = escape(query.from_user.full_name or str(clicker_id))
    verb = "✅ Approved" if new_status == "approved" else "❌ Denied"
    try:
        original = query.message.text_html if query.message else ""
        await query.edit_message_text(
            f"{original}\n\n<b>{verb}</b> by {decided_by}",
            parse_mode="HTML",
        )
    except Exception:
        # If editing fails (e.g. message too old), at least drop the buttons.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    # Tell the user.
    if new_status == "approved":
        try:
            await ctx.bot.send_message(
                target_id,
                "<b>✅ You're approved!</b>\n\n"
                "You can now use the VAT bot. Tap /start to see what's available, "
                "or just send a receipt photo to get going.",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Couldn't notify approved user %s", target_id)
    else:
        try:
            await ctx.bot.send_message(
                target_id,
                "Your access request was not approved. "
                f"Please contact {config.SUPPORT_CONTACT} directly if this is a mistake.",
            )
        except Exception:
            logger.exception("Couldn't notify denied user %s", target_id)

    await query.answer("Done.")


# ---------- Commands ----------

_WELCOME_HTML = (
    "<b>🇺🇸 Tashkent Embassy VAT Refund — V.13.0</b>\n\n"
    "Hi <b>{first_name}</b>! 👋\n"
    "Just send me a 📸 photo of any receipt — I read the QR and save it.\n\n"

    "<b>📥 Adding receipts</b>\n"
    "• Send a <b>photo</b> (best as a <i>File</i> via the paperclip)\n"
    "• /manual — type by hand (last resort)\n"
    "• /online_purchase — online buys with a soliq.uz link\n\n"

    "<b>📋 Review &amp; file</b>\n"
    "• /list — see your saved receipts\n"
    "• /export_vat — download the official Excel form\n"
    "• /export_pdf — download the receipt images PDF\n\n"

    "<b>⚙️ Setup &amp; control</b>\n"
    "• /setname — your name on the forms (do this first!)\n"
    "• /cancel_pending — discard a stuck receipt\n"
    "• /cancel_manual — abort a manual entry\n"
    "• /cancel_online — abort an online-purchase entry\n"
    "• /reset — delete all your receipts\n"
    "• /help — full guide with tips\n"
    "• /about — license &amp; disclaimer\n\n"

    "Need help? Contact <b>{support}</b>.\n\n"
    + notice.WELCOME_FOOTER_HTML
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    first_name = escape(user.first_name or "there")

    # DT approvers always get the welcome — they don't go through the queue.
    if _is_approver(user.id):
        await db.ensure_approver_user(
            user.id, user.full_name or user.username or str(user.id)
        )
        await update.message.reply_text(
            _WELCOME_HTML.format(first_name=first_name, support=escape(config.SUPPORT_CONTACT)),
            parse_mode="HTML",
        )
        await _show_notice_once(update)
        return

    status = await db.register_pending_user(
        user.id,
        user.full_name or user.username or str(user.id),
        user.username,
    )

    if status == "approved":
        await update.message.reply_text(
            _WELCOME_HTML.format(first_name=first_name, support=escape(config.SUPPORT_CONTACT)),
            parse_mode="HTML",
        )
        await _show_notice_once(update)
        return

    if status == "denied":
        await update.message.reply_text(
            "Your previous access request was not approved. "
            f"Please contact {config.SUPPORT_CONTACT} directly if this is a mistake."
        )
        return

    if status == "pending":
        await update.message.reply_text(
            "Your access request is still being reviewed by DT. "
            "You will get a message here when approved."
        )
        return

    # status == "new_pending" — first /start, just inserted as pending.
    await update.message.reply_text(
        "Request submitted to DT. You will get a message here when approved."
    )
    await _send_approval_request(ctx, user)


_HELP_HTML = (
    "<b>📖 Tashkent Embassy VAT Refund Bot — Quick Guide</b>\n\n"

    "<b>What this bot does</b>\n"
    "It collects your VAT receipts and turns them into the official "
    "VAT refund forms when you're ready to file. You take a photo, "
    "I read the QR code on the receipt and pull the verified VAT "
    "amount straight from soliq.uz (the tax authority's database). "
    "No typing, no math — and the numbers can't be wrong because "
    "they come from the tax office itself.\n\n"

    "<b>📸 Add a receipt — three ways</b>\n\n"

    "<b>1. Take a photo (most common)</b>\n"
    "Just send me a photo of the receipt. For best results, send it "
    "as a <b>file</b> (paperclip → File) instead of a regular photo "
    "— Telegram doesn't compress files, so the QR stays sharp. "
    "Make sure the QR code is clearly visible and not blurry.\n\n"

    "<b>2. Online purchase (no physical receipt)</b>\n"
    "If you bought something online and the merchant emailed you a "
    "soliq.uz receipt link, run /online_purchase and paste the link. "
    "I'll fetch the data and save it with a clean snapshot — no photo "
    "needed.\n\n"

    "<b>3. Manual entry (QR unreadable)</b>\n"
    "If the receipt's QR code is damaged or won't scan, run /manual. "
    "I'll ask you for date, vendor, receipt number, and VAT amount in "
    "four short steps.\n\n"

    "<b>🔧 If a photo's QR fails</b>\n"
    "Two ways to recover:\n"
    "• Open your phone's camera, point at the QR, long-press the "
    "yellow URL banner → Share → pick this chat. Paste it. I'll "
    "complete the entry from your earlier photo.\n"
    "• Run /manual and type the receipt in.\n"
    "• Or /cancel_pending to discard it and start over.\n\n"

    "<b>📋 Review what's saved</b>\n"
    "/list — see every receipt you've added, with the running total.\n\n"

    "<b>📥 File your refund</b>\n"
    "When you're ready:\n"
    "• /export_vat — the official VAT_Refund.xlsx, already filled in. "
    "If your receipts span multiple years, you'll get one workbook "
    "per year. If a single year has more than 120 receipts, it'll "
    "be split into copy 1, copy 2, etc., with continuous numbering.\n"
    "• /export_pdf — a PDF with every receipt image and a summary "
    "table. Hand both files to the finance office.\n\n"

    "<b>⚙️ Other commands</b>\n"
    "/setname — set your name (used in exports)\n"
    "/cancel_manual — abort a manual entry\n"
    "/cancel_online — abort an online purchase entry\n"
    "/cancel_pending — discard a pending receipt\n"
    "/reset — delete all your receipts (asks for confirmation)\n"
    "/whoami — show your Telegram ID\n"
    "/about — license and disclaimer (free open-source software, MIT)\n\n"

    "Need help? Contact <b>{support}</b>."
)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        _HELP_HTML.format(support=escape(config.SUPPORT_CONTACT)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The open-source notice plus the full MIT licence text. Open to
    everyone — there is nothing private in a licence — and counts as
    having shown the notice, so it is not sent a second time."""
    uid = update.effective_user.id
    _notice_seen.add(uid)
    try:
        await db.claim_notice(uid, notice.NOTICE_VERSION)
    except Exception:
        logger.debug("claim_notice failed in /about", exc_info=True)
    await update.message.reply_text(
        notice.about_html(), parse_mode="HTML", disable_web_page_preview=True,
    )


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Tell the caller their own Telegram user ID. Useful for collecting
    APPROVER_TELEGRAM_IDS — each DT staffer runs /whoami once and reports
    their number to the bot owner. Available to everyone, no gate."""
    user = update.effective_user
    handle = f"@{user.username}" if user.username else "(no username)"
    await update.message.reply_text(
        f"Telegram user ID: {user.id}\n"
        f"Username: {handle}\n"
        f"Name: {user.full_name}"
    )


async def cmd_setname(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_approved(update):
        return
    if ctx.args:
        name = " ".join(ctx.args).strip()
        await db.set_user_name(update.effective_user.id, name)
        ctx.user_data.pop("awaiting_name", None)
        await update.message.reply_text(f"Name set to: {name}")
        return
    # No name typed after the command — ask for it, and the next plain-text
    # message the user sends becomes their name (see handle_text).
    ctx.user_data["awaiting_name"] = True
    await update.message.reply_text(
        "Please type your full name (as it should appear in exports).\n\n"
        "Example: John Smith"
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_approved(update):
        return
    receipts = await db.list_receipts(update.effective_user.id)
    if not receipts:
        await update.message.reply_text("No receipts yet. Send me a photo to add one!")
        return

    lines = [f"📋 You have {len(receipts)} receipt(s):\n"]
    total_vat = 0.0
    for i, r in enumerate(receipts, 1):
        vat = float(r.get("vat_amount") or 0)
        total_vat += vat
        marker = " [manual]" if r.get("manual") else ""
        lines.append(
            f"{i}. {r.get('date', '?')} - {_display_vendor(r)[:25] or '?'} - "
            f"VAT: {vat:,.2f} UZS{marker}"
        )
    lines.append(f"\nTotal VAT: {total_vat:,.2f} UZS")

    # Telegram rejects messages over 4096 chars (BadRequest: Message is too long).
    # Pack lines into chunks safely under that cap.
    MAX_CHARS = 3500
    buf: list[str] = []
    buf_len = 0
    for line in lines:
        if buf and buf_len + len(line) + 1 > MAX_CHARS:
            await update.message.reply_text("\n".join(buf))
            buf = []
            buf_len = 0
        buf.append(line)
        buf_len += len(line) + 1
    if buf:
        await update.message.reply_text("\n".join(buf))


def _receipt_year(r: dict) -> str:
    """Extract the calendar year from a receipt's date for grouping. Receipts
    from soliq.uz and the /manual flow both store dates as YYYY-MM-DD; anything
    else (or missing) goes into an 'undated' bucket so nothing is lost."""
    date = (r.get("date") or "").strip()
    if len(date) >= 4 and date[:4].isdigit():
        return date[:4]
    return "undated"


def _group_by_year(receipts: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in receipts:
        groups.setdefault(_receipt_year(r), []).append(r)
    return groups


def _sorted_year_keys(groups: dict[str, list[dict]]) -> list[str]:
    """Numeric years ascending, then 'undated' last."""
    years = [k for k in groups if k != "undated"]
    years.sort()
    if "undated" in groups:
        years.append("undated")
    return years


_NO_NAME_TIP = (
    "ℹ️ Your name isn't set, so the Employee Name field is blank. "
    "Run /setname and export again to have it filled in."
)


async def cmd_export_vat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_approved(update):
        return
    uid = update.effective_user.id
    receipts = await db.list_receipts(uid)
    if not receipts:
        await update.message.reply_text("No receipts to export yet.")
        return

    groups = _group_by_year(receipts)
    years = _sorted_year_keys(groups)
    user = await db.get_user(uid)
    name = (user or {}).get("name", "")

    if len(years) > 1:
        await update.message.reply_text(
            f"You have receipts across {len(years)} calendar years: {', '.join(years)}.\n"
            "The tax office wants one VAT file per year, so I'll send one Excel for each."
        )

    cap = exporter.RECEIPTS_PER_XLSX
    loop = asyncio.get_running_loop()
    keepalive = asyncio.create_task(
        _keep_typing(ctx.bot, update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)
    )
    try:
        for year in years:
            year_receipts = groups[year]
            n_year = len(year_receipts)
            year_total = sum(float(r.get("vat_amount") or 0) for r in year_receipts)

            # The official template fits cap (120) receipts. If a year goes
            # past that, split into copy_1, copy_2, ... — each copy keeps the
            # row numbering continuous so stapled copies read 1..N end-to-end.
            copies = [
                year_receipts[i : i + cap] for i in range(0, n_year, cap)
            ]
            total_copies = len(copies)

            if total_copies > 1:
                await update.message.reply_text(
                    f"You have {n_year} receipts for {year} — that's more than "
                    f"the {cap}-row template fits, so I'll split it into "
                    f"{total_copies} workbooks (copy 1 of {total_copies}, "
                    f"copy 2 of {total_copies}, ...). Row numbers stay "
                    "continuous across copies."
                )

            for copy_idx, chunk in enumerate(copies, 1):
                start_seq = (copy_idx - 1) * cap + 1
                end_seq = start_seq + len(chunk) - 1
                chunk_total = sum(float(r.get("vat_amount") or 0) for r in chunk)

                suffix = f"_copy_{copy_idx}_of_{total_copies}" if total_copies > 1 else ""
                out_path = (
                    config.TMP_DIR
                    / f"VAT_Refund_{year}{suffix}_{uid}_{uuid.uuid4().hex[:6]}.xlsx"
                )
                try:
                    # Off the event loop: filling the workbook is CPU work
                    # and must not hold up other users' messages.
                    await loop.run_in_executor(
                        None,
                        partial(
                            exporter.build_xlsx_from_receipts,
                            chunk, name, out_path, start_seq=start_seq,
                        ),
                    )

                    if total_copies > 1:
                        caption = (
                            f"VAT {year} — copy {copy_idx} of {total_copies}\n"
                            f"Receipts #{start_seq}–#{end_seq} ({len(chunk)} in this file)\n"
                            f"This file: {chunk_total:,.2f} UZS\n"
                            f"Year total ({n_year} receipts): {year_total:,.2f} UZS"
                        )
                        filename = f"VAT_Refund_{year}_copy_{copy_idx}_of_{total_copies}.xlsx"
                    else:
                        caption = (
                            f"VAT for {year} — {len(chunk)} receipt(s) — "
                            f"Total: {chunk_total:,.2f} UZS"
                        )
                        filename = f"VAT_Refund_{year}.xlsx"

                    with open(out_path, "rb") as f:
                        await update.message.reply_document(
                            document=f, filename=filename, caption=caption,
                        )
                finally:
                    out_path.unlink(missing_ok=True)
    except Exception:
        logger.exception("Excel export failed for user %s", uid)
        await _safe_reply(
            update.message,
            "Sorry — I couldn't finish the Excel export. Your receipts are "
            "safe; please try /export_vat again in a moment.\n\n"
            f"Need help? Contact {config.SUPPORT_CONTACT}.",
        )
        return
    finally:
        await _stop_keepalive(keepalive)

    if not name:
        await _safe_reply(update.message, _NO_NAME_TIP)


async def cmd_export_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_approved(update):
        return
    uid = update.effective_user.id
    receipts = await db.list_receipts(uid)
    if not receipts:
        await update.message.reply_text("No receipts to export yet.")
        return

    groups = _group_by_year(receipts)
    years = _sorted_year_keys(groups)
    user = await db.get_user(uid)
    name = (user or {}).get("name", "")

    if len(years) > 1:
        await update.message.reply_text(
            f"You have receipts across {len(years)} calendar years: {', '.join(years)}.\n"
            "Sending one receipt-package PDF per year."
        )

    # Tell the user something is happening: a large package takes a
    # while to lay out, and silence reads as "the bot is stuck".
    status = await _safe_reply(
        update.message,
        f"Building your PDF package — {len(receipts)} receipt(s), one page each. "
        "This can take a little while…",
    )
    keepalive = asyncio.create_task(
        _keep_typing(ctx.bot, update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)
    )
    started = time.monotonic()
    try:
        for year in years:
            year_receipts = groups[year]
            prefix = f"Receipts_{year}_{uid}_{uuid.uuid4().hex[:6]}"
            paths = await exporter.build_pdfs_from_receipts(
                year_receipts, name, config.TMP_DIR, name_prefix=prefix
            )
            total_parts = len(paths)
            total_vat = sum(float(r.get("vat_amount") or 0) for r in year_receipts)

            try:
                for idx, path in enumerate(paths, 1):
                    if total_parts > 1:
                        filename = f"Receipts_{year}_part{idx}_of_{total_parts}.pdf"
                        caption = (
                            f"Receipts for {year} — part {idx} of {total_parts} "
                            f"({len(year_receipts)} receipt(s), total {total_vat:,.2f} UZS)"
                        )
                    else:
                        filename = f"Receipts_{year}.pdf"
                        caption = (
                            f"Receipts for {year} — {len(year_receipts)} receipt(s) — "
                            f"Total: {total_vat:,.2f} UZS"
                        )
                    with open(path, "rb") as f:
                        await update.message.reply_document(
                            document=f, filename=filename, caption=caption,
                        )
            finally:
                for path in paths:
                    path.unlink(missing_ok=True)
    except Exception:
        logger.exception("PDF export failed for user %s", uid)
        await _safe_edit(
            status,
            "Sorry — I couldn't finish the PDF export. Your receipts are "
            "safe; please try /export_pdf again in a moment.\n\n"
            f"Need help? Contact {config.SUPPORT_CONTACT}.",
        )
        return
    finally:
        await _stop_keepalive(keepalive)

    logger.info(
        "PDF export for user %s: %s receipt(s) in %.1fs",
        uid, len(receipts), time.monotonic() - started,
    )
    if status is not None:
        await _safe_telegram_call(status.delete, "export status cleanup")
    if not name:
        await _safe_reply(update.message, _NO_NAME_TIP)


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask before wiping. /reset sits in the command menu right next to
    /list and the exports, and it used to delete everything the moment
    it was tapped — no question, no undo."""
    if not await _require_approved(update):
        return
    uid = update.effective_user.id
    count, total_vat = await db.receipt_totals(uid)
    pending = await db.get_pending_receipt(uid)
    if not count and not pending:
        await update.message.reply_text("You have no receipts to delete.")
        return
    what = (
        f"all <b>{count}</b> of your receipts ({total_vat:,.2f} UZS of VAT)"
        if count
        else "the photo that is waiting for its QR link"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Yes, delete everything", callback_data=f"reset:yes:{uid}"),
        InlineKeyboardButton("Keep my receipts", callback_data=f"reset:no:{uid}"),
    ]])
    await update.message.reply_text(
        f"⚠️ This will permanently delete {what}.\n\n"
        "It cannot be undone. If you still need them for a refund claim, "
        "run /export_vat and /export_pdf first.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def reset_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the Yes / Keep buttons under the /reset warning."""
    query = update.callback_query
    if not query or not query.data:
        return
    try:
        _, action, owner = query.data.split(":", 2)
        owner_id = int(owner)
    except ValueError:
        await query.answer("Bad request data.", show_alert=True)
        return
    uid = query.from_user.id
    if uid != owner_id:
        await query.answer("These buttons belong to someone else.", show_alert=True)
        return
    if not await _require_approved(update):
        await query.answer("You no longer have access.", show_alert=True)
        return

    if action != "yes":
        await query.answer("Nothing deleted.")
        await _safe_telegram_call(
            lambda: query.edit_message_text("Nothing was deleted — your receipts are safe."),
            "reset cancel edit",
        )
        return

    await query.answer("Deleting…")
    await db.delete_pending_receipt(uid)
    n = await db.delete_all_receipts(uid)
    logger.info("User %s reset their records: %s receipt(s) deleted.", uid, n)
    await _safe_telegram_call(
        lambda: query.edit_message_text(f"Deleted {n} receipt(s). You can start fresh."),
        "reset done edit",
    )


async def cmd_cancel_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_approved(update):
        return
    deleted = await db.delete_pending_receipt(update.effective_user.id)
    if deleted:
        await update.message.reply_text("Pending receipt discarded. You can send a new receipt now.")
        return
    await update.message.reply_text("There is no pending QR retry right now.")


# ---------- Hidden admin command ----------

def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


async def _build_heartcheck_report() -> str:
    import shutil as _shutil
    from datetime import datetime as _dt, time as _dt_time
    from . import maintenance

    db_obj = db.get_db()

    # DB ping — most important signal.
    try:
        await db.ping()
        db_status = "✅ reachable"
    except Exception as e:
        db_status = f"❌ {type(e).__name__}: {e}"

    # Counts.
    try:
        n_users = await db_obj.users.count_documents({})
        n_receipts = await db_obj.receipts.count_documents({})
        n_pending = await db_obj.pending_receipts.count_documents({})
        n_manual = await db_obj.receipts.count_documents({"manual": True})
        # created_at is stored in UTC; convert local midnight to UTC so
        # "today" means the Tashkent day, not a window shifted by 5 hours.
        local_midnight = _dt.combine(_dt.now().date(), _dt_time.min)
        today_start = local_midnight - (_dt.now() - _dt.utcnow())
        n_today = await db_obj.receipts.count_documents({"created_at": {"$gte": today_start}})
        n_images = await db_obj.fs.files.count_documents({})
        gridfs_bytes = 0
        async for f in db_obj.fs.files.find({}, {"length": 1}):
            gridfs_bytes += f.get("length", 0) or 0
    except Exception:
        logger.exception("Heartcheck: DB stats failed")
        n_users = n_receipts = n_pending = n_manual = n_today = n_images = -1
        gridfs_bytes = 0

    # Last backup info.
    latest = maintenance._latest_backup()
    if latest:
        try:
            stamp = _dt.strptime(latest.name.replace("vat_bot_", ""), "%Y%m%d_%H%M%S")
            age = _dt.now() - stamp
            size_mb = sum(p.stat().st_size for p in latest.rglob("*") if p.is_file()) / 1024 / 1024
            backup_str = f"{latest.name} ({_format_uptime(age.total_seconds())} ago, {size_mb:.0f} MB)"
        except Exception:
            backup_str = latest.name
    else:
        backup_str = "none yet"

    # Disk.
    total, used, free = _shutil.disk_usage("/")
    disk_pct = int(used * 100 / total)
    free_gb = free / 1024**3

    # Today's error count from the rotating bot.log.
    log_path = config.BASE_DIR / "logs" / "bot.log"
    err_count = 0
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if " ERROR " in line:
                    err_count += 1
    except FileNotFoundError:
        err_count = 0
    except Exception:
        err_count = -1

    uptime_str = _format_uptime(time.time() - _PROCESS_START_TS)
    pid = os.getpid()
    proxy_note = " (proxy)" if config.SOLIQ_PROXY else ""

    return (
        "<b>🔧 Heartcheck — V.13.0</b>\n\n"
        f"Status: ✅ alive\n"
        f"Uptime: {uptime_str}\n"
        f"PID: {pid}\n"
        f"DB: {db_status}\n"
        f"Soliq route: direct{proxy_note}\n\n"
        "<b>Activity</b>\n"
        f"• Users: {n_users}\n"
        f"• Receipts (total): {n_receipts}  •  manual: {n_manual}\n"
        f"• Receipts today: {n_today}\n"
        f"• Pending QR retries: {n_pending}\n"
        f"• GridFS images: {n_images}  ({gridfs_bytes / 1024 / 1024:.1f} MB)\n\n"
        "<b>Storage</b>\n"
        f"• Last backup: {backup_str}\n"
        f"• Disk: {disk_pct}% full, {free_gb:.1f} GB free\n\n"
        "<b>Today's log</b>\n"
        f"• ERROR lines: {err_count}\n"
    )


async def cmd_heartcheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden admin-only health check. Silently no-ops for non-admins so the
    command is invisible to ordinary users (it's not in set_my_commands)."""
    uid = update.effective_user.id
    if uid not in config.ADMIN_TELEGRAM_IDS:
        # Stay silent — pretend the bot doesn't know this command.
        return
    try:
        report = await _build_heartcheck_report()
    except Exception as e:
        logger.exception("Heartcheck failed")
        report = f"<b>🔧 Heartcheck</b>\n\n❌ Failed to build report: {type(e).__name__}: {e}"
    await update.message.reply_text(report, parse_mode="HTML")


async def _fetch_usd_rate() -> tuple[float, str] | None:
    """Fetch the official UZS→USD rate from the Central Bank of Uzbekistan.
    Returns (rate_uzs_per_usd, date_str) or None on failure. CBU updates
    once per business day; we don't cache because /report is called rarely
    and a fresh number is more trustworthy than a stale one.

    Endpoint: https://cbu.uz/oz/arkhiv-kursov-valyut/json/USD/
    Returns a single-element JSON array with the latest USD rate.
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get("https://cbu.uz/oz/arkhiv-kursov-valyut/json/USD/")
            r.raise_for_status()
            data = r.json()
        if not data:
            return None
        row = data[0]
        rate = float(row.get("Rate") or 0)
        if rate <= 0:
            return None
        return rate, str(row.get("Date") or "")
    except Exception:
        logger.warning("CBU USD-rate fetch failed", exc_info=True)
        return None


async def _build_savings_report() -> str:
    """Aggregate VAT refund totals across all users and return a single
    short Telegram HTML message with the headline numbers in UZS and
    USD. No per-user breakdown — privacy by design.

    Includes a Lifetime block that sums the active receipts collection
    with the deleted_receipts_log audit trail (deletions tracked since
    2026-06-15). Pre-2026-06-15 /reset events were never logged, so the
    Lifetime number is a floor, not an exact total."""
    from datetime import datetime as _dt
    db_obj = db.get_db()
    today = _dt.utcnow().strftime("%Y-%m-%d")

    # Active receipts: group by user for distinct-count and date range.
    active_pipeline = [
        {"$group": {
            "_id": "$telegram_id",
            "receipts": {"$sum": 1},
            "vat_sum": {"$sum": {"$ifNull": ["$vat_amount", 0]}},
            "total_sum": {"$sum": {"$ifNull": ["$total_amount", 0]}},
            "first_date": {"$min": "$date"},
            "last_date": {"$max": "$date"},
        }},
    ]
    rows = [doc async for doc in db_obj.receipts.aggregate(active_pipeline)]

    active_vat = sum(float(r["vat_sum"] or 0) for r in rows)
    active_total = sum(float(r["total_sum"] or 0) for r in rows)
    active_count = sum(int(r["receipts"] or 0) for r in rows)
    user_count = len(rows)
    first_date = min((r["first_date"] for r in rows if r.get("first_date")), default="—")
    last_date = max((r["last_date"] for r in rows if r.get("last_date")), default="—")

    # Deleted receipts (audit log written by db.delete_all_receipts).
    del_pipeline = [
        {"$group": {
            "_id": None,
            "count": {"$sum": 1},
            "vat_sum": {"$sum": {"$ifNull": ["$vat_amount", 0]}},
            "total_sum": {"$sum": {"$ifNull": ["$total_amount", 0]}},
        }},
    ]
    del_doc = await db_obj.deleted_receipts_log.aggregate(del_pipeline).to_list(1)
    if del_doc:
        del_count = int(del_doc[0]["count"] or 0)
        del_vat = float(del_doc[0]["vat_sum"] or 0)
        del_total = float(del_doc[0]["total_sum"] or 0)
    else:
        del_count = 0
        del_vat = 0.0
        del_total = 0.0

    lifetime_vat = active_vat + del_vat
    lifetime_total = active_total + del_total
    lifetime_count = active_count + del_count

    rate_info = await _fetch_usd_rate()
    if rate_info:
        rate, rate_date = rate_info
        def usd(x: float) -> str:
            return f"${x / rate:,.2f}"
        rate_note = f"<i>USD rate: {rate:,.2f} UZS/USD (CBU, {rate_date})</i>"
    else:
        def usd(_: float) -> str:
            return "—"
        rate_note = "<i>USD conversion unavailable — CBU rate fetch failed.</i>"

    active_block = (
        f"<b>Active</b>\n"
        f"VAT refundable: {active_vat:,.2f} UZS  ≈ <b>{usd(active_vat)}</b>\n"
        f"Spend:          {active_total:,.2f} UZS  ≈ {usd(active_total)}\n"
        f"{active_count} receipts across {user_count} users"
    )
    if del_count:
        deleted_block = (
            f"<b>Deleted via /reset</b>\n"
            f"VAT lost:       {del_vat:,.2f} UZS  ≈ {usd(del_vat)}\n"
            f"Spend lost:     {del_total:,.2f} UZS  ≈ {usd(del_total)}\n"
            f"{del_count} receipts"
        )
        lifetime_block = (
            f"<b>Lifetime processed (active + deleted)</b>\n"
            f"VAT:            {lifetime_vat:,.2f} UZS  ≈ <b>{usd(lifetime_vat)}</b>\n"
            f"Spend:          {lifetime_total:,.2f} UZS  ≈ {usd(lifetime_total)}\n"
            f"{lifetime_count} receipts"
        )
    else:
        deleted_block = (
            f"<b>Deleted via /reset</b>\n"
            f"<i>None logged yet. Audit logging started 2026-06-15.</i>"
        )
        lifetime_block = ""

    sections = [
        f"<b>💰 VAT Savings Report — {today}</b>",
        active_block,
        deleted_block,
    ]
    if lifetime_block:
        sections.append(lifetime_block)
    sections.append(f"Receipt dates: {first_date} → {last_date}")
    sections.append(rate_note)
    return "\n\n".join(sections)


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden admin-only: aggregate VAT savings across the full receipt
    history, no per-user names. Silent for non-admins."""
    uid = update.effective_user.id
    if uid not in config.ADMIN_TELEGRAM_IDS:
        return
    try:
        msg = await _build_savings_report()
    except Exception as e:
        logger.exception("Savings report failed")
        msg = (
            f"<b>💰 VAT Savings Report</b>\n\n❌ Failed to build report: "
            f"{type(e).__name__}: {e}"
        )
    await update.message.reply_text(msg, parse_mode="HTML")


# ---------- DT-approver commands ----------

async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List users waiting for DT approval. Silent for non-approvers."""
    if not _is_approver(update.effective_user.id):
        return
    pending = await db.list_pending_users()
    if not pending:
        await update.message.reply_text("No pending access requests.")
        return
    lines = [f"<b>Pending access requests ({len(pending)})</b>\n"]
    for u in pending:
        name = escape(u.get("name") or "(no name)")
        handle = f"@{escape(u['username'])}" if u.get("username") else "(no username)"
        when = u.get("requested_at")
        lines.append(
            f"• <b>{name}</b> {handle}\n"
            f"  ID: <code>{u['telegram_id']}</code> · requested {when:%Y-%m-%d %H:%M} UTC"
        )
    lines.append("\n<i>To approve:</i> /approve &lt;id&gt;\n<i>To deny:</i> /deny &lt;id&gt;")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _decide_via_command(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, status: str
) -> None:
    if not _is_approver(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text(
            f"Usage: /{'approve' if status == 'approved' else 'deny'} <telegram_id>"
        )
        return
    try:
        target_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a Telegram ID (must be a number).")
        return
    current = await db.get_user_status(target_id)
    if current is None:
        await update.message.reply_text(f"No user found with ID {target_id}.")
        return
    if current != "pending":
        await update.message.reply_text(
            f"User {target_id} is currently '{current}', not pending — nothing to do."
        )
        return
    await db.set_user_status(target_id, status, update.effective_user.id)
    if status == "approved":
        try:
            await ctx.bot.send_message(
                target_id,
                "<b>✅ You're approved!</b>\n\n"
                "You can now use the VAT bot. Tap /start to see what's available, "
                "or just send a receipt photo to get going.",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Couldn't notify approved user %s", target_id)
        await update.message.reply_text(f"✅ Approved user {target_id}.")
    else:
        try:
            await ctx.bot.send_message(
                target_id,
                "Your access request was not approved. "
                f"Please contact {config.SUPPORT_CONTACT} directly if this is a mistake.",
            )
        except Exception:
            logger.exception("Couldn't notify denied user %s", target_id)
        await update.message.reply_text(f"❌ Denied user {target_id}.")


async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _decide_via_command(update, ctx, "approved")


async def cmd_deny(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _decide_via_command(update, ctx, "denied")


# ---------- Manual entry (when QR is unreadable) ----------

_MANUAL_DATE_FORMATS = ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"]


def _parse_manual_date(s: str) -> str | None:
    from datetime import datetime as _dt
    s = s.strip()
    for fmt in _MANUAL_DATE_FORMATS:
        try:
            return _dt.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_manual_vat(s: str) -> float | None:
    """Parse a VAT number written in any of the formats users actually type:
    36173.14 / 36,173.14 / 36 173,14 / 36173. Reject zero or negative."""
    cleaned = s.strip().replace(" ", "").replace("\u00A0", "")
    if "," in cleaned and "." in cleaned:
        # Whichever separator is rightmost is the decimal one.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        last = cleaned.rfind(",")
        if len(cleaned) - last - 1 in (1, 2):
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    try:
        v = float(cleaned)
    except ValueError:
        return None
    return v if v > 0 else None


async def cmd_manual(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a 4-step manual-entry conversation for receipts whose QR is unreadable."""
    if not await _require_approved(update):
        return
    uid = update.effective_user.id
    await db.upsert_user(uid, update.effective_user.full_name or "")
    ctx.user_data.pop("awaiting_name", None)

    # If the user got here from a QR failure, there's a pending receipt
    # already in the DB with the full photo. Attach that image to whatever
    # they type in, so the manual receipt still appears in the PDF export.
    pending = await db.get_pending_receipt(uid)
    pending_image_id = pending.get("image_file_id") if pending else None

    ctx.user_data["manual_step"] = "date"
    ctx.user_data["manual_data"] = {
        "pending_image_id": pending_image_id,
    }

    intro = (
        "Manual entry — Step 1 of 4\n\n"
        "I'll attach this to the photo you just sent.\n\n"
        if pending_image_id
        else "Manual entry — Step 1 of 4\n\n"
    )
    await update.message.reply_text(
        f"{intro}"
        "Date of the receipt? Format: YYYY-MM-DD (e.g. 2026-04-27).\n\n"
        "Type /cancel_manual to abort."
    )


async def cmd_cancel_manual(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_approved(update):
        return
    if ctx.user_data.pop("manual_step", None) is not None:
        ctx.user_data.pop("manual_data", None)
        await update.message.reply_text("Manual entry cancelled.")
        return
    await update.message.reply_text("There is no manual entry in progress.")


# ---------- Online purchase flow ----------

def _looks_like_soliq_url(text: str) -> bool:
    """True when the text contains a genuine soliq.uz link (host check,
    not a substring match — see soliq_url)."""
    return soliq.find_soliq_url(text) is not None


async def cmd_online_purchase(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Add an online-purchase receipt from a soliq.uz URL — no photo needed.
    A synthetic receipt-style PNG is generated from the verified data so the
    PDF export still has a visual record."""
    if not await _require_approved(update):
        return
    uid = update.effective_user.id
    await db.upsert_user(uid, update.effective_user.full_name or "")

    # Allow inline URL: /online_purchase https://ofd.soliq.uz/check?...
    if ctx.args:
        url = soliq.find_soliq_url(" ".join(ctx.args))
        if url:
            await _save_online_purchase(update, ctx, url)
            return

    # Otherwise prompt and capture the next text message.
    ctx.user_data["awaiting_online_url"] = True
    await update.message.reply_text(
        "Online purchase mode.\n\n"
        "Paste the soliq.uz receipt URL (the one the merchant emailed you, "
        "or the link encoded in the QR on the receipt page). I'll fetch "
        "the verified VAT data and save the entry — no photo needed.\n\n"
        "Use /cancel_online to abort."
    )


async def cmd_cancel_online(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_approved(update):
        return
    if ctx.user_data.pop("awaiting_online_url", None):
        await update.message.reply_text("Online purchase entry cancelled.")
        return
    await update.message.reply_text("There is no online purchase in progress.")


async def _save_online_purchase(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, qr_url: str
) -> None:
    """Save a receipt from a pasted soliq.uz link (no photo). Verified
    data, the snapshot image and the soliq.uz page screenshot all come
    from a single lookup — this path used to open the page twice."""
    uid = update.effective_user.id
    status = await _safe_reply(update.message, "Fetching verified data from soliq.uz...")
    try:
        outcome = await _save_verified_receipt(
            uid=uid,
            source_image_bytes=None,
            qr_url=soliq.find_soliq_url(qr_url) or qr_url,
            loop=asyncio.get_running_loop(),
            pasted_link=True,
            online_purchase=True,
        )
    except Exception:
        logger.exception("Online purchase save crashed for user %s", uid)
        await _safe_edit(status, _crash_text("this link"))
        return
    await _safe_edit(status, outcome.message, parse_mode="HTML")


async def _handle_manual_step(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    step = ctx.user_data.get("manual_step")
    data = ctx.user_data.setdefault("manual_data", {})

    if step == "date":
        normalized = _parse_manual_date(text)
        if not normalized:
            await update.message.reply_text(
                "I couldn't read that date. Please use YYYY-MM-DD (e.g. 2026-04-27)."
            )
            return
        data["date"] = normalized
        ctx.user_data["manual_step"] = "vendor"
        await update.message.reply_text(
            "Step 2 of 4\n\nVendor name? (the shop or business on the receipt)."
        )
        return

    if step == "vendor":
        vendor = text.strip()[:200]
        if not vendor:
            await update.message.reply_text("Vendor name can't be empty. Try again.")
            return
        data["vendor"] = vendor
        ctx.user_data["manual_step"] = "receipt_number"
        await update.message.reply_text(
            "Step 3 of 4\n\nReceipt number? (printed on the receipt)."
        )
        return

    if step == "receipt_number":
        rcpt = text.strip()[:80]
        if not rcpt:
            await update.message.reply_text("Receipt number can't be empty. Try again.")
            return
        data["receipt_number"] = rcpt
        ctx.user_data["manual_step"] = "vat_amount"
        await update.message.reply_text(
            "Step 4 of 4\n\nVAT amount in UZS? (e.g. 36173.14)."
        )
        return

    if step == "vat_amount":
        vat = _parse_manual_vat(text)
        if vat is None:
            await update.message.reply_text(
                "I couldn't read that as a number. Try again, e.g. 36173.14"
            )
            return
        data["vat_amount"] = vat
        uid = update.effective_user.id
        # If /manual was started after a failed QR scan, re-use that pending
        # photo so the manual receipt still has an image in the PDF export.
        # Re-check the DB at save time (instead of trusting the snapshot we
        # took at /manual): the user could have run /cancel_pending mid-flow.
        pending_now = await db.get_pending_receipt(uid)
        attached_image_id = pending_now.get("image_file_id") if pending_now else None

        receipt_doc = {
            "telegram_id": uid,
            "image_file_id": attached_image_id,
            "qr_image_file_id": None,
            "date": data["date"],
            "vendor": data["vendor"],
            "printed_vendor": "",
            "display_vendor": data["vendor"],
            "receipt_number": data["receipt_number"],
            "vat_amount": data["vat_amount"],
            "total_amount": 0.0,
            "soliq_url": "",
            "raw_qr": "",
            "manual": True,
        }
        inserted = await db.save_receipt(receipt_doc)
        ctx.user_data.pop("manual_step", None)
        ctx.user_data.pop("manual_data", None)
        if inserted is None:
            await update.message.reply_text(
                f"A receipt with number {data['receipt_number']} is already in your records — nothing saved."
            )
            return

        # The pending row pointed at this image; we now own the image as
        # part of the saved receipt. Detach the pending row WITHOUT deleting
        # the GridFS file, otherwise the manual entry would lose its photo.
        if pending_now:
            await db.detach_pending_receipt(uid)

        count = await db.count_receipts(uid)
        attach_note = (
            "\nThe photo you sent earlier is attached to this entry."
            if attached_image_id else ""
        )
        await update.message.reply_text(
            f"✅ Manual receipt saved! That's receipt #{count} for you.\n\n"
            f"Date: {data['date']}\n"
            f"Vendor: {data['vendor']}\n"
            f"Receipt #: {data['receipt_number']}\n"
            f"VAT: {data['vat_amount']:,.2f} UZS"
            f"{attach_note}\n\n"
            "<i>Manual entries are bolded in the Excel form so finance "
            "can double-check them.</i>",
            parse_mode="HTML",
        )


# ---------- Photo handler ----------

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Process a receipt photo."""
    if not await _require_approved(update):
        return
    msg = update.message
    if msg is None:
        return  # edited/channel update — no new photo to process
    uid = update.effective_user.id
    await db.upsert_user(uid, update.effective_user.full_name or "")

    photo = msg.photo[-1] if msg.photo else None
    is_image_document = bool(
        msg.document
        and msg.document.mime_type
        and msg.document.mime_type.startswith("image/")
    )
    if not photo and not is_image_document:
        return

    # Cosmetic typing ping + initial status message — both wrapped so a
    # flood-control RetryAfter or transient network error during a busy
    # burst can't abort photo processing. The receipt save runs even if
    # both fail; status will just be None and downstream edits will
    # silently no-op. Sent BEFORE asking Telegram for the file so the
    # user gets an immediate sign of life.
    await _safe_telegram_call(
        lambda: ctx.bot.send_chat_action(msg.chat_id, ChatAction.TYPING),
        "send_chat_action",
    )
    status = await _safe_reply(msg, "Got it, processing...")

    # Keep the "is typing..." indicator alive for the full duration of
    # processing — an unreadable photo can take ~10 s of QR search plus
    # the OCR fallback, well past Telegram's ~5s auto-timeout on chat
    # actions. Without this, users see the "processing..." status sit
    # silent and assume the bot is hung.
    typing_task = asyncio.create_task(_keep_typing(ctx.bot, msg.chat_id))
    try:
        try:
            tg_file = await (
                photo.get_file() if photo else msg.document.get_file()
            )
        except Exception:
            logger.exception("Could not get the file from Telegram")
            await _safe_edit(
                status,
                "I couldn't download that image from Telegram. Please send it again.",
            )
            return
        await _process_photo(
            update=update, ctx=ctx, msg=msg, uid=uid,
            tg_file=tg_file, status=status,
        )
    except Exception:
        # Last line of defence: whatever broke, the user gets an answer.
        logger.exception("Photo processing crashed for user %s", uid)
        await _safe_edit(status, _crash_text("this photo"))
    finally:
        await _stop_keepalive(typing_task)


async def _process_photo(
    *,
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    msg,
    uid: int,
    tg_file,
    status,
) -> None:
    """Body of the photo handler — extracted so handle_photo can wrap it
    in a typing-keepalive try/finally without indenting the whole block."""
    started = time.monotonic()
    try:
        image_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception:
        logger.exception("Download failed")
        await _safe_edit(
            status,
            "I couldn't download that image from Telegram. Please send it again.",
        )
        return
    downloaded = time.monotonic()

    on_crop = None
    if SAVE_DEBUG_IMAGES:
        debug_dir = Path("debug_images")
        debug_dir.mkdir(exist_ok=True)
        debug_id = uuid.uuid4().hex[:6]
        ext = (
            ".heic"
            if (
                msg.document
                and msg.document.file_name
                and msg.document.file_name.lower().endswith((".heic", ".heif"))
            )
            else ".jpg"
        )
        raw_path = debug_dir / f"raw_{uid}_{debug_id}{ext}"
        raw_path.write_bytes(image_bytes)
        logger.info("Saved raw image: %s (%s bytes)", raw_path, len(image_bytes))

        def _save_debug_crop(cropped) -> None:
            (debug_dir / f"crop_{uid}_{debug_id}.png").write_bytes(
                receipt_image._to_png_bytes(cropped)
            )

        on_crop = _save_debug_crop

    loop = asyncio.get_running_loop()

    # Look up any pending (unscanned) photo while the QR search runs.
    pending, decoded = await asyncio.gather(
        db.get_pending_receipt(uid),
        _decode_qr(loop, image_bytes, on_crop),
        return_exceptions=True,
    )
    if isinstance(pending, BaseException):
        pending = None
    if isinstance(decoded, BaseException):
        logger.error("QR decode raised", exc_info=decoded)
        qr_url = None
    else:
        qr_url, _how = decoded
    searched = time.monotonic()

    # OCR fallback: long receipts often have the QR too small for the
    # decoders to lock onto, but the URL is printed in plain text right
    # next to the QR and OCR can usually read it. We try each candidate
    # against soliq.uz — the first one that returns valid data wins. If
    # none validate, we fall through to the pending-receipt flow.
    #
    # One OCR read answers both "is there a printed link?" and "is this
    # even a fiscal receipt?", and the soliq answer that validates a
    # candidate is kept and reused for the save (it used to be fetched
    # again from scratch).
    ocr_recovered = False
    prefetched = None
    classification: dict = {"kind": "unknown"}
    if not qr_url:
        await _safe_edit(status, "QR is small — scanning the receipt text...")
        try:
            analysis = await loop.run_in_executor(
                None, receipt_ocr.analyze_unreadable, image_bytes
            )
        except Exception:
            logger.exception("OCR fallback failed")
            analysis = {}
        classification = analysis.get("classification") or classification
        for candidate in analysis.get("candidates") or []:
            result = await soliq.fetch_receipt(candidate, want_screenshot=True)
            if result.data and result.data.get("vat_amount"):
                qr_url = candidate
                prefetched = result
                ocr_recovered = True
                logger.info("OCR fallback recovered URL: %s", candidate)
                break
            if result.reason == "unreachable":
                break  # soliq is down — more guesses won't fare better

    # Each photo is treated as its own receipt attempt. We never merge a
    # new photo's decoded QR with the pending receipt's image — that path
    # used to silently corrupt entries when users sent a different receipt
    # mid-recovery. The pending receipt can only be finished via:
    #   - pasting the soliq.uz URL as text (unambiguous, image stays put)
    #   - /manual (the user types in the data, image stays attached)
    #   - /cancel_pending (give up and start over)

    if not qr_url:
        if classification.get("kind") == "payment_slip":
            vendor = classification.get("vendor") or "this merchant"
            await _safe_edit(status,
                "This looks like a bank card payment slip, not a fiscal VAT receipt.\n\n"
                f"Merchant: {vendor}\n"
                "Please send the fiscal receipt from the merchant with the tax QR code."
            )
            return

        file_name = (
            msg.document.file_name
            if msg.document and msg.document.file_name
            else f"pending_receipt_{uid}.png"
        )
        # Save the ORIGINAL uncropped bytes. If auto-crop failed badly the
        # cropped image may have clipped the QR or distorted the receipt —
        # keeping the full-quality original preserves every pixel the user
        # captured for later review or export. save_pending_receipt deletes
        # any prior pending image first, so the user's previous unscanned
        # photo (if any) gets replaced by this new one.
        await db.save_pending_receipt(uid, image_bytes, file_name)
        msg_text = _qr_failure_message(retry=bool(pending))
        if pending:
            msg_text = (
                "Replaced your previous unscanned attempt with this new photo. "
                "If those were two different receipts, the previous one is gone — "
                "please send it again later.\n\n"
            ) + msg_text
        await _safe_edit(status, msg_text, parse_mode="HTML")
        logger.info(
            "Photo from user %s: no QR (download %.2fs, search %.2fs, total %.2fs)",
            uid, downloaded - started, searched - downloaded,
            time.monotonic() - started,
        )
        return

    if ocr_recovered:
        await _safe_edit(status,
            "QR was hard to read — recovered the link from the receipt's "
            "printed text instead. Saving..."
        )
    else:
        await _safe_edit(status, "QR found. Fetching verified data from soliq.uz...")
    # Save the ORIGINAL bytes (not the crop) so the stored receipt always
    # has maximum detail. The crop is only a processing step for QR decode.
    outcome = await _save_verified_receipt(
        uid=uid,
        source_image_bytes=image_bytes,
        qr_url=qr_url,
        loop=loop,
        prefetched=prefetched,
    )
    message = outcome.message
    # Any successful save silently clears the pending receipt — most of the
    # time it's the user retaking the same receipt (image-hash detection
    # turned out to be unreliable on real phone photos). Trade-off: a user
    # who had a genuinely different failed receipt pending then saves an
    # unrelated one loses the failed one's photo. We append a short, plain
    # note so they at least know the old pending is gone — short enough
    # that the retake case can ignore it without feeling nagged.
    if outcome.saved and pending:
        try:
            await db.delete_pending_receipt(uid)
        except Exception:
            logger.exception("Failed to clean up pending after successful save")
        message += (
            "\n\n(Note: a previous unscanned receipt was dropped. "
            "If it was a different receipt, retake it or paste its "
            "soliq.uz QR link.)"
        )
    await _safe_edit(status, message, parse_mode="HTML")
    logger.info(
        "Photo from user %s handled in %.2fs (download %.2fs, QR search %.2fs, "
        "soliq+save %.2fs) -> %s",
        uid, time.monotonic() - started, downloaded - started,
        searched - downloaded, time.monotonic() - searched,
        "saved" if outcome.saved else ("not saved" if outcome.resolved else "soliq lookup failed"),
    )


_NUDGE_TEXT = (
    "To add a receipt, send me a 📸 photo of it (or of just its QR code), "
    "or paste its soliq.uz link.\n\n"
    "/list shows what you've saved · /help explains everything."
)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept a pasted soliq.uz URL to complete a pending receipt, OR a name
    typed in response to /setname without arguments."""
    if not await _require_approved(update):
        return
    if update.message is None:
        return  # edited/channel update slipped through — nothing to act on
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # Manual-entry conversation has highest priority — when the user is
    # mid-flow, every plain-text message answers the next question.
    if ctx.user_data.get("manual_step"):
        await _handle_manual_step(update, ctx, text)
        return

    # A soliq.uz link anywhere in the message ("here it is: https://...").
    # Host-checked, so only genuine soliq.uz links count.
    link = soliq.find_soliq_url(text)

    # /online_purchase mode — next message is the soliq.uz URL.
    if ctx.user_data.get("awaiting_online_url"):
        ctx.user_data.pop("awaiting_online_url", None)
        if text.startswith("/"):
            await update.message.reply_text(
                "Online purchase entry cancelled (you sent a command)."
            )
            return
        if not link:
            await update.message.reply_text(
                "That didn't look like a soliq.uz URL. Cancelled.\n\n"
                "Run /online_purchase again and paste the link."
            )
            return
        await _save_online_purchase(update, ctx, link)
        return

    # If the user clicked /setname (no args), their next plain-text message
    # is treated as their name. Guard against accidents: pasted URLs or
    # empty text don't make sense as names.
    if ctx.user_data.get("awaiting_name"):
        ctx.user_data.pop("awaiting_name", None)
        if not text or "soliq.uz" in text.lower() or text.startswith("/"):
            await update.message.reply_text(
                "That didn't look like a name — nothing saved. "
                "Tap /setname again to try once more."
            )
            return
        # Keep names short so bogus pastes don't turn into 4 KB vendor names
        name = text[:120].strip()
        await db.set_user_name(uid, name)
        await update.message.reply_text(f"Thanks — saved your name as: {name}")
        return

    pending = await db.get_pending_receipt(uid)

    # Soliq.uz URL paste with no pending photo: just save it as an online
    # purchase. Whether the user ran /online_purchase first or pasted the
    # link directly, the intent is the same — they want this URL turned
    # into a saved receipt and they have no photo to attach.
    if not pending and link:
        await _save_online_purchase(update, ctx, link)
        return

    if not pending:
        # Plain chat with nothing in progress. The bot used to stay
        # silent here, which reads as "it's broken" — answer with the
        # one thing people need to know.
        await update.message.reply_text(_NUDGE_TEXT)
        return

    if not link:
        await update.message.reply_text(
            "I'm waiting for:\n"
            "• A close-up photo of the QR code\n"
            "• The soliq.uz link — easiest way: open your phone's camera, "
            "point at the QR, long-press the yellow URL banner → Share → "
            "pick this chat. I'll read it automatically.\n"
            "• /cancel_pending to discard this receipt\n\n"
            f"Need help? Contact {config.SUPPORT_CONTACT}."
        )
        return

    pending_image_id = pending.get("image_file_id")
    if not pending_image_id:
        await db.delete_pending_receipt(uid)
        await update.message.reply_text(
            "The pending receipt expired. Please send the full receipt again."
        )
        return

    status = await _safe_reply(update.message, "Link received. Fetching verified data from soliq.uz...")
    try:
        pending_image_bytes = await db.get_image(pending_image_id)
    except Exception:
        logger.exception("Failed to load pending receipt image")
        await db.delete_pending_receipt(uid)
        await _safe_edit(status,
            "I lost the earlier receipt image. Please send the full receipt again."
        )
        return

    loop = asyncio.get_running_loop()
    try:
        outcome = await _save_verified_receipt(
            uid=uid,
            source_image_bytes=pending_image_bytes,
            qr_url=link,
            loop=loop,
            pasted_link=True,
        )
    except Exception:
        logger.exception("Completing pending receipt crashed for user %s", uid)
        await _safe_edit(status, _crash_text("this link"))
        return

    message = outcome.message
    if outcome.resolved:
        # Saved, duplicate or zero-VAT: this receipt's story is over.
        await db.delete_pending_receipt(uid)
        if outcome.saved:
            message += "\n\nReceipt completed using the link you pasted."
    else:
        # soliq could not be asked (down / receipt not there yet / bad
        # link). Keep the photo so the user can simply paste again —
        # it used to be thrown away here, forcing a re-take.
        message += (
            "\n\nI kept your photo — paste the link again to retry, "
            "or /cancel_pending to discard it."
        )
    await _safe_edit(status, message, parse_mode="HTML")


# ---------- App setup ----------

_BOT_COMMANDS = [
    BotCommand("start", "Welcome + show help"),
    BotCommand("setname", "Set your name for exports"),
    BotCommand("manual", "Add a receipt manually (when QR is unreadable)"),
    BotCommand("online_purchase", "Add an online-purchase receipt from a soliq.uz URL"),
    BotCommand("list", "Show stored receipts"),
    BotCommand("export_vat", "Download VAT_Refund.xlsx"),
    BotCommand("export_pdf", "Download PDF of all receipts"),
    BotCommand("cancel_pending", "Discard a receipt waiting for a QR close-up"),
    BotCommand("cancel_manual", "Abort a manual entry in progress"),
    BotCommand("cancel_online", "Abort an online-purchase entry in progress"),
    BotCommand("reset", "Delete everything"),
    BotCommand("help", "Show help"),
    BotCommand("about", "License and disclaimer"),
]


async def _post_init(app: Application) -> None:
    global _APP_BOT
    _APP_BOT = app.bot
    asyncio.get_running_loop().set_default_executor(_executor)

    # Retry the initial Telegram handshake — WiFi can drop for a few seconds
    # right as the bot starts and a single ConnectError should not kill us.
    for attempt in range(1, 6):
        try:
            me = await app.bot.get_me()
            logger.info("Connected to Telegram as @%s", me.username)
            break
        except Exception:
            logger.warning(
                "Telegram get_me failed (attempt %s/5) — retrying in 5s.",
                attempt,
                exc_info=True,
            )
            if attempt == 5:
                raise
            await asyncio.sleep(5)

    # Overwrite Telegram's command menu with exactly the commands we handle.
    # Without this, the "/" menu in Telegram keeps showing stale commands
    # that BotFather picked up from older iterations of this bot.
    try:
        await app.bot.set_my_commands(_BOT_COMMANDS)
        logger.info("Telegram command menu set to %d commands.", len(_BOT_COMMANDS))
    except Exception:
        logger.warning("Could not update bot command menu", exc_info=True)

    logger.info(
        "MongoDB target: host=%s db=%s uri=%s",
        config.mongodb_host_hint(),
        config.MONGODB_DB,
        config.redacted_mongodb_uri(),
    )

    for attempt in range(1, 4):
        try:
            await db.ping()
            # Backfill BEFORE ensure_indexes: the unique fiscal_id index
            # can only build once historical receipts carry the field.
            backfilled = await db.migrate_backfill_fiscal_ids()
            if backfilled:
                logger.info("Backfilled fiscal_id on %s receipt(s).", backfilled)
            await db.ensure_indexes()
            migrated = await db.migrate_legacy_users_to_approved()
            if migrated:
                logger.info("Access control: grandfathered %s legacy user(s) as approved.", migrated)
            deleted = await db.cleanup_orphaned_images()
            if deleted:
                logger.info("Deleted %s orphaned GridFS image(s).", deleted)
            logger.info("MongoDB reachable and indexes ensured.")
            break
        except Exception:
            logger.exception("MongoDB startup check failed (attempt %s/3)", attempt)
            if attempt == 3:
                raise
            await asyncio.sleep(3)

    # Schedule the bot to manage its own backups, log rotation (already
    # wired into logging), and disk-usage warnings. The bot catches up
    # immediately if we missed a nightly window (Mac was asleep, bot
    # crashed, etc.) then schedules 03:30 runs every night afterward.
    if app.job_queue is not None:
        app.job_queue.run_once(
            maintenance.run_startup_catchup,
            when=30,
            name="maintenance_catchup",
        )
        app.job_queue.run_daily(
            maintenance.run_nightly,
            time=dt_time(hour=3, minute=30),
            name="maintenance_nightly",
        )
        logger.info("Self-managed maintenance scheduled (nightly 03:30 + startup catch-up).")
    else:
        logger.warning(
            "JobQueue unavailable — install python-telegram-bot[job-queue] "
            "to enable self-managed backups."
        )

    # Warm up the headless Chromium that captures soliq.uz screenshots
    # for the PDF export. Launching once at startup keeps per-receipt
    # save latency low (one cold-start chromium is ~2s).
    try:
        await soliq_screenshot.startup()
    except Exception:
        logger.exception("soliq_screenshot startup failed (PDF will use data renderer).")


async def _post_shutdown(app: Application) -> None:
    """Close the shared Playwright browser cleanly when the bot exits."""
    try:
        await soliq_screenshot.shutdown()
    except Exception:
        logger.debug("soliq_screenshot shutdown raised", exc_info=True)


def _build_app() -> Application:
    # Everything the bot SENDS (replies, edits, files, downloads) goes
    # through ResilientRequest: when a connection to Telegram cannot be
    # opened it tries again for about a minute and a half instead of
    # dropping the reply after one attempt. A shorter connect timeout
    # (10 s, was 30 s) makes each attempt fail fast so the next can start.
    send_request = ResilientRequest(
        connection_pool_size=256,
        connect_timeout=10.0,
        read_timeout=40.0,
        write_timeout=40.0,
        pool_timeout=30.0,
    )
    # Generous long-poll timeouts — WiFi jitters on cafe/hotel networks
    # caused httpx.ConnectError bursts during get_updates. Polling has
    # its own retry loop, so it keeps the stock request class. (Once a
    # request instance is given, the builder insists on instances for
    # both — the per-timeout builder methods can no longer be used.)
    polling_request = HTTPXRequest(
        connection_pool_size=1,
        connect_timeout=30.0,
        read_timeout=40.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .concurrent_updates(True)
        .request(send_request)
        .get_updates_request(polling_request)
        .build()
    )

    app.add_error_handler(_on_error)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("setname", cmd_setname))
    app.add_handler(CommandHandler("manual", cmd_manual))
    app.add_handler(CommandHandler("cancel_manual", cmd_cancel_manual))
    app.add_handler(CommandHandler("online_purchase", cmd_online_purchase))
    app.add_handler(CommandHandler("cancel_online", cmd_cancel_online))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("export_vat", cmd_export_vat))
    app.add_handler(CommandHandler("export_pdf", cmd_export_pdf))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("cancel_pending", cmd_cancel_pending))
    # Hidden: not in set_my_commands, silent for non-admins. Admin IDs come
    # from the ADMIN_TELEGRAM_IDS env var.
    app.add_handler(CommandHandler("heartcheck", cmd_heartcheck))
    app.add_handler(CommandHandler("report", cmd_report))
    # DT-approver commands. Silent for non-approvers (APPROVER_TELEGRAM_IDS).
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("deny", cmd_deny))
    # Inline Approve/Deny buttons on the new-user notification messages.
    app.add_handler(CallbackQueryHandler(access_callback, pattern=r"^access:"))
    # Yes / Keep buttons under the /reset confirmation.
    app.add_handler(CallbackQueryHandler(reset_callback, pattern=r"^reset:"))

    # filters.UpdateType.MESSAGE restricts these to genuinely new
    # messages. Without it PTB also routes edited_message (and channel
    # posts) to the same callbacks, where update.message is None — an
    # approved user editing any text message crashed handle_text with
    # AttributeError (2026-10-03). Re-running a handler on an edit is
    # also wrong behaviourally: an edited soliq URL would re-save.
    _new_msg = filters.UpdateType.MESSAGE
    app.add_handler(MessageHandler(_new_msg & filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(_new_msg & filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(_new_msg & filters.TEXT & ~filters.COMMAND, handle_text))
    return app


# Flipped to True only when SIGINT/SIGTERM hits our handler (Ctrl-C,
# `launchctl kill SIGTERM`, system shutdown). Used to distinguish
# "user wants the process to quit" from "python-telegram-bot's library
# swallowed a network error and stopped polling cleanly" — the two
# look identical to run_polling()'s return value but require opposite
# responses from main(). Pre-2026-06-19 we treated both as quit, so a
# 13-hour overnight WiFi outage caused the process to exit when
# Telegram came back and launchd had to restart it.
_USER_REQUESTED_SHUTDOWN = False


def _handle_shutdown_signal(signum, _frame):
    """Set the shutdown flag and re-raise as KeyboardInterrupt so
    run_polling unwinds cleanly. We install this BEFORE run_polling
    starts; passing stop_signals=None to run_polling disables its own
    handlers so this one wins."""
    global _USER_REQUESTED_SHUTDOWN
    _USER_REQUESTED_SHUTDOWN = True
    logger.info("Received signal %s — shutting down.", signum)
    raise KeyboardInterrupt


def main() -> None:
    logger.info("Starting VAT bot (local Mac host).")
    # Install our own signal handlers BEFORE the library can install
    # its own. Without this, a SIGTERM from launchd would unwind via
    # python-telegram-bot's internal handler and we'd never know
    # whether the resulting clean-shutdown was user-driven or a
    # library-swallowed network error.
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    # Auto-restart loop: any non-user-signal exit (clean OR crashed)
    # is treated as a transient network blip and triggers an in-process
    # rebuild. Exponential backoff caps at 60 s.
    backoff = 5
    while True:
        # run_polling() CLOSES the event loop it ran on. Without a new
        # one, the second pass through this loop dies instantly with
        # "Event loop is closed" — every time, forever — so the
        # auto-restart this loop exists for could never actually work:
        # the process would sit there alive but deaf, and launchd would
        # not step in because nothing had exited.
        asyncio.set_event_loop(asyncio.new_event_loop())
        app = _build_app()
        logger.info("Bot polling...")
        try:
            # stop_signals=None disables the library's SIGINT/SIGTERM
            # handlers so our _handle_shutdown_signal stays authoritative.
            app.run_polling(
                allowed_updates=Update.ALL_TYPES, stop_signals=None
            )
        except (KeyboardInterrupt, SystemExit):
            logger.info("Bot stopped by user signal.")
            return
        except Exception:
            logger.exception("Polling crashed — restarting in %ss.", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        # run_polling returned normally with no exception. If the user
        # actually signalled shutdown, our handler set the flag (and
        # would have raised KeyboardInterrupt, hitting the except
        # above). If we're here without the flag, the python-telegram-bot
        # library suppressed a network error and stopped polling on its
        # own — restart in-process rather than letting launchd reap us.
        if _USER_REQUESTED_SHUTDOWN:
            logger.info("Polling exited after shutdown signal. Goodbye.")
            return
        logger.warning(
            "Polling exited cleanly with no user signal — library "
            "swallowed a network error. Restarting in %ss.",
            backoff,
        )
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
