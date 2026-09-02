# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in
this repository.

## What this is

A small FastAPI app on the GMKtec EVO-X3 that exposes **specialist, verifiable
local-model tasks** to a browser over Tailscale: PDF→markdown via marker, regex
synthesis from examples, assisted redaction, protected-span Chinese↔English
translation, and a handwriting hint tool that reads one squiggle several ways. One port,
one unit, five pages.

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
scripts/fetch-privacy-filter.sh   # ~920 MB of ONNX into ~/models/privacy-filter, once
scripts/dev.sh                    # foreground uvicorn on 127.0.0.1:8093, --reload
curl -s localhost:8093/healthz    # {"status":"ok","version":...,"queued":N} -- liveness only
```

There is no requirements file, no lockfile and no lint runner: the dependency list
lives in `scripts/bootstrap.sh` (fastapi, uvicorn, jinja2, python-multipart, pymupdf,
onnxruntime, tokenizers), each entry with a comment saying which tool needs it. Add a
dependency by editing that script, so `--force` still reproduces the venv from nothing.
The venv is deliberately *not* `~/venvs/marker` — see the script's header for why.

### Tests

There is no pytest and no suite that runs at build time; that is deliberate, because
the checks that matter here are the per-request verifiers. What exists is one
acceptance script per phase, each a thin `bash` wrapper around a `tests/phaseN_driver.py`
run under the venv — so a "single test" is one of these scripts:

```bash
tests/phase1_acceptance.sh           # the queue: serialisation, kill -9 recovery, the flock
tests/phase2_acceptance.sh           # marker: what the sidecar verifier refuses (no GPU)
tests/phase4_acceptance.sh           # redact: patterns, survivors(), pdftotext on the output
tests/phase5_acceptance.sh           # regex: the runner and the loop's contract
tests/phase5_acceptance.sh --cases   # ...plus ten real cases against :8092
tests/phase6_acceptance.sh           # translate: spans, checks, the vendored masker's sha256
tests/phase6_acceptance.sh --live    # ...plus a real round trip through :8094
tests/phase7_acceptance.sh           # hint: the spread, the refusals, the silent-answer case
tests/phase7_acceptance.sh --live CROP.png   # ...plus the real OCR models
```

Everything without a flag is offline and GPU-free, so it runs on the authoring laptop
too. `VENV=` overrides the interpreter; phases 1 and 2 spin up their own uvicorn on a
throwaway port and `WORKBENCH_STATE_DIR`, so they do not touch the resident service's
state. Where a phase's criteria are about an output rather than a process they stay in
`PLAN.md` and are run by hand against the running app.

The unit is **installed and enabled** as of 2026-08-29 (PLAN.md phase 3), so a dev
server and the resident service now compete for both the port and the state directory.
The lock makes that a clean refusal rather than a fight — stop the service first:

```bash
systemctl --user stop halo-workbench && scripts/dev.sh
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
  validates the duplication. Every endpoint also reads an env override
  (`WORKBENCH_*`), which is how the acceptance drivers point at throwaway state.
- `app/main.py` — FastAPI app: `/`, `/healthz`, the four tool routers, and the
  `lifespan` that owns the worker thread and the model lease. `/healthz` must never
  touch a model server: it has to stay answerable while `:8092` is mid-turn.
- `app/tools/` — one module per tool, each holding its routes, its job handler and
  its verifier together. `marker.py` is the pattern: `@jobs.register("marker")` for the
  work, an `APIRouter` for the pages, and `verify()` re-run at download time so a `.md`
  edited after conversion stops being downloadable.
- `app/redaction.py` — the redaction tool's pure half: patterns, the verbatim rule for
  model proposals, removal, and `survivors()`, the verifier. No FastAPI, no network, so
  it is exercised without a GPU.
- `app/pdf_redaction.py` — PyMuPDF. Deletes content, never draws over it, and refuses
  to return a file its own re-read still finds a span in.
- `app/pii_tagger.py` — the recall layer: `openai/privacy-filter` as q4 ONNX on the
  **CPU**, so it is the one model here that does not contend for the GPU. It proposes;
  it never decides. `:8092` with `enable_thinking: false` is the fallback, and the page
  always says which one ran.
- `app/regex_synth.py`, `app/regex_runner.py` — generate-and-test. The runner is a
  separate process on purpose: `re` has no timeout and cannot be interrupted, so the
  only way to survive catastrophic backtracking is to kill something.
- `app/protect.py` — **vendored** from `../anti-watermark-mcp`, sentinel forms and all.
  Do not edit it: its sha256 is checked against the source by phase 6's acceptance
  script. Layer around it instead — `translation.mask_all` shows how and why.
- `app/translation.py` — chunking, language detection, and the checks. `mask_all`'s
  ordering comment is load-bearing; two shipped-and-caught bugs are recorded in it.
- `app/tools/translate.py` — both modes. Same language on both sides means rewrite, and
  that routes to a different model, not just a different prompt.
- `app/handwriting.py`, `app/tools/hint.py`, `app/hint_store.py` — the fifth tool, and
  the one that is not a job: interactive, not a batch pipeline, and the crop it sends
  is retained 7 days (7g) through its own store, never the job queue's.
  `handwriting.py` stays the pure half — no disk, no store — with one request to a
  VLM, `token_confidences()` for the per-token hesitation view, and
  `group()`/`agreement()`, which count readings and never merge, average or rank
  them. An empty `content` with the tokens spent in `reasoning_content` is a
  *reported result*, not a non-answer; halo-prep measured exactly that on Chandra.
  `hint_store.py` is the disk-touching half: directories like `app/jobs.py`'s, but no
  queue and no worker, because there is no lifecycle here to track.
- `app/model_lease.py` — starts a `MANAGED_UNITS` unit on demand and stops it after
  `MODEL_IDLE_SECONDS` (or `MODEL_IDLE_OVERRIDES`, which is shorter for the ~20 GiB hint
  members). It refuses a cold start whose model does not fit in `MemAvailable`: no swap
  here, and an OOM kill picks its own victim. **Only ever stops a unit it started**: a unit already active
  when the job arrived belongs to someone else and is left alone forever. That one rule
  replaces halo-prep's refcounting, because this queue is one deep.
- `app/views.py`, `app/templates/` — the shared Jinja environment and the pages. Server
  rendered, no build step and no npm; the job pages poll themselves and are `no-store`.
  `hint_index.html` is the exception in size, because the camera, the crop and the
  results are all client side. **It is used from an iPad held over a desk**, which is why
  its heights are in `svh` (on iOS `vh` is the toolbars-collapsed height, so a `vh` box
  overflows the screen), why the capture controls overlay the video rather than following
  it, and why camera choice is `facingMode` with the device dropdown as the fallback
  rather than the other way round. Every one of those was a fault found by using it.
- **Icons are inline SVG, never a Unicode glyph.** `⇄` and `✕` rendered as *blank buttons*
  on iOS Safari 2026-08-30 — the system font has no glyph and nothing falls back or warns.
  The same sweep found `✓`/`✗` on the translate page, where a blank does not merely look
  wrong but erases whether a check passed; those are now the words. A glyph outside Latin
  and common punctuation is a bet on font coverage on a device you are not holding.
- `app/jobs.py` — the job store and the one worker thread. Tools register a handler
  with `@jobs.register("name")`; it either returns (`done`) or raises (`failed`).
  There is no third outcome on purpose — a result the verifier did not pass must
  raise. Startup takes an exclusive `flock` on the state directory (a second instance
  refuses to start rather than recovering the first one's live job), reaps `running`
  jobs as `interrupted` and re-queues `queued` ones.
- `scripts/` — the venv, the dev server, the privacy-filter fetch, and
  `bakeoff-translate.py`, which produced `docs/translation-bakeoff.md` and the JSON in
  `tests/bakeoff/`. Re-run it before contradicting the model choice.
- `config/halo-workbench.service` — the resident unit. `config/llama-hymt2.service` is
  the translate model's, installed but **not enabled**: on-demand is the point.

Build order and per-phase acceptance tests are in `PLAN.md`. **Phases 0–7 are done
(7 on 2026-08-30): all five tools are live and the service is installed and enabled.**

**7a was closed on 2026-08-30 and it added a fifth and sixth serve registration.**
`getUserMedia` is undefined outside a secure context, so HTTPS Certificates was enabled in
the tailnet admin console and `--https=443` registered for both the node and the Service.
Two things to know before touching it:

- **The camera page must be reached by its FULL DNS name** —
  `https://evo-x3.tail80adad.ts.net/hint` or
  `https://halo-workbench.tail80adad.ts.net/hint`. The bare `http://halo-workbench/` this
  app prefers everywhere else is not a secure context and the certificate is not issued
  for it.
- **Enabling certificates published these names to public CT logs, permanently.** That is
  a done and irreversible trade, not a setting to toggle back and forth.

Do not "work around" a future TLS problem with a self-signed certificate or a LAN
listener: the app binds `127.0.0.1` and that binding is the whole access control.

## Ports

| Port | Whose | This app |
|---|---|---|
| 8093 | this app | the only thing it listens on, `127.0.0.1` |
| 8092 | Hermes — Ornith-1.5-9B, spliced terse template | regex synthesis, PII fallback |
| 8088 | Open Notebook — Ornith-1.5-35B-A3B, prose | same-language *rewrite* |
| 8094 | this repo — Hy-MT2-7B, `llama-hymt2` | zh↔en translate; started on demand |
| 8086 | Open Notebook's vector search | not used; never touched |
| 8097 | olmOCR-2-7B | `/hint`'s primary voice; started on demand |
| 8084 | Muse Glimmer 30B (halo-prep's) | `/hint`, second voice, on request only |
| 8090 | Qwen3.8-27B (halo-prep's) | `/hint`, on request only |
| 8098 | this repo — Chandra-OCR-2, `llama-chandra` | `/hint`, third voice; unit ships here, not installed by default |

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
| redact | re-extract the output text — `pdftotext` included — and confirm no removed span survives it |
| regex | the user's own positive/negative examples, executed |
| translate | protected spans present as often as the source has them, plus residual-script and passthrough checks (a rewrite adds: no number or citation changed) |
| hint | **the operator's own eye, per request, by construction** — they are looking at the same ink at the same moment. Do not bolt a synthetic verifier onto this to satisfy the thesis on a technicality; what the thesis asks is that no quality claim goes unchecked, and here none can. What the page owes instead is never to *invite* trust: several readings, never one; disagreement shown as the finding; a silent answer announced |

If you find yourself adding a code path that returns a result the verifier did not
pass, stop. Fail the job instead.

## Load-bearing decisions

- **One port (8093), one service.** A port per tool would mean four `tailscale serve`
  registrations that live outside this repo, are invisible to grep, and do not
  survive a rebuild. halo-prep already carries that problem for four ports; do not
  add more.
- **Reached two ways, and both live outside this repo.** `http://evo-x3:8093` is a
  `tailscale serve` registration; `http://halo-workbench/` is a **Tailscale Service**
  (`svc:halo-workbench`, `tcp:80`, added 2026-08-29) — a name and a VIP owned by the
  tailnet rather than by this box, so links survive this app moving to another machine
  and carry no port. Prefer the Service name in anything user-facing, including the
  header link to `../drp-web`. Neither is regenerated by anything here: the Service also
  needs a definition in the admin console, a `grants` entry in the tailnet policy, and a
  **tagged host** — which is why `evo-x3` now carries `tag:server`, and why the policy
  carries a Tailscale SSH rule for that tag (the stock rule is scoped to
  `autogroup:self`, and a tagged node is not "self"). See `../halo-prep/docs/06` §7.
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
- **The hint tool is the one thing here that is not a job, and the crop it sends is
  retained 7 days** (operator, 2026-09-02, reversing phase 7's "nothing is stored").
  Interactive, not a batch pipeline: it is serialised by its own lock rather than by
  the queue, because a hint that takes twenty minutes is a failed hint while a
  conversion that does is a normal one. What is kept is whatever image was sent to the
  model — a crop when the operator drew one, the WHOLE capture when they pressed
  "Whole capture instead" instead — plus its reading(s), in `app/hint_store.py`'s own
  store with its own 7-day retention (`config.HINTS_DIR`, separate from the job
  store). Never a sample's token/logprob detail. The no-crop case is retained on
  purpose, confirmed with the operator, not a gap: "just the crop" describes what's
  sent, not a guarantee about size. Nothing in `app/tools/hint.py` logs a reading to
  the journal — the capture store is the only place one lands.
- **Jobs are directories, not rows.** `~/.local/state/halo-workbench/jobs/<id>/`.
  Survives a restart, inspectable with `ls` and `cat` when a user reports something
  odd. Do not introduce a database.
- **The translate model starts on demand; nothing else does** (operator, 2026-08-29,
  reversing the app's earlier "starts nothing" rule). `llama-hymt2` exists only to serve
  `/translate`, so pressing Translate is the request to start it, and `app/model_lease.py`
  does — then stops it again after `MODEL_IDLE_SECONDS` of no use. **Only units in
  `config.MANAGED_UNITS` are touched, and only ones this app started**: if the unit was
  already active when the job arrived, someone else is using it and it is left alone
  forever. `:8092`, `:8088` and `:8086` stay the operator's business, and a tool page
  still says plainly when one of those is down instead of starting it.
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

Hint captures (`config.HINTS_DIR`, under the same excluded state directory) are a
different kind of sensitive: a student's script, not the operator's own document.
What's kept is whatever image was actually sent to the model — the crop, or the whole
capture when the operator chose not to crop — never a sample's token detail. Keep it
that way if this store's shape ever changes; do not widen what it keeps without the operator
saying so again.
