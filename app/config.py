"""Every value this app shares with something outside itself.

Anything here is duplicated in a systemd unit, another repo, or `tailscaled`, and
nothing validates the duplication -- so each one carries the name of its other half.
"""

import os
from pathlib import Path

VERSION = "0.2.0"

# Port. Duplicated in: config/halo-workbench.service, scripts/dev.sh, README, and --
# invisibly, outside both repos -- TWO registrations in tailscaled: the
# `tailscale serve --http=8093` one (http://evo-x3:8093) and the Tailscale Service
# `svc:halo-workbench` (http://halo-workbench/), which proxies port 80 to this one.
# A port change means re-running both by hand; nothing here will tell you they are
# stale, remote clients will just stop working.
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
    "/home/stefan/Projects/halo-prep/config/pi/skills/"
    "ocr-and-documents/scripts/marker_run.sh"
)

# ── Optional LLM enhancement (marker's --use_llm) ────────────────────────────
# Ornith-1.5-35B-A3B, Open Notebook's resident worker on :8088, gained vision
# 2026-09. It answers as an OpenAI-compatible multimodal endpoint (verified against
# `/v1/models` and `/health`, 2026-09-15), so marker's stock
# `marker.services.openai.OpenAIService` can point straight at it -- no
# marker-specific model, and no on-demand start/stop to manage, because this unit is
# already always up for Open Notebook's own sake and is NOT started or stopped by this
# app (same rule as CLAUDE.md's old "only ever touches a unit it started" -- :8088 was
# never one of them). Enabling this on a job means marker's LLM-correction passes
# queue behind whatever Open Notebook is already doing on that port, same as
# `REPHRASE_ENDPOINT` used to when this app had a rewrite tool -- accepted, not a bug.
LLM_ENHANCE_ENDPOINT = os.environ.get("WORKBENCH_LLM_ENDPOINT", "http://127.0.0.1:8088")
LLM_ENHANCE_MODEL = os.environ.get("WORKBENCH_LLM_MODEL", "ornith-1.5-35b-a3b")
