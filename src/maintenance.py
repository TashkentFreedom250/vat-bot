"""Self-managed maintenance tasks that run inside the bot process.

The bot handles its own backups, log retention, and disk monitoring so the
operator only ever has to start the bot program.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from . import config

logger = logging.getLogger("vat_bot.maintenance")

BACKUP_DIR = config.BASE_DIR / "backups"
LOG_DIR = config.BASE_DIR / "logs"

KEEP_BACKUPS = 7
DISK_ALERT_PCT = 85
# If the most recent backup is older than this, run one at startup so we
# never miss a nightly window when the Mac was asleep or the bot crashed.
CATCHUP_HOURS = 25
# launchd's stdout/stderr capture files don't rotate. Cap each at this size
# so years of benign warnings can't quietly fill the disk or hide a real
# crash dump in noise.
LAUNCHD_LOG_MAX_BYTES = 100 * 1024


def _mongodump_path() -> str | None:
    return (
        shutil.which("mongodump")
        or ("/opt/homebrew/bin/mongodump" if Path("/opt/homebrew/bin/mongodump").exists() else None)
        or ("/usr/local/bin/mongodump" if Path("/usr/local/bin/mongodump").exists() else None)
    )


def _latest_backup() -> Path | None:
    if not BACKUP_DIR.exists():
        return None
    backups = sorted(
        (p for p in BACKUP_DIR.iterdir() if p.is_dir() and p.name.startswith("vat_bot_")),
        key=lambda p: p.name,
        reverse=True,
    )
    return backups[0] if backups else None


async def _run_backup() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    mongodump = _mongodump_path()
    if not mongodump:
        logger.warning("mongodump not found — skipping backup")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = BACKUP_DIR / f"vat_bot_{stamp}"

    proc = await asyncio.create_subprocess_exec(
        mongodump,
        "--db", config.MONGODB_DB,
        "--gzip",
        "--out", str(out),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error("mongodump failed: %s", stderr.decode(errors="replace").strip())
        shutil.rmtree(out, ignore_errors=True)
        return

    size_mb = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1024 / 1024
    logger.info("Backup created: %s (%.1f MB)", out.name, size_mb)


def _prune_old_backups() -> None:
    if not BACKUP_DIR.exists():
        return
    backups = sorted(
        (p for p in BACKUP_DIR.iterdir() if p.is_dir() and p.name.startswith("vat_bot_")),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in backups[KEEP_BACKUPS:]:
        shutil.rmtree(old, ignore_errors=True)
        logger.info("Pruned old backup: %s", old.name)


def _truncate_launchd_logs() -> None:
    """If launchd's stdout/stderr capture files have grown past the cap,
    keep the tail (last 30 KB) and drop the rest. Real diagnostics live in
    bot.log — these files only catch pre-logging crashes."""
    keep_bytes = 30 * 1024
    for name in ("launchd.err.log", "launchd.out.log"):
        path = LOG_DIR / name
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        if size <= LAUNCHD_LOG_MAX_BYTES:
            continue
        try:
            with path.open("rb") as f:
                f.seek(max(0, size - keep_bytes))
                tail = f.read()
            path.write_bytes(tail)
            logger.info("Truncated %s (%d → %d bytes)", name, size, len(tail))
        except Exception:
            logger.exception("Failed to truncate %s", name)


def _check_disk() -> None:
    total, used, free = shutil.disk_usage("/")
    pct = int(used * 100 / total)
    free_gb = free / 1024**3
    if pct >= DISK_ALERT_PCT:
        logger.warning(
            "SSD %s%% full (%.1f GB free) — MongoDB can corrupt when the disk fills. Free up space.",
            pct, free_gb,
        )
    else:
        logger.info("Disk OK: %s%% full, %.1f GB free", pct, free_gb)


async def run_nightly(_context=None) -> None:
    """Scheduled nightly job: backup, prune, disk check."""
    logger.info("Nightly maintenance starting.")
    try:
        await _run_backup()
        _prune_old_backups()
        _truncate_launchd_logs()
        _check_disk()
    except Exception:
        logger.exception("Nightly maintenance failed")
    logger.info("Nightly maintenance finished.")


async def run_startup_catchup(_context=None) -> None:
    """If the last backup is stale, run one now so missed nights self-heal."""
    latest = _latest_backup()
    now = datetime.now()
    if latest is None:
        logger.info("No prior backup found — running initial backup.")
        await run_nightly()
        return
    try:
        stamp = datetime.strptime(latest.name.replace("vat_bot_", ""), "%Y%m%d_%H%M%S")
    except ValueError:
        logger.info("Could not parse latest backup timestamp — running a fresh backup.")
        await run_nightly()
        return
    age = now - stamp
    if age > timedelta(hours=CATCHUP_HOURS):
        logger.info("Latest backup is %s old — running catch-up backup.", age)
        await run_nightly()
    else:
        logger.info("Latest backup is fresh (%s old). No catch-up needed.", age)
