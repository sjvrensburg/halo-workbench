#!/usr/bin/env bash
# Fetch openai/privacy-filter (q4 ONNX + tokenizer) for the redaction tool's recall layer.
#
#   scripts/fetch-privacy-filter.sh
#
# ~920 MB into ~/models/privacy-filter. Apache-2.0. Runs on the CPU through
# onnxruntime, so it is NOT a llama-server unit, gets no entry in halo-prep's
# config/models.conf (that manifest is units and model-switch.sh groups), and does not
# queue behind the three GPU model servers.
#
# Without it the redaction tool falls back to :8092 with thinking off and says so on
# the page; it never silently runs with no recall layer at all.
set -euo pipefail

DEST="${WORKBENCH_PII_MODEL_DIR:-$HOME/models/privacy-filter}"
command -v hf >/dev/null || { echo "hf CLI not found: curl -LsSf https://hf.co/cli/install.sh | bash -s" >&2; exit 2; }

echo ">> downloading openai/privacy-filter into $DEST"
hf download openai/privacy-filter \
    --include "onnx/model_q4.onnx" \
    --include "onnx/model_q4.onnx_data" \
    --include "tokenizer.json" \
    --include "tokenizer_config.json" \
    --include "config.json" \
    --local-dir "$DEST"

echo ">> done: $(du -sh "$DEST" | cut -f1)"
