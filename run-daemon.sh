#!/bin/sh
set -eu

APP_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
set -a
. "$APP_DIR/.env"
set +a

DATA_DIR="${QUIET_BOT_DATA_DIR:-$HOME/.local/share/telegram-quiet-hours}"
mkdir -p "$DATA_DIR"
exec /usr/bin/flock -n "$DATA_DIR/daemon-process.lock" \
  /usr/bin/python3 "$APP_DIR/quiet_hours_bot.py" daemon
