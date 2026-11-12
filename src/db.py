"""
MongoDB database layer using Motor (async) + GridFS for image storage.

Collections:
  - users: { telegram_id, name, created_at }
  - receipts: {
        telegram_id, image_file_id (GridFS), date, vendor,
        printed_vendor, display_vendor,
        receipt_number, vat_amount, total_amount,
        soliq_url, raw_qr, created_at
    }
  - pending_receipts: {
        telegram_id, image_file_id (GridFS), created_at, expires_at
    }
"""
import asyncio
import logging
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional
from urllib.parse import parse_qs, urlparse

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket
from PIL import Image
from pillow_heif import register_heif_opener

from . import config

register_heif_opener()

logger = logging.getLogger("vat_bot.db")


class DuplicateClaimError(Exception):
    """Raised when a receipt's fiscal identity is already claimed by a
    DIFFERENT user. The same physical receipt must never be refunded
    twice, so the caller rejects the save and alerts the admins."""

    def __init__(self, fiscal_id: str, existing_telegram_id: int, new_telegram_id: int):
        self.fiscal_id = fiscal_id
        self.existing_telegram_id = existing_telegram_id
        self.new_telegram_id = new_telegram_id
        super().__init__(
            f"fiscal receipt {fiscal_id} already claimed by user "
            f"{existing_telegram_id}; rejected for user {new_telegram_id}"
        )


def fiscal_id_from_url(url: str) -> Optional[str]:
    """Canonical GLOBAL identity of a fiscal receipt.

    Built from the QR query string: terminal id (t), per-terminal receipt
    counter (r), fiscal timestamp (c), fiscal sign (s). A bare
    receipt_number is NOT globally unique — every cash terminal runs its
    own counter — so cross-user duplicate detection keys on this tuple.
    Parsing the params (rather than hashing the raw URL) means the same
    receipt reached via different soliq path variants (/check, /epi,
    /epul) still maps to one identity."""
    if not url:
        return None
    try:
        qs = parse_qs(urlparse(url).query)
        t = (qs.get("t") or [None])[0]
        r = (qs.get("r") or [None])[0]
        c = (qs.get("c") or [None])[0] or ""
        s = (qs.get("s") or [None])[0] or ""
        if t and r:
            return f"{t}:{r}:{c}:{s}"
    except Exception:
        pass
    return None

# JPEG quality used when storing receipt images in GridFS. 85 is visually
# indistinguishable from the original for receipt photos and drops storage
# by ~5-10× vs PNG — the difference between 30 users and 500+ users on a
# 100 GB disk budget.
_STORAGE_JPEG_QUALITY = 85


def _to_jpeg_bytes(image_bytes: bytes) -> bytes:
    """Re-encode any input image (HEIC/PNG/JPEG/etc.) to JPEG for storage.

    QR decoding and vendor OCR have already run by the time we call this, so
    the small quality loss is harmless for long-term storage.
    """
    pil = Image.open(BytesIO(image_bytes))
    if pil.mode != "RGB":
        pil = pil.convert("RGB")
    buf = BytesIO()
    pil.save(buf, format="JPEG", quality=_STORAGE_JPEG_QUALITY, optimize=True)
    return buf.getvalue()

_client: Optional[AsyncIOMotorClient] = None
_db = None
_fs: Optional[AsyncIOMotorGridFSBucket] = None


def get_db():
    global _client, _db, _fs
    if _client is None:
        _client = AsyncIOMotorClient(
            config.MONGODB_URI,
            serverSelectionTimeoutMS=config.MONGODB_SERVER_SELECTION_TIMEOUT_MS,
            maxPoolSize=32,
            minPoolSize=4,
            connectTimeoutMS=2000,
            socketTimeoutMS=15000,
            compressors="zstd,snappy,zlib",
        )
        _db = _client[config.MONGODB_DB]
        _fs = AsyncIOMotorGridFSBucket(_db)
    return _db


def get_fs() -> AsyncIOMotorGridFSBucket:
    get_db()
    return _fs


async def ensure_indexes() -> None:
    db = get_db()
    await db.users.create_index("telegram_id", unique=True)
    await db.users.create_index("status")
    await db.receipts.create_index([("telegram_id", 1), ("created_at", -1)])
    await _ensure_receipt_number_index(db)
    await db.pending_receipts.create_index("telegram_id", unique=True)
    await db.pending_receipts.create_index("expires_at", expireAfterSeconds=0)
    # Audit trail for /reset deletions. Queried by /report for
    # lifetime-processed totals — indexes match that access pattern.
    await db.deleted_receipts_log.create_index([("deleted_at", -1)])
    await db.deleted_receipts_log.create_index("telegram_id")
    # GLOBAL uniqueness of the fiscal receipt identity — the hard
    # guarantee that no two users can claim the same physical receipt.
    # sparse: manual entries have no QR and therefore no fiscal_id.
    # If historical cross-user duplicates predate this index, creation
    # fails; fall back to a non-unique index (fast lookups for the
    # app-level check in save_receipt) and log loudly. Once the human
    # resolves the old duplicates, the next restart upgrades to unique
    # automatically.
    try:
        await db.receipts.create_index(
            "fiscal_id", unique=True, sparse=True, name="fiscal_id_unique"
        )
        try:
            await db.receipts.drop_index("fiscal_id_lookup")
        except Exception:
            pass  # lookup index only exists if we previously fell back
    except Exception:
        logger.warning(
            "Could not create UNIQUE fiscal_id index — pre-existing "
            "cross-user duplicate claims in the receipts collection. "
            "Falling back to non-unique index; save_receipt's app-level "
            "check still blocks new double-claims. Resolve the old "
            "duplicates and restart to upgrade."
        )
        await db.receipts.create_index("fiscal_id", name="fiscal_id_lookup")


_LEGACY_NUMBER_INDEX = "telegram_id_1_receipt_number_1"
_MANUAL_NUMBER_INDEX = "manual_receipt_number_unique"


async def _ensure_receipt_number_index(db) -> None:
    """Per-user uniqueness of the receipt NUMBER — for manual entries only.

    Until V.13 this was a unique index over every receipt: one user
    could not hold two receipts with the same number. But a receipt
    number is just a cash terminal's own counter, so two unrelated
    receipts from two shops can share one — and the second was refused
    as "already in your records". In the Nov 2026 data 36 numbers were
    in use by more than one terminal; for a user with 200+ receipts the
    odds of having lost a legitimate receipt this way were about 1 in 6.

    Receipts read from a QR/link don't need this rule: they carry a
    fiscal identity (terminal + number + timestamp + sign) with its own
    unique index. Hand-typed entries have nothing else to go by, so the
    rule stays for them — enforced by this partial index and by the
    number checks in save_receipt.

    ROLLING BACK to pre-V.13 code: that code recreates the old index at
    startup, which fails (and stops the bot from starting) as soon as
    any user holds two receipts with the same number. See HANDOVER.md.
    """
    try:
        existing = await db.receipts.index_information()
        legacy = existing.get(_LEGACY_NUMBER_INDEX)
        if legacy is not None and legacy.get("unique"):
            await db.receipts.drop_index(_LEGACY_NUMBER_INDEX)
            logger.info(
                "Dropped the all-receipts unique index on (user, receipt "
                "number); the rule now applies to manual entries only."
            )
        await db.receipts.create_index(
            [("telegram_id", 1), ("receipt_number", 1)],
            unique=True,
            partialFilterExpression={"manual": True},
            name=_MANUAL_NUMBER_INDEX,
        )
    except Exception:
        # Not fatal: save_receipt's own number checks still apply.
        logger.exception(
            "Could not set up the manual-entry receipt-number index; "
            "continuing with application-level checks only."
        )


async def migrate_backfill_fiscal_ids() -> int:
    """One-time (idempotent) backfill: compute fiscal_id for receipts
    saved before cross-user duplicate detection existed. Returns the
    number of receipts updated. Runs on every startup; after the first
    pass the $exists filter matches nothing and this is a no-op."""
    db = get_db()
    updated = 0
    async for r in db.receipts.find(
        {"fiscal_id": {"$exists": False}}, {"soliq_url": 1, "raw_qr": 1}
    ):
        fid = fiscal_id_from_url(r.get("soliq_url") or r.get("raw_qr") or "")
        if not fid:
            continue  # manual entry — no QR, nothing to key on
        try:
            await db.receipts.update_one(
                {"_id": r["_id"]}, {"$set": {"fiscal_id": fid}}
            )
            updated += 1
        except Exception:
            # Unique index already live and this row collides with an
            # earlier claim — leave it without fiscal_id and surface it.
            logger.warning(
                "fiscal_id backfill collision for receipt %s (fiscal %s)",
                r["_id"], fid,
            )
    return updated


async def migrate_legacy_users_to_approved() -> int:
    """Pre-seed access control: any user already in the DB before access
    control was introduced is grandfathered in as approved. Idempotent —
    running it again is a no-op once everyone has a status."""
    db = get_db()
    result = await db.users.update_many(
        {"status": {"$exists": False}},
        {"$set": {"status": "approved", "approved_at": datetime.utcnow(), "approved_by": "legacy_migration"}},
    )
    return result.modified_count


async def ping() -> None:
    db = get_db()
    await db.command("ping")


async def upsert_user(telegram_id: int, name: str) -> None:
    """Create a user record on first contact, but never overwrite the name
    the user set themselves via /setname. `name` here is the Telegram
    display name — only useful as a default; exports rely on the name the
    user explicitly chose.

    Note: this never sets `status`. Access control routes new users through
    register_pending_user() in /start. Callers that just want to refresh the
    display name on photo/text handlers can keep using upsert_user — but the
    gate (_require_approved) will block unapproved users before any work
    happens."""
    db = get_db()
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {
            "$setOnInsert": {
                "name": name,
                "created_at": datetime.utcnow(),
            },
        },
        upsert=True,
    )


async def register_pending_user(telegram_id: int, name: str, username: Optional[str]) -> str:
    """Record a /start from a stranger. Returns the resulting status:
    'approved' / 'pending' / 'denied' / 'new_pending' (just inserted).

    Existing users keep their status — re-running /start never resets a
    denial or downgrades an approval. New users are inserted with
    status='pending' and an approval request is sent by the caller."""
    db = get_db()
    existing = await db.users.find_one({"telegram_id": telegram_id})
    if existing and existing.get("status"):
        return existing["status"]
    now = datetime.utcnow()
    if existing:
        # Legacy row without a status that somehow slipped past migration —
        # treat it as approved (same grandfather rule).
        await db.users.update_one(
            {"telegram_id": telegram_id},
            {"$set": {"status": "approved", "approved_at": now, "approved_by": "legacy_implicit"}},
        )
        return "approved"
    await db.users.insert_one({
        "telegram_id": telegram_id,
        "name": name,
        "username": username,
        "status": "pending",
        "requested_at": now,
        "created_at": now,
    })
    return "new_pending"


async def get_user_status(telegram_id: int) -> Optional[str]:
    """Return 'approved' / 'pending' / 'denied' / None (= unknown user)."""
    db = get_db()
    doc = await db.users.find_one({"telegram_id": telegram_id}, {"status": 1})
    return doc.get("status") if doc else None


async def set_user_status(telegram_id: int, status: str, approver_id: Optional[int]) -> bool:
    """Approve or deny a user. Returns True if a row was actually updated."""
    db = get_db()
    field = "approved" if status == "approved" else "denied"
    result = await db.users.update_one(
        {"telegram_id": telegram_id},
        {"$set": {
            "status": status,
            f"{field}_at": datetime.utcnow(),
            f"{field}_by": approver_id,
        }},
    )
    return result.modified_count > 0


async def list_pending_users() -> list[dict]:
    db = get_db()
    cursor = db.users.find({"status": "pending"}).sort("requested_at", 1)
    return [u async for u in cursor]


async def ensure_approver_user(telegram_id: int, name: str) -> None:
    """Lazily insert/upgrade a DT approver's user row to status=approved.
    Called whenever an approver interacts with the bot — guarantees they
    can use receipt commands without going through the approval queue.
    Never overwrites an existing user's name (user-set name wins via /setname)."""
    db = get_db()
    now = datetime.utcnow()
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {
            "$set": {
                "status": "approved",
                "approved_at": now,
                "approved_by": "approver_whitelist",
            },
            "$setOnInsert": {
                "name": name,
                "created_at": now,
            },
        },
        upsert=True,
    )


async def claim_notice(telegram_id: int, version: int) -> bool:
    """Record that this user is being shown the licence / no-warranty
    notice. Returns True exactly once per user — the caller sends the
    notice only then. Atomic, so several messages arriving together
    (a photo album) cannot each win."""
    db = get_db()
    result = await db.users.update_one(
        {"telegram_id": telegram_id, "notice_shown_at": {"$exists": False}},
        {"$set": {"notice_shown_at": datetime.utcnow(), "notice_version": version}},
    )
    return result.modified_count == 1


async def unclaim_notice(telegram_id: int) -> None:
    """Undo claim_notice when the notice could not actually be sent, so
    the user is shown it on their next message instead of never."""
    db = get_db()
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {"$unset": {"notice_shown_at": "", "notice_version": ""}},
    )


async def get_user(telegram_id: int) -> Optional[dict]:
    db = get_db()
    return await db.users.find_one({"telegram_id": telegram_id})


async def set_user_name(telegram_id: int, name: str) -> None:
    db = get_db()
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {"$set": {"name": name}},
        upsert=True,
    )


async def save_image(
    telegram_id: int, image_bytes: bytes, filename: str, *, reencode: bool = True
) -> str:
    """Re-encode to JPEG and save to GridFS. Returns the file id as str.

    Accepts any image format the bot receives (iPhone HEIC, Telegram JPEG,
    OpenCV PNG). Storing as JPEG keeps the on-disk footprint small so the
    Mac SSD can host many users.

    `reencode=False` stores the bytes untouched. Only for JPEGs the bot
    produced itself (the soliq.uz screenshot): decoding and re-compressing
    those costs time and a little sharpness for nothing. User photos are
    always re-encoded — that is also what strips their EXIF metadata
    (camera model, GPS position) before storage.
    """
    if reencode or image_bytes[:3] != b"\xff\xd8\xff":
        loop = asyncio.get_running_loop()
        jpeg_bytes = await loop.run_in_executor(None, _to_jpeg_bytes, image_bytes)
    else:
        jpeg_bytes = image_bytes

    if not filename.lower().endswith((".jpg", ".jpeg")):
        root = filename.rsplit(".", 1)[0] if "." in filename else filename
        filename = f"{root}.jpg"

    fs = get_fs()
    file_id = await fs.upload_from_stream(
        filename,
        jpeg_bytes,
        metadata={"telegram_id": telegram_id, "content_type": "image/jpeg"},
    )
    return str(file_id)


async def save_pending_receipt(telegram_id: int, image_bytes: bytes, filename: str) -> str:
    db = get_db()
    await delete_pending_receipt(telegram_id)
    file_id = await save_image(telegram_id, image_bytes, filename)
    now = datetime.utcnow()
    await db.pending_receipts.update_one(
        {"telegram_id": telegram_id},
        {
            "$set": {
                "telegram_id": telegram_id,
                "image_file_id": file_id,
                "created_at": now,
                "expires_at": now + timedelta(hours=24),
            }
        },
        upsert=True,
    )
    return file_id


async def get_pending_receipt(telegram_id: int) -> Optional[dict]:
    db = get_db()
    return await db.pending_receipts.find_one({"telegram_id": telegram_id})


async def delete_pending_receipt(telegram_id: int) -> int:
    db = get_db()
    fs = get_fs()
    pending = await db.pending_receipts.find_one({"telegram_id": telegram_id})
    if pending and pending.get("image_file_id"):
        try:
            from bson import ObjectId

            await fs.delete(ObjectId(pending["image_file_id"]))
        except Exception:
            pass
    result = await db.pending_receipts.delete_one({"telegram_id": telegram_id})
    return result.deleted_count


async def detach_pending_receipt(telegram_id: int) -> int:
    """Remove the pending_receipts row but KEEP the GridFS image — the caller
    has just transferred ownership of the image to a saved receipt (e.g. a
    /manual entry that adopts the photo from a failed QR scan)."""
    db = get_db()
    result = await db.pending_receipts.delete_one({"telegram_id": telegram_id})
    return result.deleted_count


async def save_receipt(doc: dict) -> Optional[str]:
    """Insert a receipt. Returns inserted id, or None when the SAME user
    already has this receipt saved (same fiscal identity, or — where a
    hand-typed entry is involved — same receipt number).

    Raises DuplicateClaimError when a DIFFERENT user has already claimed
    the same fiscal receipt (matched on the QR's terminal/receipt#/
    timestamp/fiscal-sign tuple). The tax office refunds each fiscal
    receipt at most once, so a second claim — e.g. two colleagues
    photographing the same lunch receipt — must be rejected loudly, not
    silently deduped."""
    db = get_db()
    doc = {**doc, "created_at": datetime.utcnow()}

    # Identity: the one soliq itself reported (doc["fiscal_id"], set by
    # the caller from the verified data) wins over the one read off the
    # link, because link parameters can be altered without soliq
    # minding — see soliq._canonical_fiscal_id. Both are checked, so a
    # receipt stored under either form is recognised.
    url_fid = fiscal_id_from_url(doc.get("soliq_url") or doc.get("raw_qr") or "")
    fid = doc.get("fiscal_id") or url_fid
    doc.pop("fiscal_id", None)
    if fid:
        doc["fiscal_id"] = fid
        existing = await db.receipts.find_one(
            {"fiscal_id": {"$in": list(dict.fromkeys([fid, url_fid or fid]))}},
            {"telegram_id": 1},
        )
        if existing:
            if existing.get("telegram_id") != doc.get("telegram_id"):
                raise DuplicateClaimError(
                    fid, existing.get("telegram_id"), doc.get("telegram_id")
                )
            return None  # same user re-scanning their own receipt

    # Receipt-NUMBER rule (per user). The number alone identifies a
    # receipt only when there is nothing better to go by:
    #   * a hand-typed entry (or any receipt without a fiscal identity)
    #     may not reuse a number the user already has;
    #   * a QR/link receipt may not reuse the number of one of the
    #     user's hand-typed entries — that is how the same paper receipt
    #     would otherwise be claimed twice, once typed and once scanned.
    # Two QR/link receipts with the same number are simply two receipts
    # from two different shops; their fiscal identities tell them apart.
    number = doc.get("receipt_number")
    if number:
        clash_filter = {"telegram_id": doc.get("telegram_id"), "receipt_number": number}
        if fid and not doc.get("manual"):
            clash_filter["manual"] = True
        if await db.receipts.find_one(clash_filter, {"_id": 1}):
            return None

    try:
        result = await db.receipts.insert_one(doc)
        return str(result.inserted_id)
    except Exception as e:
        # Duplicate key on the manual-entry number index (two identical
        # manual entries racing past the check above), or on
        # fiscal_id_unique if two saves raced past the find_one.
        msg = str(e).lower()
        if "duplicate key" in msg:
            if fid and "fiscal_id" in msg:
                other = await db.receipts.find_one(
                    {"fiscal_id": fid}, {"telegram_id": 1}
                )
                if other and other.get("telegram_id") != doc.get("telegram_id"):
                    raise DuplicateClaimError(
                        fid, other.get("telegram_id"), doc.get("telegram_id")
                    ) from e
            return None
        raise


async def find_receipt_by_number(telegram_id: int, receipt_number: str) -> Optional[dict]:
    """Return the existing receipt matching this user + receipt_number, if any.
    Used by the online-purchase flow and the random-URL inquiry handler to
    tell users when a receipt is already saved."""
    if not receipt_number:
        return None
    db = get_db()
    return await db.receipts.find_one(
        {"telegram_id": telegram_id, "receipt_number": receipt_number}
    )


async def list_receipts(telegram_id: int) -> list[dict]:
    """A user's receipts, oldest receipt date first.

    The tie-breakers matter: /export_vat and /export_pdf each call this
    separately and number the rows 1..N. Sorting by date alone leaves the
    order of same-day receipts up to MongoDB, so row 17 in the Excel form
    was not guaranteed to be receipt 17 in the PDF."""
    db = get_db()
    cursor = db.receipts.find({"telegram_id": telegram_id}).sort(
        [("date", 1), ("created_at", 1), ("_id", 1)]
    )
    return [doc async for doc in cursor]


async def receipt_totals(telegram_id: int) -> tuple[int, float]:
    """(number of receipts, total VAT in UZS) for one user."""
    db = get_db()
    rows = await db.receipts.aggregate([
        {"$match": {"telegram_id": telegram_id}},
        {"$group": {
            "_id": None,
            "count": {"$sum": 1},
            "vat": {"$sum": {"$ifNull": ["$vat_amount", 0]}},
        }},
    ]).to_list(1)
    if not rows:
        return 0, 0.0
    return int(rows[0]["count"] or 0), float(rows[0]["vat"] or 0)


async def find_receipt_by_fiscal_id(*fiscal_ids: Optional[str]) -> Optional[dict]:
    """Who (if anyone) already holds this fiscal receipt. Lets the bot
    answer "already saved" / "already claimed" straight from the QR
    link, before spending a soliq.uz lookup on it. Accepts several
    candidate ids (link-derived and soliq-reported)."""
    ids = [f for f in dict.fromkeys(fiscal_ids) if f]
    if not ids:
        return None
    db = get_db()
    return await db.receipts.find_one(
        {"fiscal_id": {"$in": ids}},
        {
            "telegram_id": 1, "receipt_number": 1, "created_at": 1,
            "vendor": 1, "display_vendor": 1, "vat_amount": 1,
        },
    )


async def count_receipts(telegram_id: int) -> int:
    db = get_db()
    return await db.receipts.count_documents({"telegram_id": telegram_id})


async def delete_all_receipts(telegram_id: int) -> int:
    """Delete all of a user's receipts AND their GridFS images. Returns count.

    Before purging, write each receipt's accounting fields to
    `deleted_receipts_log` so historical totals (/report) can still
    include amounts a user removed via /reset. Only metadata is kept —
    no image bytes, no soliq URL — keeping the log small and avoiding
    any re-identification risk."""
    from bson import ObjectId
    db = get_db()
    fs = get_fs()
    receipts = await list_receipts(telegram_id)

    now = datetime.utcnow()
    log_docs = []
    for r in receipts:
        log_docs.append({
            "telegram_id": telegram_id,
            "original_id": r.get("_id"),
            "fiscal_id": r.get("fiscal_id"),
            "receipt_number": r.get("receipt_number", ""),
            "date": r.get("date", ""),
            "vat_amount": float(r.get("vat_amount") or 0),
            "total_amount": float(r.get("total_amount") or 0),
            "manual": bool(r.get("manual")),
            "online_purchase": bool(r.get("online_purchase")),
            "created_at": r.get("created_at"),
            "deleted_at": now,
        })
    if log_docs:
        try:
            await db.deleted_receipts_log.insert_many(log_docs)
        except Exception:
            # Audit log is best-effort — never let it block a /reset.
            pass

    for r in receipts:
        # Drop EVERY image file the receipt references. Missing any one
        # field (as the original code did with soliq_screenshot_file_id)
        # leaks GridFS blobs and inflates disk usage forever.
        for field in ("image_file_id", "qr_image_file_id", "soliq_screenshot_file_id"):
            fid = r.get(field)
            if fid:
                try:
                    await fs.delete(ObjectId(fid))
                except Exception:
                    pass
    result = await db.receipts.delete_many({"telegram_id": telegram_id})
    return result.deleted_count


async def get_image(file_id: str) -> bytes:
    from bson import ObjectId
    fs = get_fs()
    stream = await fs.open_download_stream(ObjectId(file_id))
    return await stream.read()


async def get_images(file_ids: list[str], concurrency: int = 8) -> dict[str, bytes]:
    """Load several GridFS images at once. Missing or unreadable files
    are simply absent from the result — callers fall back per image."""
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, bytes] = {}

    async def _one(file_id: str) -> None:
        async with sem:
            try:
                out[file_id] = await get_image(file_id)
            except Exception:
                logger.warning("Could not load image %s", file_id, exc_info=True)

    await asyncio.gather(*(_one(f) for f in dict.fromkeys(file_ids) if f))
    return out


async def image_sizes(file_ids: list[Optional[str]]) -> dict[str, int]:
    """Stored size in bytes of each GridFS image (one query, no image
    data read). Unknown ids are simply absent."""
    from bson import ObjectId
    oids = []
    for file_id in file_ids:
        if not file_id:
            continue
        try:
            oids.append(ObjectId(file_id))
        except Exception:
            continue
    if not oids:
        return {}
    db = get_db()
    return {
        str(doc["_id"]): int(doc.get("length") or 0)
        async for doc in db.fs.files.find({"_id": {"$in": oids}}, {"length": 1})
    }


async def delete_images(file_ids: list[Optional[str]]) -> None:
    """Best-effort removal of GridFS files that ended up unreferenced
    (e.g. images stored for a receipt that then turned out to be a
    duplicate)."""
    from bson import ObjectId
    fs = get_fs()
    for file_id in file_ids:
        if not file_id:
            continue
        try:
            await fs.delete(ObjectId(file_id))
        except Exception:
            pass


async def cleanup_orphaned_images() -> int:
    from bson import ObjectId

    db = get_db()
    fs = get_fs()
    referenced: set[ObjectId] = set()

    async for rec in db.receipts.find(
        {},
        {"image_file_id": 1, "qr_image_file_id": 1, "soliq_screenshot_file_id": 1},
    ):
        # All THREE image file ids a receipt can carry. Forgetting any
        # one of these makes the nightly cleanup treat valid blobs as
        # orphans — that bug deleted every soliq.uz screenshot saved
        # before 2026-05-20 startup (V.10 added the field but this
        # query didn't).
        for field in ("image_file_id", "qr_image_file_id", "soliq_screenshot_file_id"):
            file_id = rec.get(field)
            if not file_id:
                continue
            try:
                referenced.add(ObjectId(file_id))
            except Exception:
                continue

    async for rec in db.pending_receipts.find({}, {"image_file_id": 1}):
        file_id = rec.get("image_file_id")
        if not file_id:
            continue
        try:
            referenced.add(ObjectId(file_id))
        except Exception:
            continue

    deleted = 0
    async for entry in db.fs.files.find({}, {"_id": 1}):
        file_id = entry["_id"]
        if file_id in referenced:
            continue
        try:
            await fs.delete(file_id)
            deleted += 1
        except Exception:
            continue
    return deleted
