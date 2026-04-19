"""
Receipt image processing:
  - Auto-crop the receipt from the background (largest bright quadrilateral)
  - Detect & decode the soliq.uz QR code
"""
from io import BytesIO
from typing import Optional, Tuple

import cv2
import numpy as np
import zxingcpp
from PIL import Image
from pillow_heif import register_heif_opener
from pyzbar.pyzbar import decode as pyzbar_decode

register_heif_opener()


def _to_cv(image_bytes: bytes) -> np.ndarray:
    """Load bytes into an OpenCV BGR image. Falls back to PIL for HEIC/HEIF."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is not None:
        return img
    # PIL fallback handles HEIC (iPhone) and other formats OpenCV can't read
    pil = Image.open(BytesIO(image_bytes)).convert("RGB")
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


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
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4 and cv2.contourArea(c) > (small.shape[0] * small.shape[1]) * 0.1:
            receipt_contour = approx
            break

    if receipt_contour is not None:
        pts = receipt_contour.reshape(4, 2).astype("float32") / scale if scale < 1 else receipt_contour.reshape(4, 2).astype("float32")
        warped = _four_point_transform(orig, pts)
        return _to_png_bytes(warped)

    # Fallback: bounding box of largest contour
    c = contours[0]
    if cv2.contourArea(c) < (small.shape[0] * small.shape[1]) * 0.1:
        return _to_png_bytes(orig)
    x, y, cw, ch = cv2.boundingRect(c)
    if scale < 1:
        x, y, cw, ch = int(x / scale), int(y / scale), int(cw / scale), int(ch / scale)
    # Add small padding
    pad = 10
    x = max(0, x - pad); y = max(0, y - pad)
    cw = min(orig.shape[1] - x, cw + 2 * pad)
    ch = min(orig.shape[0] - y, ch + 2 * pad)
    cropped = orig[y:y + ch, x:x + cw]
    return _to_png_bytes(cropped)


def _to_png_bytes(cv_img: np.ndarray) -> bytes:
    """Convert a BGR OpenCV image to PNG bytes."""
    rgb = cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)
    buf = BytesIO()
    pil.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _decode_image(img_bgr: np.ndarray) -> list[str]:
    """Try zxing-cpp (primary) then pyzbar (fallback). Returns list of decoded strings."""
    results = []
    pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    for r in zxingcpp.read_barcodes(pil):
        if r.text and r.text not in results:
            results.append(r.text)
    if not results:
        for r in pyzbar_decode(img_bgr):
            try:
                payload = r.data.decode("utf-8", errors="ignore")
                if payload and payload not in results:
                    results.append(payload)
            except Exception:
                pass
    return results


def extract_qr_url(image_bytes: bytes) -> Optional[str]:
    """
    Decode QR codes from the image. Returns the first soliq.uz URL found,
    or the first QR payload if none match.
    """
    img = _to_cv(image_bytes)
    h, w = img.shape[:2]

    regions = [
        img,
        img[h // 2:, :],                        # bottom half
        cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC),
    ]

    found = []
    for region in regions:
        for text in _decode_image(region):
            if text not in found:
                found.append(text)
        for url in found:
            if "soliq.uz" in url.lower():
                return url

    if not found:
        return None
    for url in found:
        if "soliq.uz" in url.lower():
            return url
    return found[0]
