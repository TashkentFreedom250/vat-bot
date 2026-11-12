"""
Receipt image processing:
  - Auto-crop the receipt from the background (largest bright quadrilateral)
  - Detect & decode the soliq.uz QR code

How the QR search is organised (find_qr_url), and why
-----------------------------------------------------
Measured on 240 real receipt photos (Nov 2026):

  * 92% decode on the first try in ~0.15 s.
  * The rest used to take 1-27 s, and an unreadable photo churned for
    20-30 s before the user was told so. Profiling showed where it
    went: OpenCV's detector burned ~75% of the time and pyzbar ~22%,
    while zxing — 4% of the time — was the one that found most of the
    difficult codes. The old loop ran all three decoders on each image
    variant before moving to the next variant, so the cheap, productive
    decoder kept waiting behind the slow one.

So the search now runs in two steps:

  1. Quick pass: the photo as sent, a few border/contrast variants,
     fastest decoder first, stop at the first hit.
  2. Deep search, only if that misses: several "lanes" run side by
     side — zoom in on wherever a QR-like pattern is detected, zxing
     over every variant, pyzbar over every variant, OpenCV over every
     variant, and the same again on an auto-cropped copy. The first
     lane to find the link wins and the others are told to stop. The
     whole thing is capped by DEEP_SEARCH_BUDGET_S.

Every preprocessing variant of the old pipeline is still tried; only
the order changed, plus the zoom lane which is new.
"""
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from io import BytesIO
from typing import Callable, Iterator, Optional

import cv2
import numpy as np
import zxingcpp
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from .soliq_url import find_soliq_url

try:
    from pyzbar import pyzbar as _pyzbar
    _HAS_PYZBAR = True
except Exception:
    _pyzbar = None
    _HAS_PYZBAR = False

logger = logging.getLogger("vat_bot.qr")

register_heif_opener()

# Wall-clock cap for the deep search (seconds). The slowest genuine
# decode seen in the benchmark needed ~7 s of OpenCV work; everything
# past 10 s was time spent on photos that never decoded at all.
DEEP_SEARCH_BUDGET_S = float(os.getenv("QR_DEEP_SEARCH_BUDGET_S", "10"))

# Worker threads for the deep-search lanes. The decoders are native
# code that releases the GIL, so lanes really do run in parallel.
_LANE_POOL = ThreadPoolExecutor(
    max_workers=max(5, os.cpu_count() or 4),
    thread_name_prefix="qr-lane",
)

# One OpenCV detector PER THREAD. A single module-level instance used
# to be shared by every worker thread at once; cv2 detector objects
# carry internal state and are not documented as thread-safe. Given
# this bot's history of native OpenCV crashes (see below), each thread
# gets its own.
_tls = threading.local()


def _qr_detector() -> "cv2.QRCodeDetector":
    detector = getattr(_tls, "qr_detector", None)
    if detector is None:
        detector = cv2.QRCodeDetector()
        _tls.qr_detector = detector
    return detector


# NOTE: cv2.QRCodeDetectorAruco is intentionally NOT used. Its native
# marker-candidate search corrupted the heap on a real phone photo
# (SIGTRAP inside cv::aruco::ArucoDetectorImpl::identifyCandidates,
# production crash 2026-08-09) — the second cv2 native crash after
# detectAndDecodeCurved (2026-05-14). C++ crashes kill the whole
# process and cannot be caught from Python. zxing-cpp + the classic
# OpenCV detector + pyzbar cover the same inputs safely.


def _to_cv(image_bytes: bytes) -> np.ndarray:
    """Load bytes into an OpenCV BGR image, honoring EXIF orientation.

    Phone cameras encode rotation as an EXIF tag rather than rotating the
    pixels (so the JPEG payload is unchanged on landscape vs portrait
    shots). cv2.imdecode ignores EXIF — a portrait close-up of a QR code
    comes back sideways, and every later region/rotation step has to
    work around it. Going through PIL with ImageOps.exif_transpose
    applies the rotation up front so the rest of the pipeline sees the
    image the way the user took it.

    We still use the PIL path even when OpenCV could decode the bytes,
    because the EXIF tag is what determines orientation regardless of
    format. The cost is one extra decode-encode, well worth it for the
    capture-rate uplift on close-up phone shots.
    """
    try:
        pil = Image.open(BytesIO(image_bytes))
        pil = ImageOps.exif_transpose(pil)
        pil = pil.convert("RGB")
        return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    except Exception:
        # Fallback: raw OpenCV decode without EXIF awareness.
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            return img
        raise


def _order_points(pts: np.ndarray) -> np.ndarray:
    """Order 4 points: top-left, top-right, bottom-right, bottom-left."""
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def _four_point_transform(image: np.ndarray, pts: np.ndarray) -> np.ndarray:
    rect = _order_points(pts)
    (tl, tr, br, bl) = rect
    widthA = np.linalg.norm(br - bl)
    widthB = np.linalg.norm(tr - tl)
    maxWidth = max(int(widthA), int(widthB))
    heightA = np.linalg.norm(tr - br)
    heightB = np.linalg.norm(tl - bl)
    maxHeight = max(int(heightA), int(heightB))
    dst = np.array(
        [[0, 0], [maxWidth - 1, 0], [maxWidth - 1, maxHeight - 1], [0, maxHeight - 1]],
        dtype="float32",
    )
    M = cv2.getPerspectiveTransform(rect, dst)
    return cv2.warpPerspective(image, M, (maxWidth, maxHeight))


def auto_crop_receipt(image_bytes: bytes) -> bytes:
    """
    Detect the receipt in the image and return a cropped, deskewed PNG.

    Strategy:
      1. Find the largest bright ~rectangular region (the receipt paper).
      2. If a 4-point contour is found, apply perspective warp.
      3. Otherwise, fall back to a bounding-box crop of the bright region.
      4. If nothing reliable is detected, return the original image as PNG.
    """
    cropped, _changed = _auto_crop_array(_to_cv(image_bytes))
    return _to_png_bytes(cropped)


def _auto_crop_array(img: np.ndarray) -> tuple[np.ndarray, bool]:
    """auto_crop_receipt on decoded pixels. Returns (image, changed);
    `changed` is False when no receipt outline was found and the input
    is handed back as-is. Working on arrays spares the QR search a PNG
    encode + decode round trip (0.4 s on a typical photo, 5 s on a
    12-megapixel one)."""
    orig = img
    h, w = img.shape[:2]

    # Resize for faster processing while keeping aspect ratio
    scale = 1000.0 / max(h, w)
    small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1 else img.copy()

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    # Threshold: receipts are bright (white paper) vs. darker background
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Clean up
    kernel = np.ones((5, 5), np.uint8)
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return orig, False

    # Largest contour = probably the receipt
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]
    receipt_contour = None
    for c in contours:
        peri = cv2.arcLength(c, True)
        # Slightly looser epsilon catches receipts with curved/tapered edges
        approx = cv2.approxPolyDP(c, 0.03 * peri, True)
        if len(approx) == 4 and cv2.contourArea(c) > (small.shape[0] * small.shape[1]) * 0.1:
            receipt_contour = approx
            break

    if receipt_contour is not None:
        pts = receipt_contour.reshape(4, 2).astype("float32") / scale if scale < 1 else receipt_contour.reshape(4, 2).astype("float32")
        # Expand corners outward so the warp doesn't clip receipt edges
        pts = _expand_corners(pts, margin=0.025)
        pts[:, 0] = np.clip(pts[:, 0], 0, orig.shape[1] - 1)
        pts[:, 1] = np.clip(pts[:, 1], 0, orig.shape[0] - 1)
        warped = _four_point_transform(orig, pts)
        warped = _fix_orientation(warped)
        return warped, True

    # Fallback: bounding box of largest contour
    c = contours[0]
    if cv2.contourArea(c) < (small.shape[0] * small.shape[1]) * 0.1:
        return orig, False
    x, y, cw, ch = cv2.boundingRect(c)
    if scale < 1:
        x, y, cw, ch = int(x / scale), int(y / scale), int(cw / scale), int(ch / scale)
    pad = 40
    x = max(0, x - pad); y = max(0, y - pad)
    cw = min(orig.shape[1] - x, cw + 2 * pad)
    ch = min(orig.shape[0] - y, ch + 2 * pad)
    cropped = orig[y:y + ch, x:x + cw]
    cropped = _fix_orientation(cropped)
    return cropped, True


def _expand_corners(pts: np.ndarray, margin: float = 0.025) -> np.ndarray:
    """Push 4 corner points outward from centroid by margin fraction."""
    centroid = pts.mean(axis=0)
    return pts + (pts - centroid) * margin


def _fix_orientation(img: np.ndarray) -> np.ndarray:
    """Rotate 180° when the soliq.uz QR is in the top half (upside-down photo)."""
    h = img.shape[0]
    top = img[:h // 2, :]
    try:
        detector = _qr_detector()
        data, _, _ = detector.detectAndDecode(top)
        if data and find_soliq_url(data):
            bot = img[h // 2:, :]
            b_data, _, _ = detector.detectAndDecode(bot)
            if not (b_data and find_soliq_url(b_data)):
                return cv2.rotate(img, cv2.ROTATE_180)
    except Exception:
        pass
    return img


def _to_png_bytes(cv_img: np.ndarray) -> bytes:
    """Convert a BGR OpenCV image to PNG bytes."""
    rgb = cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)
    buf = BytesIO()
    pil.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _normalize_payload(text: str) -> str:
    return " ".join(str(text).strip().split())


def _append_unique(found: list[str], values: list[str]) -> None:
    for value in values:
        normalized = _normalize_payload(value)
        if normalized and normalized not in found:
            found.append(normalized)


def _with_border(img: np.ndarray, size: int = 24) -> np.ndarray:
    value = 255 if img.ndim == 2 else (255, 255, 255)
    return cv2.copyMakeBorder(
        img, size, size, size, size, cv2.BORDER_CONSTANT, value=value
    )


def _resize_variant(img: np.ndarray, scale: int, interpolation: int) -> np.ndarray:
    h, w = img.shape[:2]
    return cv2.resize(img, (w * scale, h * scale), interpolation=interpolation)


def _downscale_for_decode(img: np.ndarray, max_dim: int = 1800) -> np.ndarray:
    h, w = img.shape[:2]
    current_max = max(h, w)
    if current_max <= max_dim:
        return img
    scale = max_dim / current_max
    return cv2.resize(
        img,
        (max(1, int(w * scale)), max(1, int(h * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _decode_with_opencv(img: np.ndarray) -> list[str]:
    results: list[str] = []
    for detector in (_qr_detector(),):
        try:
            data, _, _ = detector.detectAndDecode(img)
            if data:
                _append_unique(results, [data])
        except Exception:
            pass

        try:
            ok, infos, _, _ = detector.detectAndDecodeMulti(img)
            if ok:
                _append_unique(results, [info for info in infos if info])
        except Exception:
            pass

    # NOTE: cv2.QRCodeDetector.detectAndDecodeCurved() is intentionally
    # NOT called. In OpenCV 4.10 it has a null-pointer deref inside
    # cv::QRDecode::straightenQRCodeInParts that segfaults the whole
    # Python process on certain phone close-ups — Python try/except
    # can't catch a SIGSEGV in a C extension. We had a production
    # crash on 2026-05-14 traced to this exact frame and pulled the
    # call. zxing's curved/perspective handling + the Aruco detector
    # cover the same cases without crashing.
    return results


def _decode_with_pyzbar(img: np.ndarray) -> list[str]:
    """pyzbar (libzbar) is a third independent decoder. It often succeeds
    on phone close-ups where zxing and OpenCV both miss — different
    finder-pattern logic, different binarization. Trying it costs ~5–15ms
    per call, well worth the capture-rate uplift on close-ups."""
    if not _HAS_PYZBAR:
        return []
    results: list[str] = []
    try:
        # libzbar takes grayscale; converting once here is cheaper than
        # letting pyzbar re-do it under the hood.
        if img.ndim == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = img
        decoded = _pyzbar.decode(gray, symbols=[_pyzbar.ZBarSymbol.QRCODE])
        for d in decoded:
            try:
                text = d.data.decode("utf-8", errors="ignore")
            except Exception:
                continue
            if text:
                _append_unique(results, [text])
    except Exception:
        pass
    return results


def _decode_with_zxing(
    img: np.ndarray, *, allow_internal_downscale: bool = False
) -> list[str]:
    results: list[str] = []
    for binarizer in (
        zxingcpp.Binarizer.LocalAverage,
        zxingcpp.Binarizer.GlobalHistogram,
    ):
        try:
            decoded = zxingcpp.read_barcodes(
                img,
                formats=zxingcpp.BarcodeFormat.QRCode,
                try_rotate=True,
                # Internal downscaling helps when the QR is tiny in-frame, but
                # it can be less reliable on already-good full receipt photos.
                # Keep it for the rescue pass instead of every decode attempt.
                try_downscale=allow_internal_downscale,
                try_invert=True,
                binarizer=binarizer,
            )
        except Exception:
            continue
        _append_unique(results, [barcode.text for barcode in decoded if barcode.text])
    return results


def _decode_image(
    img: np.ndarray, *, zxing_allow_downscale: bool = False
) -> list[str]:
    """Try the QR decoders, cheapest first, and return unique payloads.

    Stops as soon as one of them yields a soliq.uz link: zxing takes
    ~10 ms and settles the typical photo on its own, so there is no
    reason to also run pyzbar and OpenCV (~100 ms together) on it."""
    results: list[str] = []
    _append_unique(
        results,
        _decode_with_zxing(img, allow_internal_downscale=zxing_allow_downscale),
    )
    if _soliq_payload(results):
        return results
    _append_unique(results, _decode_with_pyzbar(img))
    if _soliq_payload(results):
        return results
    _append_unique(results, _decode_with_opencv(img))
    return results


def _candidate_regions(img: np.ndarray, aggressive: bool) -> list[np.ndarray]:
    """Return region crops likely to contain the QR.

    The old logic assumed a full receipt photo with the QR near the
    bottom. Close-up shots break that assumption: the QR is usually
    centered, sometimes off-center, but rarely confined to the bottom
    half. This adds center crops and quadrant crops so the decoder gets
    a chance at the QR even when the user framed it tight or slightly
    off-center.
    """
    h, w = img.shape[:2]
    regions = [img]
    # Close-up oriented crops: tight center, looser center, and a small
    # off-center pad. Each picks out the QR while excluding shadow/glare
    # at the photo edges, which is the most common close-up failure mode.
    closeup_crops = [
        img[int(h * 0.10):int(h * 0.90), int(w * 0.10):int(w * 0.90)],  # 80%
        img[int(h * 0.20):int(h * 0.80), int(w * 0.20):int(w * 0.80)],  # 60%
        img[int(h * 0.05):int(h * 0.70), int(w * 0.05):int(w * 0.95)],  # upper-biased
        img[int(h * 0.30):int(h * 0.95), int(w * 0.05):int(w * 0.95)],  # lower-biased
    ]
    legacy_crops = (
        [
            img[int(h * 0.45):, :],
            img[int(h * 0.60):, :],
            img[int(h * 0.45):, int(w * 0.15):int(w * 0.85)],
            img[h // 2:, :w // 2],
            img[h // 2:, w // 2:],
        ]
        if not aggressive
        else [
            img[int(h * 0.60):, :],
            img[int(h * 0.70):, :],
            img[int(h * 0.55):, int(w * 0.2):int(w * 0.8)],
            img[int(h * 0.35):, int(w * 0.1):int(w * 0.9)],
        ]
    )
    for region in closeup_crops + legacy_crops:
        if region.size:
            regions.append(region)
    return regions


def _lab_normalized(region: np.ndarray) -> np.ndarray:
    """CLAHE on the L channel of LAB — evens out shadows from phone photos
    without crushing QR contrast like grayscale CLAHE can."""
    lab = cv2.cvtColor(region, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(l)
    return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)


def _variant_groups(
    region: np.ndarray, aggressive: bool, low_quality: bool
) -> list[np.ndarray]:
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    sharp = cv2.addWeighted(clahe, 1.6, cv2.GaussianBlur(clahe, (0, 0), 2), -0.6, 0)
    lab_norm = _lab_normalized(region)

    if not aggressive:
        variants: list[np.ndarray] = [
            region,
            _with_border(region),
            lab_norm,
            clahe,
            _with_border(clahe),
        ]
        if max(region.shape[:2]) < 1600:
            variants.extend(
                [
                    _with_border(_resize_variant(region, 2, cv2.INTER_CUBIC)),
                    _with_border(_resize_variant(gray, 2, cv2.INTER_CUBIC)),
                ]
            )
        if low_quality:
            variants.extend(
                [
                    _with_border(_resize_variant(gray, 2, cv2.INTER_CUBIC)),
                    _with_border(_resize_variant(clahe, 2, cv2.INTER_CUBIC)),
                ]
            )
        return variants

    thresh_gauss = cv2.adaptiveThreshold(
        clahe,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        4,
    )
    variants = [
        sharp,
        _with_border(sharp),
        _with_border(thresh_gauss),
    ]
    if low_quality and max(region.shape[:2]) < 1200:
        variants.extend(
            [
                _with_border(_resize_variant(clahe, 3, cv2.INTER_CUBIC)),
                _with_border(_resize_variant(thresh_gauss, 3, cv2.INTER_NEAREST)),
            ]
        )
    return variants


def _soliq_payload(found: list[str]) -> Optional[str]:
    """The soliq.uz receipt link among decoded QR payloads, if any.

    Receipts often carry other QR codes too (payment, delivery, ads).
    The check is on the link's HOST, not on the text containing
    "soliq.uz" somewhere — see soliq_url for why that matters."""
    for value in found:
        url = find_soliq_url(value)
        if url:
            return url
    return None


def _decode_stages(aggressive: bool) -> list[bool]:
    return [False, True] if aggressive else [False]


def _rotations(img: np.ndarray) -> list[np.ndarray]:
    """The four cardinal rotations of an image — phone shots come in at any angle."""
    return [
        img,
        cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE),
        cv2.rotate(img, cv2.ROTATE_180),
        cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE),
    ]


def _laplacian_blur_score(img: np.ndarray) -> float:
    """Variance of the Laplacian — a cheap focus/sharpness estimator.

    Lower = more blurry. Phone close-ups with shaky hands or autofocus
    misses come back around 30–60; tack-sharp shots are 200+. We use
    this to gate extra deblur passes only when the image actually needs
    them, so good photos stay fast."""
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())
    except Exception:
        return 1000.0  # treat unknown as not-blurry


def _deblur_variants(img: np.ndarray) -> list[np.ndarray]:
    """Three unsharp-mask intensities for blurry close-ups. Light, medium,
    and aggressive — each picks up a different blur character (focus
    miss vs. motion vs. handshake)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    blurred = cv2.GaussianBlur(gray, (0, 0), 2)
    variants = [
        cv2.addWeighted(gray, 1.5, blurred, -0.5, 0),
        cv2.addWeighted(gray, 2.0, blurred, -1.0, 0),
        cv2.addWeighted(gray, 2.6, cv2.GaussianBlur(gray, (0, 0), 3), -1.6, 0),
    ]
    return variants


def _fast_variants(hires: np.ndarray) -> list[np.ndarray]:
    """The handful of variants that settle nearly every decent photo:
    the frame itself, three border thicknesses (restores the quiet zone
    on tight close-ups) and one shadow-balanced copy."""
    return [
        hires,
        _with_border(hires, size=24),
        _with_border(hires, size=60),
        _with_border(hires, size=120),
        _lab_normalized(hires),
    ]


def _fast_pass(hires: np.ndarray) -> Optional[str]:
    found: list[str] = []
    for variant in _fast_variants(hires):
        _append_unique(found, _decode_image(variant))
        soliq = _soliq_payload(found)
        if soliq:
            return soliq
    return None


def extract_qr_url_fast(image_bytes: bytes) -> Optional[str]:
    """Quick pass only (~0.1-0.2 s). None means "not on the first try",
    not "no QR" — follow up with find_qr_url / extract_qr_url."""
    native = _to_cv(image_bytes)
    return _fast_pass(_downscale_for_decode(native, max_dim=2500))


# Tags on sweep variants: which decoders they are meant for.
_STD = "std"            # every decoder
_ROTATED = "rot"        # every decoder; zxing with internal downscale
_ZXING_DOWN = "zxdown"  # zxing only, with internal downscale


def _sweep_variants(
    native: np.ndarray, hires: np.ndarray, *, low_quality_hint: bool
) -> Iterator[tuple[np.ndarray, str]]:
    """Every preprocessing variant of the deep search, lazily, in the
    order the stages were introduced: blur rescue, region scan, heavy
    preprocessing, rotations, zxing downscale rescue.

    Lazy on purpose — the full set is ~100 images, and each lane walks
    it independently, so nothing is built that isn't about to be tried.
    """
    # Blur rescue: only when the focus score says the photo is soft.
    blur_score = _laplacian_blur_score(hires)
    if blur_score < 120:
        for deblurred in _deblur_variants(hires):
            yield _with_border(deblurred, size=40), _STD

    # Region scan, raw + one shadow-balanced pass per region.
    for region in _candidate_regions(hires, aggressive=False)[:5]:
        yield region, _STD
        yield _lab_normalized(region), _STD

    # Heavy preprocessing.
    img = _downscale_for_decode(native)
    aggressive = low_quality_hint or min(img.shape[:2]) < 1000
    for stage in _decode_stages(aggressive):
        for region in _candidate_regions(img, aggressive=stage):
            for variant in _variant_groups(
                region, aggressive=stage, low_quality=aggressive
            ):
                yield variant, _STD

    # Rotations: EXIF is applied at load, but sideways shots still happen.
    for rotated in _rotations(img)[1:]:
        yield rotated, _ROTATED

    # ZXing's internal downscaling helps when the QR is huge in frame.
    for region in _candidate_regions(hires, aggressive=True)[1:]:
        yield region, _ZXING_DOWN


def _sweep(
    variants: Iterator[tuple[np.ndarray, str]],
    decoder: str,
    stop: Callable[[], bool],
) -> Optional[str]:
    """Run ONE decoder over the variant stream until it finds the
    soliq.uz link, the stream ends, or `stop()` says time is up."""
    for variant, tag in variants:
        if stop():
            return None
        if decoder == "zxing":
            payloads = _decode_with_zxing(
                variant, allow_internal_downscale=(tag != _STD)
            )
        elif tag == _ZXING_DOWN:
            continue
        elif decoder == "pyzbar":
            payloads = _decode_with_pyzbar(variant)
        else:
            payloads = _decode_with_opencv(variant)
        soliq = _soliq_payload(payloads)
        if soliq:
            return soliq
    return None


def _locate_qr_boxes(img: np.ndarray) -> list[tuple[float, float, float, float]]:
    """Where OpenCV's detector thinks QR codes are, without decoding
    them. Finding the three finder squares is far more forgiving than
    reading the code, so this often succeeds on photos where every
    decode attempt on the full frame fails."""
    h, w = img.shape[:2]
    boxes: list[tuple[float, float, float, float]] = []
    detector = _qr_detector()
    for scale in (1.0, 0.5):
        src = (
            img
            if scale == 1.0
            else cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        )
        for method in ("detectMulti", "detect"):
            try:
                ok, points = getattr(detector, method)(src)
            except Exception:
                continue
            if not ok or points is None:
                continue
            quads = np.array(points, dtype=np.float32).reshape(-1, 4, 2) / scale
            for quad in quads:
                x0, y0 = quad.min(axis=0)
                x1, y1 = quad.max(axis=0)
                side = max(x1 - x0, y1 - y0)
                if side < 20 or side > max(h, w):
                    continue
                box = (float(x0), float(y0), float(x1), float(y1))
                if not any(
                    all(abs(a - b) < 15 for a, b in zip(box, seen)) for seen in boxes
                ):
                    boxes.append(box)
    return boxes


def _zoom_search(img: np.ndarray, stop: Callable[[], bool]) -> Optional[str]:
    """Locate the QR, cut it out with some margin, enlarge it, decode.

    The usual reason a full-frame decode fails is that the code is a
    small part of a long receipt — a couple of pixels per module. Once
    we know where it is, enlarging just that patch is cheap and gives
    the decoders something they can read. In the benchmark this found
    8 of the 19 slow-or-failing photos in under half a second each."""
    h, w = img.shape[:2]
    for x0, y0, x1, y1 in _locate_qr_boxes(img)[:3]:
        side = max(x1 - x0, y1 - y0)
        for margin in (0.30, 0.60):
            if stop():
                return None
            m = side * margin
            patch = img[
                int(max(0, y0 - m)):int(min(h, y1 + m)),
                int(max(0, x0 - m)):int(min(w, x1 + m)),
            ]
            if patch.size == 0:
                continue
            longest = max(patch.shape[:2])
            factors = [max(1.0, min(4.0, 640.0 / longest))]
            bigger = max(1.0, min(4.0, 1300.0 / longest))
            if bigger > factors[0] * 1.3:
                factors.append(bigger)
            for factor in factors:
                zoomed = (
                    cv2.resize(patch, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
                    if factor > 1.05
                    else patch
                )
                gray = cv2.cvtColor(zoomed, cv2.COLOR_BGR2GRAY)
                clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
                sharp = cv2.addWeighted(
                    clahe, 1.6, cv2.GaussianBlur(clahe, (0, 0), 2), -0.6, 0
                )
                _, otsu = cv2.threshold(
                    cv2.GaussianBlur(gray, (3, 3), 0), 0, 255,
                    cv2.THRESH_BINARY + cv2.THRESH_OTSU,
                )
                adaptive = cv2.adaptiveThreshold(
                    clahe, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                    cv2.THRESH_BINARY, 31, 4,
                )
                for variant in (zoomed, clahe, sharp, otsu, adaptive):
                    if stop():
                        return None
                    soliq = _soliq_payload(
                        _decode_image(
                            _with_border(variant, 24), zxing_allow_downscale=True
                        )
                    )
                    if soliq:
                        return soliq
    return None


def _search_all_decoders(
    native: np.ndarray,
    hires: np.ndarray,
    stop: Callable[[], bool],
    *,
    low_quality_hint: bool,
) -> Optional[str]:
    """Zoom, then one decoder at a time over every variant — cheapest
    decoder first. Used for the cropped copy and by extract_qr_url."""
    soliq = _zoom_search(hires, stop)
    if soliq:
        return soliq
    for decoder in ("zxing", "pyzbar", "opencv"):
        if stop():
            return None
        soliq = _sweep(
            _sweep_variants(native, hires, low_quality_hint=low_quality_hint),
            decoder, stop,
        )
        if soliq:
            return soliq
    return None


def find_qr_url(
    image_bytes: bytes,
    *,
    budget_s: Optional[float] = None,
    on_crop: Optional[Callable[[np.ndarray], None]] = None,
) -> tuple[Optional[str], str]:
    """Find the soliq.uz receipt link in a photo.

    Returns (url or None, how) where `how` names the step that found it
    ("quick pass", "zoom", "zxing sweep", ...) or says "not found" —
    useful in logs when tuning.

    Receipts may carry several QR codes (payment, delivery, ads); only
    the soliq.uz one is ever returned.

    Blocking: call it from a worker thread. `on_crop` (debugging aid)
    receives the auto-cropped image when one is produced.
    """
    started = time.monotonic()
    budget = DEEP_SEARCH_BUDGET_S if budget_s is None else budget_s
    native = _to_cv(image_bytes)
    hires = _downscale_for_decode(native, max_dim=2500)

    soliq = _fast_pass(hires)
    if soliq:
        return soliq, "quick pass"

    cancel = threading.Event()
    deadline = started + budget

    def stop() -> bool:
        return cancel.is_set() or time.monotonic() >= deadline

    low_quality_hint = len(image_bytes) < 250_000

    def lane_zoom() -> Optional[str]:
        return _zoom_search(hires, stop)

    def lane_sweep(decoder: str) -> Callable[[], Optional[str]]:
        def run() -> Optional[str]:
            if decoder == "zxing":
                # One immediate try with zxing's internal downscale —
                # in the old pipeline this only happened at the very
                # end (rotation rescue), 20 s in.
                soliq_now = _soliq_payload(
                    _decode_with_zxing(hires, allow_internal_downscale=True)
                )
                if soliq_now:
                    return soliq_now
            return _sweep(
                _sweep_variants(native, hires, low_quality_hint=low_quality_hint),
                decoder, stop,
            )
        return run

    def lane_cropped() -> Optional[str]:
        cropped, changed = _auto_crop_array(native)
        if not changed:
            return None  # no receipt outline found — nothing new to look at
        if on_crop is not None:
            try:
                on_crop(cropped)
            except Exception:
                logger.debug("on_crop callback failed", exc_info=True)
        if stop():
            return None
        cropped_hires = _downscale_for_decode(cropped, max_dim=2500)
        soliq_c = _fast_pass(cropped_hires)
        if soliq_c:
            return soliq_c
        return _search_all_decoders(
            cropped, cropped_hires, stop, low_quality_hint=False
        )

    lanes: dict[str, Callable[[], Optional[str]]] = {
        "zxing sweep": lane_sweep("zxing"),
        "zoom": lane_zoom,
        "cropped copy": lane_cropped,
        "pyzbar sweep": lane_sweep("pyzbar"),
        "opencv sweep": lane_sweep("opencv"),
    }
    futures = {_LANE_POOL.submit(fn): name for name, fn in lanes.items()}
    try:
        # Lanes check stop() between attempts, so they end shortly
        # after the deadline; the extra seconds cover one long attempt.
        for future in as_completed(futures, timeout=budget + 8):
            try:
                soliq = future.result()
            except Exception:
                logger.exception("QR lane '%s' crashed", futures[future])
                continue
            if soliq:
                return soliq, futures[future]
    except FuturesTimeout:
        logger.warning("QR deep search overran its %.0fs budget", budget)
    finally:
        cancel.set()  # stragglers stop at their next checkpoint
    return None, "not found"


def extract_qr_url(
    image_bytes: bytes,
    *,
    cancel: Optional[threading.Event] = None,
    budget_s: Optional[float] = None,
    skip_fast: bool = False,
) -> Optional[str]:
    """Decode the soliq.uz link from an image, in the calling thread.

    Same search as find_qr_url but sequential and on this one image
    only (no auto-crop, no parallel lanes): quick pass, zoom, then each
    decoder over every variant. Kept for scripts and tests; the bot
    uses find_qr_url. Returns None when no soliq.uz QR is found.
    """
    started = time.monotonic()
    deadline = None if budget_s is None else started + budget_s

    def stop() -> bool:
        if cancel is not None and cancel.is_set():
            return True
        return deadline is not None and time.monotonic() >= deadline

    native = _to_cv(image_bytes)
    hires = _downscale_for_decode(native, max_dim=2500)
    if not skip_fast:
        soliq = _fast_pass(hires)
        if soliq:
            return soliq
    return _search_all_decoders(
        native, hires, stop, low_quality_hint=len(image_bytes) < 250_000
    )
