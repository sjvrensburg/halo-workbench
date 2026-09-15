# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in
this repository.

## What this is

A small FastAPI app on the GMKtec EVO-X3 that exposes **PDF → markdown via marker** to a
browser over Tailscale. One port, one tool, verified on every request.

It was a five-tool workbench (marker, assisted redaction, regex synthesis, translation,
a handwriting hint tool); the other four moved to AnythingLLM and were removed
2026-09-15. Marker is the one thing here that still earns its own page: it shells out to
a wrapper script that is not safely callable from a general chat tool, and its output is
checked by a checksummed provenance sidecar rather than judged by eye. Their old code and
docs are in git history if any of it is ever needed again — this file no longer describes
them.

It is a sibling of `../halo-prep`, which is the runbook that built the box and holds
the llama-server units, the model manifest and the pitfall list. **Read
`../halo-prep/CLAUDE.md` too** — the couplings it describes (ports, `tailscale serve`
living outside the repo) apply here.

## Machine check first

```bash
cat /sys/class/dmi/id/product_name    # "EVO-X3" = the real box
```

On the EVO-X3 the app and marker are live and yours to run. On the authoring laptop
nothing here works — no marker, no GPU.

**You cannot run `sudo`.** `tailscale serve` and anything system-level goes to the
operator; in Claude Code suggest they prefix the command with `!` so its output lands
in the conversation.

## Commands

```bash
scripts/bootstrap.sh              # create ~/venvs/workbench (--force recreates it)
scripts/dev.sh                    # foreground uvicorn on 127.0.0.1:8093, --reload
curl -s localhost:8093/healthz    # {"status":"ok","version":...,"queued":N} -- liveness only
```

There is no requirements file, no lockfile and no lint runner: the dependency list
lives in `scripts/bootstrap.sh` (fastapi, uvicorn, jinja2, python-multipart), each entry
with a comment saying which piece needs it. Add a dependency by editing that script, so
`--force` still reproduces the venv from nothing. The venv is deliberately *not*
`~/venvs/marker` — see the script's header for why.

### Tests

There is no pytest and no suite that runs at build time; that is deliberate, because
the checks that matter here are the per-request verifier. What exists is one
acceptance script per phase, each a thin `bash` wrapper around a `tests/phaseN_driver.py`
run under the venv:

```bash
tests/phase1_acceptance.sh           # the queue: serialisation, kill -9 recovery, the flock
tests/phase2_acceptance.sh           # marker: what the sidecar verifier refuses (no GPU)
```

Both are offline and GPU-free, so they run on the authoring laptop too. `VENV=`
overrides the interpreter; both spin up their own uvicorn on a throwaway port and
`WORKBENCH_STATE_DIR`, so they do not touch the resident service's state.

The unit is **installed and enabled**, so a dev server and the resident service now
compete for both the port and the state directory. The lock makes that a clean refusal
rather than a fight — stop the service first:

```bash
systemctl --user stop halo-workbench && scripts/dev.sh
systemctl --user status halo-workbench          # and: journalctl --user -u halo-workbench -f
```

## Code map

- `app/config.py` — every value shared with something outside the process (the port,
  the marker wrapper's path, the state dir), and each one names its other half, because
  nothing validates the duplication. `WORKBENCH_STATE_DIR` is how the acceptance
  drivers point at throwaway state.
- `app/main.py` — FastAPI app: `/` (redirects to `/marker`), `/healthz`, the marker
  router, and the `lifespan` that owns the worker thread.  `/healthz` must never touch
  marker or the GPU: it has to stay answerable while a conversion is mid-run.
- `app/tools/marker.py` — the only tool: its routes, its job handler and its verifier
  together. Shells out to `marker_run.sh` (config.MARKER_RUN, in `../halo-prep`), runs
  it in its own process group/cgroup and reaps it on exit (surya leaves ~2.9 GB of model
  servers behind otherwise), checks the output has markdown structure, and writes the
  checksummed `.marker-ok` sidecar. **The sidecar is the verifier** — `verify()` re-runs
  at download time so a `.md` edited after conversion stops being downloadable.
  The upload form exposes three of `marker_single`'s many flags (mode, page range,
  force OCR) — `_validate_options()` checks them at upload time rather than letting
  marker_single's own parser reject a bad one 20 minutes into a job, and `_extra_args()`
  only ever emits flags it already knows about, never a user string used as a flag
  name. `marker_run.sh`'s sidecar records whichever mode actually ran (fixed
  2026-09-15 in `../halo-prep`, alongside this) rather than a hardcoded `fast`.
- `app/jobs.py` — the job store and the one worker thread. Tools register a handler
  with `@jobs.register("name")`; it either returns (`done`) or raises (`failed`). There
  is no third outcome on purpose — a result the verifier did not pass must raise.
  Startup takes an exclusive `flock` on the state directory (a second instance refuses
  to start rather than recovering the first one's live job), reaps `running` jobs as
  `interrupted` and re-queues `queued` ones.
- `app/views.py`, `app/templates/` — the shared Jinja environment and the two pages
  (`marker_index.html`, `marker_job.html`). Server rendered, no build step and no npm;
  the job page polls itself via meta refresh and is `no-store`.
- **Icons are inline SVG, never a Unicode glyph.** A Unicode glyph rendered as a *blank
  button* on iOS Safari once (system font has no glyph, nothing falls back or warns) —
  a bet on font coverage on a device you are not holding.
- `scripts/` — the venv and the dev server.
- `config/halo-workbench.service` — the resident unit.

## The rule the whole project is organised around

**Marker ships a verifier that runs on every request.** This box is good at narrow
tasks with a cheap automatic check and bad at anything judged by eye. Marker is not an
LLM — its output is deterministic and its failure mode is a non-zero exit rather than
plausible slop — but the substitution risk is real (see "Never call `marker_single`
directly" below), so the checksummed `.marker-ok` sidecar is what stands between a
download and a file that only looks like marker's output.

If you find yourself adding a code path that returns a result the verifier did not
pass, stop. Fail the job instead.

## Load-bearing decisions

- **One port (8093), one service.** Still true with one tool: this app is reached the
  same two ways it always was, and neither registration lives in this repo.
- **Reached two ways, and both live outside this repo.** `http://evo-x3:8093` is a
  `tailscale serve` registration; `http://halo-workbench/` is a **Tailscale Service**
  (`svc:halo-workbench`, `tcp:80`) — a name and a VIP owned by the tailnet rather than
  by this box, so links survive this app moving to another machine and carry no port.
  Prefer the Service name in anything user-facing, including the header link to
  `../drp-web`. Neither is regenerated by anything here: the Service also needs a
  definition in the admin console, a `grants` entry in the tailnet policy, and a
  **tagged host**. See `../halo-prep/docs/06` §7.
- **No auth, tailnet only.** The app binds `127.0.0.1` and is reached only through
  `tailscale serve` — that binding is the whole access control, so do not change
  `--host` for convenience, and do not add a LAN listener "for testing".
- **Concurrency 1, and not a config knob.** Marker spawns its own surya model servers
  (~2.9 GB) on top of whatever else this GPU is doing for other apps on this box (see
  `../halo-prep`). This box has **no swap** and OOM-kills under pressure — serialising
  is the cheap insurance.
- **Never call `marker_single` directly.** Always
  `../halo-prep/config/hermes/skills/productivity/ocr-and-documents/scripts/marker_run.sh`.
  Its header comment documents two separate incidents where a substitute — first
  `pdftotext` output, then an LLM's own transcription — was written into marker's
  output path and went undetected. That script exists because prose in a skill file is
  advisory and an exit code is not. The same applies to code here.
- **Jobs are directories, not rows.** `~/.local/state/halo-workbench/jobs/<id>/`.
  Survives a restart, inspectable with `ls` and `cat` when a user reports something
  odd. Do not introduce a database.

## Conventions

Follow halo-prep's editorial conventions, because this repo will be read the same way:

- Every non-obvious value carries a comment saying **why**, ideally naming the failure
  it avoids or the source it contradicts.
- Contested claims stay contested, with an instruction to measure.
- Performance claims are concrete and attributed to a measurement condition. No
  unsourced numbers.
- Shell scripts: `#!/usr/bin/env bash`, `set -euo pipefail`, header comment saying what
  it does and how to run it.
- Python: standard library plus FastAPI; no framework beyond it. Prefer boring.
- Markdown wraps ~95 columns.

## Privacy

Uploaded PDFs are the user's real documents — invoices, papers, correspondence.
`.gitignore` excludes the job state directory and any stray PDF. Never commit an
upload, never paste document contents into a commit message, and do not add logging
that writes document text to the journal.
