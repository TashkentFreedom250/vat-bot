#!/bin/bash
# VAT Bot - Mac setup & launcher
# Run: bash run_bot.sh

set -e
cd "$(dirname "$0")"

echo "=============================="
echo "  VAT Bot Setup & Launch"
echo "=============================="

# Require Homebrew
if ! command -v brew &>/dev/null; then
  echo "Homebrew not found. Install it first: https://brew.sh"
  exit 1
fi

# Require Python 3.11
if ! command -v /opt/homebrew/bin/python3.11 &>/dev/null; then
  echo "Installing Python 3.11..."
  brew install python@3.11
fi

# Require zbar (for pyzbar QR decoding)
if ! brew list zbar &>/dev/null; then
  echo "Installing zbar..."
  brew install zbar
fi

# Require MongoDB Community and ensure it's running
if ! brew list mongodb-community &>/dev/null; then
  echo "Installing MongoDB Community..."
  brew tap mongodb/brew
  brew install mongodb-community
fi
if ! brew services list | grep mongodb-community | grep -q started; then
  echo "Starting MongoDB..."
  brew services start mongodb/brew/mongodb-community
fi

# Create venv with Python 3.11 if needed
if [ ! -d ".venv" ]; then
  echo "Creating Python 3.11 virtual environment..."
  /opt/homebrew/bin/python3.11 -m venv .venv
fi

source .venv/bin/activate

echo "Installing Python dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo ""
echo "=============================="
echo "  Starting VAT Bot..."
echo "  Press Ctrl+C to stop"
echo "=============================="
echo ""

python -m src.bot
