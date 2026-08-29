#!/usr/bin/env bash
# PLAN.md phase 2 acceptance, the parts that need no GPU: what the sidecar verifier
# refuses, and what a non-PDF does.
#
#   tests/phase2_acceptance.sh
#
# The GPU half -- a real paper converting, the sidecar validating, `pgrep -f surya`
# empty afterwards -- is run by hand, because it takes up to ~20 minutes and holds
# the single worker while it does.
set -euo pipefail

cd "$(dirname "$0")/.."
VENV="${VENV:-$HOME/venvs/workbench}"
PY="$VENV/bin/python"
[ -x "$PY" ] || { echo "no venv at $VENV -- run scripts/bootstrap.sh" >&2; exit 2; }

PORT="${PORT:-8194}"   # not 8093: do not fight the resident service for the port
WORKBENCH_STATE_DIR="$(mktemp -d /tmp/workbench-phase2-XXXXXX)"
export WORKBENCH_STATE_DIR
BASE="http://127.0.0.1:$PORT"

"$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$PORT" \
    > "$WORKBENCH_STATE_DIR/server.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true; rm -rf "$WORKBENCH_STATE_DIR"' EXIT

for _ in $(seq 40); do curl -sf "$BASE/healthz" >/dev/null && break; sleep 0.25; done
curl -sf "$BASE/healthz" >/dev/null || { echo "FAIL: server never came up" >&2; exit 1; }

fails=0
check() {  # check <description> <expected> <actual>
    if [ "$2" = "$3" ]; then printf '  ok   %s\n' "$1"
    else printf '  FAIL %s (expected %s, got %s)\n' "$1" "$2" "$3"; fails=$((fails+1)); fi
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo ">> a non-PDF fails the job, with a reason, and offers no download"
NOT_PDF="$WORKBENCH_STATE_DIR/notes.pdf"
printf 'this is plain text wearing a .pdf extension\n' > "$NOT_PDF"
id=$(curl -s -o /dev/null -w '%{redirect_url}' -F "file=@$NOT_PDF" "$BASE/marker" | sed 's#.*/##')
for _ in $(seq 40); do
    status=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['status'])" \
        "$WORKBENCH_STATE_DIR/jobs/$id/meta.json")
    [ "$status" = "failed" ] && break; sleep 0.25
done
check "job status is failed" failed "$status"
err=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['error'])" \
    "$WORKBENCH_STATE_DIR/jobs/$id/meta.json")
case "$err" in *"is not a PDF"*) check "error says it is not a PDF" y y;; *) check "error says it is not a PDF ($err)" y n;; esac
check "status page renders" 200 "$(code "$BASE/marker/$id")"
check "download is refused" 409 "$(code "$BASE/marker/$id/download")"

echo; echo ">> a verified job downloads, and stops downloading the moment it stops matching"
id=$("$PY" tests/phase2_driver.py fake-done "$WORKBENCH_STATE_DIR")
MD="$WORKBENCH_STATE_DIR/jobs/$id/out/paper/paper.md"
check "markdown downloads" 200 "$(code "$BASE/marker/$id/download")"
check "zip downloads" 200 "$(code "$BASE/marker/$id/download.zip")"

printf '\nan extra line nobody signed\n' >> "$MD"
check "corrupted markdown is refused" 409 "$(code "$BASE/marker/$id/download")"
check "corrupted markdown is refused for the zip too" 409 "$(code "$BASE/marker/$id/download.zip")"

# Restore the bytes, then remove the sidecar instead: checksum and provenance are two
# separate ways to fail, and only the second one catches a file that was never marker's.
"$PY" - "$MD" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); p.write_text(p.read_text().replace("\nan extra line nobody signed\n", ""))
PYEOF
check "restored markdown downloads again" 200 "$(code "$BASE/marker/$id/download")"
rm "$WORKBENCH_STATE_DIR/jobs/$id/out/paper/paper.marker-ok"
check "missing sidecar is refused" 409 "$(code "$BASE/marker/$id/download")"

echo; echo ">> a job that is not marker's, and a path that is not a job"
check "unknown job is 404" 404 "$(code "$BASE/marker/nope")"
check "traversal is 404" 404 "$(code --path-as-is "$BASE/marker/..%2f..%2fetc")"

echo
if [ "$fails" -eq 0 ]; then echo ">> phase 2 acceptance (no-GPU parts): PASS"; else
    echo ">> phase 2 acceptance: $fails FAILED"; exit 1; fi
