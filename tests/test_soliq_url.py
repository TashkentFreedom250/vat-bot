import unittest

from src.soliq_url import find_soliq_url, is_soliq_url

GOOD = "https://ofd.soliq.uz/check?t=ZZ000000000001&r=4321&c=20260115101530&s=123456789012"


class SoliqUrlTests(unittest.TestCase):
    def test_accepts_soliq_hosts(self) -> None:
        self.assertTrue(is_soliq_url(GOOD))
        self.assertTrue(is_soliq_url("http://soliq.uz/x"))
        self.assertTrue(is_soliq_url("HTTPS://OFD.SOLIQ.UZ/epi?t=1&r=2"))

    def test_rejects_lookalike_hosts(self) -> None:
        # "soliq.uz" appearing in the text is not enough — the HOST must be soliq.uz.
        for url in (
            "https://example.com/soliq.uz?t=1&r=2",
            "https://example.com/?next=https://ofd.soliq.uz/check",
            "https://ofd.soliq.uz.example.com/check?t=1",
            "https://notsoliq.uz/check?t=1",
            "https://ofd.soliq.uz@example.com/check?t=1",
            "ftp://ofd.soliq.uz/check",
            "javascript:alert('soliq.uz')",
            "",
        ):
            self.assertFalse(is_soliq_url(url), url)

    def test_finds_link_inside_a_sentence(self) -> None:
        self.assertEqual(find_soliq_url(f"here you go: {GOOD}. thanks!"), GOOD)
        self.assertEqual(find_soliq_url(f"  {GOOD}\n"), GOOD)

    def test_adds_missing_scheme(self) -> None:
        self.assertEqual(
            find_soliq_url("ofd.soliq.uz/check?t=AB1&r=2&c=3&s=4"),
            "https://ofd.soliq.uz/check?t=AB1&r=2&c=3&s=4",
        )

    def test_ignores_text_without_a_soliq_link(self) -> None:
        self.assertIsNone(find_soliq_url("hello"))
        self.assertIsNone(find_soliq_url("see https://example.com/soliq.uz/check?t=1"))
        self.assertIsNone(find_soliq_url("I paid at soliq.uz yesterday"))
        self.assertIsNone(find_soliq_url(""))


if __name__ == "__main__":
    unittest.main()
