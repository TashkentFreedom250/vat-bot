"""
Receipt image processing:
  - Auto-crop the receipt from the background (largest bright quadrilateral)
  - Detect & decode the soliq.uz QR code
"""
import logging
from io import BytesIO
from typing import Optional

import cv2
import numpy as np
import zxingcpp
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

try:
    from pyzbar import pyzbar as _pyzbar
    _HAS_PYZBAR = True
except Exception:
    _pyzbar = None
    _HAS_PYZBAR = False

logger = logging.getLogger("vat_bot.qr")

register_heif_opener()

_QR_DETECTOR = cv2.QRCodeDetector()
# QRCodeDetectorAruco (OpenCV ≥4.7) uses an Aruco-style finder pattern locator
# that handles tilted / perspective-distorted QR codes better than the classic
# detector. We use it alongside the classic detector, not as a replacement —
# they fail on different inputs, so trying both raises the success rate.
try:
    _QR_DETECTOR_ARUCO = cv2.QRCodeDetectorAruco()
except AttributeError:
    _QR_DETECTOR_ARUCO = None


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
    img = _to_cv(image_bytes)
    orig = img.copy()
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
        return _to_png_bytes(orig)

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
        return _to_png_bytes(warped)

    # Fallback: bounding box of largest contour
    c = contours[0]
    if cv2.contourArea(c) < (small.shape[0] * small.shape[1]) * 0.1:
        return _to_png_bytes(orig)
    x, y, cw, ch = cv2.boundingRect(c)
    if scale < 1:
        x, y, cw, ch = int(x / scale), int(y / scale), int(cw / scale), int(ch / scale)
    pad = 40
    x = max(0, x - pad); y = max(0, y - pad)
    cw = min(orig.shape[1] - x, cw + 2 * pad)
    ch = min(orig.shape[0] - y, ch + 2 * pad)
    cropped = orig[y:y + ch, x:x + cw]
    cropped = _fix_orientation(cropped)
    return _to_png_bytes(cropped)


def _expand_corners(pts: np.ndarray, margin: float = 0.025) -> np.ndarray:
    """Push 4 corner points outward from centroid by margin fraction."""
    centroid = pts.mean(axis=0)
    return pts + (pts - centroid) * margin


def _fix_orientation(img: np.ndarray) -> np.ndarray:
    """Rotate 180° when the soliq.uz QR is in the top half (upside-down photo)."""
    h = img.shape[0]
    top = img[:h // 2, :]
    try:
        data, _, _ = _QR_DETECTOR.detectAndDecode(top)
        if data and "soliq.uz" in data.lower():
            bot = img[h // 2:, :]
            b_data, _, _ = _QR_DETECTOR.detectAndDecode(bot)
            if not (b_data and "soliq.uz" in b_data.lower()):
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
    detectors = [_QR_DETECTOR]
    if _QR_DETECTOR_ARUCO is not None:
        detectors.append(_QR_DETECTOR_ARUCO)
    for detector in detectors:
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

    # Curved-QR detection is only on the classic detector — the Aruco one
    # doesn't expose it. Worth one attempt on the raw image: receipts
    # photographed at extreme angles can warp the QR enough to look curved.
    try:
        data, _, _ = _QR_DETECTOR.detectAndDecodeCurved(img)
        if data:
            _append_unique(results, [data])
    except Exception:
        pass
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
    """Try several QR decoders and return unique payloads."""
    results = []
    _append_unique(results, _decode_with_opencv(img))
    _append_unique(
        results,
        _decode_with_zxing(img, allow_internal_downscale=zxing_allow_downscale),
    )
    _append_unique(results, _decode_with_pyzbar(img))
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
    for value in found:
        if "soliq.uz" in value.lower():
            return value
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


def extract_qr_url(image_bytes: bytes) -> Optional[str]:
    """
    Decode QR codes from the image and return the soliq.uz URL.

    Receipts may contain multiple QR codes (payment, delivery, ads).
    We only ever return the soliq.uz one — if none is found, return None
    so the caller can tell the user no fiscal QR was detected.
    """
    native = _to_cv(image_bytes)
    found: list[str] = []
    hires = _downscale_for_decode(native, max_dim=2500)

    # --- Close-up fast path -------------------------------------------------
    # Users now photograph just the QR code, so the most likely failure is
    # a tight crop with no quiet zone, glare on the frame, or slight blur.
    # Try the raw frame plus several border thicknesses (restored quiet
    # zones) and one shadow-balanced variant before doing any region work.
    # These five variants alone catch the vast majority of decent close-up
    # shots; expensive stages only run when these all fail.
    fast_variants = [
        hires,
        _with_border(hires, size=24),
        _with_border(hires, size=60),
        _with_border(hires, size=120),
        _lab_normalized(hires),
    ]
    for variant in fast_variants:
        _append_unique(found, _decode_image(variant))
        soliq = _soliq_payload(found)
        if soliq:
            return soliq

    # --- Blur rescue --------------------------------------------------------
    # Close-up shots from phones often miss autofocus and come in soft.
    # Run deblur passes ONLY when the focus score is bad — sharp images
    # would just waste CPU here and rarely benefit from extra unsharp
    # masks anyway.
    blur_score = _laplacian_blur_score(hires)
    if blur_score < 120:
        logger.debug("QR pipeline: low focus score %.1f → deblur pass", blur_score)
        for deblurred in _deblur_variants(hires):
            _append_unique(found, _decode_image(_with_border(deblurred, size=40)))
            soliq = _soliq_payload(found)
            if soliq:
                return soliq

    # --- Region scan --------------------------------------------------------
    for region in _candidate_regions(hires, aggressive=False)[:5]:
        _append_unique(found, _decode_image(region))
        soliq = _soliq_payload(found)
        if soliq:
            return soliq

        # One quick LAB pass per region — shadow-heavy photos often decode
        # here when a raw pass couldn't lock onto the finder patterns.
        _append_unique(found, _decode_image(_lab_normalized(region)))
        soliq = _soliq_payload(found)
        if soliq:
            return soliq

    # --- Heavy preprocessing ------------------------------------------------
    img = _downscale_for_decode(native)
    aggressive = len(image_bytes) < 250_000 or min(img.shape[:2]) < 1000
    low_quality = aggressive

    for stage in _decode_stages(aggressive):
        for region in _candidate_regions(img, aggressive=stage):
            for variant in _variant_groups(
                region, aggressive=stage, low_quality=low_quality
            ):
                _append_unique(found, _decode_image(variant))
                soliq = _soliq_payload(found)
                if soliq:
                    return soliq

    # --- Rotation rescue (moved earlier than before) ------------------------
    # Phones mount receipts at any angle. EXIF rotation is applied at load
    # time so most shots come in upright, but landscape vs portrait
    # mismatch + sideways close-ups still happen. The 90/180/270° tries
    # are cheap when the decoders have already given up.
    for rotated in _rotations(img)[1:]:
        _append_unique(found, _decode_image(rotated, zxing_allow_downscale=True))
        soliq = _soliq_payload(found)
        if soliq:
            return soliq

    # --- ZXing downscale rescue ---------------------------------------------
    # ZXing's internal downscaling search helps when the QR is huge in
    # frame (close-ups where the QR fills most of the image) — the
    # decoder is happier looking at a 600×600 version of it.
    for region in _candidate_regions(hires, aggressive=True)[1:]:
        _append_unique(
            found,
            _decode_with_zxing(region, allow_internal_downscale=True),
        )
        soliq = _soliq_payload(found)
        if soliq:
            return soliq

    return None
