# PLAN.md

This was originally a five-tool build plan (marker, assisted redaction, regex
synthesis, translation, a handwriting hint tool), built out in phases 0–7 between
2026-08-22 and 2026-09-03. AnythingLLM took over the other four tools' jobs, so on
2026-09-15 the repo was pared back to marker only — the other tools' code, tests and
docs were deleted, and the old phase-by-phase detail for them was deleted from this
file too. It's still in git history (`git log -- PLAN.md`) if it's ever needed again.

## What remains: phases 0–2 (done)

- **Phase 0 — skeleton.** FastAPI app, `app/config.py`, `app/jobs.py`'s directory-based
  job store and single worker thread, `/healthz`.
- **Phase 1 — the queue.** Serialisation (one job at a time), crash recovery
  (`running` → `interrupted` on restart, `queued` jobs re-queued), and the exclusive
  `flock` on the state directory that makes a second instance refuse to start rather
  than fight the first one for a live job. Acceptance: `tests/phase1_acceptance.sh`.
- **Phase 2 — marker.** `app/tools/marker.py` shells out to `../halo-prep`'s
  `marker_run.sh` (never `marker_single` directly — see CLAUDE.md), runs it in a
  reapable cgroup scope, and writes the checksummed `.marker-ok` sidecar that is the
  tool's verifier, re-checked at download time. Acceptance: `tests/phase2_acceptance.sh`
  for what needs no GPU; a real conversion is run by hand.
- **Phase 3 — installed as a resident service**, `config/halo-workbench.service`,
  reached over Tailscale two ways (`CLAUDE.md` "Reached two ways").

## Shape

One tool, one page (`/marker`), one job at a time, verified before it is ever offered
for download. No auth beyond the tailnet. No database — jobs are directories under
`~/.local/state/halo-workbench/jobs/`, pruned after `JOB_RETENTION_DAYS`.

There is no phase currently planned beyond this. If marker's UI or verifier needs to
change, edit `app/tools/marker.py` and the two templates directly rather than adding a
new phase here — the phase structure earned its keep while this was a five-tool build
with real sequencing constraints between them; a single tool does not need it.
