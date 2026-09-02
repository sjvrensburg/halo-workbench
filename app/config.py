"""Every value this app shares with something outside itself.

Anything here is duplicated in a systemd unit, another repo, or `tailscaled`, and
nothing validates the duplication -- so each one carries the name of its other half.
"""

import os
from pathlib import Path

VERSION = "0.1.0"

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

# ── The hint tool's capture store (7g, operator 2026-09-02) ──────────────────
# REVERSES phase 7's "nothing is written to disk": the operator asked for 7-day
# retention of hint captures, matching the other tools' job store. Deliberately a
# SEPARATE directory and a SEPARATE retention knob, not JOBS_DIR/JOB_RETENTION_DAYS,
# because hint has none of a job's lifecycle (no queued/running/done, no worker) and
# because these are a STUDENT's script, not the operator's own document -- if the two
# retention periods are ever tuned differently, they should not be coupled by
# accident. Only the crop actually sent to the model is kept, not the full capture
# frame it came from -- see app/hint_store.py.
HINTS_DIR = STATE_DIR / "hints"
HINT_RETENTION_DAYS = int(os.environ.get("WORKBENCH_HINT_RETENTION_DAYS", 7))

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

# ── The handwriting hint tool's ensemble (PLAN.md phase 7d) ──────────────────
# Every endpoint here is a llama-server unit owned by ../halo-prep except the last, whose
# unit file this repo ships. All are on-demand and none is enabled; the hint page starts
# a member when the operator asks for its voice and model_lease stops it again on idle.
#
# THIS IS NOT halo-prep's BATCH RANKING, and the difference is the point. That bake-off
# scored models for unattended transcription of whole pages (docs/04). This tool is
# attended and works on crops, so two of its disqualifiers stop applying — a silently
# empty answer is harmless when a human is watching for it, and slowness costs seconds on
# a word rather than hours on a stack. `approx_gib` is from halo-prep's config/models.conf
# and is checked against MemAvailable before a start: this box has no swap and OOM-kills
# under pressure (halo-prep pitfall #17), and three of these resident is ~30 GiB.
#
# Gemma-4-26B is deliberately absent, for two separate reasons: it paraphrases at low
# resolution (a paraphraser in an ensemble produces fluent disagreement that is not about
# the ink), and its unit binds :8088 — Open Notebook's port. PLAN.md 7d's page-triage idea
# needs a port that is not someone else's before it can be built.
HINT_MEMBERS = [
    {
        "id": "olmocr",
        "name": "olmOCR-2-7B",
        "endpoint": os.environ.get("WORKBENCH_OLMOCR_ENDPOINT", "http://127.0.0.1:8097"),
        "unit": "llama-olmocr",
        "approx_gib": 10,
        "primary": True,
        # It answers directly, so a word needs almost no room. Measured on a 1459x125
        # crop 2026-08-30: 291 prompt tokens, 19 generated, 0.7 s per sample.
        "max_tokens": {"crop": 256, "page": 1024},
        "note": "15/15 on symbols and the fastest measured (2.7–18.4 s/page). "
                "Apache-2.0. Reads a strikethrough without inventing a negation.",
    },
    {
        "id": "muse",
        "name": "Muse Glimmer 30B",
        "endpoint": os.environ.get("WORKBENCH_MUSE_ENDPOINT", "http://127.0.0.1:8084"),
        "unit": "llama-vision",
        "approx_gib": 20,
        "primary": False,
        # MEASURED THE HARD WAY, 2026-08-30, twice. At 256 tokens Muse returned an empty
        # `content` on three samples out of three, and at 1024 it still did: it spends the
        # budget reasoning and reaches the answer only afterwards. The tool called that
        # silence honestly both times, which is the design working -- and both times the
        # ceiling was the bug, not the model. Given 3000 it answered in **1236 completion
        # tokens for a two-line crop**, 79 of which were the answer. So the ceiling here
        # buys the rumination, not the reading, and 2048 is the measured floor plus room.
        "max_tokens": {"crop": 2048, "page": 3072},
        "note": "A different lineage entirely, so agreement with it means something. "
                "But it hallucinates fluently inside strikethroughs — halo-prep measured "
                "it rendering a struck scribble as `not`, reversing the claim.",
    },
    {
        "id": "chandra",
        "name": "Chandra-OCR-2",
        "endpoint": os.environ.get("WORKBENCH_CHANDRA_ENDPOINT", "http://127.0.0.1:8098"),
        # The one unit here this repo owns: config/llama-chandra.service, which the
        # operator installs. halo-prep rejected the model for batch work and never gave
        # it a unit, and a resident one is not wanted (PLAN.md 7e).
        "unit": "llama-chandra",
        "approx_gib": 6,
        "primary": False,
        # Also a reasoner, and untested on a crop from here -- its measured failure was
        # 568 completion tokens with an empty `content`. Sized like Muse until someone
        # measures it: give it room, then believe the silence when it happens anyway.
        "max_tokens": {"crop": 2048, "page": 3072},
        "note": "Marks struck text `<del>` instead of resolving it, and was the only "
                "model to preserve `generalisability`. Its batch disqualifier was "
                "returning a page as zero bytes; the page says so when it does.",
    },
    {
        "id": "qwen38",
        "name": "Qwen3.8-27B",
        "endpoint": os.environ.get("WORKBENCH_QWEN38_ENDPOINT", "http://127.0.0.1:8090"),
        "unit": "llama-qwen38",
        "approx_gib": 19,
        "primary": False,
        # ~2,000 reasoning tokens per page in the bake-off. Whether a crop is cheaper is
        # the open measurement this member exists to settle.
        "max_tokens": {"crop": 2048, "page": 4096},
        "note": "15/15 in the bake-off at 52–199 s per page, all of it ~2,000 reasoning "
                "tokens. Untested on crops — this is the measurement, not the "
                "recommendation.",
    },
]

# Headroom demanded above a member's own footprint before its unit is started. No swap on
# this box: the failure mode of getting this wrong is the OOM killer taking whichever
# process it likes, which on a marking afternoon is as likely to be Open Notebook as us.
HINT_MEMORY_HEADROOM_GIB = 8

# Samples drawn from one member per press, and the temperature they are drawn at. Sampling
# answers "how sure is this model", which is a different question from "is the ink
# ambiguous" — only asking another model answers that. Cheap because the prefill is
# reused: measured on :8097 2026-08-30, 4080 of 4081 prompt tokens cached, 18.4 s for the
# first sample and 10.5 s for each further one on a FULL PAGE.
HINT_SAMPLES = int(os.environ.get("WORKBENCH_HINT_SAMPLES", 3))

# ...but only from the primary. A second voice is asked a DIFFERENT question -- "is the
# ink ambiguous" -- and one answer settles it, while three do not make it three times
# more independent. It is also what keeps the page inside the operator's patience:
# measured 2026-08-30, Muse takes 8.1-11.7 s per sample on a crop against olmOCR-2's 0.7,
# so three of them is 28 s on their own.
HINT_SECOND_VOICE_SAMPLES = 1
HINT_TEMPERATURE = float(os.environ.get("WORKBENCH_HINT_TEMPERATURE", 0.6))

# Where the hint page's camera actually works, most-preferred first. THE THIRD PAIR OF
# tailscaled REGISTRATIONS THIS FILE NAMES, and like the other two nothing here creates or
# validates them (see PORT above):
#
#   sudo tailscale serve --bg --https=443 http://127.0.0.1:8093
#   tailscale serve --service=svc:halo-workbench --bg --yes --https=443 http://127.0.0.1:8093
#
# The page cannot work these out for itself and must not try. `location.host` keeps the
# port, so swapping the scheme on `http://evo-x3.tail80adad.ts.net:8093` yields
# `https://...:8093`, where nothing listens; and the short MagicDNS name `evo-x3` has no
# certificate at all, because a certificate is issued for a FULL DNS name. Both produce a
# button that fails, which is worse than no button.
#
# So these are written down. If the tailnet is ever renamed or this app moves, they go
# stale silently and the camera link 404s -- that is the same bargain as PORT, and the
# reason both live here rather than in a template.
HINT_HTTPS_ORIGINS = [
    origin for origin in os.environ.get(
        "WORKBENCH_HINT_HTTPS_ORIGINS",
        # The Service name first: it is a name and VIP owned by the tailnet, so it survives
        # this app moving to another machine, and it is what the rest of the UI prefers.
        "https://halo-workbench.tail80adad.ts.net,https://evo-x3.tail80adad.ts.net",
    ).split(",") if origin.strip()
]

# The long edge below which a capture is called too small, on the page and in the API.
# halo-prep measured MiniCPM-V 4.6 at 5/15 on symbols purely because of the resolution it
# reads at; a 640x480 webcam default would reproduce that failure with the good model and
# look like the model's fault. The samples that scored 15/15 were 2851–4096 px.
HINT_MIN_LONG_EDGE_PX = 2000

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
MANAGED_UNITS.update({member["endpoint"]: member["unit"] for member in HINT_MEMBERS})

# Idle before a unit we started is stopped again. Long enough to survive a person
# reading the translation and pasting the next paragraph; short enough that a forgotten
# tab does not hold ~6 GiB of GTT all afternoon.
MODEL_IDLE_SECONDS = int(os.environ.get("WORKBENCH_MODEL_IDLE_SECONDS", 900))

# ...except for the big ones. A marking session is bursty — a squiggle every few minutes —
# so the hint members must survive a pause without a reload, but Muse and Qwen3.8 are ~20
# GiB each and holding them through a coffee break is what PLAN.md 7e says not to do:
# release on idle rather than at end of session. Anything over 12 GiB gets the short
# timer; the 6-10 GiB members keep the long one, because restarting them is ~2 s anyway.
MODEL_IDLE_OVERRIDES = {
    member["unit"]: int(os.environ.get("WORKBENCH_HINT_IDLE_SECONDS", 300))
    for member in HINT_MEMBERS if member["approx_gib"] > 12
}
