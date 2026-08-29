#!/usr/bin/env bash
# Create (or recreate) the virtualenv this app runs in.
#
#   scripts/bootstrap.sh [--force]
#
# Deliberately its own venv rather than ~/venvs/marker: marker's environment is a
# large, fragile ROCm/torch stack that took real effort to get working, and adding a
# web framework to it means a future `pip install` here can break document
# conversion. This app never imports marker — it shells out to marker_run.sh — so
# they have no reason to share an environment.
set -euo pipefail

VENV="${VENV:-$HOME/venvs/workbench}"

if [ "${1:-}" = "--force" ] && [ -d "$VENV" ]; then
    echo ">> removing $VENV"
    rm -rf "$VENV"
fi

if [ ! -d "$VENV" ]; then
    echo ">> creating $VENV"
    python3 -m venv "$VENV"
fi

echo ">> installing dependencies"
"$VENV/bin/pip" install --quiet --upgrade pip
# fastapi + uvicorn: the app. jinja2: server-rendered pages (no build step, no npm --
# this is three forms and a status page, not a frontend). python-multipart: FastAPI
# refuses file uploads without it, with an error that reads like a bug in your code.
"$VENV/bin/pip" install --quiet fastapi 'uvicorn[standard]' jinja2 python-multipart

"$VENV/bin/python" - <<'PY'
import fastapi, uvicorn, jinja2
print(f">> fastapi {fastapi.__version__}, uvicorn {uvicorn.__version__}, jinja2 {jinja2.__version__}")
PY

cat <<'EOF'

>> Done.

   Run it:      scripts/dev.sh
   Check it:    curl -s localhost:8093/healthz
   Next:        PLAN.md phase 1 (the job queue)
EOF
