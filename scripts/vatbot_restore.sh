#!/bin/bash
# VAT Bot — one-click restore.
#
# Brings the whole stack back after a power cut, reboot, or any state
# where the bot stopped serving. Safe to run when things are already
# healthy: every step is idempotent.
#
# Driven by the "Restore VAT Bot" app on the Desktop, but also fine to
# run directly:  bash scripts/vatbot_restore.sh
#
# GUI-launched processes inherit a minimal PATH, so every binary below
# is called by absolute path or via an explicit PATH export.

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin"

PROJECT_DIR="/Users/tashkent/Tashkent/vat-bot"
LABEL="com.vatbot.bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$PROJECT_DIR/logs/bot.log"
UID_NUM="$(id -u)"

step()  { echo "==> $*"; }
ok()    { echo "    OK: $*"; }
warn()  { echo "    !! $*"; }

cd "$PROJECT_DIR" || { echo "FATAL: project dir missing: $PROJECT_DIR"; exit 1; }

# ---------------------------------------------------------------- 1. MongoDB
step "Checking MongoDB"
if /usr/bin/pgrep -x mongod >/dev/null 2>&1; then
  ok "already running"
else
  warn "not running - starting"
  /opt/homebrew/bin/brew services start mongodb-community >/dev/null 2>&1
  for i in $(seq 1 20); do
    /usr/bin/pgrep -x mongod >/dev/null 2>&1 && break
    sleep 1
  done
  if /usr/bin/pgrep -x mongod >/dev/null 2>&1; then ok "started"; else
    echo "FATAL: MongoDB would not start. Receipts cannot be saved."
    exit 1
  fi
fi

# ------------------------------------------------------------ 2. Launch agent
step "Starting the bot service"
if [ ! -f "$PLIST" ]; then
  echo "FATAL: launch agent missing: $PLIST"
  echo "Re-install with:  bash run_bot.sh"
  exit 1
fi

# Capture the log size first so we only read lines produced by THIS start.
MARK=0
[ -f "$LOG" ] && MARK=$(/usr/bin/wc -c < "$LOG" | /usr/bin/tr -d ' ')

if /bin/launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  ok "service loaded - restarting it"
  /bin/launchctl kickstart -k "gui/$UID_NUM/$LABEL" >/dev/null 2>&1
else
  warn "service not loaded - loading"
  /bin/launchctl load "$PLIST" >/dev/null 2>&1
fi

# ------------------------------------------------------------- 3. Verify live
step "Waiting for the bot to come online"
# "Application started" is the last line of a successful boot and lands
# after both the Telegram handshake and the Playwright launch, so we
# wait on that rather than racing the earlier lines.
READY=0
for i in $(seq 1 60); do
  sleep 1
  [ -f "$LOG" ] || continue
  # Only inspect log content written after the restart.
  NEW=$(/usr/bin/tail -c "+$((MARK + 1))" "$LOG" 2>/dev/null)
  if echo "$NEW" | /usr/bin/grep -q "Application started"; then
    READY=1
    break
  fi
done

NEW=$(/usr/bin/tail -c "+$((MARK + 1))" "$LOG" 2>/dev/null)

if [ "$READY" -ne 1 ]; then
  if echo "$NEW" | /usr/bin/grep -q "Connected to Telegram"; then
    warn "reached Telegram but did not finish starting within 60s"
  else
    echo "FATAL: bot did not reach Telegram within 60s."
    echo "Most likely the internet is down. Last lines of the log:"
    /usr/bin/tail -5 "$LOG" 2>/dev/null | /usr/bin/sed 's/^/    /'
    exit 1
  fi
else
  ok "connected to Telegram"
fi

# Playwright is required for reading VAT amounts (soliq.uz is a SPA).
if echo "$NEW" | /usr/bin/grep -q "Playwright Chromium launched"; then
  ok "screenshot engine ready"
else
  warn "screenshot engine not confirmed - receipts may save without images"
fi

# -------------------------------------------------------- 4. Keep Mac awake
step "Checking sleep prevention"
if /usr/bin/pgrep -f "caffeinate -imsu" >/dev/null 2>&1; then
  ok "Mac set to stay awake"
else
  warn "caffeinate not attached (bot still runs; Mac may sleep)"
fi

# ------------------------------------------------------------- 5. Final word
PID=$(/bin/launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | /usr/bin/awk '/^\tpid =/{print $3}')
echo
echo "SUCCESS: VAT Bot is back online (pid ${PID:-?})."
echo "Users can send receipts again."
exit 0
