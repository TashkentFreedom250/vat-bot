"""Quick QR decode test — run: python test_qr.py <image_path>"""
import sys
import cv2
import numpy as np
from pyzbar.pyzbar import decode as pyzbar_decode


def try_all(img, label=""):
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    variants = {
        "original": img,
        "gray": cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR),
        "thresh": cv2.cvtColor(cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10), cv2.COLOR_GRAY2BGR),
        "sharp": cv2.cvtColor(cv2.filter2D(gray, -1, np.array([[-1,-1,-1],[-1,9,-1],[-1,-1,-1]])), cv2.COLOR_GRAY2BGR),
        "2x": cv2.resize(img, (w*2, h*2), interpolation=cv2.INTER_CUBIC),
        "3x": cv2.resize(img, (w*3, h*3), interpolation=cv2.INTER_CUBIC),
    }

    for name, v in variants.items():
        # pyzbar
        results = pyzbar_decode(v)
        if results:
            for r in results:
                print(f"  [pyzbar/{label}/{name}] {r.data.decode('utf-8', errors='ignore')}")

        # OpenCV built-in detector
        det = cv2.QRCodeDetector()
        data, _, _ = det.detectAndDecode(v)
        if data:
            print(f"  [cv2/{label}/{name}] {data}")


def main(path):
    img = cv2.imread(path)
    if img is None:
        print(f"Could not load {path}")
        return

    h, w = img.shape[:2]
    print(f"Image size: {w}x{h}")

    print("\n--- Full image ---")
    try_all(img, "full")

    print("\n--- Bottom 40% ---")
    bottom = img[int(h * 0.6):, :]
    try_all(bottom, "bottom40")

    print("\n--- Bottom 25% ---")
    bottom2 = img[int(h * 0.75):, :]
    try_all(bottom2, "bottom25")

    print("\nDone. If nothing printed above, QR could not be decoded.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_qr.py <image_path>")
    else:
        main(sys.argv[1])
