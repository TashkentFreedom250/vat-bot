# VAT Refund Bot — Handover

For whoever takes over this system. Read the first section even if you
read nothing else.

---

## 1. If the bot is down, do this

1. **Power on the Mac** (it restarts itself after a power cut, but
   someone must be physically present for step 2).
2. **Unlock it with the FileVault password.** Nothing — no service, no
   script — can run before this. There is no way around it, and that is
   correct for an embassy machine.
3. **Double-click "VAT Bot Dashboard"** on the Desktop.
4. If the page is **red**, press the big red **"Fix it now"** button and
   wait ~20 seconds. Press it again if needed; it is safe to repeat.

If it is still red after two tries: check that the building's internet
works, then escalate. The dashboard's own checks tell you which part is
unhappy in plain language.

There is also a **"Restore VAT Bot"** Desktop icon that does step 4
without opening the dashboard.

---

## 2. What this system actually does

An employee photographs a receipt in Telegram. The bot reads the QR
code, fetches the **verified VAT amount from soliq.uz** (never from
OCR — the tax authority is the source of truth), stores it, and can
later export a filled official XLSX plus a PDF package for the tax
office.

Moving parts, all on this one Mac:

| Part | What it is | If it stops |
|---|---|---|
| `com.vatbot.bot` | the bot itself (launchd agent) | no receipts processed |
| MongoDB | stores receipts + images | nothing can be saved |
| Playwright/Chromium | reads soliq.uz | VAT amounts come back empty |
| `caffeinate` | stops the Mac sleeping | Mac sleeps, bot pauses |

---

## 3. Things that will bite you

**soliq.uz changed from a normal website to a JavaScript app (June
2026).** The receipt data is no longer in the page HTML — it is fetched
by the page's own code from `new-ofd.soliq.uz/api/payment`, signed with
a header we cannot reproduce. So the bot drives a real headless browser
and intercepts that response. If soliq redesigns again, symptoms are:
every receipt saves with **0 VAT** and users are told "nothing to
refund." That is the signature. Fix is in `src/soliq_screenshot.py`
(`capture`) and `src/soliq.py` (`_normalize_new_api`).

**OpenCV's native QR detectors have crashed this bot twice** — once in
`detectAndDecodeCurved`, once in the Aruco detector. A C++ crash kills
the whole process and **cannot be caught by Python**. Both are now
removed, with comments saying so. Do not add them back.

**This Mac's network drops out for a minute now and then.** The log
is full of it (2,380 network errors on 31 Oct alone), and on
12 Nov 2026 it happened seconds after a deploy: the bot kept
*receiving* messages over its open long-poll connection but could not
open a new connection to *send* the answers, so to users it looked
dead. Since then every outgoing call retries the connection for about a
minute and a half (`src/tg_request.py`) — answers arrive late instead
of never. In the log, `Could not connect to Telegram … retry` is that
mechanism working; `a reply may not have been delivered` means it ran
out of retries. If users report silence, check the network first:
`curl -m 8 https://api.telegram.org/` should answer in under a second.

**soliq.uz connections stall now and then.** The site answers in
~0.5 s when healthy, but some connections just hang. The bot gives a
stalled try 8 s, then retries (three tries in all), so users rarely
notice. Lines like `soliq read attempt 1 failed … retrying` in the log
are that mechanism working, not a fault. About 13% of older receipts
have no stored screenshot from the days before the retry; their PDF
page falls back to a clean data view. Harmless.

**Going back to code older than V.13 needs one manual step.** Up to
V.12 a user could not hold two receipts with the same receipt *number*
— wrong, because every shop's terminal runs its own counter, so
unrelated receipts collide (for someone with 200+ receipts, about a
1-in-6 chance of having had a legitimate receipt refused). V.13
replaced the database index behind that rule
(`telegram_id_1_receipt_number_1`, all receipts) with one that covers
only hand-typed entries (`manual_receipt_number_unique`). The old code
recreates the old index at startup and **will refuse to start** once any
user holds two receipts with the same number. If you must roll back,
keep `src/db.py` from V.13 or later.

**The bot is a LaunchAgent, not a LaunchDaemon.** It starts when a user
*logs in*, not when the Mac boots. In November 2026 the Mac ran for
6.5 days with the bot dead because nobody had logged in. The dashboard
and restore button exist because of that outage.

---

## 4. Compliance: the double-claim guard

The tax committee asked whether two people could claim the same
receipt. They can't, as of June 2026. Every QR receipt gets a global
`fiscal_id` (terminal + receipt number + timestamp + fiscal sign). A
second person scanning the same physical receipt is refused, and an
alert is sent to the admin Telegram IDs. Enforced twice: in
`db.save_receipt` and by a unique database index.

The identity is taken from **soliq.uz's own answer**, not from the
link. This matters: soliq returns the same receipt when the link's
timestamp is changed by a digit or its number is given a leading zero,
so an identity read off the link could be dodged by editing the link.
(Found and closed in November 2026; an audit of all 2,878 stored links
showed it had never been used.) An alert for such an attempt says the
link "had been altered".

**This only works if a real person receives those alerts.** See below.

---

## 5. Three things that need an owner — DO NOT SKIP

1. **Admin Telegram IDs.** `ADMIN_TELEGRAM_IDS` in `.env` currently
   lists one departing person. Whoever owns this must put their own
   Telegram ID there, or double-claim alerts, `/heartcheck` and
   `/report` go to nobody. Get an ID by messaging the bot `/whoami`.
   Restart the bot after editing `.env`.

2. **Backups are on the same disk as the live data.** `backups/` holds
   ~10 GB of nightly snapshots on the *same physical drive* as the
   database they protect. If this Mac fails, 2,700+ receipts and every
   stored image are gone together. Copy them somewhere else —
   a network share, an external drive, anything off this machine.

3. **`.env` exists nowhere else.** It holds the Telegram bot token.
   It is deliberately not in git. Without it the bot cannot be
   rebuilt on a new machine. Keep a copy somewhere safe.

---

## 6. Rebuilding on a new Mac

```bash
git clone https://github.com/TashkentFreedom250/vat-bot.git
cd vat-bot
cp /path/to/your/saved/.env .env     # see item 3 above
bash run_bot.sh                      # installs everything, incl. Chromium
```

`run_bot.sh` installs Python, MongoDB, dependencies, the headless
browser, the launch agent, and both Desktop icons. Then restore the
newest `backups/` snapshot with `mongorestore --gzip`.

**It must run on an Uzbekistan internet connection.** soliq.uz blocks
cloud providers and most foreign IPs — AWS, Railway, Render will not
work. That is why this lives on a Mac in the building.

---

## 7. Useful commands

```bash
tail -f logs/bot.log                    # watch it live
grep " ERROR " logs/bot.log             # today's errors
grep "handled in" logs/bot.log          # how long each photo took, and why
.venv/bin/python -m unittest discover -s tests   # run the tests
bash scripts/vatbot_restore.sh          # restart everything
.venv/bin/python scripts/dashboard.py   # status page, port 8808
```

In Telegram, as an admin: `/heartcheck` for health, `/report` for
total VAT refunded (UZS and USD).
