import unittest
from unittest.mock import patch

import numpy as np

from src.receipt_image import _decode_stages, _decode_with_zxing, extract_qr_url


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

    @patch("src.receipt_image._decode_with_zxing")
    @patch("src.receipt_image._decode_image")
    @patch("src.receipt_image._to_cv")
    def test_extract_qr_url_returns_from_fast_path_before_rescue(
        self,
        to_cv,
        decode_image,
        decode_with_zxing,
    ) -> None:
        img = np.zeros((1800, 1200, 3), dtype=np.uint8)
        qr_url = "https://ofd.soliq.uz/epul/?t=1&r=2&c=20260423010101&s=5000"
        to_cv.return_value = img
        decode_image.side_effect = [[qr_url]]

        result = extract_qr_url(b"image-bytes")

        self.assertEqual(result, qr_url)
        self.assertEqual(decode_image.call_count, 1)
        decode_with_zxing.assert_not_called()

    @patch("src.receipt_image._rotations")
    @patch("src.receipt_image._variant_groups")
    @patch("src.receipt_image._candidate_regions")
    @patch("src.receipt_image._decode_with_zxing")
    @patch("src.receipt_image._decode_image")
    @patch("src.receipt_image._to_cv")
    def test_extract_qr_url_reserves_internal_downscale_for_rescue(
        self,
        to_cv,
        decode_image,
        decode_with_zxing,
        candidate_regions,
        variant_groups,
        rotations,
    ) -> None:
        img = np.zeros((1600, 1000, 3), dtype=np.uint8)
        qr_url = "https://ofd.soliq.uz/epul/?t=9&r=8&c=20260423010101&s=7000"
        to_cv.return_value = img
        decode_image.return_value = []
        decode_with_zxing.side_effect = [[qr_url]]
        candidate_regions.side_effect = (
            lambda image, aggressive: [image, image] if aggressive else [image]
        )
        variant_groups.return_value = [img]
        rotations.return_value = [img, img, img, img]

        result = extract_qr_url(b"image-bytes")

        self.assertEqual(result, qr_url)
        self.assertEqual(decode_with_zxing.call_count, 1)
        self.assertTrue(decode_with_zxing.call_args.kwargs["allow_internal_downscale"])


if __name__ == "__main__":
    unittest.main()
