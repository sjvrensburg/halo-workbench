"""halo-workbench — specialist local-model tools over Tailscale.

Phase 0 scaffold: routing, config and the health check only. The three tools are
PLAN.md phases 2, 4 and 5; the job queue they all sit on is phase 1.
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app import config

app = FastAPI(title="halo-workbench", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> dict:
    """Liveness only -- deliberately does not touch the model servers.

    systemd and `curl` from the laptop use this to answer "is the app up", which
    must stay answerable while :8092 is busy with a Hermes turn. Model reachability
    is a separate concern and belongs on the tool pages, where a stalled server is
    the user's problem rather than the supervisor's.
    """
    return {"status": "ok", "version": config.VERSION}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """<!doctype html>
<title>halo-workbench</title>
<style>
 body{font:16px/1.5 system-ui,sans-serif;max-width:44rem;margin:3rem auto;padding:0 1rem}
 li{margin:.4rem 0} .todo{color:#888}
</style>
<h1>halo-workbench</h1>
<p>Specialist tools on the EVO-X3. Phase 0 &mdash; nothing is built yet.</p>
<ul>
  <li class="todo">PDF &rarr; markdown (marker) &mdash; PLAN.md phase 2</li>
  <li class="todo">Regex from examples &mdash; PLAN.md phase 4</li>
  <li class="todo">Translate with protected spans &mdash; PLAN.md phase 5</li>
</ul>
"""
