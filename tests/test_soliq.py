import unittest

from bs4 import BeautifulSoup

from src.db import fiscal_id_from_url
from src.soliq import (
    _canonical_fiscal_id,
    _extract_total_from_table,
    _extract_vendor,
    _is_spa_shell,
    _normalize_new_api,
    _to_float,
)

GENUINE = "https://ofd.soliq.uz/check?t=ZZ000000000001&r=4321&c=20260115101530&s=123456789012"
API_ANSWER = {
    "data": {
        "terminalId": "ZZ000000000001",
        "paymentNo": 4321,
        "paymentDate": "15.01.2026 10:15:30",
        "vatTotal": 12000.0,
        "cardTotal": 112000.0,
        "cashTotal": 0,
        "tin": 123456789,
        "extraInfo": {"companyName": '"TEST SHOP" MCHJ', "address": "Toshkent"},
        "paymentDetails": [{"name": "Tea", "amount": 2, "price": 56000, "vat": 12000}],
    }
}


class SoliqParserTests(unittest.TestCase):
    def test_total_prefers_first_numeric_cell_after_label(self) -> None:
        soup = BeautifulSoup(
            """
            <table>
              <tr>
                <td>Jami to`lov:</td>
                <td>298,000.00</td>
                <td>Umumiy QQS qiymati</td>
                <td>0.00</td>
              </tr>
            </table>
            """,
            "html.parser",
        )

        self.assertEqual(_extract_total_from_table(soup), 298000.0)

    def test_vendor_skips_terminal_identifier_row(self) -> None:
        html = """
        <table>
          <tr><td>LG420211632113</td></tr>
          <tr><td>ALINA MISHINA ALEKSANDROVNA Toshkent shahri, Yunusobod tumani</td></tr>
        </table>
        """
        soup = BeautifulSoup(html, "html.parser")

        self.assertEqual(_extract_vendor(soup, soup.get_text("\n", strip=True)), "ALINA MISHINA ALEKSANDROVNA")

    def test_to_float_handles_grouped_amounts(self) -> None:
        self.assertEqual(_to_float("298,000.00"), 298000.0)
        self.assertEqual(_to_float("298.000,00"), 298000.0)
        self.assertEqual(_to_float("298 000,00"), 298000.0)


class NewApiTests(unittest.TestCase):
    def test_normalizes_the_payment_api_answer(self) -> None:
        data = _normalize_new_api(API_ANSWER, GENUINE)

        self.assertEqual(data["vendor"], '"TEST SHOP" MCHJ')
        self.assertEqual(data["date"], "2026-01-15")
        self.assertEqual(data["receipt_number"], "4321")
        self.assertEqual(data["vat_amount"], 12000.0)
        self.assertEqual(data["total_amount"], 112000.0)
        self.assertEqual(data["meta"]["time"], "10:15")
        self.assertEqual(len(data["items"]), 1)

    def test_identity_of_a_genuine_link_matches_the_link(self) -> None:
        # Ids already stored in the database were built from the link;
        # the soliq-reported id must be identical for an untouched link.
        self.assertEqual(
            _normalize_new_api(API_ANSWER, GENUINE)["fiscal_id"],
            fiscal_id_from_url(GENUINE),
        )

    def test_identity_ignores_altered_link_parameters(self) -> None:
        # soliq.uz returns the same receipt for these altered links, so
        # they must all resolve to the same identity (double-claim guard).
        genuine_id = fiscal_id_from_url(GENUINE)
        for altered in (
            GENUINE.replace("c=20260115101530", "c=20260115101535"),  # timestamp nudged
            GENUINE.replace("r=4321", "r=04321"),                   # leading zero
        ):
            # The link alone would pass for a different receipt...
            self.assertNotEqual(fiscal_id_from_url(altered), genuine_id, altered)
            # ...soliq's own answer says which receipt it really is.
            self.assertEqual(
                _canonical_fiscal_id(API_ANSWER["data"], altered), genuine_id, altered
            )
        # An extra parameter changes nothing either way.
        self.assertEqual(fiscal_id_from_url(GENUINE + "&x=1"), genuine_id)

    def test_identity_is_none_when_soliq_answer_is_incomplete(self) -> None:
        for broken in (
            {**API_ANSWER["data"], "paymentDate": ""},
            {**API_ANSWER["data"], "terminalId": None},
            {**API_ANSWER["data"], "paymentNo": None},
        ):
            self.assertIsNone(_canonical_fiscal_id(broken, GENUINE))
        self.assertIsNone(
            _canonical_fiscal_id(API_ANSWER["data"], "https://ofd.soliq.uz/check?t=1&r=2")
        )

    def test_recognises_the_empty_app_shell(self) -> None:
        self.assertTrue(_is_spa_shell('<!doctype html><html><body><div id="root"></div></body></html>'))
        self.assertFalse(_is_spa_shell("<html><table><tr><td>Jami</td></tr></table></html>"))


if __name__ == "__main__":
    unittest.main()
