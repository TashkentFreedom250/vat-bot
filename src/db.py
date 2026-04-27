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
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket
from PIL import Image
from pillow_heif import register_heif_opener

from . import config

register_heif_opener()

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
    await db.receipts.create_index([("telegram_id", 1), ("created_at", -1)])
    await db.receipts.create_index(
        [("telegram_id", 1), ("receipt_number", 1)], unique=True, sparse=True
    )
    await db.pending_receipts.create_index("telegram_id", unique=True)
    await db.pending_receipts.create_index("expires_at", expireAfterSeconds=0)


async def ping() -> None:
    db = get_db()
    await db.command("ping")


async def upsert_user(telegram_id: int, name: str) -> None:
    """Create a user record on first contact, but never overwrite the name
    the user set themselves via /setname. `name` here is the Telegram
    display name — only useful as a default; exports rely on the name the
    user explicitly chose."""
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


async def save_image(telegram_id: int, image_bytes: bytes, filename: str) -> str:
    """Re-encode to JPEG and save to GridFS. Returns the file id as str.

    Accepts any image format the bot receives (iPhone HEIC, Telegram JPEG,
    OpenCV PNG). Storing as JPEG keeps the on-disk footprint small so the
    Mac SSD can host many users.
    """
    loop = asyncio.get_running_loop()
    jpeg_bytes = await loop.run_in_executor(None, _to_jpeg_bytes, image_bytes)

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
    """Insert a receipt. Returns inserted id, or None if duplicate receipt_number."""
    db = get_db()
    doc = {**doc, "created_at": datetime.utcnow()}
    try:
        result = await db.receipts.insert_one(doc)
        return str(result.inserted_id)
    except Exception as e:
        # Likely duplicate key on (telegram_id, receipt_number)
        if "duplicate key" in str(e).lower():
            return None
        raise


async def list_receipts(telegram_id: int) -> list[dict]:
    db = get_db()
    cursor = db.receipts.find({"telegram_id": telegram_id}).sort("date", 1)
    return [doc async for doc in cursor]


async def count_receipts(telegram_id: int) -> int:
    db = get_db()
    return await db.receipts.count_documents({"telegram_id": telegram_id})


async def delete_all_receipts(telegram_id: int) -> int:
    """Delete all of a user's receipts AND their GridFS images. Returns count."""
    from bson import ObjectId
    db = get_db()
    fs = get_fs()
    receipts = await list_receipts(telegram_id)
    for r in receipts:
        for field in ("image_file_id", "qr_image_file_id"):
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


async def cleanup_orphaned_images() -> int:
    from bson import ObjectId

    db = get_db()
    fs = get_fs()
    referenced: set[ObjectId] = set()

    async for rec in db.receipts.find({}, {"image_file_id": 1, "qr_image_file_id": 1}):
        for field in ("image_file_id", "qr_image_file_id"):
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
