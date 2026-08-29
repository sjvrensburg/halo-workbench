# halo-workbench

Specialist local-model tools for the EVO-X3, in a browser, over Tailscale.

Not a coding assistant. This box is good at narrow tasks that come with a cheap
automatic check, and bad at anything judged by eye — so each tool here ships a
verifier that runs on every request:

| Tool | Does | Verified by |
|---|---|---|
| **marker** | upload a PDF, get markdown back | marker's own checksummed provenance sidecar |
| **redact** | strip PII from a document (markdown or true-redacted PDF) | re-extract the output — `pdftotext` included — and search it for every span removed |
| **regex** | give examples, get a regex | your examples, actually executed against the pattern |
| **translate** | zh↔en translate/paraphrase, protecting spans you mark | protected spans round-trip byte-identical; residual-script check |

**Status: phase 2.** The job queue and the marker tool work; redact, regex and
translate are not started. `PLAN.md` is the build plan; `CLAUDE.md` is the decisions.

## Run it (development)

```bash
scripts/bootstrap.sh      # create ~/venvs/workbench
scripts/dev.sh            # uvicorn on :8093 with --reload
curl -s localhost:8093/healthz
```

Then `http://localhost:8093/marker` to convert a PDF. Acceptance tests:

```bash
tests/phase1_acceptance.sh    # the queue: serialisation, and kill -9 recovery
tests/phase2_acceptance.sh    # marker: what the sidecar verifier refuses (no GPU needed)
```

## Run it (resident) — not yet installed

```bash
cp config/halo-workbench.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now halo-workbench
```

Remote access needs one more command, run by the operator, whose config lives in
`tailscaled` rather than in this repo:

```bash
tailscale serve --bg --http=8093 http://127.0.0.1:8093
```

Then `http://evo-x3:8093` from any machine on the tailnet.

## Related

- `../halo-prep` — the runbook that built the box: model units, ports, pitfalls.
- `../deep-research-podcast` — the other thing this box is genuinely good at.
