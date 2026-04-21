# VAT Refund Telegram Bot

A Telegram bot that helps US Embassy employees in Uzbekistan collect receipts and file VAT refund requests automatically.

## What it does

1. User sends a photo of a receipt
2. Bot **auto-crops** the receipt from the photo background
3. Bot **decodes the QR code** on the receipt
4. Bot fetches **verified VAT data from soliq.uz** (Uzbekistan tax authority) — not from OCR, so the amounts are always correct
5. All receipts are stored per user in MongoDB (image + metadata)
6. On command, bot generates the official **VAT_Refund.xlsx** already filled in, or a **PDF** with all receipt images

## Commands

| Command | What it does |
|---|---|
| `/start` | Welcome + register |
| `/setname John Smith` | Set employee name used in exports |
| *(send photo)* | Add a receipt |
| `/list` | List all stored receipts |
| `/export_vat` | Download filled `VAT_Refund.xlsx` |
| `/export_pdf` | Download PDF package (summary + images) |
| `/reset` | Delete all receipts |
| `/help` | Show help |

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
│   ├── receipt_image.py    # Auto-crop + QR decode
│   ├── soliq.py            # Fetch verified VAT data from soliq.uz
│   └── exporter.py         # XLSX + PDF export
├── templates/
│   └── VAT_Refund.xlsx     # Official template
├── run_bot.sh              # One-command Mac setup & launcher
├── requirements.txt
├── .env.example
└── README.md
```

## Tech stack

- **python-telegram-bot 21** — bot framework (async)
- **MongoDB Community + Motor + GridFS** — local receipt storage (free, no cloud needed)
- **OpenCV + Pillow** — auto-crop receipts via contour detection + perspective warp
- **pyzbar / zxing-cpp** — QR code decoding
- **httpx + BeautifulSoup** — async scraping of soliq.uz / ofd.soliq.uz
- **openpyxl** — fill the official XLSX template
- **reportlab** — generate PDF package

## How the soliq.uz scraper works

Every Uzbek fiscal receipt has a QR code like:

```
https://ofd.soliq.uz/epul/?t=<terminal_id>&r=<receipt_no>&c=<date>&s=<amount>
```

The bot:
1. Decodes the QR with `pyzbar`
2. First tries the structured **JSON endpoint** at `ofd.soliq.uz/check`
3. Falls back to **HTML scraping** of the consumer-facing receipt page

This gives the **authoritative** VAT amount straight from the tax authority — far more reliable than OCR.

## Notes & limitations

- **Template capacity**: 120 receipts (30 per sheet × 4 sheets). The bot warns if you exceed this.
- **Duplicate detection**: the same receipt number can't be added twice per user.
- **soliq.uz availability**: if soliq.uz is down, the bot tells the user and doesn't save a broken record.
- **Privacy**: receipts are stored per Telegram user ID. Only you can see your receipts.
- **Geo-blocking**: the bot must run on a machine with a Uzbekistan IP. Cloud providers (AWS, Railway, Render, Fly.io) are blocked.
- **WiFi can't reach soliq.uz?** Some local WiFi ISPs block `ofd.soliq.uz` even inside Uzbekistan. Workarounds:
  1. **Easiest** — connect the Mac to your phone's cellular hotspot (or USB tether) and keep the bot running. No config change needed.
  2. **Mixed network** — set `SOLIQ_PROXY` in `.env` to an HTTP/HTTPS proxy reachable from the Mac. The bot will send *only* soliq.uz traffic through the proxy while WiFi handles Telegram/MongoDB:
     ```
     SOLIQ_PROXY=http://127.0.0.1:8888
     ```
     (For `socks5://...` also run `pip install httpx[socks]`.)
