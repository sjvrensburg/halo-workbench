"""halo-workbench — PDF-to-markdown over Tailscale.

Routing, config, the health check and the job queue's lifecycle. Marker itself is
PLAN.md phase 2; it registers its handler with `app.jobs`.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app import config, jobs
from app.tools import marker


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

    try:
        yield
    finally:
        jobs.stop()


app = FastAPI(title="halo-workbench", docs_url=None, redoc_url=None, lifespan=lifespan)

app.include_router(marker.router)


@app.get("/healthz")
def healthz() -> dict:
    """Liveness only -- deliberately does not touch marker or the GPU.

    systemd and `curl` from the laptop use this to answer "is the app up", which must
    stay answerable while a conversion is mid-run. Whether marker itself is healthy is
    a separate concern and belongs on `/marker`, where a stalled conversion is the
    user's problem rather than the supervisor's.
    """
    return {
        "status": "ok",
        "version": config.VERSION,
        # Local and free to read -- it is a queue length in this process, not a probe
        # of anything. Useful in `watch curl` when a job seems to be sitting still.
        "queued": jobs.queue_depth(),
    }


@app.get("/")
def index():
    # One tool now -- a separate landing page is a click a user does not need.
    return RedirectResponse("/marker")
