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

echo; echo ">> 1/3: three jobs at once run one at a time, in order"
"$PY" tests/phase1_driver.py serial

echo; echo ">> 2/3: kill -9 mid-job leaves failed/interrupted, queue drains on restart"
# The SIGKILL is self-inflicted inside the driver, so the shell sees exit code 137.
set +e
"$PY" tests/phase1_driver.py crash
rc=$?
set -e
[ "$rc" -eq 137 ] || { echo "FAIL: crash driver exited $rc, expected 137 (SIGKILL)" >&2; exit 1; }
"$PY" tests/phase1_driver.py recover

echo; echo ">> 3/3: a second instance refuses the state directory instead of recovering it"
PORT="${PORT:-8195}"
"$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$PORT" \
    > "$WORKBENCH_STATE_DIR/first.log" 2>&1 &
FIRST=$!
trap 'kill $FIRST 2>/dev/null || true; rm -rf "$WORKBENCH_STATE_DIR"' EXIT
for _ in $(seq 40); do curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && break; sleep 0.25; done

# Second instance, same state dir, different port -- so the only thing that can stop
# it is the lock, not the port bind. It must fail BEFORE recovery: uvicorn runs the
# lifespan before binding, so "address already in use" would have been too late.
set +e
"$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port $((PORT + 1)) \
    > "$WORKBENCH_STATE_DIR/second.log" 2>&1
rc=$?
set -e
if [ "$rc" -eq 0 ]; then echo "  FAIL second instance started anyway" >&2; exit 1; fi
grep -q "StateDirBusy\|already using" "$WORKBENCH_STATE_DIR/second.log" \
    || { echo "  FAIL second instance failed for the wrong reason:" >&2;
         tail -3 "$WORKBENCH_STATE_DIR/second.log" >&2; exit 1; }
grep -q "marked interrupted" "$WORKBENCH_STATE_DIR/second.log" \
    && { echo "  FAIL second instance ran recovery before refusing" >&2; exit 1; }
echo "  ok   it exits $rc, says why, and touches nothing"
kill $FIRST 2>/dev/null || true

echo; echo ">> phase 1 acceptance: PASS"
