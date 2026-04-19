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
import logging
import os
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, db, exporter, receipt_image, soliq

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("vat_bot")


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        body = b"ok\n"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003 - stdlib signature
        return


def _start_health_server() -> None:
    port = os.getenv("PORT")
    if not port:
        return

    try:
        server = ThreadingHTTPServer(("0.0.0.0", int(port)), _HealthHandler)
    except OSError:
        logger.exception("Failed to bind healthcheck server on PORT=%s", port)
        raise

    thread = Thread(target=server.serve_forever, name="healthcheck-server", daemon=True)
    thread.start()
    logger.info("Healthcheck server listening on PORT=%s", port)


async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing an update.", exc_info=ctx.error)


# ---------- Commands ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await db.upsert_user(user.id, user.full_name or user.username or str(user.id))
    await update.message.reply_text(
        f"Hi {user.first_name}! I'm your VAT Refund bot.\n\n"
        "Send receipts as files (not photos) for best QR results:\n"
        "  Tap paperclip -> File -> choose image\n\n"
        "I will auto-crop, read the QR, fetch VAT from soliq.uz, and store it.\n\n"
        "Commands:\n"
        "/setname <your full name> - used in exports\n"
        "/list - show stored receipts\n"
        "/export_vat - download filled VAT_Refund.xlsx\n"
        "/export_pdf - download PDF with all receipts\n"
        "/reset - delete everything\n"
        "/help - show this message"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def cmd_setname(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("Usage: /setname John Smith")
        return
    name = " ".join(ctx.args).strip()
    await db.set_user_name(update.effective_user.id, name)
    await update.message.reply_text(f"Name set to: {name}")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    receipts = await db.list_receipts(update.effective_user.id)
    if not receipts:
        await update.message.reply_text("No receipts yet. Send me a photo to add one!")
        return

    lines = [f"You have {len(receipts)} receipt(s):\n"]
    total_vat = 0.0
    for i, r in enumerate(receipts, 1):
        vat = float(r.get("vat_amount") or 0)
        total_vat += vat
        lines.append(
            f"{i}. {r.get('date', '?')} - {r.get('vendor', '?')[:25]} - "
            f"VAT: {vat:,.2f} UZS"
        )
    lines.append(f"\nTotal VAT: {total_vat:,.2f} UZS")
    await update.message.reply_text("\n".join(lines))


async def cmd_export_vat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    count = await db.count_receipts(uid)
    if count == 0:
        await update.message.reply_text("No receipts to export yet.")
        return
    if count > 120:
        await update.message.reply_text(
            "You have more than 120 receipts. The template only fits 120. "
            "Only the first 120 will be included."
        )

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_DOCUMENT)
    user = await db.get_user(uid)
    name = (user or {}).get("name", "")

    out_path = config.TMP_DIR / f"VAT_Refund_{uid}_{uuid.uuid4().hex[:6]}.xlsx"
    await exporter.build_xlsx(uid, name, out_path)

    with open(out_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename="VAT_Refund.xlsx",
            caption=f"Filled with {count} receipt(s).",
        )
    out_path.unlink(missing_ok=True)


async def cmd_export_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    count = await db.count_receipts(uid)
    if count == 0:
        await update.message.reply_text("No receipts to export yet.")
        return

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_DOCUMENT)
    user = await db.get_user(uid)
    name = (user or {}).get("name", "")

    out_path = config.TMP_DIR / f"Receipts_{uid}_{uuid.uuid4().hex[:6]}.pdf"
    await exporter.build_pdf(uid, name, out_path)

    with open(out_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename="Receipts.pdf",
            caption=f"{count} receipt(s) packaged.",
        )
    out_path.unlink(missing_ok=True)


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    n = await db.delete_all_receipts(update.effective_user.id)
    await update.message.reply_text(f"Deleted {n} receipt(s).")


# ---------- Photo handler ----------

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Process a receipt photo."""
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

    loop = asyncio.get_running_loop()

    try:
        cropped_bytes = await loop.run_in_executor(
            None, receipt_image.auto_crop_receipt, image_bytes
        )
        (debug_dir / f"crop_{uid}_{debug_id}.png").write_bytes(cropped_bytes)
        logger.info("Cropped image: %s bytes", len(cropped_bytes))
    except Exception:
        logger.exception("Crop failed")
        cropped_bytes = image_bytes

    qr_url = None
    try:
        qr_url = await loop.run_in_executor(
            None, receipt_image.extract_qr_url, image_bytes
        )
        logger.info("QR from original: %s", qr_url)
        if not qr_url:
            qr_url = await loop.run_in_executor(
                None, receipt_image.extract_qr_url, cropped_bytes
            )
            logger.info("QR from cropped: %s", qr_url)
    except Exception:
        logger.exception("QR decode failed")

    if not qr_url:
        await status.edit_text(
            "Could not read the QR code - the photo resolution is too low.\n\n"
            "Please resend as a file (not a photo):\n"
            "  - Tap the paperclip -> File -> choose your image\n"
            "  - This keeps full resolution and the QR will decode correctly."
        )
        return

    await status.edit_text("QR found. Fetching verified data from soliq.uz...")

    data = await soliq.fetch_receipt_data(qr_url)
    if not data:
        await status.edit_text(
            "I read the QR code but could not fetch data from soliq.uz "
            "(the page may be down, or this QR is not a soliq.uz fiscal link).\n"
            f"QR content: {qr_url[:200]}"
        )
        return

    file_id = await db.save_image(uid, cropped_bytes, f"receipt_{uid}.png")
    receipt_doc = {
        "telegram_id": uid,
        "image_file_id": file_id,
        "date": data.get("date", ""),
        "vendor": data.get("vendor", ""),
        "receipt_number": data.get("receipt_number", ""),
        "vat_amount": data.get("vat_amount", 0.0),
        "total_amount": data.get("total_amount", 0.0),
        "soliq_url": qr_url,
        "raw_qr": qr_url,
    }
    inserted = await db.save_receipt(receipt_doc)
    if inserted is None:
        await status.edit_text(
            f"This receipt (#{data.get('receipt_number')}) is already in your records."
        )
        return

    count = await db.count_receipts(uid)
    await status.edit_text(
        f"Receipt added (#{count})\n\n"
        f"Vendor: {data.get('vendor', '-')}\n"
        f"Date: {data.get('date', '-')}\n"
        f"Receipt #: {data.get('receipt_number', '-')}\n"
        f"Total: {data.get('total_amount', 0):,.2f} UZS\n"
        f"VAT: {data.get('vat_amount', 0):,.2f} UZS"
    )


# ---------- App setup ----------

async def _post_init(app: Application) -> None:
    me = await app.bot.get_me()
    logger.info("Connected to Telegram as @%s", me.username)
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
            logger.info("MongoDB reachable and indexes ensured.")
            return
        except Exception:
            logger.exception("MongoDB startup check failed (attempt %s/3)", attempt)
            if attempt == 3:
                raise
            await asyncio.sleep(3)


def main() -> None:
    logger.info(
        "Starting VAT bot. Railway service=%s environment=%s",
        os.getenv("RAILWAY_SERVICE_NAME", ""),
        os.getenv("RAILWAY_ENVIRONMENT_NAME", ""),
    )
    _start_health_server()
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    app.add_error_handler(_on_error)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("setname", cmd_setname))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("export_vat", cmd_export_vat))
    app.add_handler(CommandHandler("export_pdf", cmd_export_pdf))
    app.add_handler(CommandHandler("reset", cmd_reset))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_photo))

    logger.info("Bot starting...")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
        logger.exception("Bot crashed during startup or polling.")
        raise


if __name__ == "__main__":
    main()
