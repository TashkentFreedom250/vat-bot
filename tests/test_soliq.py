import unittest

from bs4 import BeautifulSoup

from src.soliq import _extract_total_from_table, _extract_vendor, _to_float


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


if __name__ == "__main__":
    unittest.main()
