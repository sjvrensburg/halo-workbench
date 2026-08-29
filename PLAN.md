# halo-workbench — build plan

Written 2026-08-29 on the EVO-X3. This is the plan a later session should execute.
Phase 0 is already done and verified; phases 1–6 are not started.

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
registration, four pages. Not four services — see CLAUDE.md "One port".

```
  browser (laptop / iPad, over Tailscale)
        │  http://evo-x3:8093        no auth — tailnet only (operator, 2026-08-29)
        ▼
  halo-workbench  (uvicorn, ~/venvs/workbench)
        ├── /marker    → subprocess: halo-prep marker_run.sh   (no LLM of ours)
        ├── /redact    → deterministic patterns FIRST, model as recall booster
        ├── /regex     → llama-server :8092  + generate-and-test loop
        └── /translate → MT model (phase 6a picks it) + round-trip check
                │
                └── all four go through ONE job queue, concurrency 1
```

`/redact` composes with `/marker`: a converted PDF should be redactable in one click
from its result page, without a round trip through the user's disk.

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

## Phase 1 — the job queue ✅ DONE 2026-08-29

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
- Retention: prune job dirs older than **7 days** (operator, 2026-08-29) on startup
  *and* on a timer — startup-only leaves a long-running service never pruning. Uploads
  are the user's documents; do not keep them indefinitely and do not silently delete
  today's. Delete the whole job directory, not just the output: the uploaded original
  is the sensitive half.

**Acceptance**

- Submit three jobs at once; they run one at a time, in order, and all three finish.
- `kill -9` the service mid-job, restart: that job reads `failed / interrupted`,
  the queued ones still run.

Both are automated in `tests/phase1_acceptance.sh` (throwaway state dir, real
SIGKILL) and passed on 2026-08-29. Retention was checked separately: an 8-day-old
job and a meta-less directory are pruned, a fresh job and a stale *queued* job are
not.

## Phase 2 — the marker tool ✅ DONE 2026-08-29

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

All four passed on 2026-08-29. `dang_ullah_2022.pdf` (12 pages) converted in 81 s,
4 images extracted, sidecar validated, markdown and `.zip` downloaded; corrupting the
`.md` and removing the sidecar each turn the download into a 409; a non-PDF fails the
job with a reason and offers nothing. The refusals are scripted in
`tests/phase2_acceptance.sh` (no GPU needed); the conversion itself is run by hand.

**The surya check failed first, and the fix is in this repo rather than the wrapper.**
Measured here: after a conversion that *succeeded*, `surya.fast_layout.server` (1.5 GB)
and `surya.ocr_error.server` (1.0 GB) were still resident, reparented to pid 1 —
2.6 GB stranded on a box with no swap. `marker_run.sh` does try to prevent this: it
runs marker in its own process group and kills the group. These two put themselves in
a process group of their own, so `kill -- -PGID` never reaches them. The app therefore
runs the wrapper inside a transient `systemd-run --user --scope` and empties that
cgroup on every exit path, plus any scope left behind by a killed service at startup.
A cgroup is the one grouping a child cannot leave. **The wrapper was fixed too**
(halo-prep 92ffc7d, pitfall #37); this app sets `MARKER_RUN_SCOPE=0` so there is one
scope rather than a nested pair, and it is the outer one — the only scope that can
still be reaped after *this service* is killed mid-conversion.

Verified 2026-08-29 with the service run as a systemd unit: `systemctl --user kill -s
KILL` mid-conversion leaves two surya servers and a live scope; the restart marks the
job `failed / interrupted`, stops that job's scope, and `pgrep -f surya` is empty. On
the success path, empty 30 s after the job finishes.

**Known limitation, for phase 3.** Two instances sharing a state directory (`dev.sh`
alongside the installed unit) will fight: uvicorn runs the lifespan *before* it binds
the port, so the second instance recovers and reaps the first one's live job even
though it then exits with "address already in use". Concurrency 1 assumes one
instance; nothing enforces it yet.

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
if the other three never get built, the project has already paid for itself.

## Phase 4 — the redaction tool

Added on the operator's request 2026-08-29. Ordered before regex and translate
because it composes with marker (redact what you just converted) and because the box
has the right shape for it: entity spotting is narrow, and removal is checkable.

**Read this section's limitation before designing the UI.** The verifier can prove
that *everything found* was removed. It cannot prove *nothing was missed* — recall is
unverifiable here, and that is the one property a redaction tool's user assumes it
has. So the tool is **assisted redaction with a human review step**, and every page,
button and filename must say so. Never label an output "redacted"; label it
"redaction candidate — review before sending". A tool that quietly under-redacts one
invoice is worse than no tool, because it replaces the user's own care with false
confidence.

**Build**

- **Deterministic layer first, and it is the primary detector.** Patterns for emails,
  phone numbers, IBANs/account numbers, ID numbers, URLs, dates, postal addresses,
  plus a user-supplied literal/regex list (names, employers, case numbers). This layer
  is exact, testable offline, and needs no GPU.
- **The model is a recall booster over that layer, never a replacement.** Ask `:8092`
  to propose *additional* spans — names, org names, oblique identifiers ("my
  daughter's school") that no pattern catches. Every proposal must be located in the
  source by exact string match; a proposed span that does not literally occur is
  discarded, not fuzzy-matched. That single rule removes hallucinated spans entirely.
- **Everything is a proposal until the user confirms.** Render the document with every
  candidate highlighted, categorised, and individually toggleable. Default all
  deterministic hits on and all model proposals on, but visible.
- **Verifier — re-extract, then search.** After producing output, extract its text
  afresh and assert that no confirmed span survives, in any casing or whitespace
  variant. Fail the job if one does. Report the count of spans removed by category.
- Also redact metadata: PDF `/Info`, XMP, and any author name marker carried through.
  A document whose body is clean and whose properties still say the author's full name
  has not been redacted.

**Output formats — both, in this order (operator, 2026-08-29)**

*4a. Text/markdown out.* Build this first. It has no coordinate problem: the spans are
substrings and removing a substring is exact. It is also the format that composes with
`/marker`.

*4b. PDF in, PDF out.* Wanted as an option, so build it — but second, and only behind
every check below.

- **True redaction only.** PyMuPDF `add_redact_annot` + `apply_redactions`, which
  removes the underlying content stream. **Never draw a black rectangle over text.**
  That is *the* classic redaction failure: the text sits untouched underneath and
  `pdftotext` recovers all of it in one command. If a code path here ever produces a
  covering shape without deleting content, it is a bug of the highest severity in this
  repo, not a cosmetic shortcut.
- **The hard part is span → coordinates, and it fails quietly.** `page.search_for()`
  will not find a span that the text layer splits across a line break, hyphenates,
  renders with ligatures, or spaces oddly. The span is *in* the document and *not*
  findable as a quad. So: **every confirmed span must be located and covered, or the
  job fails.** An unlocatable span is a failure to report, never a span to skip — a
  PDF where 9 of 10 names were removed is more dangerous than one where none were,
  because it looks done.
- **Verifier runs on the output PDF, not on the plan.** Extract text from the produced
  file afresh (PyMuPDF *and* `pdftotext`, they disagree) and assert no confirmed span
  survives. Also assert the file's metadata is clean.
- **Rasterised / scanned pages have no text layer**, so there is nothing to search and
  nothing for the verifier to check. Detect this case and say so plainly rather than
  producing a confident-looking file: the honest options are to run the page through
  marker/OCR first and redact by OCR coordinates (with the OCR's own error rate now in
  the blast radius), or to refuse. Refusing is a legitimate answer here.
- PyMuPDF is deliberately not installed anywhere on this box today (halo-prep, the
  `ocr-and-documents` skill rewrite pointed away from it). Add it to
  `~/venvs/workbench` in this phase — its own venv is exactly why that is safe — and
  note it in `scripts/bootstrap.sh` with a comment saying what needs it.

**Acceptance**

- A real invoice (the kind `.gitignore` exists for) → every email, phone, account
  number and address is caught by the deterministic layer alone, before any model runs.
- Verifier catches a deliberately broken redactor that replaces only the first
  occurrence of a repeated name.
- A model proposal that does not occur verbatim in the source is dropped, and the drop
  is visible in the job log rather than silent.
- **`pdftotext` on the produced PDF finds none of the redacted strings.** Non-negotiable
  — this single test is what separates real redaction from a picture of redaction.
- A span deliberately made unlocatable (hyphenated across a line break) fails the job
  loudly instead of being skipped.
- A scanned image-only PDF is refused or OCR-routed, never silently returned unchanged.
- Output PDF metadata (`/Info`, XMP) contains none of the redacted strings.

## Phase 5 — the regex tool

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

## Phase 6 — the paraphrase / translate tool

**Scope, set by the operator 2026-08-29: Chinese ↔ English. Afrikaans is out of
scope** — do not spend time on it, and delete this sentence's premise rather than
re-litigating it later.

Lowest priority of the three, and the only one that may need a new model on disk.

### 6a. Bake-off first — no UI until a model is chosen

Four candidates, and the box already owns two of them:

| Candidate | On disk? | Notes |
|---|---|---|
| **MiLMMT-46-1B** (`:8089`, `llama-milmmt.service`) | yes, Q8_0 | purpose-built MT, already wired for `../anti-watermark-mcp`, ~1 GiB, on-demand |
| **MiLMMT-46-12B** (`~/models/milmmt-46-12b`) | yes, Q8_0 | same family, no unit; the 1B unit's comment says swap `--model` and `--alias` to try it |
| **Hy-MT2-7B** (`tencent/Hy-MT2-7B-GGUF`) | **no** | **try this one first.** Same trained special-instruction support, vendor-published GGUF (no conversion step), small enough to be a cheap experiment |
| **Hy-MT2-30B-A3B** (Tencent, HF) | **no** | the same idea at ~18 GiB. Only worth the download if the 7B's *quality* falls short — not for speed |

Hy-MT2's trained instruction support is the interesting part, and the reason to
take the download seriously: **a native lever beats a hand-rolled one.** That is a
lesson this box has already paid for — halo-prep pitfall #30, where a hand-spliced
terseness template took Granite from 15/15 to 1/15 while the model's own `low_effort`
flag bought the same latency at 15/15. If Hy-MT2 can be *told* which spans not to
translate, the sentinel machinery in 6b becomes a fallback rather than the mechanism.

**But a native lever still has to be verified, not trusted.** Whichever mechanism is
used, the checks in 6b run on every request either way.

**Start with Hy-MT2-7B, not the 30B.** The question the bake-off answers is whether
trained special-instruction support beats MiLMMT for this use, and the 7B answers that
for a ~5 GiB download and a vendor-published GGUF. Reach for the 30B-A3B only if the
7B's *translation quality* is the thing that falls short — not if it is merely slower,
since a 30B-A3B is not the fix for that.

Cost of adopting either, all of it manual and none of it generated:

- The download (3.2 TiB free, so disk is not the constraint), a new
  `llama-hymt2.service`, and a new port — **`:8094`**, since `:8089` belongs to
  MiLMMT and `../anti-watermark-mcp` pins that unit by name via
  `AWM_TRANSLATOR_MODEL`.
- A hand-added line in halo-prep's `config/models.conf` (nothing generates it), e.g.
  `hymt2-7b|llama-hymt2.service|8094|~5|translation`. **Group `translation`**,
  matching MiLMMT, so `model-switch.sh` never leaves two translators fighting for GTT.
  On-demand, not enabled — three units are already resident and the operator moved
  things *off* auto-start twice in August for GTT pressure.
- A `tailscale serve` registration only if it needs to be reachable directly; the
  workbench proxies it, so probably not.
- Read halo-prep's CLAUDE.md "Cross-file couplings" before writing the unit. The
  `--ctx-size` ↔ consumer rule applies: this app is the consumer. Check the vendor
  GGUF's chat template with `curl -s localhost:8094/props | jq -r .chat_template`
  before assuming how special instructions are passed — for an MT model that is where
  the do-not-translate syntax actually lives.

**Bake-off method.** ~10 real paragraphs of the kind actually wanted (both directions,
zh→en and en→zh), each with 3–5 protected spans. Score: adequacy by eye, plus the
mechanical checks from 6b, plus wall-clock per paragraph. Write the result to
`docs/translation-bakeoff.md` — including if the answer is "the 1B already on disk is
good enough", which would make this phase nearly free. **If none of them is good, say
so and stop.** A translation tool that quietly produces poor Chinese is worse than no
translation tool.

### 6b. The tool

- Input: text, plus regex patterns (and/or literal selections) marking spans that must
  survive untranslated — citations, variable names, product names, LaTeX.
- Mechanism: either the model's native do-not-translate instruction (Hy-MT2) or
  sentinel substitution (`⟦0⟧`, `⟦1⟧`, …) before the call, restored after. **Models
  drop, translate, renumber and duplicate sentinels** — that is why 6b's checks exist
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

1. ~~**Auth.**~~ **Decided 2026-08-29: no auth.** Tailnet-only, matching the four
   existing ports. Recorded here because it is a deliberate choice rather than an
   oversight, and because it is the assumption that would have to be revisited if the
   tailnet ever gains a second user or a shared node — the upload and redaction pages
   hold the user's real documents, which the inference ports do not.
2. ~~**Languages**~~ **Decided: Chinese ↔ English. Afrikaans out of scope.**
3. ~~**Retention**~~ **Decided: 7 days**, as in phase 1. Keep the pruner honest — the
   job store holds documents specifically *because* they contain things worth removing,
   so an upload that outlives its window is the failure mode, not a stale job record.
4. ~~**PDF-in/PDF-out redaction**~~ **Decided: yes, as an option** — phase 4b, after
   4a's markdown path works. It is most of the work and all of the risk.
5. Does anything else want the job queue (batch marker over a directory, whisper
   transcription)? Do not build for it now; just do not make it impossible.
6. **Should marker use `--mode balanced`, and should it use an LLM at all?** Two
   separate questions that want one bake-off, both raised 2026-08-29 and neither
   decided.

   `marker_run.sh` hardcodes `--mode fast`. This marker's own help says `balanced`
   "uses the VLM layout model + full-page OCR (best on GPU)" while `fast` "uses
   lightweight CPU detectors for layout/tables and only block-OCRs garbled/empty
   content". Marker is already on the GPU either way — torch 2.11.0+rocm7.13.0 sees
   the Radeon 8060S through the HIP shim, and surya's OCR-2 VLM runs on the Vulkan
   llama-server with `-ngl 99` — so this is about how much of the GPU it uses, and it
   is a change to a halo-prep file, not to this repo.

   `--use_llm` is the bigger question. Mechanically it works: marker ships an
   OpenAI-compatible service (`marker/services/openai.py`, `--openai_base_url`), and
   two VLMs with mmproj are already on disk (`qwen38-27b`, 17 G, in models.conf as
   :8090; `muse-glimmer`, 15 G). It would need an on-demand unit, not a fourth
   resident one — ~17 G plus marker's own ~2.9 G on a box with no swap is pitfall #17's
   exact shape.

   **The objection that decides it:** phase 2 rests on "marker is not an LLM". Turning
   on `--use_llm` enables `llm_equation`, `llm_table`, `llm_table_merge`,
   `llm_page_correction` — processors that rewrite content with a model. The
   `.marker-ok` sidecar would still be written, because it proves the output came from
   `marker_run.sh`, not that no LLM touched the text. The verifier would keep saying
   the same words while meaning less. If this is ever built it must be a per-job
   opt-in, labelled on the page, and recorded in `meta.json` so a later reader can
   tell the two kinds of output apart.
