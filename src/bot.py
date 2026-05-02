"""
Telegram bot entry point.

Commands:
  /start        - welcome + register
  /setname      - set employee name (used in exports)
  /add          - reply to this, then send receipt photo(s)
  /list         - show all stored receipts
  /export_vat   - get the filled VAT_Refund.xlsx
  /export_pdf   - get a PDF with all receipt images + summary
  /reset        - delete all your receipts
  /help         - show help
"""
from __future__ import annotations

import asyncio
from datetime import time as dt_time
from html import escape
import logging
import logging.handlers
import os
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
    online_snapshot,
    receipt_image,
    receipt_ocr,
    soliq,
)


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

# Image processing (OpenCV, zxing, PIL) releases the GIL — use all cores
_executor = ThreadPoolExecutor(
    max_workers=(os.cpu_count() or 4) * 2,
    thread_name_prefix="vat-worker",
)


async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.error import Conflict, NetworkError, TimedOut
    err = ctx.error
    # Transient connectivity hiccups (DNS blips, Mac WiFi sleep, brief
    # api.telegram.org reachability gaps) are already retried internally by
    # python-telegram-bot's polling loop and self-heal within seconds. Logging
    # the full traceback at ERROR makes routine network noise look like a
    # crash in /heartcheck — record a one-line WARNING instead.
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning("Transient network error (auto-retried): %s", err)
        return
    # Conflict means two bot instances briefly polled at the same time
    # (e.g. during a launchd restart while a manual run was still alive).
    # The duplicate dies within seconds and the survivor keeps polling, so
    # this is recovery noise, not an outage.
    if isinstance(err, Conflict):
        logger.warning("Polling conflict (duplicate instance, auto-resolved): %s", err)
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


async def _decode_qr(loop: asyncio.AbstractEventLoop, image_bytes: bytes, cropped_bytes: bytes) -> str | None:
    try:
        # Try both images concurrently — crop and original are independent inputs
        original_task = loop.run_in_executor(None, receipt_image.extract_qr_url, image_bytes)
        cropped_task = loop.run_in_executor(None, receipt_image.extract_qr_url, cropped_bytes)
        results = await asyncio.gather(original_task, cropped_task, return_exceptions=True)
        for r in results:
            if isinstance(r, str) and r:
                logger.info("QR found: %s", r)
                return r
    except Exception:
        logger.exception("QR decode failed")
    return None


async def _save_verified_receipt(
    *,
    uid: int,
    source_image_bytes: bytes,
    qr_url: str,
    loop: asyncio.AbstractEventLoop,
    qr_image_bytes: bytes | None = None,
) -> tuple[str, bool]:
    data, diag = await soliq.fetch_receipt_with_diag(qr_url)
    if not data:
        return (
            f"I read the QR code but could not fetch data from soliq.uz.\n\n"
            f"Reason: {diag}\n"
            f"QR content: {qr_url[:200]}\n\n"
            f"Need help? Contact {config.SUPPORT_CONTACT}.",
            False,
        )

    if not data.get("vat_amount"):
        vendor = data.get("vendor", "") or data.get("receipt_number", "this receipt")
        return (
            f"This receipt ({vendor}) has 0 VAT — nothing to refund.\n"
            "It will not be added to your records.",
            False,
        )

    printed_vendor = None
    try:
        printed_vendor = await loop.run_in_executor(
            None, receipt_ocr.extract_printed_vendor, source_image_bytes
        )
    except Exception:
        logger.exception("Printed vendor OCR failed")

    display_vendor = data.get("vendor", "") or printed_vendor or ""
    file_id = await db.save_image(uid, source_image_bytes, f"receipt_{uid}.png")

    qr_file_id = None
    if qr_image_bytes:
        try:
            qr_file_id = await db.save_image(uid, qr_image_bytes, f"qr_{uid}.png")
        except Exception:
            logger.exception("Failed to save QR close-up image")

    receipt_doc = {
        "telegram_id": uid,
        "image_file_id": file_id,
        "qr_image_file_id": qr_file_id,
        "date": data.get("date", ""),
        "vendor": data.get("vendor", ""),
        "printed_vendor": printed_vendor or "",
        "display_vendor": display_vendor,
        "receipt_number": data.get("receipt_number", ""),
        "vat_amount": data.get("vat_amount", 0.0),
        "total_amount": data.get("total_amount", 0.0),
        "soliq_url": qr_url,
        "raw_qr": qr_url,
    }
    inserted = await db.save_receipt(receipt_doc)
    if inserted is None:
        return (
            f"This receipt (#{data.get('receipt_number')}) is already in your records.",
            False,
        )

    count = await db.count_receipts(uid)
    return (
        f"✅ Saved! That's receipt #{count} for you.\n\n"
        f"Vendor: {display_vendor or '-'}\n"
        f"Date: {data.get('date', '-')}\n"
        f"Receipt #: {data.get('receipt_number', '-')}\n"
        f"Total: {data.get('total_amount', 0):,.2f} UZS\n"
        f"VAT: {data.get('vat_amount', 0):,.2f} UZS\n\n"
        "Verified by soliq.uz",
        True,
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
        return True
    status = await db.get_user_status(uid)
    if status == "approved":
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
    "<b>🇺🇸 Tashkent Embassy VAT Refund — V.7.0</b>\n\n"
    "Hi <b>{first_name}</b>! 👋\n\n"
    "<b>To add a receipt:</b> just send me a photo. That's it — "
    "I do the rest.\n\n"
    "<b>When you're ready to file:</b>\n"
    "• /export_vat — official Excel form\n"
    "• /export_pdf — receipt images package\n\n"
    "<b>First time?</b> Run /setname so the forms have your name on them.\n\n"
    "Tap <b>/</b> in the message box to see every command.\n\n"
    "Need help? Contact <b>{support}</b>."
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
    "/whoami — show your Telegram ID\n\n"

    "Need help? Contact <b>{support}</b>."
)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        _HELP_HTML.format(support=escape(config.SUPPORT_CONTACT)),
        parse_mode="HTML",
        disable_web_page_preview=True,
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

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_DOCUMENT)

    cap = exporter.RECEIPTS_PER_XLSX

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
            exporter.build_xlsx_from_receipts(
                chunk, name, out_path, start_seq=start_seq
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
            out_path.unlink(missing_ok=True)


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

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_DOCUMENT)

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


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_approved(update):
        return
    await db.delete_pending_receipt(update.effective_user.id)
    n = await db.delete_all_receipts(update.effective_user.id)
    await update.message.reply_text(f"Deleted {n} receipt(s).")


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
        today_start = _dt.combine(_dt.now().date(), _dt_time.min)
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
        "<b>🔧 Heartcheck — V.7.0</b>\n\n"
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
    t = text.strip()
    return t.startswith("http") and "soliq.uz" in t.lower()


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
        url = " ".join(ctx.args).strip()
        if _looks_like_soliq_url(url):
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
    uid = update.effective_user.id
    status = await update.message.reply_text("Fetching verified data from soliq.uz...")

    data, diag = await soliq.fetch_receipt_with_diag(qr_url)
    if not data:
        await status.edit_text(
            "I could not fetch data from soliq.uz for that URL.\n\n"
            f"Reason: {diag}\n\n"
            f"Need help? Contact {config.SUPPORT_CONTACT}."
        )
        return

    if not data.get("vat_amount"):
        vendor = data.get("vendor", "") or data.get("receipt_number", "this receipt")
        await status.edit_text(
            f"This receipt ({vendor}) has 0 VAT — nothing to refund.\n"
            "It will not be added to your records."
        )
        return

    receipt_no = data.get("receipt_number", "")
    existing = await db.find_receipt_by_number(uid, receipt_no)
    if existing:
        await status.edit_text(
            f"This receipt (#{receipt_no}) is already in your records — "
            f"saved on {existing.get('created_at', '?')}."
        )
        return

    # Build the synthetic snapshot and save it as the receipt's image.
    loop = asyncio.get_running_loop()
    try:
        png_bytes = await loop.run_in_executor(
            None, online_snapshot.render_online_receipt_png, data, qr_url
        )
        file_id = await db.save_image(uid, png_bytes, f"online_{uid}.png")
    except Exception:
        logger.exception("Online snapshot render/save failed")
        # Save the receipt without an image rather than dropping the entry.
        file_id = None

    receipt_doc = {
        "telegram_id": uid,
        "image_file_id": file_id,
        "qr_image_file_id": None,
        "date": data.get("date", ""),
        "vendor": data.get("vendor", ""),
        "printed_vendor": "",
        "display_vendor": data.get("vendor", ""),
        "receipt_number": receipt_no,
        "vat_amount": data.get("vat_amount", 0.0),
        "total_amount": data.get("total_amount", 0.0),
        "soliq_url": qr_url,
        "raw_qr": qr_url,
        "online_purchase": True,
    }
    inserted = await db.save_receipt(receipt_doc)
    if inserted is None:
        await status.edit_text(
            f"This receipt (#{receipt_no}) is already in your records."
        )
        return

    count = await db.count_receipts(uid)
    await status.edit_text(
        f"✅ Online purchase saved! That's receipt #{count} for you.\n\n"
        f"Vendor: {data.get('vendor', '-') or '-'}\n"
        f"Date: {data.get('date', '-')}\n"
        f"Receipt #: {receipt_no or '-'}\n"
        f"Total: {data.get('total_amount', 0):,.2f} UZS\n"
        f"VAT: {data.get('vat_amount', 0):,.2f} UZS\n\n"
        "Verified by soliq.uz"
    )


async def _handle_random_soliq_url(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Inquiry-only handler when a user pastes a soliq.uz URL with no
    pending receipt. Tells them whether the receipt is already on file
    and, if not, points them at the right next step."""
    uid = update.effective_user.id
    status = await update.message.reply_text("Checking soliq.uz...")

    data, diag = await soliq.fetch_receipt_with_diag(text)
    if not data:
        await status.edit_text(
            "I could not fetch data from soliq.uz for that URL.\n\n"
            f"Reason: {diag}"
        )
        return

    receipt_no = data.get("receipt_number", "")
    existing = await db.find_receipt_by_number(uid, receipt_no)
    if existing:
        when = existing.get("created_at")
        when_str = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else "?"
        await status.edit_text(
            f"This receipt is already in your records.\n\n"
            f"Vendor: {existing.get('display_vendor', '-') or '-'}\n"
            f"Date: {existing.get('date', '-') or '-'}\n"
            f"Receipt #: {receipt_no or '-'}\n"
            f"VAT: {float(existing.get('vat_amount') or 0):,.2f} UZS\n"
            f"Saved on: {when_str}"
        )
        return

    vendor = data.get("vendor", "") or "-"
    await status.edit_text(
        "I have the tax data for this receipt — but it's not saved yet.\n\n"
        f"Vendor: {vendor}\n"
        f"Date: {data.get('date', '-') or '-'}\n"
        f"Receipt #: {receipt_no or '-'}\n"
        f"VAT: {float(data.get('vat_amount') or 0):,.2f} UZS\n\n"
        "To save it:\n"
        "• Send a <b>photo</b> of the physical receipt to add it the normal way.\n"
        "• If this was an <b>online purchase</b> (no physical receipt), run "
        "/online_purchase and paste the link again — I'll save it with a "
        "verified snapshot.",
        parse_mode="HTML",
    )


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
    uid = update.effective_user.id
    await db.upsert_user(uid, update.effective_user.full_name or "")

    photo = msg.photo[-1] if msg.photo else None
    if (
        not photo
        and msg.document
        and msg.document.mime_type
        and msg.document.mime_type.startswith("image/")
    ):
        tg_file = await msg.document.get_file()
    elif photo:
        tg_file = await photo.get_file()
    else:
        return

    await ctx.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    status = await msg.reply_text("Got it, processing...")

    try:
        image_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        logger.exception("Download failed")
        await status.edit_text(f"Could not download the image: {e}")
        return

    debug_dir = None
    debug_id = uuid.uuid4().hex[:6]
    if SAVE_DEBUG_IMAGES:
        debug_dir = Path("debug_images")
        debug_dir.mkdir(exist_ok=True)
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

    loop = asyncio.get_running_loop()

    # Run DB lookup and image crop in parallel — they're fully independent
    pending, crop_result = await asyncio.gather(
        db.get_pending_receipt(uid),
        loop.run_in_executor(None, receipt_image.auto_crop_receipt, image_bytes),
        return_exceptions=True,
    )
    if isinstance(pending, Exception):
        pending = None
    if isinstance(crop_result, Exception):
        logger.exception("Crop failed: %s", crop_result)
        cropped_bytes = image_bytes
    else:
        cropped_bytes = crop_result
        if debug_dir is not None:
            (debug_dir / f"crop_{uid}_{debug_id}.png").write_bytes(cropped_bytes)
        logger.info("Cropped image: %s bytes", len(cropped_bytes))

    qr_url = await _decode_qr(loop, image_bytes, cropped_bytes)

    # OCR fallback: long receipts often have the QR too small for zxing
    # to lock onto, but the URL is printed in plain text right next to
    # the QR and OCR can usually read it. We try each candidate against
    # soliq.uz — the first one that returns valid data wins. If none
    # validate, we fall through to the existing pending-receipt flow,
    # so worst case behavior is identical to before.
    ocr_recovered = False
    if not qr_url:
        try:
            candidates = await loop.run_in_executor(
                None, receipt_ocr.extract_soliq_url_candidates, image_bytes
            )
        except Exception:
            logger.exception("OCR fallback: candidate extraction failed")
            candidates = []
        for candidate in candidates:
            try:
                test_data = await soliq.fetch_receipt_data(candidate)
            except Exception:
                logger.exception("OCR fallback: soliq fetch crashed for %s", candidate)
                test_data = None
            if test_data and test_data.get("vat_amount"):
                qr_url = candidate
                ocr_recovered = True
                logger.info("OCR fallback recovered URL: %s", candidate)
                break

    # Each photo is treated as its own receipt attempt. We never merge a
    # new photo's decoded QR with the pending receipt's image — that path
    # used to silently corrupt entries when users sent a different receipt
    # mid-recovery. The pending receipt can only be finished via:
    #   - pasting the soliq.uz URL as text (unambiguous, image stays put)
    #   - /manual (the user types in the data, image stays attached)
    #   - /cancel_pending (give up and start over)

    if not qr_url:
        try:
            classification = await loop.run_in_executor(
                None, receipt_ocr.classify_document, cropped_bytes
            )
        except Exception:
            logger.exception("Receipt classification failed")
            classification = {"kind": "unknown"}

        if classification.get("kind") == "payment_slip":
            vendor = classification.get("vendor") or "this merchant"
            await status.edit_text(
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
        await status.edit_text(msg_text, parse_mode="HTML")
        return

    if ocr_recovered:
        await status.edit_text(
            "QR was hard to read — recovered the link from the receipt's "
            "printed text instead. Fetching verified data from soliq.uz..."
        )
    else:
        await status.edit_text("QR found. Fetching verified data from soliq.uz...")
    # Save the ORIGINAL bytes (not the crop) so the stored receipt always
    # has maximum detail. The crop is only a processing step for QR decode.
    message, saved = await _save_verified_receipt(
        uid=uid,
        source_image_bytes=image_bytes,
        qr_url=qr_url,
        loop=loop,
    )
    # Any successful save silently clears the pending receipt — most of the
    # time it's the user retaking the same receipt (the case the previous
    # image-hash heuristic was meant to catch but couldn't reliably detect
    # under real-world lighting/framing changes), and even when it isn't,
    # the reminder text we used to show was ignored anyway. Trade-off: a
    # user who had a genuinely different failed receipt pending then saves
    # an unrelated one loses the failed one's photo. Recoverable — they
    # just retake it. Pending state is low-value (image only, no data),
    # so the loss is small and the reduced nagging is worth it.
    if saved and pending:
        try:
            await db.delete_pending_receipt(uid)
        except Exception:
            logger.exception("Failed to clean up pending after successful save")
    await status.edit_text(message)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept a pasted soliq.uz URL to complete a pending receipt, OR a name
    typed in response to /setname without arguments."""
    if not await _require_approved(update):
        return
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # Manual-entry conversation has highest priority — when the user is
    # mid-flow, every plain-text message answers the next question.
    if ctx.user_data.get("manual_step"):
        await _handle_manual_step(update, ctx, text)
        return

    # /online_purchase mode — next message is the soliq.uz URL.
    if ctx.user_data.get("awaiting_online_url"):
        ctx.user_data.pop("awaiting_online_url", None)
        if text.startswith("/"):
            await update.message.reply_text(
                "Online purchase entry cancelled (you sent a command)."
            )
            return
        if not _looks_like_soliq_url(text):
            await update.message.reply_text(
                "That didn't look like a soliq.uz URL. Cancelled.\n\n"
                "Run /online_purchase again and paste the link."
            )
            return
        await _save_online_purchase(update, ctx, text)
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

    # Random soliq.uz URL paste with no pending photo: treat as an inquiry.
    # Check if the receipt is already saved; if not, prompt the user to
    # either send a photo or run /online_purchase.
    if not pending and _looks_like_soliq_url(text):
        await _handle_random_soliq_url(update, ctx, text)
        return

    if not pending:
        return

    if "soliq.uz" not in text.lower():
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

    status = await update.message.reply_text("Link received. Fetching verified data from soliq.uz...")
    try:
        pending_image_bytes = await db.get_image(pending_image_id)
    except Exception:
        logger.exception("Failed to load pending receipt image")
        await db.delete_pending_receipt(uid)
        await status.edit_text(
            "I lost the earlier receipt image. Please send the full receipt again."
        )
        return

    loop = asyncio.get_running_loop()
    message, saved = await _save_verified_receipt(
        uid=uid,
        source_image_bytes=pending_image_bytes,
        qr_url=text,
        loop=loop,
    )
    await db.delete_pending_receipt(uid)
    if saved:
        message += "\n\nReceipt completed using the link you pasted."
    await status.edit_text(message)


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
]


async def _post_init(app: Application) -> None:
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


def _build_app() -> Application:
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .concurrent_updates(True)
        # Generous long-poll timeouts — WiFi jitters on cafe/hotel networks
        # caused httpx.ConnectError bursts during get_updates.
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(40.0)
        .get_updates_write_timeout(30.0)
        .get_updates_pool_timeout(30.0)
        .connect_timeout(30.0)
        .read_timeout(40.0)
        .write_timeout(40.0)
        .pool_timeout(30.0)
        .build()
    )

    app.add_error_handler(_on_error)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
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
    # DT-approver commands. Silent for non-approvers (APPROVER_TELEGRAM_IDS).
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("deny", cmd_deny))
    # Inline Approve/Deny buttons on the new-user notification messages.
    app.add_handler(CallbackQueryHandler(access_callback, pattern=r"^access:"))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def main() -> None:
    logger.info("Starting VAT bot (local Mac host).")
    # Auto-restart loop: if WiFi drops long enough to break polling, rebuild
    # the Application and resume. Exponential backoff caps at 60s so we don't
    # hammer the network while it's down.
    backoff = 5
    while True:
        app = _build_app()
        logger.info("Bot polling...")
        try:
            app.run_polling(allowed_updates=Update.ALL_TYPES)
            logger.info("Polling exited cleanly. Shutting down.")
            return
        except (KeyboardInterrupt, SystemExit):
            logger.info("Bot stopped by user.")
            return
        except Exception:
            logger.exception("Polling crashed — restarting in %ss.", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
