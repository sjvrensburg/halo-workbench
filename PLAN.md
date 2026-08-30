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

One FastAPI app, one port (**8093**), one systemd user unit, five pages (four built,
`/hint` planned as phase 7). Not five services — see CLAUDE.md "One port". Two tailnet
registrations, both outside this repo: the `tailscale serve` port and, since 2026-08-29,
the Tailscale Service `svc:halo-workbench` (`http://halo-workbench/`). **Phase 7 needs a
third, and it is a prerequisite rather than a nicety**: an HTTPS registration, because
the browser will not give a page the camera outside a secure context.

```
  browser (laptop / iPad, over Tailscale)
        │  http://halo-workbench/    (or http://evo-x3:8093 — both tailnet only,
        │                             no auth, operator 2026-08-29)
        ▼
  halo-workbench  (uvicorn, ~/venvs/workbench)
        ├── /marker    → subprocess: halo-prep marker_run.sh   (no LLM of ours)
        ├── /redact    → deterministic patterns FIRST, model as recall booster
        ├── /regex     → llama-server :8092  + generate-and-test loop
        ├── /translate → MT model (phase 6a picks it) + round-trip check
        │       │
        │       └── all four go through ONE job queue, concurrency 1
        │
        └── /hint      → llama-server :8097 (olmOCR-2), phase 7, NOT via the queue:
                         interactive, ephemeral, no stored artefact. Needs HTTPS.
```

`/redact` composes with `/marker`: a converted PDF should be redactable in one click
from its result page, without a round trip through the user's disk. **Built 2026-08-29**
— `POST /marker/<id>/redact` re-verifies the sidecar, then hands the bytes to a scan job
in memory. A file that would be refused as a download is refused as an input too:
passing unverified text to the tool whose promise is "everything found was removed"
would launder the substitution the sidecar exists to catch.

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

**Fixed 2026-08-29.** Two instances sharing a state directory used to fight: uvicorn
runs the lifespan *before* it binds the port, so a second instance recovered and reaped
the first one's live job on its way to exiting with "address already in use". `jobs`
now takes an exclusive `flock` on `<state>/lock` as the first thing `start()` does,
before recovery touches a single `meta.json`, and a second instance exits 3 with a
message naming the holding pid. flock rather than a pid file, so a `kill -9` cannot
leave a stale lock. Covered by `tests/phase1_acceptance.sh` check 3.

## Phase 3 — reachability ✅ DONE 2026-08-29 (bar the operator's one command)

- Install the unit: `systemctl --user enable --now halo-workbench` (add to halo-prep's
  `config/models.conf`? **No** — that manifest is for llama-server units and
  `model-switch.sh` groups. This is not one. Note it in halo-prep's README instead.)
- `tailscale serve --bg --http=8093 http://127.0.0.1:8093` — **operator step, needs
  sudo/operator rights, and it lives outside both repos.** halo-prep's CLAUDE.md
  already warns that the serve config is invisible to grep; this adds a fifth port to
  the four in `docs/06` §7 and `docs/10`, so update those tables in the same commit.
- Verify from the laptop: `curl http://evo-x3:8093/healthz`.

Done 2026-08-29: the unit is installed, enabled and running (`Linger=yes`, so it
survives logout and reboot), it restarts cleanly, and `/healthz` answers on loopback.
Smoke-tested through the resident service rather than a dev server: a 33-page spec
converted, 70 KB of markdown with two diagrams, download 200, `pgrep -f surya` empty
30 s later.
halo-prep's `docs/06` §7, `docs/10`'s two port tables and its README now carry `:8093`
as a fifth serve registration — with the note that it is the only one of the five that
exposes *documents* rather than inference.

**Done since, both of them (2026-08-29):**

```bash
sudo tailscale serve --bg --http=8093 http://127.0.0.1:8093            # http://evo-x3:8093
tailscale serve --service=svc:halo-workbench --bg --yes --http=80 \
    http://127.0.0.1:8093                                             # http://halo-workbench/
```

The second is a **Tailscale Service**: a name and a VIP owned by the tailnet rather than
by this box, so the address survives this app moving to another machine and carries no
port. It cost more than the command suggests — a Service host must be a *tagged* node, so
`evo-x3` now carries `tag:server`, which moves the device's ownership from the user to the
tailnet and would have ended Tailscale SSH to the box had the policy not gained a
`tag:server` rule first. See `../halo-prep/docs/06` §7 and pitfall #38.

Do phases 1–3 and stop. Get real use out of marker before building anything else —
if the other three never get built, the project has already paid for itself.

## Phase 4 — the redaction tool ✅ DONE 2026-08-29

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
- **The model is a recall booster over that layer, never a replacement.** ~~Ask
  `:8092`~~ **Changed 2026-08-29, on the operator's suggestion: the recall layer is
  `openai/privacy-filter`**, a token classifier run as q4 ONNX on the **CPU** in this
  process (`app/pii_tagger.py`, `scripts/fetch-privacy-filter.sh`, ~920 MB, Apache-2.0).
  It labels a sequence in one forward pass and decodes BIOES spans over eight privacy
  categories, so it does not queue behind the three GPU units at all.

  Why the change, measured on the same 1 KB letter: `:8092` is a **thinking** model, and
  with an 800-token budget it spent the lot in `reasoning_content` and returned an
  **empty `content`** after 138.9 s — the first scan through the app got zero proposals,
  silently. With `enable_thinking: false` it answered in 2.4 s and named all four people
  (a measured counter-example to halo-prep pitfall #25 for extraction tasks; recorded
  there). `privacy-filter` takes 5.7 s on CPU and finds the same names *plus* the
  structured identifiers, with categories.

  `:8092` with thinking off remains the fallback when the weights are not installed, and
  which layer ran is stated on the page — a recall layer that silently did not run would
  be exactly the false confidence this tool is built to avoid.

  Whichever layer runs, the rule is unchanged: propose *additional* spans — names, org names, oblique identifiers ("my
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

All of these pass, scripted in `tests/phase4_acceptance.sh` (no GPU, no running app —
the tagger section skips itself if the weights are absent). Verified end to end through
the resident service on 2026-08-29, markdown and PDF: `pdftotext` on the *downloaded*
PDF finds none of "Anna Meyer", "Johannes", "Thandiwe", the email, the account number,
the phone or the IBAN, and its `/Info` — which carried the author's name, a title
naming the recipient and a subject containing the account number — comes back empty.

Two notes from real use. The tagger returns a multi-line address as one span; those are
split per line, because a span crossing a line break is precisely what the PDF path
cannot locate and would (correctly) fail the whole job over. And it proposed the literal
word `IBAN` as an account number — a harmless false positive that costs one untick,
which is the trade the whole design makes on purpose.

## Phase 5 — the regex tool ✅ DONE 2026-08-29

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

Both pass. `tests/phase5_acceptance.sh` runs the offline half (the runner, the timeout,
the parser, and the loop's contract that a pattern is never returned before it passes);
`--cases` runs the ten against `:8092`. Measured 2026-08-29, thinking off:

    9/10 solved, 1 honestly unsolved, 0 wrong

Most solved in one or two rounds and 1–13 s. The unsolved one ("words ending in -ing",
with `'ing '` as a negative under `fullmatch`) ran all six rounds, tried 23 candidates,
and returned nothing — which is the behaviour the acceptance asks for. **`0 wrong` is
the number that matters**: it counts patterns returned that do not pass the user's own
examples, and it is the one thing this design exists to make impossible.

**Found in that run, and worth more than the score.** Given one positive example and no
useful negative, the model answered `^8801015009087$` — the example itself, anchored. It
passes honestly and generalises to nothing, and no verifier can catch that, because the
pattern does exactly what was asked and the user asked for too little. So the result page
warns when the winning pattern embeds an example literally, when only one string had to
match, or when nothing had to be rejected (`overfit_warnings`). Same rule as the
redaction tool's "candidate" wording: where the check cannot cover a property the user
assumes, say so where they will read it.

## Phase 6 — the paraphrase / translate tool

**Scope, set by the operator 2026-08-29: Chinese ↔ English. Afrikaans is out of
scope** — do not spend time on it, and delete this sentence's premise rather than
re-litigating it later.

Lowest priority of the three, and the only one that may need a new model on disk.

### 6a. Bake-off first — no UI until a model is chosen ✅ DONE 2026-08-29

**Answered: Hy-MT2-7B on `:8094`, with ZnZ sentinels rather than its native
instruction.** Full write-up, method and raw output in
[`docs/translation-bakeoff.md`](docs/translation-bakeoff.md); the table below is the
plan as written beforehand and is kept for the record.

Three findings that change 6b, briefly: the sentinels beat the native lever on the very
model chosen for that lever (10/10 against 8/10); two `temperature: 0` runs of the same
arm differed on 7 of 10 paragraphs, so the per-request verifier is load-bearing rather
than ceremonial; and 6b's span checks needed two corrections — "present once" must be
"as often as the source has it", and "in order" has to go, because translation reorders
legitimately. MiLMMT-46-1B was rejected on adequacy despite scoring 9/10 mechanically:
it renders "falls by 12.4%" as 「下降了 12.4% 倍」 and inverts who serves notice on whom.


Four candidates, and the box already owns two of them:

| Candidate | On disk? | Notes |
|---|---|---|
| **MiLMMT-46-1B** (`:8089`, `llama-milmmt.service`) | yes, Q8_0 | purpose-built MT, already wired for `../anti-watermark-mcp`, ~1 GiB, on-demand |
| **MiLMMT-46-12B** (`~/models/milmmt-46-12b`) | **no — corrected 2026-08-29**, only the 1B is on disk | same family, no unit; would need downloading, and the 1B's adequacy failures are not a size problem the 12B obviously fixes |
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

### 6b. The tool ✅ DONE 2026-08-29

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

All pass — `tests/phase6_acceptance.sh` (offline) and `--live` for a real round trip.
Built as **one window with a swap control**, on the operator's UX ask (2026-08-29):
source and target selectors with a ⇄ between them, and **when both sides are the same
language it rewrites instead of translating**. One control, two tools, no second page.

Decisions taken while building it:

- **The masker is vendored, not imported** (`app/protect.py`, from
  `../anti-watermark-mcp`), because its measurements are the value and a paraphrase would
  lose them. Vendoring buys drift, so the source's sha256 is recorded and re-checked by
  the acceptance script — a copy that silently diverges becomes a test failure with a
  diff to read.
- **Rewriting goes to `:8088`, translation to `:8094`.** An MT model cannot rewrite
  English into better English; the language pair picks the model, not just the prompt.
- **Style presets in-repo, plus any markdown file as a guide** — a skill's `SKILL.md`
  works as one, `academic-humanizer` included. A guide runs in **two passes**: it lists
  what is wrong, then the rewrite fixes exactly that, and the user sees the list. One
  pass would be a shallow imitation of what is actually a read-audit-edit procedure.
- **A rewrite gets a fourth check the translation does not need:** every number and every
  citation must come back identical. That is the one part of an editorial rewrite that
  can be verified, and it is the guides' own promise — academic-humanizer says it "never
  changes a number, result, or citation". A rewrite that edits one fails the job.
- **The model starts itself.** `llama-hymt2` serves nothing but this page, so pressing
  Translate starts it (~2 s warm, tens of seconds cold) and 15 minutes idle stops it. Only
  units this app started are ever stopped — verified both ways: a unit started by a job is
  released on the idle timer, and one started by hand survives it.
- **No translate-as-you-type.** Explicit button and ⌘⏎: every keystroke would queue a GPU
  job on a box whose queue is one deep and shared with Hermes.

Two bugs the verifier caught during the build, both of which would have shipped silently
without it:

1. `finance@example.co.za` came back as `financeZ3Z`. The vendored masker is written for
   LaTeX, where `@name` is a citation key, so its rule matched the domain and masked half
   the address — leaving a sentinel glued to a letter, which its own restoration rule
   deliberately refuses to touch. Contact details are now protected *whole*, before that
   masker runs, reusing the redaction tool's pattern layer rather than writing a third one.
2. The first fix used `Z900Z` sentinels, and `protect.mask`'s `number` rule then masked
   the `900` *inside* them; the model dropped what was left. Literal spans are now staged
   as digit-free `ZQxxZ` tokens and renumbered into the same space afterwards.

---

## Phase 7 — the handwriting hint tool

Requested by the operator 2026-08-29, after the halo-prep search that put olmOCR-2-7B
on `:8097`. **Not started.**

**What it is, and what it is deliberately not.** The operator sits at work with a
document camera (visualiser) over a student's script, hits a word they cannot read, and
wants a second opinion. It is a *hint tool for an ambiguous squiggle*, with the human
looking at the same ink at the same moment. It is **not** a transcription service, not a
marking aid, and not a batch pipeline — and that distinction settles most of the design,
so do not let it drift.

**Why the distinction matters more than it sounds.** halo-prep's search measured
over-correction as the dominant risk in handwriting OCR: every model tested silently
"fixes" student spelling, and [arXiv 2604.22774](https://arxiv.org/abs/2604.22774)
measures that in 42–66% of transcriptions across 15 VLMs, with faithfulness prompting
worth 4 percentage points and no net gain. **That risk is specific to batch work** —
a transcript produced now and marked later, where nobody re-reads the ink and a repaired
misconception disappears. Here the operator is beside the transcript, not downstream of
it. Same for the two silent-failure modes that disqualified candidates in that search
(Chandra returning an empty page, Qwen3.8 truncating inside its own reasoning): in a
batch of 200 scripts one comes back short and nothing says which; in an interactive UI a
blank result announces itself and you press the button again.

**This tool's verifier is the human, per request, by construction** — which is a
stronger gate than anything automatic this repo has built, not a weaker one. Do not
bolt on a synthetic verifier to satisfy the thesis on a technicality. What the thesis
actually asks is that no quality claim goes unchecked, and here none can.

**Home: a fifth page on `:8093`, not a new service.** See CLAUDE.md "One port". A new
service would cost a unit, a port, a `tailscale serve` registration, a Tailscale
Service, and one more thing to forget is not on the tailnet. It also keeps `:8097`
bound to localhost — the browser talks only to `:8093`, so no second serve
registration and no auth question.

### 7a. HTTPS first — this is a prerequisite, not a detail

**Do this before writing any of the app, and stop if it cannot be done.**
`navigator.mediaDevices.getUserMedia` exists only in a **secure context**: HTTPS or
`localhost`. Every current serve registration is plain HTTP and `tailscale status`
reports `CertDomains: None`, so from the operator's laptop `http://evo-x3:8093` is
neither — `navigator.mediaDevices` is `undefined`, with no permission prompt and no
error worth reading. Every other line of this phase depends on this working.

- Enable *HTTPS Certificates* in the tailnet admin console.
- `tailscale serve --bg --https=443 http://127.0.0.1:8093` for a real Let's Encrypt
  cert on `evo-x3.tail80adad.ts.net`.
- This is a **change outside both repos**, like every other serve registration
  (halo-prep CLAUDE.md "Served ports live in `tailscaled`"). A rebuilt box loses it.
  Record it beside the others in halo-prep `docs/06` §7 rather than only here.
- Note the existing Service name `http://halo-workbench/` is HTTP. Whether the Service
  can carry HTTPS too, or whether the camera page has to be reached by the
  `ts.net` name, is the one thing to establish first — it decides the URL the operator
  actually types.

**Acceptance for 7a alone:** from the laptop, over the tailnet, on the real URL,
`navigator.mediaDevices.getUserMedia({video:true})` resolves and a frame renders. No
app, no model. If this fails the phase does not proceed.

### 7b. Capture

**Resolution is the whole ballgame and the one place this can quietly fail.** halo-prep
measured MiniCPM-V 4.6 at 5/15 on symbols purely because it spends ~700 tokens on a
page; olmOCR-2 scores 15/15 at ~4,070. A default `getUserMedia` stream is often
640×480, which would reproduce that failure in the browser **with the good model** and
look like the model's fault. A bad hint is worse than no hint, because the operator
spends time evaluating it.

- Enumerate devices and let the operator pick — the laptop's own webcam will be in the
  list and must not be the default.
- Request `{ width: {ideal: 3840}, height: {ideal: 2160} }`, and prefer
  `ImageCapture.takePhoto()` over grabbing the preview stream: doc cams commonly
  expose a much higher still resolution than their video feed.
- **Assert what actually arrived.** Show the captured pixel dimensions in the UI and
  warn below ~2000 px on the long edge. The samples that scored 15/15 were 2851–4096 px.
- Captures are **ephemeral**: student scripts carry names. Do not write them to the job
  store, do not reuse phase 1's 7-day retention. In memory, for the life of the request.

### 7c. Crop-first interaction

The primary gesture is **drag a box around the squiggle**, not "transcribe this page".
olmOCR-2 spends ~4,070 prompt tokens regardless of how much page it is given, so a crop
of one line is a large effective-resolution gain — for free.

**Do not read this as "band-slicing works".** halo-prep measured band-slicing as a
failure: bands came back empty and truncated. That was **MiniCPM-specific** — its
LLaVA-UHD v4 resampler emits a fixed 64 queries per slice, so slicing bought resolution
and lost content. olmOCR-2 has dynamic resolution and should behave differently.
**Should. Measure it before relying on it** — one crop, one full page, same word, and
check the crop is at least as good.

### 7d. A distribution over readings, not one answer

The operator's framing, and the right one: the page should return **several candidate
readings with their support**, not a single confident string. They are pattern-matching
against ink they can see, so three readings beat one — and where the candidates diverge
is exactly where their eye is needed.

**Two sources of spread, and they answer different questions. Use both, label them
differently.**

- **Sampling one model at temperature** answers *"how sure is this model?"* Cheap:
  measured on `:8097` 2026-08-30, repeat samples of the same crop reuse the prefill
  almost completely — `cached_tokens` **4080 of 4081**, first request 18.4 s, each
  further sample **10.5 s** on a full page. The image encode and prefill are paid once.
  A one-word crop should be far below this; measure it rather than extrapolating.
  Note `llama-olmocr.service` runs `--temp 0`, which is a **batch** decision — every
  halo-prep measurement behind that unit was greedy. Override temperature **per
  request** here; do not edit the unit.
- **Asking different models** answers *"is the ink ambiguous?"* — which is the question
  the operator actually has. Different inductive biases agreeing is real evidence;
  one model sampled five times agreeing only means it is confident, which it can be
  while wrong.

**Who belongs in the ensemble is NOT the batch ranking.** halo-prep's bake-off scored
models for unattended transcription of whole pages. This tool is attended and works on
crops, so several of its disqualifiers stop applying and one of its criteria inverts:

| model | batch verdict | as an ensemble member here |
|---|---|---|
| **olmOCR-2** `:8097` | adopted, 15/15 | **primary.** Fast (2.7–18.4 s/page), Apache-2.0, already the unit |
| **Muse** `:8084` | 15/15, 8x slower | **good second voice** — different lineage entirely. But it *hallucinates fluently inside strikethroughs* (rendered an illegible struck scribble as `not`, reversing a claim). On a struck word, expect exactly that |
| **Chandra-OCR-2** | rejected: dropped a whole page silently | **arguably the best third voice.** Its disqualifier was silence in an unattended batch, which is harmless when a human is watching. It scored 10/10 on pages it *did* answer, **marks struck text with `<del>` instead of resolving it**, and was the only model to preserve `generalisability`. When the ambiguity IS a strikethrough — a common case on a marked script — it is the only candidate that shows its working. Weights at `~/models/ocr-field/chandra2/`, 5.4 GiB |
| **Qwen3.8-27B** `:8090` | 15/15 but 52–199 s/page | **untested on crops.** Its cost was ~2,000 reasoning tokens per page; a one-word crop may be cheap enough. Worth one measurement before ruling out — it is already on the box |
| **Gemma-4-E4B** | 14/15, ~320 tok/page | **exclude from word-level candidates.** It paraphrases at low resolution — a *paraphraser in an ensemble is worse than useless*, because it produces fluent disagreement that is not about the ink, and the UI would present it as evidence of ambiguity when it is evidence of nothing. See halo-prep `docs/04` |

**Gemma has a different job here.** At ~320 prompt tokens a page it is roughly an eighth
of olmOCR-2's cost, which makes it the right model for questions *about* the capture
before a crop is spent: is this page blank, is the camera in focus, which question is
this. Page triage, never word candidates.

**Presenting it.** Show agreement as agreement and disagreement as the finding, not as
an error — this is the one screen where "the models disagree" is the most useful thing
it can say. Do not average, vote, or pick a winner: collapsing the distribution throws
away the only signal that distinguishes "hard to read" from "easy to read and I was
wrong". Where a member marks up a strikethrough, preserve the markup rather than
flattening it to text.

**Not a verifier.** The ensemble is a better *presentation*, not a safety mechanism —
the verifier is still the operator's eye on the ink (see this phase's opening). Do not
let the spread become a confidence score that invites trusting the top candidate
without looking.

### 7e. Starting the models

`llama-olmocr.service` is `disabled` and on-demand (~2.1 s to load). The page must start
it, and the established pattern on this box is `scripts/vibevoice-lease.sh` — refcounted
acquire/release, **never a bare `systemctl stop`**, because a second consumer mid-request
gets cut off. Lease on first capture, release on idle.

7d makes this plural, and that changes the sizing. olmOCR-2 (8.8 G) plus Muse (15.4 G)
plus Chandra (5.4 G) is ~30 GiB of GTT held for as long as the operator is marking — fine
against a 112 GiB ceiling **with nothing else running**, and not fine on a day the
podcast pipeline is going (halo-prep's README: `llama-research` is ~23 G and Qwen3.8 was
moved off always-on precisely because that peak was OOM-killing runs, on a box with no
swap). So: lease each member independently, start the second and third voices **only when
the operator asks for more candidates**, and release on idle rather than at end of
session. Chandra has no unit yet — it would need one, or a lease helper that starts a
bare `llama-server`; do not add a resident unit for it.

### Acceptance

- 7a passes on the operator's real laptop at work, not just on the LAN.
- A capture from the document camera arrives at ≥2000 px on the long edge, and the UI
  says what it was.
- A crop of a known-ambiguous word from the halo-prep sample set returns candidates in
  **under 20 s** end to end, with at least two independent members answering. Latency is the metric that matters here, not page accuracy —
  the operator is waiting. olmOCR-2 measured 2.7–18.4 s per full page, so a crop should
  be comfortably inside this; if it is not, something in 7b/7c is wrong.
- No capture is on disk after the request completes.
- On a crop of a **struck-out** word, the page shows that the members disagree rather
  than silently picking one. This is the case halo-prep measured Muse getting confidently
  wrong, so it is the acceptance test that matters most.

**Deliberately not in scope:** marking, scoring, whole-script transcription, and any
storage of student work. If those are ever wanted they are a different tool with a
different risk profile, and the over-correction finding above applies to them in full.

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
