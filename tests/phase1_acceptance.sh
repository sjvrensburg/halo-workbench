#!/usr/bin/env bash
# PLAN.md phase 1 acceptance: the job queue serialises, and survives a kill -9.
#
#   tests/phase1_acceptance.sh
#
# Runs against a throwaway state directory, never ~/.local/state/halo-workbench --
# these tests kill processes mid-job and delete job trees, and the real one holds the
# user's documents.
set -euo pipefail

cd "$(dirname "$0")/.."
VENV="${VENV:-$HOME/venvs/workbench}"
PY="$VENV/bin/python"
[ -x "$PY" ] || { echo "no venv at $VENV -- run scripts/bootstrap.sh" >&2; exit 2; }

WORKBENCH_STATE_DIR="$(mktemp -d /tmp/workbench-phase1-XXXXXX)"
export WORKBENCH_STATE_DIR
trap 'rm -rf "$WORKBENCH_STATE_DIR"' EXIT
echo ">> state dir: $WORKBENCH_STATE_DIR"

echo; echo ">> 1/2: three jobs at once run one at a time, in order"
"$PY" tests/phase1_driver.py serial

echo; echo ">> 2/2: kill -9 mid-job leaves failed/interrupted, queue drains on restart"
# The SIGKILL is self-inflicted inside the driver, so the shell sees exit code 137.
set +e
"$PY" tests/phase1_driver.py crash
rc=$?
set -e
[ "$rc" -eq 137 ] || { echo "FAIL: crash driver exited $rc, expected 137 (SIGKILL)" >&2; exit 1; }
"$PY" tests/phase1_driver.py recover

echo; echo ">> phase 1 acceptance: PASS"
