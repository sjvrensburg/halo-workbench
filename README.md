# halo-workbench

[![marker ships a verifier](https://img.shields.io/badge/marker-ships_a_verifier-2f6f5b)](CLAUDE.md#the-rule-the-whole-project-is-organised-around)
[![runs on](https://img.shields.io/badge/runs_on-EVO--X3_·_Strix_Halo-5a5750)](https://github.com/sjvrensburg/halo-prep)
[![python](https://img.shields.io/badge/python-3.14_+_FastAPI-5a5750)](scripts/bootstrap.sh)
[![access](https://img.shields.io/badge/access-tailnet_only-5a5750)](#run-it-resident--installed)

<!-- Static badges on purpose. This repo is private, so shields.io's dynamic ones
     (last-commit, build status, license) render "repo not found" for everyone who can
     read it -- checked 2026-08-29, they answer 200 with that text inside the SVG. A
     badge that reports a false failure is worse than no badge. -->

PDF → markdown on the EVO-X3, in a browser, over Tailscale.

Not a coding assistant. Upload a PDF, get markdown back with tables, headings and
display math intact, converted by [marker](https://github.com/datalab-to/marker) on the
box's GPU. Every conversion is checked against marker's own checksummed provenance
sidecar before it is ever offered for download — a download route is exactly where a
substitute (plain text extraction, an LLM's own transcription) could otherwise slip in
wearing marker's filename.

This app used to be a five-tool workbench (marker, assisted redaction, regex synthesis,
translation, a handwriting hint tool); the other four moved to AnythingLLM and were
removed 2026-09-15. `CLAUDE.md` has the decisions that still apply.

## Run it (development)

```bash
scripts/bootstrap.sh      # create ~/venvs/workbench
scripts/dev.sh            # uvicorn on :8093 with --reload
curl -s localhost:8093/healthz
```

Then `http://localhost:8093/` (redirects to `/marker`). Acceptance tests:

```bash
tests/phase1_acceptance.sh    # the queue: serialisation, and kill -9 recovery
tests/phase2_acceptance.sh    # marker: what the sidecar verifier refuses (no GPU needed)
```

## Run it (resident) — installed

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
# Node port: http://evo-x3:8093
sudo tailscale serve --bg --http=8093 http://127.0.0.1:8093

# A Tailscale Service: http://halo-workbench/ , its own name and VIP on the tailnet,
# so the address survives this moving to another machine and carries no port.
tailscale serve --service=svc:halo-workbench --bg --yes --http=80 http://127.0.0.1:8093
```

Then `http://halo-workbench/` — or `http://evo-x3:8093`, which still works — from any
machine on the tailnet. Always the name, never the `100.x` address, which returns
tailscaled's own 404 (halo-prep pitfall #33).

The Service form needs three things this repo does not create: the Service defined in
the admin console (`svc:halo-workbench`, `tcp:80`), a tailnet policy that grants it, and
a **tagged host** — a Service host must be a tagged node, and tagging moves the device's
ownership from you to the tailnet (which is why the policy also carries a Tailscale SSH
rule for `tag:server`; the stock rule is scoped to `autogroup:self` and stops covering a
tagged box). The host must then be approved in the console before it serves anything.

## Related

- `../halo-prep` — the runbook that built the box: model units, ports, pitfalls, and
  `marker_run.sh`, the wrapper this app shells out to.
- `../deep-research-podcast` — the other thing this box is genuinely good at.
- `../drp-web` — its web interface, at `http://drp-web/` (a Tailscale Service; also
  reachable as `http://evo-x3:8095`), linked from the header here.
