"""Configuration loaded from environment variables."""
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Human support contact shown in welcome + failure messages. Set this to
# something clickable in Telegram ("@ShiraliDT") for best UX.
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "Shirali DT").strip()

# Comma-separated Telegram user IDs allowed to call hidden admin commands
# like /heartcheck. The command returns silently for everyone else, so
# leaving this empty effectively disables the admin surface.
ADMIN_TELEGRAM_IDS: set[int] = {
    int(x) for x in (os.getenv("ADMIN_TELEGRAM_IDS", "")).split(",") if x.strip().isdigit()
}

# Comma-separated Telegram user IDs of DT staff who approve new-user requests.
# When someone runs /start for the first time, these people receive a
# Telegram message with Approve/Deny buttons. Anyone not on this list cannot
# approve users, even if they are an ADMIN_TELEGRAM_IDS technical admin.
APPROVER_TELEGRAM_IDS: set[int] = {
    int(x) for x in (os.getenv("APPROVER_TELEGRAM_IDS", "")).split(",") if x.strip().isdigit()
}

# MongoDB
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.getenv("MONGODB_DB", "vat_bot")

# Soliq.uz scraping
SOLIQ_TIMEOUT = int(os.getenv("SOLIQ_TIMEOUT", "30"))
# Optional proxy for soliq.uz calls — use when the Mac's WiFi can't reach
# ofd.soliq.uz (e.g., ISP block). Point at a proxy that *can* (typically a
# proxy running on the user's phone over USB/hotspot). httpx-compatible:
#   http://user:pass@host:port   https://...   socks5://host:port
SOLIQ_PROXY = os.getenv("SOLIQ_PROXY", "").strip() or None
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
