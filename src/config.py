"""Configuration loaded from environment variables."""
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# MongoDB
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.getenv("MONGODB_DB", "vat_bot")

# Soliq.uz scraping
SOLIQ_TIMEOUT = int(os.getenv("SOLIQ_TIMEOUT", "30"))
MONGODB_SERVER_SELECTION_TIMEOUT_MS = int(
    os.getenv("MONGODB_SERVER_SELECTION_TIMEOUT_MS", "10000")
)

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = BASE_DIR / "templates" / "VAT_Refund.xlsx"
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required. Set it in your .env file.")


def redacted_mongodb_uri() -> str:
    """Return a log-safe MongoDB URI with credentials removed."""
    uri = MONGODB_URI or ""
    if "://" not in uri:
        return uri
    scheme, rest = uri.split("://", 1)
    if "@" not in rest:
        return uri
    return f"{scheme}://<redacted>@{rest.split('@', 1)[1]}"


def mongodb_host_hint() -> str:
    """Return a best-effort hostname hint for diagnostics."""
    try:
        parsed = urlparse(MONGODB_URI)
        return parsed.hostname or "unknown"
    except Exception:
        return "unknown"
