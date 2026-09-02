"""halo-workbench — specialist local-model tools over Tailscale.

Routing, config, the health check and the job queue's lifecycle. The tools themselves
are PLAN.md phases 2, 4 and 5; each registers a handler with `app.jobs`.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import config, hint_store, jobs, model_lease
from app.tools import hint, marker, redact, regex, translate
from app.views import templates


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Own the worker thread for exactly as long as the process serves requests.

    Startup also reaps jobs left `running` by a crash and re-queues jobs left
    `queued` -- see jobs._recover. Both must happen before the worker starts, so
    they happen here rather than in a route or at import time (import runs twice
    under `uvicorn --reload`).
    """
    try:
        # A job the previous process was running is holding GPU memory in its cgroup
        # right now, and the first thing this service will do is hand marker the GPU
        # again.
        marker.reap_scopes_for(jobs.start())
    except jobs.StateDirBusy as exc:
        # One line the operator can act on, then fail: a second instance that carried
        # on would recover the first one's live job out from under it.
        logging.getLogger("uvicorn.error").error("refusing to start — %s", exc)
        raise

    # Owns nothing yet: it only ever stops a unit a job here started (app/model_lease.py).
    model_lease.start()

    # 7g: hint captures are retained 7 days now, in their own store -- see
    # app/hint_store.py for why this is not just another jobs.py.
    hint_store.start()

    try:
        yield
    finally:
        jobs.stop()
        model_lease.stop()
        hint_store.stop()


app = FastAPI(title="halo-workbench", docs_url=None, redoc_url=None, lifespan=lifespan)

# One router per tool, so a tool's routes, its job handler and its verifier stay in
# one file. Importing the module is also what registers its handler with app.jobs.
app.include_router(marker.router)
app.include_router(redact.router)
app.include_router(regex.router)
app.include_router(translate.router)
# The fifth page is deliberately not a fifth service: a port per tool means a
# `tailscale serve` registration per tool, living outside this repo and invisible to
# grep (CLAUDE.md "One port"). It also keeps :8097 and friends on loopback.
app.include_router(hint.router)

# Vendored assets only (currently: KaTeX for the hint page's math rendering, see
# app/static/katex/VERSION). Not a build output and not user content -- no reason
# for this to be anything but a plain directory mount.
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
          name="static")


@app.get("/healthz")
def healthz() -> dict:
    """Liveness only -- deliberately does not touch the model servers.

    systemd and `curl` from the laptop use this to answer "is the app up", which
    must stay answerable while :8092 is busy with a Hermes turn. Model reachability
    is a separate concern and belongs on the tool pages, where a stalled server is
    the user's problem rather than the supervisor's.
    """
    return {
        "status": "ok",
        "version": config.VERSION,
        # Local and free to read -- it is a queue length in this process, not a probe
        # of anything. Useful in `watch curl` when a job seems to be sitting still.
        "queued": jobs.queue_depth(),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html",
                                      headers={"cache-control": "no-store"})
