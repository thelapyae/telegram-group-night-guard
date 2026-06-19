#!/bin/sh
set -eu

APP_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
set -a
. "$APP_DIR/.env"
set +a
exec /usr/bin/python3 "$APP_DIR/quiet_hours_bot.py"
