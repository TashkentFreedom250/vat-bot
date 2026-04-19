import unittest

from src.receipt_ocr import (
    _extract_date_from_lines,
    _extract_total_from_lines,
    _extract_vat_rate_from_lines,
    _extract_vendor_from_lines,
)


class ReceiptOCRTests(unittest.TestCase):
    def test_vendor_prefers_later_close_company_candidate(self) -> None:
        lines = [
            {"text": "ERA", "score": 0.98, "top": 10.0, "left": 20.0},
            {"text": '"SABAT LAND" MCHJ', "score": 0.927, "top": 32.0, "left": 20.0},
            {"text": '"SABAI LAND" MCHJ', "score": 0.905, "top": 50.0, "left": 20.0},
            {"text": "Manzil", "score": 0.99, "top": 70.0, "left": 20.0},
        ]

        self.assertEqual(
            _extract_vendor_from_lines(lines, image_height=400),
            '"SABAI LAND" MCHJ',
        )

    def test_extracts_total_from_labeled_lines(self) -> None:
        lines = [
            {"text": "Jami", "score": 0.95, "top": 500.0, "left": 10.0},
            {"text": "940000.00", "score": 0.95, "top": 520.0, "left": 10.0},
            {"text": "To'lov uchun jami", "score": 0.97, "top": 560.0, "left": 10.0},
            {"text": "940080.00", "score": 0.98, "top": 582.0, "left": 10.0},
        ]

        self.assertEqual(_extract_total_from_lines(lines), 940080.0)

    def test_extracts_vat_rate_from_qqs_siz(self) -> None:
        lines = [
            {"text": "QQS s1z", "score": 0.88, "top": 300.0, "left": 10.0},
        ]

        self.assertEqual(_extract_vat_rate_from_lines(lines), "0%")

    def test_extracts_date(self) -> None:
        lines = [
            {"text": "03-04-2026 20:26:50", "score": 0.95, "top": 200.0, "left": 10.0},
        ]

        self.assertEqual(_extract_date_from_lines(lines), "2026-04-03")


if __name__ == "__main__":
    unittest.main()
