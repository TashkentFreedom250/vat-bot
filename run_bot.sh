#!/bin/bash
# VAT Bot — Mac setup & launcher.
#
# First manual run:
#   bash run_bot.sh
# …installs dependencies, starts MongoDB as a brew service, registers the
# bot as a login-launched launchd agent that auto-restarts on crash, and
# exits. From then on the bot manages itself — you don't run anything.
#
# Later manual runs (after install) simply tail the logs.

set -e
cd "$(dirname "$0")"

PROJECT_DIR="$(pwd)"
BOT_LABEL="com.vatbot.bot"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
BOT_PLIST_SRC="$PROJECT_DIR/scripts/$BOT_LABEL.plist"
BOT_PLIST_DST="$LAUNCH_AGENTS/$BOT_LABEL.plist"

mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/backups"

# ----- Helper: detect if launchd is running us -----
# The plist sets VAT_BOT_LAUNCHD=1 so run_bot.sh knows "just run the bot".
LAUNCHD_MARKER="${VAT_BOT_LAUNCHD:-}"

# ----- Ensure Homebrew + Python 3.11 + zbar + MongoDB are installed -----
ensure_dependencies() {
  if ! command -v brew &>/dev/null; then
    echo "Homebrew not found. Install it first: https://brew.sh"
    exit 1
  fi

  if ! command -v /opt/homebrew/bin/python3.11 &>/dev/null; then
    echo "Installing Python 3.11..."
    brew install python@3.11
  fi

  if ! brew list zbar &>/dev/null; then
    echo "Installing zbar..."
    brew install zbar
  fi

  if ! brew list mongodb-community &>/dev/null; then
    echo "Installing MongoDB Community..."
    brew tap mongodb/brew
    brew install mongodb-community
  fi

  # Start MongoDB and configure it to auto-start on login
  if ! brew services list | grep mongodb-community | grep -q started; then
    echo "Starting MongoDB (and enabling auto-start on login)..."
    brew services start mongodb/brew/mongodb-community
  fi

  if [ ! -d ".venv" ]; then
    echo "Creating Python 3.11 virtual environment..."
    /opt/homebrew/bin/python3.11 -m venv .venv
  fi

  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
}

# ----- Install the launchd agent (login-launched, auto-restart) -----
install_launchd_agent() {
  mkdir -p "$LAUNCH_AGENTS"
  cp "$BOT_PLIST_SRC" "$BOT_PLIST_DST"
  launchctl unload "$BOT_PLIST_DST" 2>/dev/null || true
  launchctl load "$BOT_PLIST_DST"
}

# ======================================================================
# Case 1 — launchd is running us. Just exec the bot.
# ======================================================================
if [ -n "$LAUNCHD_MARKER" ]; then
  ensure_dependencies
  # shellcheck disable=SC1091
  source .venv/bin/activate
  exec python -m src.bot
fi

# ======================================================================
# Case 2 — manual run. Do the full setup, self-install, and exit.
# ======================================================================
echo "=============================="
echo "  VAT Bot — setup & install"
echo "=============================="

ensure_dependencies

if launchctl list | grep -q "$BOT_LABEL"; then
  echo ""
  echo "The bot is already installed as a launchd agent."
  echo "It auto-starts at login and auto-restarts if it crashes."
  echo ""
  echo "  Tail logs:  tail -f logs/bot.log"
  echo "  Stop:       launchctl unload $BOT_PLIST_DST"
  echo "  Start:      launchctl load $BOT_PLIST_DST"
  echo ""
  exit 0
fi

echo ""
echo "Installing the bot as a login-launched service..."
install_launchd_agent
sleep 2
echo ""
echo "Done. The bot is now running and will auto-start every time you log in."
echo "MongoDB is configured to auto-start as a brew service."
echo "Backups, log rotation, and disk monitoring run inside the bot every night."
echo ""
echo "  Logs:       tail -f logs/bot.log"
echo "  Backups:    ls -lht backups/ | head"
echo "  Status:     launchctl list | grep $BOT_LABEL"
echo "  Stop:       launchctl unload $BOT_PLIST_DST"
echo ""
