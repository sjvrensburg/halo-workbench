#!/usr/bin/env bash
# PLAN.md phase 7 acceptance: the handwriting hint tool.
#
#   tests/phase7_acceptance.sh                  # offline: no GPU, no models, no camera
#   tests/phase7_acceptance.sh --live IMAGE     # ...plus the real members on the real box
#
# The offline half stands a fake OpenAI-compatible server in for the models, which is the
# only way to reproduce the failure that matters most here: HTTP 200 with an empty
# `content` and every token spent in `reasoning_content`. It runs on the authoring laptop.
#
# --live needs an image because the halo-prep handwriting samples were student scripts and
# are not on this box. Give it the crop you care about. It starts the members on demand
# and app/model_lease.py's idle timer is what stops them again -- this script does not,
# because a bare `systemctl stop` would cut off a second consumer mid-request.
#
# 7a is NOT tested here and cannot be: whether the camera works depends on the scheme the
# operator's browser reached this box on, which is a `tailscale serve` registration living
# outside both repos. Check it by opening /hint on the laptop -- the page says so itself.
set -euo pipefail

cd "$(dirname "$0")/.."
VENV="${VENV:-$HOME/venvs/workbench}"
[ -x "$VENV/bin/python" ] || { echo "no venv at $VENV -- run scripts/bootstrap.sh" >&2; exit 2; }

echo ">> the readings, the refusals, the routes (no model)"
"$VENV/bin/python" tests/phase7_driver.py offline

if [ "${1:-}" = "--live" ]; then
    [ -n "${2:-}" ] || { echo "--live needs an image path" >&2; exit 2; }
    echo; echo ">> the real members, on the real GPU"
    shift
    "$VENV/bin/python" tests/phase7_driver.py live "$@"
fi
