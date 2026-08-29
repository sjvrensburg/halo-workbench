"""Every value this app shares with something outside itself.

Anything here is duplicated in a systemd unit, another repo, or `tailscaled`, and
nothing validates the duplication -- so each one carries the name of its other half.
"""

import os
from pathlib import Path

VERSION = "0.1.0"

# Port. Duplicated in: config/halo-workbench.service, scripts/dev.sh, README, and --
# invisibly, outside both repos -- the `tailscale serve --http=8093` registration in
# tailscaled. A port change means re-running that command by hand; nothing here will
# tell you it is stale, remote clients will just stop working.
PORT = 8093

# Job state. Directories, not a database: survives a restart and can be read with
# `ls` when a user reports something odd. See CLAUDE.md "Jobs are directories".
STATE_DIR = Path(
    os.environ.get("WORKBENCH_STATE_DIR", Path.home() / ".local/state/halo-workbench")
)
JOBS_DIR = STATE_DIR / "jobs"

# Uploads are the user's real documents. 7 days is a guess pending the operator's
# answer (PLAN.md "Open questions"); it is short on purpose -- this is not storage.
JOB_RETENTION_DAYS = 7

# ── The marker wrapper ───────────────────────────────────────────────────────
# Lives in the sibling repo and is called as a subprocess, never imported and never
# bypassed for a bare `marker_single`. Its header comment explains why in detail:
# two incidents in two days where a substitute (pdftotext output, then an LLM's own
# transcription) landed in marker's output path undetected.
MARKER_RUN = Path(
    "/home/stefan/Projects/halo-prep/config/hermes/skills/productivity/"
    "ocr-and-documents/scripts/marker_run.sh"
)

# ── Model endpoints ──────────────────────────────────────────────────────────
# Both are llama-server units owned by halo-prep and BOTH RUN --parallel 1 while
# already serving someone else. Requests from here queue behind theirs. That is
# accepted (CLAUDE.md "Model ports are single-slot"); do not add slots to someone
# else's unit to speed this app up -- halo-prep measured -np 2 on :8088 on
# 2026-08-29 and reverted it, because prefill saturates the GPU with one request.
#
# :8092 is Ornith-1.5-9B, Hermes's model. Short prompts, so it suits the regex loop.
# It also serves a SPLICED TERSE CHAT TEMPLATE (halo-prep pitfall #29): prompts must
# be tuned against `curl -s localhost:8092/props`, not against the model card.
REGEX_ENDPOINT = os.environ.get("WORKBENCH_REGEX_ENDPOINT", "http://127.0.0.1:8092")

# :8088 is Ornith-1.5-35B-A3B, Open Notebook's worker -- prose, and deliberately NOT
# given the terse splice, since terseness is a defect in prose output.
#
# PROVISIONAL. Translation is zh<->en (operator, 2026-08-29) and a general chat model
# is the fallback, not the plan: a purpose-built MT model already sits on disk at
# :8089 (MiLMMT-46-1B, on-demand, owned by ../anti-watermark-mcp), and Hy-MT2-30B-A3B
# is a candidate for its trained do-not-translate instructions. PLAN.md phase 6a is a
# bake-off that decides this value; do not build against it until that has run.
TRANSLATE_ENDPOINT = os.environ.get(
    "WORKBENCH_TRANSLATE_ENDPOINT", "http://127.0.0.1:8088"
)

# Neither server auto-restarts on demand and neither is started by this app. If one
# is stopped, the tool page must say so plainly rather than hanging on a refused
# connection: `systemctl --user is-active llama-ornith` / `llama-ornith35`.
