# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in
this repository.

## What this is

A small FastAPI app on the GMKtec EVO-X3 that exposes **specialist, verifiable
local-model tasks** to a browser over Tailscale: PDF→markdown via marker, regex
synthesis from examples, assisted redaction, and protected-span Chinese↔English
translation. One port, one unit, four pages.

It is a sibling of `../halo-prep`, which is the runbook that built the box and holds
the llama-server units, the model manifest and the pitfall list. **Read
`../halo-prep/CLAUDE.md` too** — the couplings it describes (ports, `--parallel 1`,
`tailscale serve` living outside the repo) all apply here.

`PLAN.md` is the build plan and is the file to start from. It is written to be
executed in order.

## Machine check first

```bash
cat /sys/class/dmi/id/product_name    # "EVO-X3" = the real box
```

On the EVO-X3 the app and the models are live and yours to run. On the authoring
laptop nothing here works — no models, no marker, no GPU.

**You cannot run `sudo`.** `tailscale serve` and anything system-level goes to the
operator; in Claude Code suggest they prefix the command with `!` so its output lands
in the conversation.

## Commands

```bash
scripts/bootstrap.sh              # create ~/venvs/workbench (--force recreates it)
scripts/dev.sh                    # foreground uvicorn on 127.0.0.1:8093, --reload
curl -s localhost:8093/healthz    # {"status":"ok","version":...} -- liveness only
```

There is no requirements file, no lockfile and no lint or test runner yet: the
dependency list lives in `scripts/bootstrap.sh` (fastapi, uvicorn, jinja2,
python-multipart) and `tests/` is empty. Add a dependency by editing that script, so
`--force` still reproduces the venv from nothing. The venv is deliberately *not*
`~/venvs/marker` — see the script's header for why.

Acceptance tests live in `PLAN.md`, one per phase. Where a phase's criteria are
about the *process* rather than an output they are scripted — `tests/phase1_acceptance.sh`
runs the queue's serialisation and `kill -9` recovery against a throwaway state dir —
otherwise they are run by hand against the running app. That is the intended shape: the checks that matter here are the
per-request verifiers, not a suite that runs at build time.

The unit is written but **not installed** (PLAN.md phase 3 installs it, after marker
works):

```bash
cp config/halo-workbench.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now halo-workbench
systemctl --user status halo-workbench          # and: journalctl --user -u halo-workbench -f
```

Checking the model servers this app depends on — neither is started by this app, and
a stopped one must produce a plain message on the tool page, never a hang:

```bash
systemctl --user is-active llama-ornith llama-ornith35   # :8092 and :8088
curl -s localhost:8092/props                             # tune prompts against this
```

## Code map

- `app/config.py` — **read this first.** It is not settings; it is every value shared
  with something outside the process (the port, the marker wrapper's path, the two
  model endpoints, the state dir), and each one names its other half, because nothing
  validates the duplication. `TRANSLATE_ENDPOINT` is explicitly provisional pending
  the phase 6a bake-off.
- `app/main.py` — FastAPI app, currently `/` and `/healthz` only. `/healthz` must
  never touch a model server: it has to stay answerable while `:8092` is mid-turn.
- `app/tools/` — one module per tool, each holding its routes, its job handler and
  its verifier together. `marker.py` is the pattern: `@jobs.register("marker")` for the
  work, an `APIRouter` for the pages, and `verify()` re-run at download time so a `.md`
  edited after conversion stops being downloadable.
- `app/views.py`, `app/templates/` — the shared Jinja environment and the pages.
- `app/jobs.py` — the job store and the one worker thread. Tools register a handler
  with `@jobs.register("name")`; it either returns (`done`) or raises (`failed`).
  There is no third outcome on purpose — a result the verifier did not pass must
  raise. Startup takes an exclusive `flock` on the state directory (a second instance
  refuses to start rather than recovering the first one's live job), reaps `running`
  jobs as `interrupted` and re-queues `queued` ones.
- `scripts/`, `config/halo-workbench.service` — the venv, dev server, and unit.

Build order and per-phase acceptance tests are in `PLAN.md`; phase 0 (scaffold) is
done, phases 1–6 are not started.

## The rule the whole project is organised around

**Every tool ships a verifier that runs on every request.**

This box is good at narrow tasks with a cheap automatic check and bad at anything
judged by eye — that is the lesson of halo-prep's whole history, and it is why this
project exists in the shape it does. A feature without a verifier is not a smaller
version of the feature; it is a different, worse thing that looks the same.

The four verifiers, concretely:

| Tool | Verifier |
|---|---|
| marker | the `.marker-ok` provenance sidecar, checksum-matched against the `.md` |
| redact | re-extract the output text and confirm no removed span survives it |
| regex | the user's own positive/negative examples, executed |
| translate | protected spans present once and in order, plus a residual-script check |

If you find yourself adding a code path that returns a result the verifier did not
pass, stop. Fail the job instead.

## Load-bearing decisions

- **One port (8093), one service.** A port per tool would mean four `tailscale serve`
  registrations that live outside this repo, are invisible to grep, and do not
  survive a rebuild. halo-prep already carries that problem for four ports; do not
  add more.
- **No auth, tailnet only** (operator, 2026-08-29), matching the four existing ports.
  It follows that the app binds `127.0.0.1` and is reached only through
  `tailscale serve` — that binding is the whole access control, so do not change
  `--host` for convenience, and do not add a LAN listener "for testing".
- **Redaction output is a candidate, not a redacted document.** The verifier proves
  everything found was removed; nothing can prove nothing was missed. Every label the
  user sees must say so. See PLAN.md phase 4 — this is a wording rule with a real
  failure behind it, not a disclaimer.
- **PDF redaction deletes content; it never draws over it.** PyMuPDF
  `apply_redactions`, and the acceptance test is `pdftotext` on the *output* finding
  none of the removed strings. A black rectangle over live text is the classic way this
  goes wrong and would be the most severe bug this repo could ship. Equally: a
  confirmed span that cannot be located on the page fails the job — never skip it.
- **Concurrency 1, and not a config knob.** Every tool here contends for the same GPU
  as three resident llama-server units. Marker additionally spawns its own surya model
  servers (~2.9 GB). This box has **no swap** (phase 2 of halo-prep removes zram) and
  OOM-kills under pressure — halo-prep pitfall #17. Serialising is the cheap insurance;
  parallelism buys nothing anyway, since prefill already saturates the GPU with one
  request (halo-prep measured `-np 2` and reverted it, 2026-08-29).
- **Never call `marker_single` directly.** Always
  `../halo-prep/config/hermes/skills/productivity/ocr-and-documents/scripts/marker_run.sh`.
  Its header comment documents two separate incidents where a substitute — first
  `pdftotext` output, then an LLM's own transcription — was written into marker's
  output path and went undetected. That script exists because prose in a skill file is
  advisory and an exit code is not. The same applies to code here.
- **Jobs are directories, not rows.** `~/.local/state/halo-workbench/jobs/<id>/`.
  Survives a restart, inspectable with `ls` and `cat` when a user reports something
  odd. Do not introduce a database.
- **Model ports are single-slot and shared.** `:8092` is Hermes's model and `:8088` is
  Open Notebook's; both run `--parallel 1`. Requests from here queue behind theirs and
  theirs behind ours. That is accepted, not a bug to fix — do not add `--parallel` to
  someone else's unit to make this app faster.
- **`:8092` serves a spliced terse chat template** (halo-prep pitfalls #29, #30). Tune
  prompts against `curl -s localhost:8092/props`, not against the model card. And do
  not carry that splice to another model to make it terse — that was measured and took
  a different model from 15/15 to 1/15.

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
