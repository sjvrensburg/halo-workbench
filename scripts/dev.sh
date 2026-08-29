#!/usr/bin/env bash
# Run the app in the foreground with auto-reload, for development.
#
#   scripts/dev.sh
#
# Binds 127.0.0.1 only. Remote access is `tailscale serve` proxying to loopback
# (see README) -- the app itself never listens on a routable address, so a
# misconfigured firewall cannot expose it to the LAN by accident.
set -euo pipefail

VENV="${VENV:-$HOME/venvs/workbench}"
cd "$(dirname "$0")/.."

[ -x "$VENV/bin/uvicorn" ] || { echo "no venv at $VENV -- run scripts/bootstrap.sh" >&2; exit 2; }

exec "$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port 8093 --reload
