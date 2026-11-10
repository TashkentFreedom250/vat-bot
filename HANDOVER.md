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

**Screenshot capture times out occasionally** (soliq being slow). About
13% of receipts have no stored screenshot. Harmless: their PDF page
falls back to a clean data view. Not worth chasing.

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
bash scripts/vatbot_restore.sh          # restart everything
.venv/bin/python scripts/dashboard.py   # status page, port 8808
```

In Telegram, as an admin: `/heartcheck` for health, `/report` for
total VAT refunded (UZS and USD).
