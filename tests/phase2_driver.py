"""Fabricate marker job directories for tests/phase2_acceptance.sh.

The verifier's job is to refuse things, and every refusal is cheap to provoke *if*
you can put a plausible job on disk without spending twenty GPU-minutes. So this
builds one by hand: real markdown, a real `.marker-ok` sidecar with a real checksum,
in the layout marker_run.sh produces. The GPU half of phase 2 -- an actual paper
converting -- is run by hand; PLAN.md says so.

    python tests/phase2_driver.py fake-done <state_dir>   # prints the job id
"""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

state = Path(sys.argv[2])
job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-fake01"
job = state / "jobs" / job_id
out = job / "out" / "paper"
out.mkdir(parents=True)

(job / "paper.pdf").write_bytes(b"%PDF-1.4 not really\n")
md = out / "paper.md"
md.write_text("# A Paper\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n$$e^{i\\pi}+1=0$$\n")
(out / "paper_page_1_Figure_1.jpeg").write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")

# The same fields marker_run.sh writes, and the same one that matters: the checksum.
(out / "paper.marker-ok").write_text(
    "marker_run_version=1\n"
    f"generated_at={datetime.now(timezone.utc).isoformat()}\n"
    f"markdown={md.resolve()}\n"
    f"markdown_sha256={hashlib.sha256(md.read_bytes()).hexdigest()}\n"
    "mode=fast\n"
)
(job / "meta.json").write_text(
    json.dumps(
        {
            "id": job_id,
            "tool": "marker",
            "status": "done",
            "params": {},
            "input_filename": "paper.pdf",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": None,
            "result": {
                "markdown_rel": "out/paper/paper.md",
                "markdown_name": "paper.md",
                "markdown_bytes": md.stat().st_size,
                "images": ["paper_page_1_Figure_1.jpeg"],
            },
        },
        indent=2,
    )
)
print(job_id)
