# halo-workbench — build plan

Written 2026-08-29 on the EVO-X3. This is the plan a later session should execute.
Phase 0 is already done and verified; phases 1–4 are not started.

Read `CLAUDE.md` before touching anything — it holds the decisions that are easy to
reverse by accident. This file holds the *order* and the *acceptance tests*.

---

## The thesis this project is built on

The EVO-X3 is not a Claude Code substitute and every attempt to make it one has been
retired (halo-prep: the Executor/Mentor split lasted a day; `kat-coder` is disabled;
`config/opencode.json` has one provider and it is not resident). What has survived on
this box is narrow, specialist work: Hermes, the podcast pipeline, embeddings, marker.

The pattern that separates the two: **local models earn their keep where there is a
cheap automatic verifier.** Whisper-verify on TTS clips, 15/15 on the tool harness,
wikitext PPL 37.25 vs 14468 — each of those caught a real regression that a human
reading output would have waved through. Large coding tasks have no verifier, so
every quality judgement about them stayed vibes and every adoption got reversed.

Every tool in this project must therefore ship with a verifier that runs on every
request, not a test suite that runs at build time. Where the tool's own plan below
does not name one, that tool is not ready to build.

## Shape

One FastAPI app, one port (**8093**), one systemd user unit, one `tailscale serve`
registration, three pages. Not three services — see CLAUDE.md "One port".

```
  browser (laptop / iPad, over Tailscale)
        │  http://evo-x3:8093
        ▼
  halo-workbench  (uvicorn, ~/venvs/workbench)
        ├── /marker    → subprocess: halo-prep marker_run.sh   (no LLM of ours)
        ├── /regex     → llama-server :8092  + generate-and-test loop
        └── /translate → llama-server :8088  + placeholder round-trip check
                │
                └── all three go through ONE job queue, concurrency 1
```

---

## Phase 0 — scaffold ✅ DONE 2026-08-29

Done and verified in this session:

- repo at `/home/stefan/Projects/halo-workbench`, `git init`, first commit
- `~/venvs/workbench` with fastapi + uvicorn + jinja2 + python-multipart
- `app/main.py` serving `/` and `/healthz`
- `scripts/bootstrap.sh` (recreates the venv from scratch)
- `scripts/dev.sh` (foreground uvicorn on :8093, `--reload`)
- `config/halo-workbench.service` (written, **not installed, not enabled**)

Verified by: `scripts/bootstrap.sh` exits 0, `scripts/dev.sh` serves
`{"status":"ok"}` on `/healthz`. Both were run.

---

## Phase 1 — the job queue

Nothing else can be built first. Marker takes up to ~20 minutes on a real paper
(observed in halo-prep, 2026-08-23 — an agent concluded it had stalled at that point
and it had not), so no tool here can be request/response.

**Build**

- `app/jobs.py`: a job store on disk at `~/.local/state/halo-workbench/jobs/<id>/`,
  one directory per job holding `meta.json` (status, tool, timestamps, error),
  the input file, and whatever the tool produced. No database — the directory *is*
  the record, it survives a restart, and the operator can `cat` it when something
  goes wrong.
- A single worker thread draining a `queue.Queue`. **Concurrency stays 1** and is not
  a config knob (CLAUDE.md "Concurrency 1").
- Status values: `queued` → `running` → `done` | `failed`. A job that was `running`
  when the process died is `failed` on next startup with `error: "interrupted"` —
  never left `running`, or the UI hangs forever on a job nobody is doing.
- Retention: prune job dirs older than 7 days on startup. Uploads are the user's
  documents; do not keep them indefinitely and do not silently delete today's.

**Acceptance**

- Submit three jobs at once; they run one at a time, in order, and all three finish.
- `kill -9` the service mid-job, restart: that job reads `failed / interrupted`,
  the queued ones still run.

## Phase 2 — the marker tool  ← ship this first, it is the whole point of the project

The highest-value tool and the lowest-risk, because **marker is not an LLM**: its
output is deterministic and its failure mode is a non-zero exit rather than plausible
slop.

**Build**

- `POST /marker` — multipart PDF upload → job id. `GET /marker/<id>` — status page.
  `GET /marker/<id>/download` — the markdown; `.zip` variant when marker extracted
  images (it writes them beside the `.md`).
- The worker shells out to
  `/home/stefan/Projects/halo-prep/config/hermes/skills/productivity/ocr-and-documents/scripts/marker_run.sh <pdf> <jobdir>`.
  **Do not reimplement it and do not call `marker_single` directly.** That script is
  ~150 lines of scar tissue from two substitution incidents in two days; read its
  header comment before you consider bypassing it. It sets the two mandatory env vars,
  runs marker in its own process group and kills the *group* on exit (surya leaves
  1.7 GB + 1.2 GB of model servers behind otherwise), checks the output has markdown
  structure, and writes a checksummed `.marker-ok` provenance sidecar.
- **The sidecar is the verifier.** Serve a download only when
  `<output>.marker-ok` exists AND its `markdown_sha256` matches the file on disk.
  No sidecar, no download — show the job as failed with the stderr log. This is what
  makes it impossible to hand a user `pdftotext` output, or an LLM transcription,
  wearing marker's filename.
- Surface `marker_run.sh` exit 3 (structure check failed) distinctly from exit 1.
  It means "suspect output" and the page should say so rather than offering a file.
- Show elapsed time on the status page, prominently. The single most likely user
  error is concluding a 20-minute conversion has hung.

**Acceptance**

- A real paper PDF converts, the sidecar validates, the markdown downloads.
- Corrupt the produced `.md` by hand → download is refused (checksum mismatch).
- Convert a non-PDF / a scanned image-only PDF that marker rejects → the page shows
  the failure and the stderr tail, and offers nothing to download.
- `pgrep -f surya` is empty 30 s after a job finishes, and after a job is killed.

## Phase 3 — reachability

- Install the unit: `systemctl --user enable --now halo-workbench` (add to halo-prep's
  `config/models.conf`? **No** — that manifest is for llama-server units and
  `model-switch.sh` groups. This is not one. Note it in halo-prep's README instead.)
- `tailscale serve --bg --http=8093 http://127.0.0.1:8093` — **operator step, needs
  sudo/operator rights, and it lives outside both repos.** halo-prep's CLAUDE.md
  already warns that the serve config is invisible to grep; this adds a fifth port to
  the four in `docs/06` §7 and `docs/10`, so update those tables in the same commit.
- Verify from the laptop: `curl http://evo-x3:8093/healthz`.

Do phases 1–3 and stop. Get real use out of marker before building anything else —
if the other two never get built, the project has already paid for itself.

## Phase 4 — the regex tool

Right instinct, wrong shape as originally sketched: not a chatbot. There is no
specialist regex model worth chasing on HF; the good ones are general models. But
regex is the most verifiable task on the list, so make the *loop* the product.

**Build**

- Input: positive examples (must match), negative examples (must not match), an
  optional prose description, and a flavour (Python `re` first; PCRE/JS later).
- Ask the model for **5 candidates at once**, test each against every example,
  feed the failures back, iterate to a cap (~6 rounds). Return the first candidate
  that passes everything, plus the ones that nearly did.
- **The examples are the verifier** — that is the entire design. Never return a
  pattern that has not been executed against every example the user gave.
- Test each candidate in a **subprocess with a hard timeout** (~1 s). Catastrophic
  backtracking is not hypothetical for generated patterns, and `re` has no timeout;
  a hung candidate must fail that candidate, not the request.
- Model: `:8092` (Ornith-1.5-9B). Prompts are short, so it is a good fit — but that
  server is `--parallel 1` and **shared with Hermes**, so a long Hermes turn queues
  this and vice versa (halo-prep pitfall #34). Acceptable at this scale; do not "fix"
  it by adding slots (halo-prep: `-np 2` was measured and reverted 2026-08-29).
- `:8092` serves the **spliced terse chat template** (halo-prep pitfall #29/#30).
  Tune prompts against what actually answers on that port, not against stock Ornith.

**Acceptance**

- Ten hand-written cases with known-good answers; the loop finds a passing pattern
  for at least eight, and for the two it misses it says so instead of returning
  something untested.
- A prompt engineered to produce `(a+)+$` against a long non-matching input fails
  that candidate on timeout and the request still returns.

## Phase 5 — the paraphrase / translate tool

**Scope, set by the operator 2026-08-29: Chinese ↔ English. Afrikaans is out of
scope** — do not spend time on it, and delete this sentence's premise rather than
re-litigating it later.

Lowest priority of the three, and the only one that may need a new model on disk.

### 5a. Bake-off first — no UI until a model is chosen

Three candidates, and the box already owns two of them:

| Candidate | On disk? | Notes |
|---|---|---|
| **MiLMMT-46-1B** (`:8089`, `llama-milmmt.service`) | yes, Q8_0 | purpose-built MT, already wired for `../anti-watermark-mcp`, ~1 GiB, on-demand |
| **MiLMMT-46-12B** (`~/models/milmmt-46-12b`) | yes, Q8_0 | same family, no unit; the 1B unit's comment says swap `--model` and `--alias` to try it |
| **Hy-MT2-30B-A3B** (Tencent, HF) | **no** | operator's suggestion; ships *trained* support for special instructions (terminology, do-not-translate, style) |

Hy-MT2's trained instruction support is the interesting part, and the reason to
take the download seriously: **a native lever beats a hand-rolled one.** That is a
lesson this box has already paid for — halo-prep pitfall #30, where a hand-spliced
terseness template took Granite from 15/15 to 1/15 while the model's own `low_effort`
flag bought the same latency at 15/15. If Hy-MT2 can be *told* which spans not to
translate, the sentinel machinery in 5b becomes a fallback rather than the mechanism.

**But a native lever still has to be verified, not trusted.** Whichever mechanism is
used, the checks in 5b run on every request either way.

Cost of adopting Hy-MT2, all of it manual and none of it generated:

- ~17–20 GiB download (3.2 TiB free, so disk is not the constraint), a new
  `llama-hymt2.service`, and a new port — **`:8094`**, since `:8089` belongs to
  MiLMMT and `../anti-watermark-mcp` pins that unit by name via
  `AWM_TRANSLATOR_MODEL`.
- A hand-added line in halo-prep's `config/models.conf` (nothing generates it):
  `hymt2-30b-a3b|llama-hymt2.service|8094|~18|translation`. **Group `translation`**,
  matching MiLMMT, so `model-switch.sh` never leaves two translators fighting for GTT.
  30B-A3B is ~18 GiB against three resident units — it must be on-demand, not enabled.
- A `tailscale serve` registration only if it needs to be reachable directly; the
  workbench proxies it, so probably not.
- Read halo-prep's CLAUDE.md "Cross-file couplings" before writing the unit. The
  `--ctx-size` ↔ consumer rule applies: this app is the consumer.

**Bake-off method.** ~10 real paragraphs of the kind actually wanted (both directions,
zh→en and en→zh), each with 3–5 protected spans. Score: adequacy by eye, plus the
mechanical checks from 5b, plus wall-clock per paragraph. Write the result to
`docs/translation-bakeoff.md` — including if the answer is "the 1B already on disk is
good enough", which would make this phase nearly free. **If none of them is good, say
so and stop.** A translation tool that quietly produces poor Chinese is worse than no
translation tool.

### 5b. The tool

- Input: text, plus regex patterns (and/or literal selections) marking spans that must
  survive untranslated — citations, variable names, product names, LaTeX.
- Mechanism: either the model's native do-not-translate instruction (Hy-MT2) or
  sentinel substitution (`⟦0⟧`, `⟦1⟧`, …) before the call, restored after. **Models
  drop, translate, renumber and duplicate sentinels** — that is why 5b's checks exist
  regardless of which mechanism is chosen.
- **Verifier, three cheap mechanical checks, every request:**
  1. **Spans.** Every protected span present in the output exactly once, in the
     original order. Fail loudly and offer a retry; never silently patch a mangled
     sentinel.
  2. **Script ratio.** For zh→en, the output should contain essentially no CJK
     codepoints outside protected spans (and the inverse for en→zh). A high residual
     ratio means the model translated part of the input and copied the rest — the
     characteristic failure of a small MT model on a long paragraph, and invisible to
     anyone who does not read both languages.
  3. **Passthrough.** Output ≈ input means the model declined and echoed. Fail it.
- Chunking: MiLMMT's unit comment notes ~1500-character chunks for that stage.
  Whole-document translation in one call is where check 2 starts firing.

**Acceptance**

- A paragraph with 5 protected spans round-trips with all 5 byte-identical.
- A sentinel-hostile input (sentinels adjacent to CJK punctuation, one at the very
  start, one at the very end) either round-trips or fails visibly.
- A paragraph the model half-translates is caught by check 2 rather than returned.

---

## Open questions for the operator

1. **Auth.** `tailscale serve` is tailnet-only and there is no API key on any of the
   four existing ports. Those are inference endpoints; this one accepts file uploads
   and stores documents. Single-user tailnet makes it defensible, but it is a
   deliberate choice, not an oversight — decide it explicitly before phase 3.
2. **Languages** for phase 5, and whether Afrikaans is in scope.
3. **Retention**: 7 days for uploaded PDFs is a guess. Confirm.
4. Does anything else want the job queue (batch marker over a directory, whisper
   transcription)? Do not build for it now; just do not make it impossible.
