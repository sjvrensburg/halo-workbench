# halo-workbench

[![every tool ships a verifier](https://img.shields.io/badge/every_tool-ships_a_verifier-2f6f5b)](CLAUDE.md#the-rule-the-whole-project-is-organised-around)
[![status](https://img.shields.io/badge/status-five_tools_live-2f6f5b)](PLAN.md)
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
| **hint** | drag a box round a handwritten squiggle, get several readings and where the models part company | **you** — you are looking at the same ink at the same moment, and the page is built so that nothing invites you not to |

**Status: phases 0–7 done — all five tools work.** Job queue, marker, assisted
redaction, regex-from-examples, translate/rewrite, and the handwriting hint tool; the
service is installed and reachable on the tailnet. `PLAN.md` is the build plan;
`CLAUDE.md` is the decisions.

**The camera needs HTTPS, and that is now done** (2026-08-30).
`navigator.mediaDevices.getUserMedia` exists only in a secure context, so the hint page is
the one page here that must be reached over TLS:

    https://evo-x3.tail80adad.ts.net/hint                 ← node name
    https://halo-workbench.tail80adad.ts.net/hint         ← Service name, also HTTPS

Note the **full** DNS name: a certificate is issued for one, so the bare
`http://halo-workbench/` used everywhere else in this app is not a secure context. Every
other page is happy on either.

**Type the `https://` — a bookmark or a bare hostname gets you `http://` and no camera.**
The page notices and offers a one-tap link to the right address rather than leaving you to
retype a URL on a tablet.

Enabling this had a permanent cost, taken deliberately: certificate names are published
to public Certificate Transparency logs and cannot be withdrawn. See PLAN.md 7a.

The file and paste capture paths need no secure context and work over plain HTTP, so the
tool is usable from anywhere on the tailnet; only the live camera needs the TLS URL.

## Run it (development)

```bash
scripts/bootstrap.sh      # create ~/venvs/workbench
scripts/fetch-privacy-filter.sh   # ~920 MB, the redaction recall layer (CPU, ONNX)
scripts/dev.sh            # uvicorn on :8093 with --reload
curl -s localhost:8093/healthz
```

Then `http://localhost:8093/` for the five tools. A converted PDF is redactable in one
click from its result page. Acceptance tests:

```bash
tests/phase1_acceptance.sh    # the queue: serialisation, and kill -9 recovery
tests/phase2_acceptance.sh    # marker: what the sidecar verifier refuses (no GPU needed)
tests/phase4_acceptance.sh    # redact: patterns, the verifier, and pdftotext on the output
tests/phase5_acceptance.sh    # regex: the runner and the loop's contract
tests/phase5_acceptance.sh --cases   # ...plus ten real cases against :8092
tests/phase6_acceptance.sh    # translate: spans, the checks, the vendored masker's drift guard
tests/phase6_acceptance.sh --live    # ...plus a real round trip through :8094
tests/phase7_acceptance.sh    # hint: the spread, the refusals, and the silent-answer case
tests/phase7_acceptance.sh --live CROP.png   # ...plus the real OCR models on the real GPU
```

The translation model is on-demand, and the app starts it when you press Translate —
then stops it again after 15 minutes idle. Install the unit once:

```bash
cp config/llama-hymt2.service ~/.config/systemd/user/
systemctl --user daemon-reload      # do NOT enable it: on-demand is the point
```

Start it by hand and the app leaves it alone — it only ever stops a unit it started
itself. The hint tool's ensemble works the same way, over four units that are all
on-demand and none enabled: `llama-olmocr` (:8097), `llama-vision` (:8084) and
`llama-qwen38` (:8090) are halo-prep's, and the third voice is this repo's:

```bash
cp config/llama-chandra.service ~/.config/systemd/user/    # Chandra-OCR-2 on :8098
systemctl --user daemon-reload      # again: do NOT enable it
```

Chandra is the one member halo-prep's batch bake-off *rejected* — for returning a page
as zero bytes, silently. Attended, that failure is harmless and announces itself on the
page; what it buys is the only member that marks a strikethrough `<del>` instead of
resolving it, which is exactly the ambiguity a marked script produces. The unit file
carries the argument in full.

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
# Node port: http://evo-x3:8093
sudo tailscale serve --bg --http=8093 http://127.0.0.1:8093

# A Tailscale Service: http://halo-workbench/ , its own name and VIP on the tailnet,
# so the address survives this moving to another machine and carries no port.
tailscale serve --service=svc:halo-workbench --bg --yes --http=80 http://127.0.0.1:8093

# HTTPS on both, added 2026-08-30 for the hint tool's camera. Needs HTTPS Certificates
# enabled in the admin console first -- without it `tailscale cert` answers "your
# Tailscale account does not support getting TLS certs", which is the toggle, not a plan.
sudo tailscale serve --bg --https=443 http://127.0.0.1:8093
tailscale serve --service=svc:halo-workbench --bg --yes --https=443 http://127.0.0.1:8093
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

- `../halo-prep` — the runbook that built the box: model units, ports, pitfalls.
- `../deep-research-podcast` — the other thing this box is genuinely good at.
- `../drp-web` — its web interface, at `http://drp-web/` (a Tailscale Service; also
  reachable as `http://evo-x3:8095`), linked from the header here.
  Same skeleton as this app; hours-long runs, so they are systemd units rather than
  queued jobs, and it gets a service of its own rather than a sixth tab.
