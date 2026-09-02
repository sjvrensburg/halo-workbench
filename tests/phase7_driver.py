"""PLAN.md phase 7 acceptance: the handwriting hint tool.

    python tests/phase7_driver.py offline           # no GPU, no models, no camera
    python tests/phase7_driver.py live IMAGE [id..] # real models on the real box

What `offline` asserts is everything that decides what the operator SEES, which for this
tool is the whole product: this is the one page here whose verifier is the human, so the
things that can go quietly wrong are all presentational. A silent answer must announce
itself, disagreement must survive to the screen, markup must not be flattened, and a
capture's real dimensions must be read from the bytes rather than taken on trust.

It never touches a model: a fake OpenAI-compatible server stands in, which is also the
only way to reproduce the failure that matters most — HTTP 200 with an empty `content`
and every token spent in `reasoning_content` (halo-prep measured it on Chandra-OCR-2).

`live` is the latency and plumbing measurement against the real units, and it needs an
image because the halo-prep handwriting samples were student scripts and are not on this
box (rightly). Give it the crop you actually care about.
"""

import base64
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Point the primary member at the fake server BEFORE app.config is imported: every
# endpoint in this repo reads its env override at import time, which is what lets the
# acceptance drivers run without touching anything resident.
#
# ONLY for `offline`. Doing it unconditionally sent the live run at the fake port too, so
# it started the real :8097 unit and then polled a dead address for three minutes before
# reporting the model down — with the model up and answering the whole time (2026-08-30).
FAKE_PORT = int(os.environ.get("WORKBENCH_FAKE_PORT", 8399))
if sys.argv[1:2] != ["live"]:
    os.environ.setdefault("WORKBENCH_OLMOCR_ENDPOINT", f"http://127.0.0.1:{FAKE_PORT}")

from app import config, handwriting, hint_store  # noqa: E402

MEMBERS_BY_ID = {member["id"]: member for member in config.HINT_MEMBERS}

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


# ── a fake llama-server ──────────────────────────────────────────────────────

class Fake(BaseHTTPRequestHandler):
    """Replies from a script, and records the bodies it was sent."""

    script: list[dict] = []
    seen: list[dict] = []

    def log_message(self, *args):  # keep the output readable
        pass

    def do_GET(self):
        """/health, and it must answer 200.

        Not decoration: model_lease asks this first, and a fake that 404s here would send
        the driver off to `systemctl --user start llama-olmocr` and then poll for three
        minutes. An offline acceptance test that starts a 10 GiB model server is not an
        offline acceptance test -- this cost one hung run on 2026-08-30.
        """
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        Fake.seen.append(body)
        reply = Fake.script[min(len(Fake.seen) - 1, len(Fake.script) - 1)]
        choice = {"message": {"content": reply.get("content", ""),
                              "reasoning_content": reply.get("reasoning", "")},
                  "finish_reason": reply.get("finish_reason", "stop")}
        if "logprobs" in reply:
            choice["logprobs"] = {"content": reply["logprobs"]}
        payload = json.dumps({
            "choices": [choice],
            "usage": {"prompt_tokens": 4081, "completion_tokens": 12,
                      "prompt_tokens_details": {"cached_tokens": 4080}},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", FAKE_PORT), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ── images, without an image library ─────────────────────────────────────────

def sample_images() -> dict[str, bytes]:
    """A real PNG and a real JPEG at a known size, made with PyMuPDF (already a dep)."""
    import pymupdf

    page = pymupdf.open()
    page.new_page(width=300, height=200)
    pixmap = page[0].get_pixmap(dpi=72)
    return {"png": pixmap.tobytes("png"), "jpeg": pixmap.tobytes("jpg")}


def data_url(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


# ── the offline checks ───────────────────────────────────────────────────────

def offline() -> int:
    print(">> the capture's real dimensions, read from the bytes")
    images = sample_images()
    check("PNG size", handwriting.image_size(images["png"]) == (300, 200),
          str(handwriting.image_size(images["png"])))
    check("JPEG size", handwriting.image_size(images["jpeg"]) == (300, 200),
          str(handwriting.image_size(images["jpeg"])))
    check("not an image at all", handwriting.image_size(b"not an image") is None)

    print("\n>> what the endpoint refuses")
    for label, value in [("a bare string", "hello"),
                         ("a non-image data URL", "data:text/plain;base64,aGk="),
                         ("bad base64", "data:image/png;base64,!!!!")]:
        try:
            handwriting.decode_data_url(value)
            check(label, False, "was accepted")
        except handwriting.HintError:
            check(label, True)
    raw, mime = handwriting.decode_data_url(data_url(images["jpeg"], "image/jpeg"))
    check("a real JPEG data URL round-trips", raw == images["jpeg"] and mime == "image/jpeg")

    print("\n>> one request, and the failure that must not be silent")
    server = serve()
    endpoint = f"http://127.0.0.1:{FAKE_PORT}"
    try:
        Fake.script = [{"content": "  the reading  "}]
        Fake.seen = []
        result = handwriting.read_once(endpoint, images["jpeg"], "image/jpeg",
                                       mode="crop", temperature=0.6)
        check("content is stripped and returned", result["text"] == "the reading")
        check("temperature is sent per request, not left to the unit",
              Fake.seen[0]["temperature"] == 0.6,
              "llama-olmocr.service runs --temp 0; that is a batch decision")
        check("the crop prompt is the one sent",
              Fake.seen[0]["messages"][0]["content"][1]["text"] == handwriting.CROP_PROMPT)
        check("the image is sent as a data URL with its own mime",
              Fake.seen[0]["messages"][0]["content"][0]["image_url"]["url"]
              .startswith("data:image/jpeg;base64,"))
        check("prefill reuse is surfaced", result["cached_tokens"] == 4080)

        # THE failure: HTTP 200, finish_reason stop, every token in reasoning_content.
        Fake.script = [{"content": "", "reasoning": "I will correct the typos." * 20}]
        Fake.seen = []
        silent = handwriting.read_once(endpoint, images["jpeg"], "image/jpeg")
        check("an empty answer is reported, not returned as a reading",
              silent["silent"] and silent["reasoning_chars"] > 0,
              "halo-prep: Chandra returned a page as zero bytes with 568 reasoning tokens")

        Fake.script = [{"content": "a very long", "finish_reason": "length"}]
        Fake.seen = []
        cut = handwriting.read_once(endpoint, images["jpeg"], "image/jpeg")
        check("hitting the token ceiling is flagged", cut["truncated"])

        print("\n>> per-token hesitation (7f) -- a different thing from the spread")
        check("no logprobs is None, not an empty list",
              handwriting.token_confidences(None) is None
              and handwriting.token_confidences([]) is None)

        ascii_tokens = handwriting.token_confidences([
            {"bytes": [102, 105, 116], "logprob": -0.0001,
             "top_logprobs": [{"bytes": [102, 105, 116], "logprob": -0.0001},
                              {"bytes": [70, 105, 116], "logprob": -9.9}]},
        ])
        check("a plain ASCII token round-trips with its probability and alternates",
              ascii_tokens[0]["text"] == "fit" and ascii_tokens[0]["prob"] > 0.999
              and ascii_tokens[0]["alternates"][1]["text"] == "Fit")

        # A UTF-8 character split across two BPE tokens' bytes ("é" = 0xC3 0xA9) --
        # the incremental decoder must reassemble it rather than raise or misalign.
        split_char = handwriting.token_confidences([
            {"bytes": [0xC3], "logprob": -1.0, "top_logprobs": []},
            {"bytes": [0xA9, ord("!")], "logprob": -0.2, "top_logprobs": []},
        ])
        check("a character split across two tokens' bytes still decodes correctly",
              split_char == [{"text": "é!", "prob": split_char[0]["prob"], "alternates": []}])

        def _tok(text, logprob=-0.01):
            return {"bytes": list(text.encode()), "logprob": logprob, "top_logprobs": []}

        Fake.script = [{"content": "fit",
                        "logprobs": [_tok("fit")]}]
        Fake.seen = []
        aligned = handwriting.read_once(endpoint, images["jpeg"], "image/jpeg")
        check("tokens that DO reconstruct the reading are kept",
              aligned["tokens"] is not None and aligned["tokens"][0]["text"] == "fit")

        # Measured on Muse Glimmer 2026-09-02: logprobs.content covered the model's
        # WHOLE generation (reasoning, channel markers, even an echo of the prompt),
        # not just `content`. Reproduced here without a live reasoning model.
        Fake.script = [{"content": "the answer is x = 5",
                        "logprobs": [_tok("<|message|>"), _tok("This is a crop")]}]
        Fake.seen = []
        misaligned = handwriting.read_once(endpoint, images["jpeg"], "image/jpeg")
        check("tokens that do NOT reconstruct the reading are dropped, not shown wrong",
              misaligned["text"] == "the answer is x = 5" and misaligned["tokens"] is None)

        print("\n>> the spread, and what it must not do to it")
        samples = [{"text": "the cat", "silent": False},
                   {"text": "The  cat", "silent": False},
                   {"text": "the cot", "silent": False},
                   {"text": "", "silent": True, "reasoning_chars": 400}]
        readings = handwriting.group(samples)
        check("case and whitespace fold together", len(readings) == 2)
        check("the best-supported reading is first",
              readings[0]["count"] == 2 and readings[1]["count"] == 1)
        check("the model's own text is kept, not the folded key",
              readings[0]["text"] == "the cat")
        check("a silent sample is not a reading",
              sum(r["count"] for r in readings) == 3)

        struck = handwriting.group([{"text": "may <del>fit</del> fit", "silent": False}])
        check("strikethrough markup survives verbatim",
              struck[0]["text"] == "may <del>fit</del> fit",
              "a member that marks it up has READ it; flattening throws that away")
        check("two readings differing by one character stay two",
              len(handwriting.group([{"text": "generalisability", "silent": False},
                                     {"text": "generalizability", "silent": False}])) == 2,
              "no fuzzy matching: the difference IS the finding")

        print("\n>> what the page says across members")
        agree = handwriting.agreement([
            {"id": "olmocr", "readings": [{"text": "the cat", "count": 3}]},
            {"id": "muse", "readings": [{"text": "The cat", "count": 1}]}])
        check("same reading from two models reads as agreement", agree["state"] == "agree")
        disagree = handwriting.agreement([
            {"id": "olmocr", "readings": [{"text": "fit", "count": 3}]},
            {"id": "muse", "readings": [{"text": "not fit", "count": 2}]}])
        check("different readings read as disagreement", disagree["state"] == "disagree",
              "this is the finding, not an error -- PLAN.md 7d")
        alone = handwriting.agreement([{"id": "olmocr", "readings": [{"text": "fit", "count": 5}]}])
        check("one member alone is never agreement", alone["state"] == "insufficient",
              "five samples agreeing means confident, which it can be while wrong")

        print("\n>> the routes")
        from fastapi.testclient import TestClient
        from app.main import app
        # No `with`: the lifespan is what takes the flock on the state directory, and the
        # resident service is holding it. These routes need neither the queue nor a job.
        client = TestClient(app)
        page = client.get("/hint")
        check("the page renders", page.status_code == 200)
        check("the page is no-store", page.headers.get("cache-control") == "no-store",
              "captures are student scripts; nothing about them should sit in a cache")
        check("the page carries a working HTTPS address for the camera",
              "HTTPS_ORIGINS" in page.text and config.HINT_HTTPS_ORIGINS,
              "a browser given a bare hostname picks http:// and the camera API vanishes")
        check("every configured HTTPS origin is https and portless",
              all(u.scheme == "https" and u.port is None and "." in u.hostname
                  for u in map(urlsplit, config.HINT_HTTPS_ORIGINS)),
              "the TLS registration is on 443, and a certificate is issued for a FULL name "
              "-- swapping the scheme on `host:8093` or on the short MagicDNS name gives a "
              "link that cannot work")
        check("the camera wall is in the markup for JS to reveal",
              "not a secure context" in page.text,
              "getUserMedia is undefined outside HTTPS, with no error worth reading")

        listing = client.get("/hint/members").json()
        check("every member is offered", len(listing["members"]) == len(config.HINT_MEMBERS))
        check("exactly one member is primary",
              sum(1 for m in listing["members"] if m["primary"]) == 1)
        check("an uninstalled unit is reported as such, not as 'not running'",
              all("installed" in m for m in listing["members"]))

        check("an unknown model is a 400",
              client.post("/hint/read", json={"member": "nope", "image": "x"}).status_code == 400)
        check("a bad image is a 400",
              client.post("/hint/read",
                          json={"member": "olmocr", "image": "hello"}).status_code == 400)

        Fake.script = [{"content": "the cat"}, {"content": "the cot"}, {"content": "the cat"}]
        Fake.seen = []
        before = set(config.JOBS_DIR.glob("*")) if config.JOBS_DIR.exists() else set()
        before_hints = set(config.HINTS_DIR.glob("*")) if config.HINTS_DIR.exists() else set()
        answer = client.post("/hint/read", json={
            "member": "olmocr", "image": data_url(images["jpeg"], "image/jpeg"),
            "samples": 3, "capture_px": {"width": 640, "height": 480}}).json()
        check("three samples were drawn", len(answer["samples"]) == 3)
        check("the spread is grouped", [r["count"] for r in answer["readings"]] == [2, 1])
        check("a 640x480 capture is called out",
              answer["low_resolution"] and answer["min_long_edge"] == 2000,
              "halo-prep measured 5/15 on symbols purely on resolution")
        check("the crop's own size is reported from the bytes",
              answer["crop_px"] == {"width": 300, "height": 200})
        after = set(config.JOBS_DIR.glob("*")) if config.JOBS_DIR.exists() else set()
        check("nothing reached the job store", before == after,
              "the crop is retained (7g), but through its own store, never the job queue's")

        print("\n>> the 7-day capture store (7g)")
        new_entries = (set(config.HINTS_DIR.glob("*")) if config.HINTS_DIR.exists() else set()) - before_hints
        check("exactly one capture was saved", len(new_entries) == 1)
        entry = next(iter(new_entries))
        check("the crop actually sent is what's on disk",
              (entry / "crop.jpeg").read_bytes() == images["jpeg"])
        meta = json.loads((entry / "meta.json").read_text())
        check("the meta records the member, mode and readings",
              meta["member"] == "olmocr" and meta["mode"] == "crop"
              and [r["text"] for r in meta["readings"]] == ["the cat", "the cot"])
        check("token/logprob detail did not follow the reading onto disk",
              all("tokens" not in r and "alternates" not in r for r in meta["readings"]))

        # A no-crop read ("Whole capture instead") sends mode="page" with the WHOLE
        # frame as `image` -- caught 2026-09-02 when the operator asked exactly this
        # question. Confirmed that case should be retained too, not silently skipped,
        # so this locks in that it actually is, and with the whole image, not a crop.
        Fake.script = [{"content": "a page of writing"}]
        Fake.seen = []
        before_page = set(config.HINTS_DIR.glob("*"))
        client.post("/hint/read", json={
            "member": "olmocr", "image": data_url(images["png"], "image/png"),
            "mode": "page", "samples": 1})
        page_entries = set(config.HINTS_DIR.glob("*")) - before_page
        check("a no-crop read is retained too, not silently skipped", len(page_entries) == 1)
        page_entry = next(iter(page_entries))
        check("...and what's kept is the WHOLE capture that was actually sent",
              (page_entry / "crop.png").read_bytes() == images["png"])
        check("...tagged as a page, not a crop",
              json.loads((page_entry / "meta.json").read_text())["mode"] == "page")

        # prune(now=...) takes `now`, not a cutoff: it subtracts the retention window
        # itself, so `now` must be further out than that window for a just-created
        # entry to have aged past it.
        far_future = datetime.now(timezone.utc) + timedelta(days=config.HINT_RETENTION_DAYS + 1)
        removed = hint_store.prune(now=far_future)
        check("prune() removes a capture whose window has passed", removed >= 1)
        check("...and it is actually gone", not entry.exists())

        Fake.script = [{"content": "the cat"}]
        Fake.seen = []
        second = client.post("/hint/read", json={
            "member": "olmocr", "image": data_url(images["jpeg"], "image/jpeg")}).json()
        check("the primary's default is the spread", len(second["samples"]) == config.HINT_SAMPLES)
        check("every member carries its own token ceiling",
              all(set(m["max_tokens"]) == {"crop", "page"} for m in config.HINT_MEMBERS),
              "one ceiling for all of them made Muse return empty three times out of three")
        check("a reasoning member gets more room than the primary",
              all(m["max_tokens"]["crop"] > MEMBERS_BY_ID["olmocr"]["max_tokens"]["crop"]
                  for m in config.HINT_MEMBERS if not m["primary"]),
              "they reach the answer only after the reasoning")
        check("the ceiling actually reaches the request",
              Fake.seen[0]["max_tokens"] == MEMBERS_BY_ID["olmocr"]["max_tokens"]["crop"])

        one = client.post("/hint/read", json={
            "member": "olmocr", "image": data_url(images["jpeg"], "image/jpeg"),
            "samples": 1}).json()
        check("a single sample is greedy", one["temperature"] == 0.0,
              "every halo-prep measurement behind these models was taken at temp 0")

        Fake.script = [{"content": "", "reasoning": "x" * 100}]
        Fake.seen = []
        empty = client.post("/hint/read", json={
            "member": "olmocr", "image": data_url(images["jpeg"], "image/jpeg"),
            "samples": 2}).json()
        check("an all-silent answer says so on the page",
              empty["silent_count"] == 2 and empty["silent_note"],
              "silence must not read as 'there was nothing written there'")
    finally:
        server.shutdown()

    print("\n>> the wiring that nothing else validates")
    endpoints = [member["endpoint"] for member in config.HINT_MEMBERS]
    check("no two members share a port", len(set(endpoints)) == len(endpoints))
    check("every member is leasable",
          all(config.MANAGED_UNITS.get(e) for e in endpoints),
          "a member missing from MANAGED_UNITS would never be started, only reported down")
    check("the translate unit is still managed",
          config.MANAGED_UNITS.get(config.TRANSLATE_ENDPOINT) == "llama-hymt2")
    check("the big members get the short idle timer",
          all(config.MODEL_IDLE_OVERRIDES.get(m["unit"], config.MODEL_IDLE_SECONDS)
              < config.MODEL_IDLE_SECONDS
              for m in config.HINT_MEMBERS if m["approx_gib"] > 12),
          "~20 GiB held through a coffee break is what PLAN.md 7e says not to do")
    unit = Path(__file__).resolve().parent.parent / "config/llama-chandra.service"
    # Line-anchored, not a substring: the file's own comment explains why the section is
    # absent, and a naive `in` test reads that explanation as the thing it warns against.
    check("the unit this repo ships has no [Install] section",
          unit.exists() and not any(line.startswith("[Install]")
                                    for line in unit.read_text().splitlines()),
          "there must be nothing to enable by accident")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} — " + "; ".join(FAILURES))
        return 1
    print("all offline checks passed")
    return 0


# ── the live measurement ─────────────────────────────────────────────────────

def live(argv: list[str]) -> int:
    """Real models, real GPU. `live IMAGE [member-id ...]`, default: every member.

    Latency is the acceptance criterion here, not accuracy — the operator is standing at
    a visualiser waiting. Accuracy is theirs to judge against the ink, which is the whole
    design (PLAN.md phase 7, "the verifier is the human").
    """
    from app import model_lease

    if not argv:
        print("usage: phase7_driver.py live IMAGE [member-id ...]", file=sys.stderr)
        print("  the halo-prep handwriting samples were student scripts and are not on "
              "this box; supply your own crop.", file=sys.stderr)
        return 2
    path = Path(argv[0])
    raw = path.read_bytes()
    mime = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    size = handwriting.image_size(raw)
    print(f"{path.name}: {size[0]}x{size[1]}" if size else f"{path.name}: unknown size")
    if size and max(size) < config.HINT_MIN_LONG_EDGE_PX and len(argv) < 2:
        print(f"  note: under {config.HINT_MIN_LONG_EDGE_PX} px on the long edge. That is "
              f"expected for a CROP and a problem for a whole page.")

    wanted = argv[1:] or [member["id"] for member in config.HINT_MEMBERS]
    answers = []
    for member in config.HINT_MEMBERS:
        if member["id"] not in wanted:
            continue
        print(f"\n{member['name']} ({member['unit']}, ~{member['approx_gib']} GiB)")
        if not model_lease.unit_exists(member["unit"]):
            print("  unit is not installed — skipped")
            continue
        try:
            waited = model_lease.ensure_running(
                member["endpoint"],
                needs_gib=member["approx_gib"] + config.HINT_MEMORY_HEADROOM_GIB)
        except RuntimeError as exc:
            print(f"  {exc}")
            continue
        if waited:
            print(f"  started in {waited:.1f}s")

        # Same split as the page: the primary is sampled for spread, a second voice is
        # asked once, because what it answers is whether the ink is ambiguous.
        samples_wanted = (config.HINT_SAMPLES if member["primary"]
                          else config.HINT_SECOND_VOICE_SAMPLES)
        samples, started = [], time.time()
        for index in range(samples_wanted):
            try:
                sample = handwriting.read_once(
                    member["endpoint"], raw, mime, mode="crop",
                    temperature=0.0 if samples_wanted == 1 else config.HINT_TEMPERATURE,
                    max_tokens=member["max_tokens"]["crop"])
            except handwriting.HintError as exc:
                print(f"  sample {index + 1}: {exc}")
                break
            samples.append(sample)
            flag = " SILENT" if sample["silent"] else ""
            print(f"  sample {index + 1}: {sample['elapsed']:>5.1f}s  "
                  f"{sample['prompt_tokens']} prompt / {sample['completion_tokens']} out"
                  f"{flag}  {sample['text'][:70]!r}")
        total = time.time() - started
        readings = handwriting.group(samples)
        print(f"  {len(readings)} distinct reading(s) in {total:.1f}s"
              + ("  <- over the 20 s the operator is waiting on" if total > 20 else ""))
        if readings:
            answers.append({"id": member["id"], "readings": readings})

    verdict = handwriting.agreement(answers)
    print(f"\nacross {verdict['answered']} member(s): {verdict['state']} — {verdict['note']}")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "offline"
    sys.exit(offline() if mode == "offline" else live(sys.argv[2:]))
