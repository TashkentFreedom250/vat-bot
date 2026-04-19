# VAT Refund Telegram Bot 🧾

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

## Tech stack

- **python-telegram-bot 21** — bot framework (async)
- **MongoDB + Motor + GridFS** — receipt metadata + image storage
- **OpenCV + Pillow** — auto-crop receipts via contour detection + perspective warp
- **pyzbar** — QR code decoding
- **httpx + BeautifulSoup** — async scraping of soliq.uz / ofd.soliq.uz
- **openpyxl** — fill the official XLSX template
- **reportlab** — generate PDF package

## Project layout

```
vat_bot/
├── src/
│   ├── bot.py              # Telegram handlers + entry point
│   ├── config.py           # Env loading
│   ├── db.py               # MongoDB + GridFS (async)
│   ├── receipt_image.py    # Auto-crop + QR decode
│   ├── soliq.py            # Fetch verified VAT data from soliq.uz
│   └── exporter.py         # XLSX + PDF export
├── templates/
│   └── VAT_Refund.xlsx     # Official template
├── requirements.txt
├── Dockerfile              # For Railway deployment
├── Procfile                # For Heroku (if you prefer)
├── railway.json
├── .env.example
└── README.md
```

## Quick deploy (office PC)

```bash
git clone https://github.com/TashkentFreedom250/vat-bot.git
cd vat-bot
pip install -r requirements.txt
# copy your .env file across (it has TELEGRAM_BOT_TOKEN, MONGODB_URI, etc.)
python -m src.bot
```

> **Important:** This bot must run on a machine physically located in Uzbekistan.
> `ofd.soliq.uz` (the Uzbekistan tax authority) geo-blocks all cloud provider IPs (AWS, Railway, Render, Fly.io, etc.).
> It only responds to Uzbekistan ISP IPs. Running it on an office PC in Tashkent works perfectly.

## Local setup (first time)

```bash
# 1. Clone and enter the project
git clone https://github.com/TashkentFreedom250/vat-bot.git
cd vat-bot

# 2. Install system deps (for pyzbar and opencv)
# Ubuntu/Debian:
sudo apt-get install libzbar0 libgl1

# 3. Python deps
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env and fill in TELEGRAM_BOT_TOKEN and MONGODB_URI

# 5. Run
python -m src.bot
```

### Getting a bot token

1. Talk to **@BotFather** on Telegram
2. `/newbot` → follow the prompts
3. Copy the token into `TELEGRAM_BOT_TOKEN` in `.env`

### Getting MongoDB

Since you already know MongoDB, the easiest free path:
1. Sign up at **MongoDB Atlas** (free tier: 512 MB, plenty for this)
2. Create a cluster → Database Access → add a user
3. Network Access → allow `0.0.0.0/0` (or Railway's IPs)
4. Copy the connection string into `MONGODB_URI` in `.env`

## Cloud deployment

> **Not recommended** — `ofd.soliq.uz` geo-blocks all cloud provider IPs (AWS, Railway, Render, Fly.io, Cloudflare).
> Requests time out or return HTTP 522. Run the bot on a PC inside Uzbekistan instead.
>
> If you later get a Uzbekistan-based VPS (e.g. Comnet.uz, Uztelecom hosting, ~$5–10/month),
> it will work fine there too — just clone and run as above.

## How the soliq.uz scraper works

Every Uzbek fiscal receipt has a QR code like:

```
https://ofd.soliq.uz/epul/?t=<terminal_id>&r=<receipt_no>&c=<date>&s=<amount>
```

The bot:
1. Decodes the QR with `pyzbar`
2. First tries the structured **JSON endpoint** at `ofd.soliq.uz/check`
3. Falls back to **HTML scraping** of the consumer-facing receipt page

This is much more reliable than OCR'ing the paper receipt — you get the **authoritative** VAT amount straight from the tax authority.

## Notes & limitations

- **Template capacity**: 120 receipts (30 per sheet × 4 sheets). The bot warns if you exceed this.
- **Duplicate detection**: the same `receipt_number` can't be added twice per user.
- **soliq.uz availability**: if soliq.uz is down, the bot tells the user and doesn't save a broken record.
- **Privacy**: receipts are stored per Telegram user ID. Only you can see your receipts.

## Future ideas

- OCR fallback when the QR can't be decoded
- Automatic currency conversion to USD using CBU.uz rates
- Multi-language UI (Uzbek / Russian / English)
- `/delete <n>` to remove a single receipt
- Export by date range
