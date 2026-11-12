"""
The open-source licence and no-warranty notice.

Every user is shown this once, the first time they use the bot after
being approved (and existing users once, the first time they use it
after this was added). /about repeats it on request together with the
full licence text, and /start carries a one-line reminder.

The wording follows the MIT License in the LICENSE file at the root of
the repository — that file is the authoritative text; this module only
presents it. If the licence ever changes, bump NOTICE_VERSION so the
database records which wording a user was shown.
"""
from __future__ import annotations

from html import escape

from . import config

NOTICE_VERSION = 1


def notice_html() -> str:
    """The notice as Telegram HTML."""
    source = escape(config.SOURCE_URL)
    return (
        "ℹ️ <b>About this bot — please read once</b>\n\n"
        "This bot is free, open-source software released under the "
        "<b>MIT License</b>.\n\n"
        "It is provided <b>\"as is\", without warranty of any kind</b>, "
        "express or implied, including but not limited to the warranties "
        "of merchantability, fitness for a particular purpose and "
        "noninfringement. In no event shall the authors or copyright "
        "holders be liable for any claim, damages or other liability "
        "arising from, out of or in connection with the software or its "
        "use.\n\n"
        "It is a convenience tool: the VAT figures come from soliq.uz, and "
        "you remain responsible for checking your refund claim before you "
        "file it.\n\n"
        "By continuing to use the bot you accept these terms.\n\n"
        f"Source code and full license: {source}\n"
        "/about shows this notice and the license text at any time."
    )


WELCOME_FOOTER_HTML = (
    "<i>Free open-source software (MIT License), provided \"as is\" "
    "without warranty — see /about.</i>"
)


def license_text() -> str:
    """The LICENSE file, verbatim. Falls back to a pointer if the file
    is missing so /about never fails."""
    try:
        return (config.BASE_DIR / "LICENSE").read_text(encoding="utf-8").strip()
    except OSError:
        return f"MIT License — full text: {config.SOURCE_URL}"


def about_html() -> str:
    """Notice followed by the complete licence text."""
    return f"{notice_html()}\n\n<pre>{escape(license_text())}</pre>"
