# halo-workbench

[![every tool ships a verifier](https://img.shields.io/badge/every_tool-ships_a_verifier-2f6f5b)](CLAUDE.md#the-rule-the-whole-project-is-organised-around)
[![status](https://img.shields.io/badge/status-four_tools_live-2f6f5b)](PLAN.md)
[![runs on](https://img.shields.io/badge/runs_on-EVO--X3_·_Strix_Halo-5a5750)](https://github.com/sjvrensburg/halo-prep)
[![python](https://img.shields.io/badge/python-3.14_+_FastAPI-5a5750)](scripts/bootstrap.sh)
[![access](https://img.shields.io/badge/access-tailnet_only-5a5750)](README.md#run-it-resident--installed-2026-08-29)

<!-- Static badges on purpose. This repo is private, so shields.io's dynamic ones
     (last-commit, build status, license) render "repo not found" for everyone who can
     read it -- checked 2026-08-29, they answer 200 with that text inside the SVG. A
     badge that reports a false failure is worse than no badge. -->

Specialist local-model tools for the EVO-X3, in a browser, over Tailscale.

Not a coding assistant. This box is good at narrow tasks that come with a cheap
automatic check, and bad at anything judged by eye — so each tool here ships a
verifier that runs on every request:

| Tool | Does | Verified by |
|---|---|---|
| **marker** | upload a PDF, get markdown back | marker's own checksummed provenance sidecar |
| **redact** | strip PII from markdown, text or PDF (true redaction, not black boxes) | re-extract the output — `pdftotext` included — and search it for every span removed |
| **regex** | give examples, get a regex | your examples, actually executed against the pattern |
| **translate** | zh↔en translate/paraphrase, protecting spans you mark | protected spans round-trip byte-identical; residual-script check |

**Status: phase 6 — all four tools work.** Job queue, marker, assisted redaction,
regex-from-examples, and translate/rewrite; the service is installed and reachable on
the tailnet. `PLAN.md` is the build plan; `CLAUDE.md` is the decisions.

## Run it (development)

```bash
scripts/bootstrap.sh      # create ~/venvs/workbench
scripts/fetch-privacy-filter.sh   # ~920 MB, the redaction recall layer (CPU, ONNX)
scripts/dev.sh            # uvicorn on :8093 with --reload
curl -s localhost:8093/healthz
```

Then `http://localhost:8093/` for the four tools. A converted PDF is redactable in one
click from its result page. Acceptance tests:

```bash
tests/phase1_acceptance.sh    # the queue: serialisation, and kill -9 recovery
tests/phase2_acceptance.sh    # marker: what the sidecar verifier refuses (no GPU needed)
tests/phase4_acceptance.sh    # redact: patterns, the verifier, and pdftotext on the output
tests/phase5_acceptance.sh    # regex: the runner and the loop's contract
tests/phase5_acceptance.sh --cases   # ...plus ten real cases against :8092
tests/phase6_acceptance.sh    # translate: spans, the checks, the vendored masker's drift guard
tests/phase6_acceptance.sh --live    # ...plus a real round trip through :8094
```

The translation model is on-demand, and the app starts it when you press Translate —
then stops it again after 15 minutes idle. Install the unit once:

```bash
cp config/llama-hymt2.service ~/.config/systemd/user/
systemctl --user daemon-reload      # do NOT enable it: on-demand is the point
```

Start it by hand and the app leaves it alone — it only ever stops a unit it started
itself.

## Run it (resident) — installed 2026-08-29

```bash
cp config/halo-workbench.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now halo-workbench
systemctl --user status halo-workbench
journalctl --user -u halo-workbench -f
```

Lingering is on, so it comes back after a reboot without anyone logging in.

Remote access needs one more command, run by the operator, whose config lives in
`tailscaled` rather than in this repo — nothing here regenerates it and nothing here
will tell you it is missing:

```bash
sudo tailscale serve --bg --http=8093 http://127.0.0.1:8093
```

Then `http://evo-x3:8093` from any machine on the tailnet — the name, never the
`100.x` address, which returns tailscaled's own 404 (halo-prep pitfall #33).

## Related

- `../halo-prep` — the runbook that built the box: model units, ports, pitfalls.
- `../deep-research-podcast` — the other thing this box is genuinely good at.
- `../drp-web` — its web interface, at `http://drp-web/` (a Tailscale Service; also
  reachable as `http://evo-x3:8095`), linked from the header here.
  Same skeleton as this app; hours-long runs, so they are systemd units rather than
  queued jobs, and it gets a service of its own rather than a fifth tab.
