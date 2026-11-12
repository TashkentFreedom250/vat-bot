import unittest

from src import config, notice


class NoticeTests(unittest.TestCase):
    def test_notice_states_the_license_and_the_disclaimer(self) -> None:
        text = notice.notice_html()
        for phrase in (
            "MIT License",
            "open-source",
            "as is",
            "without warranty of any kind",
            "liable",
            config.SOURCE_URL,
            "/about",
        ):
            self.assertIn(phrase, text)

    def test_about_includes_the_full_license_file(self) -> None:
        about = notice.about_html()
        license_file = (config.BASE_DIR / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Permission is hereby granted, free of charge", about)
        self.assertIn("WITHOUT WARRANTY OF ANY KIND", about)
        # The whole file is there (quotes are HTML-escaped inside <pre>).
        self.assertIn(license_file.strip().splitlines()[-1], about)
        self.assertLess(len(about), 4096, "must fit in one Telegram message")

    def test_license_file_is_mit(self) -> None:
        self.assertTrue(notice.license_text().startswith("MIT License"))


if __name__ == "__main__":
    unittest.main()
