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

# The redaction tool's recall layer: openai/privacy-filter as ONNX, on the CPU, in this
# process. NOT a llama-server unit and not on the GPU -- 50M active parameters answer in
# one forward pass, so it owes nothing to the queue the other three ports share, and it
# is the only model in this repo that does not contend for the GPU.
#
# Downloaded by scripts/fetch-privacy-filter.sh; deliberately NOT in halo-prep's
# config/models.conf, which is a manifest of llama-server units and model-switch.sh
# groups. This is neither.
PII_MODEL_DIR = Path(
    os.environ.get("WORKBENCH_PII_MODEL_DIR", Path.home() / "models/privacy-filter")
)

# :8088 is Ornith-1.5-35B-A3B, Open Notebook's worker -- prose, and deliberately NOT
# given the terse splice, since terseness is a defect in prose output.
#
# DECIDED by the phase 6a bake-off, 2026-08-29 (docs/translation-bakeoff.md): Hy-MT2-7B
# on :8094, an on-demand unit this repo owns (config/llama-hymt2.service). 10/10 on the
# mechanical checks at 1.5 s per paragraph, adequacy equal to Ornith-35B, and it does not
# queue behind Open Notebook the way :8088 does. Started on demand and never by this app:
# `systemctl --user start llama-hymt2`.
TRANSLATE_ENDPOINT = os.environ.get(
    "WORKBENCH_TRANSLATE_ENDPOINT", "http://127.0.0.1:8094"
)

# Rephrase is a different job with a different model. An MT model cannot rewrite English
# into better English -- MiLMMT is a pure completion MT model and Hy-MT2 is MT-only -- so
# same-language work goes to :8088, Ornith-1.5-35B-A3B, which is prose-shaped and
# deliberately NOT given the terse splice. It is Open Notebook's worker, so this queues
# behind it; that is accepted, not a bug to fix.
REPHRASE_ENDPOINT = os.environ.get("WORKBENCH_REPHRASE_ENDPOINT", "http://127.0.0.1:8088")

# Where style guides are read from, in order. A guide is a markdown file -- a SKILL.md
# works as-is -- used as the instruction for a rewrite. The Claude skills directory is
# included read-only so a skill like academic-humanizer can be used without copying it,
# and nothing here ever writes to those paths.
STYLE_GUIDE_DIRS = [
    Path(__file__).resolve().parent.parent / "docs/style-guides",
    Path.home() / ".claude/skills",
]

# ── On-demand model units ────────────────────────────────────────────────────
# CHANGED 2026-08-29 on the operator's instruction, reversing "this app starts nothing".
# `llama-hymt2` exists only to serve the translate page, so pressing Translate IS the
# request to start it, and nothing else on the box wants it resident meanwhile.
#
# ONLY units in this map are ever started or stopped, and only when this app started
# them (see app/model_lease.py). :8092, :8088 and :8086 are Hermes's, Open Notebook's
# and its vector search's; those stay the operator's business and a tool page still says
# plainly when one of them is down rather than starting it.
MANAGED_UNITS = {TRANSLATE_ENDPOINT: "llama-hymt2"}

# Idle before a unit we started is stopped again. Long enough to survive a person
# reading the translation and pasting the next paragraph; short enough that a forgotten
# tab does not hold ~6 GiB of GTT all afternoon.
MODEL_IDLE_SECONDS = int(os.environ.get("WORKBENCH_MODEL_IDLE_SECONDS", 900))
