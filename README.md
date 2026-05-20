# Tashkent Embassy VAT Refund Bot — V12

A Telegram bot that helps US Embassy employees in Uzbekistan collect receipts and file VAT refund requests automatically. Access is gated to approved staff via a DT-managed approval queue.

## What it does

1. User sends a photo of a receipt (close-up of just the QR code is enough — the tax office takes the original paper receipt)
2. Bot **auto-crops** the receipt from the photo background
3. Bot **decodes the QR code** with a multi-decoder stack (zxing-cpp, OpenCV classic + Aruco, pyzbar) with EXIF-orientation handling, multi-border quiet-zone restoration, and blur-triggered deblur passes
4. Bot fetches **verified VAT data from soliq.uz** (Uzbekistan tax authority) — not from OCR, so the amounts are always correct
5. Bot **captures a headless-Chromium screenshot of the soliq.uz page** at save time (clipped to the totals row, map widget removed) — this is the visual record used in the PDF export, pixel-perfect against the website
6. If the QR is unreadable, the user can fall back to `/manual` and type the amounts in
7. All receipts are stored per user in MongoDB (screenshot + photo + metadata via GridFS)
8. On command, bot generates the official **VAT_Refund.xlsx** already filled in (split by calendar year when receipts span multiple years), or a **PDF package** with one page per receipt: the soliq.uz screenshot on the left and a large scannable QR (encoding the soliq URL) plus the identity block on the right, so the tax office can verify any receipt by scanning the printed page

## Commands

| Command | What it does |
|---|---|
| `/start` | Welcome + register (or request access if first-time) |
| `/setname John Smith` | Set employee name used in exports |
| *(send photo)* | Add a receipt |
| `/manual` | Add a receipt by hand when the QR can't be read |
| `/online_purchase` | Add an online-purchase receipt from a soliq.uz URL (no photo needed) |
| `/cancel_manual` | Abort a manual entry in progress |
| `/cancel_online` | Abort an online-purchase entry in progress |
| `/cancel_pending` | Discard a receipt waiting for a QR close-up |
| `/list` | List all stored receipts |
| `/export_vat` | Download filled `VAT_Refund.xlsx` (one file per year, split into copies of 120 if needed) |
| `/export_pdf` | Download PDF package (summary cover + one page per receipt: soliq.uz screenshot + scannable QR) |
| `/reset` | Delete all receipts |
| `/help` | Show help |

**Pasting a soliq.uz URL into the chat (no other context)** triggers an inquiry: the bot fetches the receipt data and either tells you it's already saved (with the original save date) or shows the data and points you at `/online_purchase` (for online buys) or sending a photo (for physical receipts).

### Hidden commands

- **DT approvers** (`APPROVER_TELEGRAM_IDS`): `/pending` lists access requests, `/approve <user_id>` and `/deny <user_id>` resolve them via command (in addition to the inline buttons sent on each new request).
- **Technical admins** (`ADMIN_TELEGRAM_IDS`): `/heartcheck` returns a health snapshot (DB reachable, uptime, GridFS size, backup age, today's error count). `/report` returns a per-user VAT-savings report across the full receipt history — total VAT refundable, per-user breakdown sorted by savings, receipt counts and date ranges.
- **Anyone**: `/whoami` replies with the caller's Telegram ID — used to collect IDs from new DT staff before adding them to `APPROVER_TELEGRAM_IDS`.

All hidden commands silently no-op for anyone not on the relevant list — they don't appear in the Telegram command menu.

## Access control

V6 added a DT-managed approval queue so the bot is restricted to approved staff.

- **First /start**: a stranger lands in a `pending` queue and sees *"Request submitted to DT. You will get a message here when approved."*
- **DT approvers** receive a Telegram message with the requester's name, username, Telegram ID, and **✅ Approve** / **❌ Deny** inline buttons. Whoever taps first wins; the message edits to show *"Approved by …"* so the others see it's handled. There's no race — the bot rejects the second click as already handled.
- **Approved**: the user gets a Telegram ping ("You're approved!") and can immediately use the bot.
- **Denied**: the user gets a polite "contact DT" message and remains unable to use any other command.
- **Approvers bypass the queue entirely** — anyone in `APPROVER_TELEGRAM_IDS` is treated as approved by definition. Their user row is lazily upserted with `status=approved` on first interaction so they can use receipt commands without going through approval themselves.
- **Backwards-compatible migration**: when V6 starts for the first time on an existing database, every pre-existing user is grandfathered in as approved. Nobody who was already using the bot is interrupted.

### Onboarding new DT approvers

1. Ask the new approver to message the bot with `/whoami` once.
2. Add their Telegram ID to `APPROVER_TELEGRAM_IDS` in `.env`, comma-separated:
   ```
   APPROVER_TELEGRAM_IDS=7962068286,85189405,1234567890
   ```
3. Restart the bot (`launchctl kickstart -k "gui/$(id -u)/com.vatbot.bot"` if running under launchd).

## Running on Mac (recommended)

The bot runs best on a Mac physically located in Uzbekistan. `ofd.soliq.uz` geo-blocks all cloud provider IPs — it only responds to Uzbekistan ISP addresses.

### First-time setup

```bash
git clone https://github.com/TashkentFreedom250/vat-bot.git
cd vat-bot

# Copy and fill in your bot token
cp .env.example .env
# Edit .env — set TELEGRAM_BOT_TOKEN (from @BotFather on Telegram)
# Leave MONGODB_URI as mongodb://localhost:27017

# Run — installs everything automatically on first launch
bash run_bot.sh
```

`run_bot.sh` handles:
- Installing Python 3.11 (via Homebrew)
- Installing & starting MongoDB locally (free, no Atlas account needed)
- Installing all Python dependencies into a `.venv`
- Starting the bot

### Subsequent launches

```bash
bash run_bot.sh
```

MongoDB starts automatically on Mac login, so the bot will always have a database ready.

### Getting a bot token

1. Talk to **@BotFather** on Telegram
2. `/newbot` → follow the prompts
3. Copy the token into `TELEGRAM_BOT_TOKEN` in `.env`

## Project layout

```
vat-bot/
├── src/
│   ├── bot.py              # Telegram handlers + entry point
│   ├── config.py           # Env loading
│   ├── db.py               # MongoDB + GridFS (async)
│   ├── receipt_image.py    # Auto-crop + multi-decoder QR pipeline (zxing, OpenCV, Aruco, pyzbar)
│   ├── receipt_ocr.py      # Optional OCR fallback (RapidOCR)
│   ├── online_snapshot.py  # Synthetic PNG for /online_purchase entries
│   ├── soliq.py            # Fetch verified VAT data from soliq.uz (JSON + HTML, items + meta)
│   ├── soliq_screenshot.py # Headless-Chromium capture of the soliq.uz page (Playwright)
│   ├── exporter.py         # XLSX + PDF export (year-split, screenshot+QR per page)
│   └── maintenance.py      # Nightly backup + disk/log housekeeping
├── templates/
│   └── VAT_Refund.xlsx     # Official template
├── scripts/
│   ├── com.vatbot.bot.plist  # launchd unit for auto-start on login
│   └── build_tutorial.py     # Generates the user-facing PDF tutorial
├── logs/                   # Daily-rotating bot.log files
├── backups/                # mongodump snapshots (one per day)
├── run_bot.sh              # One-command Mac setup & launcher
├── requirements.txt
├── .env.example
└── README.md
```

## Tech stack

- **python-telegram-bot 21** — bot framework (async)
- **MongoDB Community + Motor + GridFS** — local receipt storage (free, no cloud needed)
- **OpenCV + Pillow + pillow-heif** — auto-crop receipts via contour detection + perspective warp; HEIC support for iPhone photos; EXIF-orientation handling for QR close-ups
- **zxing-cpp + OpenCV QR detectors (classic + Aruco) + pyzbar** — three independent QR decoders running in parallel; multi-border quiet-zone restoration and blur-triggered unsharp passes for close-up phone shots
- **RapidOCR + ONNX Runtime** — OCR fallback for the soliq URL itself when no decoder can find the QR
- **httpx + BeautifulSoup + lxml** — async scraping of soliq.uz / ofd.soliq.uz (header + items + STIR + time)
- **Playwright (headless Chromium)** — captures a pixel-perfect screenshot of the soliq.uz receipt page at save time; reused across all receipts via a long-running shared browser
- **qrcode** — generates the per-receipt verification QR embedded in the PDF
- **openpyxl** — fill the official XLSX template
- **reportlab (Platypus + KeepInFrame)** — generate PDF package with one receipt per A4 page
- **APScheduler** — nightly self-maintenance jobs

## Self-managed service

V12 runs as a self-maintaining service on the host Mac. No external monitor or cron is required.

- **Nightly backup** at 03:30 UTC: `mongodump` snapshot under `backups/`, one folder per day. Old snapshots are pruned automatically (the most recent 7 are kept).
- **Startup catch-up**: if the Mac was asleep at 03:30 and the latest backup is older than 24 h, a backup runs as soon as the bot starts.
- **Disk check**: each maintenance run logs free space and warns if the volume drops below 10 GB free.
- **Daily log rotation**: `logs/bot.log` rotates every night at midnight; old days stay as `bot.log.YYYY-MM-DD`.
- **Auto-start on login**: `scripts/com.vatbot.bot.plist` is a ready-made launchd unit. Copy it to `~/Library/LaunchAgents/` and `launchctl load` it once — the bot will start at every Mac login and restart if it crashes.

## How the soliq.uz scraper works

Every Uzbek fiscal receipt has a QR code like:

```
https://ofd.soliq.uz/epul/?t=<terminal_id>&r=<receipt_no>&c=<date>&s=<amount>
```

The bot:
1. Decodes the QR via a multi-decoder pipeline (`zxing-cpp` + OpenCV + Aruco + `pyzbar`)
2. First tries the structured **JSON endpoint** at `ofd.soliq.uz/check`
3. Falls back to **HTML scraping** of the consumer-facing receipt page, extracting header + line items + STIR + terminal + time
4. **Captures a headless-Chromium screenshot** of the soliq.uz page (clipped to the totals row, map widget removed) — stored in GridFS as the visual record for the PDF export

This gives the **authoritative** VAT amount straight from the tax authority — far more reliable than OCR — plus a pixel-perfect visual that the tax office can verify by scanning the per-receipt QR in the printed PDF.

## Notes & limitations

- **Template capacity**: 120 receipts per workbook (30 per sheet × 4 sheets). When receipts span more than one calendar year, `/export_vat` produces one workbook per year, each with its own running totals on the continuation sheets. If a single year exceeds 120 receipts, the year is split into multiple workbooks (`VAT_Refund_2026_copy_1_of_3.xlsx`, `…_copy_2_of_3.xlsx`, `…_copy_3_of_3.xlsx`) with continuous row numbers (1–120, 121–240, …) so the copies read as one document if stapled together.
- **Online purchases**: `/online_purchase` accepts a soliq.uz URL with no photo. The bot fetches the verified VAT data, renders a clean fallback snapshot PNG, and also captures the live soliq.uz page screenshot — both are stored in GridFS so finance has a visual record matching what's on the website.
- **Duplicate detection**: the same receipt number can't be added twice per user.
- **soliq.uz availability**: if soliq.uz is down, the bot tells the user and doesn't save a broken record. `/manual` is the fallback when a QR genuinely can't be read.
- **Privacy**: receipts are stored per Telegram user ID. Only you can see your receipts.
- **Geo-blocking**: the bot must run on a machine with a Uzbekistan IP. Cloud providers (AWS, Railway, Render, Fly.io) are blocked.
- **WiFi can't reach soliq.uz?** Some local WiFi ISPs block `ofd.soliq.uz` even inside Uzbekistan. Workarounds:
  1. **Easiest** — connect the Mac to your phone's cellular hotspot (or USB tether) and keep the bot running. No config change needed.
  2. **Mixed network** — set `SOLIQ_PROXY` in `.env` to an HTTP/HTTPS proxy reachable from the Mac. The bot will send *only* soliq.uz traffic through the proxy while WiFi handles Telegram/MongoDB:
     ```
     SOLIQ_PROXY=http://127.0.0.1:8888
     ```
     (For `socks5://...` also run `pip install httpx[socks]`.)
