import unittest
from unittest.mock import patch

import numpy as np

from src.receipt_image import (
    _ROTATED,
    _STD,
    _ZXING_DOWN,
    _decode_image,
    _decode_stages,
    _decode_with_zxing,
    _sweep,
    extract_qr_url,
    extract_qr_url_fast,
    find_qr_url,
)


class ReceiptImageTests(unittest.TestCase):
    @patch("src.receipt_image.zxingcpp.read_barcodes", return_value=[])
    def test_decode_with_zxing_skips_internal_downscale_by_default(self, read_barcodes) -> None:
        img = np.zeros((64, 64, 3), dtype=np.uint8)

        _decode_with_zxing(img)

        self.assertGreater(read_barcodes.call_count, 0)
        self.assertTrue(
            all(not call.kwargs["try_downscale"] for call in read_barcodes.call_args_list)
        )

    @patch("src.receipt_image.zxingcpp.read_barcodes", return_value=[])
    def test_decode_with_zxing_can_enable_internal_downscale(self, read_barcodes) -> None:
        img = np.zeros((64, 64, 3), dtype=np.uint8)

        _decode_with_zxing(img, allow_internal_downscale=True)

        self.assertGreater(read_barcodes.call_count, 0)
        self.assertTrue(
            all(call.kwargs["try_downscale"] for call in read_barcodes.call_args_list)
        )

    def test_decode_stages_only_adds_aggressive_retry_when_needed(self) -> None:
        self.assertEqual(_decode_stages(False), [False])
        self.assertEqual(_decode_stages(True), [False, True])

    @patch("src.receipt_image._zoom_search")
    @patch("src.receipt_image._decode_image")
    @patch("src.receipt_image._to_cv")
    def test_extract_qr_url_returns_from_fast_path_before_rescue(
        self,
        to_cv,
        decode_image,
        zoom_search,
    ) -> None:
        img = np.zeros((1800, 1200, 3), dtype=np.uint8)
        qr_url = "https://ofd.soliq.uz/epul/?t=1&r=2&c=20260423010101&s=5000"
        to_cv.return_value = img
        decode_image.side_effect = [[qr_url]]

        result = extract_qr_url(b"image-bytes")

        self.assertEqual(result, qr_url)
        self.assertEqual(decode_image.call_count, 1)
        zoom_search.assert_not_called()

    @patch("src.receipt_image._decode_with_zxing", return_value=[])
    def test_sweep_reserves_zxing_internal_downscale_for_rescue_variants(
        self, decode_with_zxing
    ) -> None:
        img = np.zeros((64, 64, 3), dtype=np.uint8)
        variants = [(img, _STD), (img, _ROTATED), (img, _ZXING_DOWN)]

        self.assertIsNone(_sweep(iter(variants), "zxing", lambda: False))

        flags = [c.kwargs["allow_internal_downscale"] for c in decode_with_zxing.call_args_list]
        self.assertEqual(flags, [False, True, True])

    @patch("src.receipt_image._decode_with_opencv", return_value=[])
    @patch("src.receipt_image._decode_with_pyzbar", return_value=[])
    def test_sweep_keeps_zxing_only_variants_away_from_other_decoders(
        self, decode_with_pyzbar, decode_with_opencv
    ) -> None:
        img = np.zeros((64, 64, 3), dtype=np.uint8)
        variants = [(img, _STD), (img, _ZXING_DOWN)]

        _sweep(iter(variants), "pyzbar", lambda: False)
        _sweep(iter(variants), "opencv", lambda: False)

        self.assertEqual(decode_with_pyzbar.call_count, 1)
        self.assertEqual(decode_with_opencv.call_count, 1)

    @patch("src.receipt_image._decode_with_zxing", return_value=[])
    def test_sweep_stops_when_told_to(self, decode_with_zxing) -> None:
        img = np.zeros((64, 64, 3), dtype=np.uint8)

        self.assertIsNone(_sweep(iter([(img, _STD)] * 5), "zxing", lambda: True))

        decode_with_zxing.assert_not_called()

    def test_decode_image_stops_at_first_decoder_that_finds_the_link(self) -> None:
        img = np.zeros((64, 64, 3), dtype=np.uint8)
        qr_url = "https://ofd.soliq.uz/check?t=1&r=2&c=20260423010101&s=5000"
        with patch("src.receipt_image._decode_with_zxing", return_value=[qr_url]), \
             patch("src.receipt_image._decode_with_pyzbar") as pyzbar, \
             patch("src.receipt_image._decode_with_opencv") as opencv:
            self.assertEqual(_decode_image(img), [qr_url])
        pyzbar.assert_not_called()
        opencv.assert_not_called()


def _photo_with_qr(payload: str, *, qr_px: int, canvas=(960, 1280), angle: int = 0) -> bytes:
    """A synthetic 'receipt photo': a QR code on white paper on a grey
    table, JPEG-compressed like a phone photo."""
    import cv2
    import qrcode

    qr = qrcode.QRCode(border=4, box_size=10)
    qr.add_data(payload)
    qr.make(fit=True)
    code = np.array(qr.make_image().convert("L"))
    code = cv2.resize(code, (qr_px, qr_px), interpolation=cv2.INTER_AREA)
    h, w = canvas[1], canvas[0]
    photo = np.full((h, w), 110, dtype=np.uint8)                      # table
    photo[60:h - 60, 200:w - 200] = 245                               # paper
    y, x = h - 60 - qr_px - 80, (w - qr_px) // 2
    photo[y:y + qr_px, x:x + qr_px] = code
    if angle:
        photo = cv2.rotate(photo, {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180}[angle])
    ok, buf = cv2.imencode(".jpg", photo, [cv2.IMWRITE_JPEG_QUALITY, 80])
    assert ok
    return buf.tobytes()


class RealDecodeTests(unittest.TestCase):
    """End-to-end on generated images — no mocks, real decoders."""

    URL = "https://ofd.soliq.uz/check?t=ZZ000000000001&r=4321&c=20260115101530&s=123456789012"

    def test_finds_a_clear_qr_on_the_quick_pass(self) -> None:
        url, how = find_qr_url(_photo_with_qr(self.URL, qr_px=420))
        self.assertEqual(url, self.URL)
        self.assertEqual(how, "quick pass")
        self.assertEqual(extract_qr_url_fast(_photo_with_qr(self.URL, qr_px=420)), self.URL)

    def test_finds_an_upside_down_and_a_sideways_qr(self) -> None:
        for angle in (90, 180):
            url, _ = find_qr_url(_photo_with_qr(self.URL, qr_px=420, angle=angle))
            self.assertEqual(url, self.URL, f"angle {angle}")

    def test_finds_a_small_qr_on_a_long_receipt(self) -> None:
        url, _ = find_qr_url(_photo_with_qr(self.URL, qr_px=150))
        self.assertEqual(url, self.URL)
        self.assertEqual(extract_qr_url(_photo_with_qr(self.URL, qr_px=150)), self.URL)

    def test_ignores_qr_codes_that_are_not_soliq(self) -> None:
        for payload in (
            "https://example.com/pay?order=12345",
            # contains "soliq.uz" but is NOT hosted on soliq.uz:
            "https://example.com/soliq.uz/check?t=ZZ000000000001&r=1&c=20260115101530&s=1",
        ):
            url, how = find_qr_url(_photo_with_qr(payload, qr_px=420), budget_s=1.0)
            self.assertIsNone(url, payload)
            self.assertEqual(how, "not found")

    def test_gives_up_within_the_time_budget(self) -> None:
        import time

        blank = _photo_with_qr("x", qr_px=30)  # nothing decodable
        started = time.monotonic()
        url, _ = find_qr_url(blank, budget_s=1.0)
        self.assertIsNone(url)
        self.assertLess(time.monotonic() - started, 6.0)


if __name__ == "__main__":
    unittest.main()
