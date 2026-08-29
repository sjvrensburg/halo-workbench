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
# pymupdf: the redaction tool's PDF path (phase 4b). It is the only library here that
# can DELETE page content rather than draw over it -- add_redact_annot +
# apply_redactions -- which is the difference between redaction and a picture of
# redaction. Deliberately installed in this venv and not in ~/venvs/marker: halo-prep's
# ocr-and-documents rewrite pointed away from PyMuPDF, and marker's ROCm/torch stack is
# not somewhere to add a C extension for a different tool's sake.
# onnxruntime + tokenizers: the redaction tool's recall layer runs openai/privacy-filter
# as ONNX on the CPU (scripts/fetch-privacy-filter.sh fetches the weights). CPU on
# purpose -- 50M active parameters answer in one forward pass, and this box's GPU is
# already shared by three llama-server units.
"$VENV/bin/pip" install --quiet fastapi 'uvicorn[standard]' jinja2 python-multipart pymupdf \
    onnxruntime tokenizers

"$VENV/bin/python" - <<'PY'
import fastapi, uvicorn, jinja2, pymupdf, onnxruntime
print(f">> fastapi {fastapi.__version__}, uvicorn {uvicorn.__version__}, "
      f"jinja2 {jinja2.__version__}, pymupdf {pymupdf.__version__}, "
      f"onnxruntime {onnxruntime.__version__}")
PY

cat <<'EOF'

>> Done.

   Run it:      scripts/dev.sh
   Check it:    curl -s localhost:8093/healthz
   Models:      scripts/fetch-privacy-filter.sh   (the redaction recall layer)
EOF
