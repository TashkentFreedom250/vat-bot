"""Extended QR test using zxing-cpp + pyzbar + OpenCV"""
import sys
import cv2
import numpy as np
import zxingcpp
from PIL import Image
from pyzbar.pyzbar import decode as pyzbar_decode


def try_zxing(img_bgr, label):
    pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    results = zxingcpp.read_barcodes(pil)
    for r in results:
        print(f"  [zxing/{label}] type={r.format} text={r.text}")
    return [r.text for r in results]


def try_pyzbar(img_bgr, label):
    results = pyzbar_decode(img_bgr)
    for r in results:
        text = r.data.decode("utf-8", errors="ignore")
        print(f"  [pyzbar/{label}] type={r.type} text={text}")
    return [r.data.decode("utf-8", errors="ignore") for r in results]


def try_cv2(img_bgr, label):
    det = cv2.QRCodeDetector()
    data, pts, _ = det.detectAndDecode(img_bgr)
    if data:
        print(f"  [cv2/{label}] text={data}")
    return [data] if data else []


def test_variants(img, label):
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    variants = [
        ("orig", img),
        ("2x", cv2.resize(img, (w*2, h*2), interpolation=cv2.INTER_CUBIC)),
        ("3x", cv2.resize(img, (w*3, h*3), interpolation=cv2.INTER_CUBIC)),
        ("gray2x", cv2.cvtColor(cv2.resize(gray, (w*2, h*2), interpolation=cv2.INTER_CUBIC), cv2.COLOR_GRAY2BGR)),
        ("thresh2x", cv2.cvtColor(cv2.adaptiveThreshold(
            cv2.resize(gray, (w*2, h*2), interpolation=cv2.INTER_CUBIC),
            255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
        ), cv2.COLOR_GRAY2BGR)),
    ]

    found = []
    for vname, v in variants:
        lbl = f"{label}/{vname}"
        found += try_zxing(v, lbl)
        found += try_pyzbar(v, lbl)
        found += try_cv2(v, lbl)
    return found


def main(path):
    img = cv2.imread(path)
    if img is None:
        print(f"Cannot load {path}")
        return
    h, w = img.shape[:2]
    print(f"Image: {w}x{h}\n")

    all_found = []

    print("=== Full image ===")
    all_found += test_variants(img, "full")

    print("\n=== Bottom 50% ===")
    all_found += test_variants(img[h//2:, :], "bot50")

    print("\n=== Bottom 30% ===")
    all_found += test_variants(img[int(h*0.7):, :], "bot30")

    print("\n=== Bottom-right quadrant ===")
    all_found += test_variants(img[h//2:, w//2:], "bot-right")

    print("\n=== Bottom-left quadrant ===")
    all_found += test_variants(img[h//2:, :w//2], "bot-left")

    if not any(all_found):
        print("\n*** Nothing decoded. QR may be too blurry/small in this photo. ***")
    else:
        print(f"\n=== All decoded values ===")
        for v in set(all_found):
            if v:
                print(f"  {v}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_qr2.py <image_path>")
    else:
        main(sys.argv[1])
