"""ProbeAI's live UI: point it at a URL from the browser instead of the command line (D10).

Run:  python -m uvicorn web.server:app --port 8000
Page: http://127.0.0.1:8000/

Mode (real/record/replay/fake) is fixed for the life of this process, exactly like the CLI
(probe/agent.py's `main()`): set PROBE_LLM_MODE before starting the server, never from the page.
real/record additionally need PROBE_WEB_YES_SPEND=1 - the browser-facing equivalent of the CLI's
--yes-spend, checked once at startup rather than per click, since there is no page control that
should be allowed to authorise real spending. replay also needs PROBE_REPLAY_URL: a recorded run's
StepDecisions name refs that only resolve on the exact page the recording was made against, so the
page's URL field is locked to it rather than left free (D10). The server refuses to start at all if
any of this is wrong - see validate_startup().

Only one run is ever in progress: `RunState` is one global, lock-protected slot, and POST /api/run
returns 409 while it is occupied.

Implementation note (a deviation from D10's "launch one shared browser at startup," found while
building this): sync Playwright refuses to run inside a thread that has an asyncio event loop
running - FastAPI's own startup/shutdown lifespan is exactly such a loop, so starting the browser
there raises "Playwright Sync API inside the asyncio loop" (reproduced while writing this file's
tests). run_test() already launches and closes its own browser per call when none is given, and it
does so from the background thread, which has no asyncio loop - so each run gets its own browser
instead of a shared one. The cost is about a second of Chromium start-up per run, which is not
noticeable against a run that already takes tens of seconds; the D10 goal (a single-presenter,
one-demo-at-a-time tool, no shared mutable browser state) is unaffected.
"""
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from probe.agent import PROFILES, Profile, RunResult, check_spend_confirmed, run_test
from probe.llm import DEFAULT_MAX_COST_USD, DEFAULT_TOTAL_BUDGET_USD, ENV_FILE, MODES, LLMClient, load_dotenv

STATIC = Path(__file__).parent / "static"
RUNS_DIR = Path("runs")
MISSION_ID = re.compile(r"^m\d+$")
SHOT_NAME = re.compile(r"^step_\d+_(before|after)\.png$")

load_dotenv(ENV_FILE)
MODE = os.environ.get("PROBE_LLM_MODE")
REPLAY_URL = os.environ.get("PROBE_REPLAY_URL")
YES_SPEND = os.environ.get("PROBE_WEB_YES_SPEND") == "1"


def cap_usd() -> float:
    try:
        return float(os.environ.get("PROBE_MAX_COST_USD") or DEFAULT_MAX_COST_USD)
    except ValueError:
        return DEFAULT_MAX_COST_USD


def total_budget_usd() -> float:
    try:
        return float(os.environ.get("PROBE_TOTAL_BUDGET_USD") or DEFAULT_TOTAL_BUDGET_USD)
    except ValueError:
        return DEFAULT_TOTAL_BUDGET_USD


def run_profile() -> Profile | str:
    """The profile a run actually uses. In replay mode this is NOT PROFILES["live"] (found while
    testing this): a recording only ever has as many "step"/"judge"/"disprove" answers as the
    missions that were actually explored when it was made - the raw "plan" answer itself can (and
    for the 2026-09-22 recording, does: 5 proposed missions, 4 recorded step answers) list more
    missions than that. Replaying under the standard 3-mission profile tried a second mission the
    recording never explored and ran out of answers mid-run. PROBE_REPLAY_MAX_MISSIONS (default 1,
    matching every recording made so far) keeps replay honest about what it can actually replay;
    change it only alongside making a new, larger recording."""
    if MODE != "replay":
        return "live"
    try:
        max_missions = int(os.environ.get("PROBE_REPLAY_MAX_MISSIONS") or 1)
    except ValueError:
        max_missions = 1
    live = PROFILES["live"]
    return Profile("replay", max_missions=max_missions, max_total_steps=live.max_total_steps,
                   max_steps_per_mission=live.max_steps_per_mission, wall_clock_s=live.wall_clock_s,
                   replays=live.replays)


def validate_startup(mode: str | None, yes_spend: bool, replay_url: str | None) -> str | None:
    """None if the server may start; otherwise the message to print and refuse to start on.
    A pure function so this is testable directly, the same way check_spend_confirmed already is -
    without needing to actually launch (or fail to launch) a live server."""
    if mode not in MODES:
        return f"PROBE_LLM_MODE must be one of {', '.join(MODES)}, not {mode!r}."
    if check_spend_confirmed(mode, yes_spend) is not None:
        return (f"PROBE_LLM_MODE={mode} spends real API money (this run's cap: {cap_usd()} USD). "
               "There is no page control that can confirm this, on purpose - set "
               "PROBE_WEB_YES_SPEND=1 before starting the server to confirm.")
    if mode == "replay" and not replay_url:
        return ("PROBE_LLM_MODE=replay needs PROBE_REPLAY_URL: the exact app the recording was made "
               "against (a replayed step only resolves correctly on that same page).")
    return None


def compute_run_status(result: RunResult) -> str:
    """completed | partial, from a RunResult that run_test() actually returned (a run that raised
    before returning is "failed" - handled separately in _run_in_background, there is no
    RunResult to inspect for that case)."""
    if result.timed_out:
        return "partial"
    if any(m["status"] != "done" for m in result.missions):
        return "partial"
    return "completed"


# ---- the one run this server currently knows about (or the last one) ------------------------

@dataclass
class RunState:
    lock: threading.Lock
    run_id: str | None = None
    queue: "queue.Queue | None" = None
    running: bool = False
    last_run_status: str | None = None
    last_report: dict | None = None
    last_findings: list | None = None


STATE = RunState(lock=threading.Lock())


def _run_in_background(url: str, reset_path: str | None, out_dir: Path, q: "queue.Queue") -> None:
    """Runs in its own thread. Never raises: any problem becomes one 'error' event with
    run_status "failed", so the queue's consumer (the SSE generator) always gets a terminal event."""
    start = time.monotonic()
    held_run_finished: dict = {}

    def on_event(event: dict) -> None:
        if event["type"] == "run_finished":
            held_run_finished.update(event)  # run_status is not known until run_test() returns
            return
        q.put(event)

    try:
        llm = LLMClient(out_dir, mode=MODE)  # source=None: falls back to PROBE_LLM_SOURCE if fake/replay
        # browser=None: run_test() launches and closes its own here, in this plain thread (see the
        # module docstring for why it cannot be a single browser shared from the async lifespan).
        result = run_test(url, run_profile(), llm, out_dir, on_event=on_event, reset_path=reset_path)
    except Exception as exc:
        # Playwright's own exceptions are multi-line (the real error, then a blank line, then a
        # verbose "Call log:" retry trace) - found by actually watching a failed run (T6-c): the
        # full text is exactly the kind of thing that looks alarming to a non-engineer for no
        # reason. The first line already says what went wrong (e.g. "Page.goto:
        # net::ERR_CONNECTION_REFUSED at http://..."); the rest stays in server logs, not the page.
        raw = str(exc).strip() or type(exc).__name__  # guaranteed non-empty either way
        message = raw.splitlines()[0]
        q.put({"type": "error", "t": round(time.monotonic() - start, 3), "run_status": "failed",
              "message": message, "kind": type(exc).__name__})
        with STATE.lock:
            STATE.running = False
            STATE.last_run_status = "failed"
            STATE.last_report = None
            STATE.last_findings = None
        return

    run_status = compute_run_status(result)
    if not held_run_finished:  # run_test() is contractually supposed to have emitted this itself;
        held_run_finished = {"type": "run_finished", "t": round(time.monotonic() - start, 3),
                             "report": result.report}  # defensive fallback if it somehow did not
    held_run_finished["run_status"] = run_status
    q.put(held_run_finished)
    with STATE.lock:
        STATE.running = False
        STATE.last_run_status = run_status
        STATE.last_report = result.report
        STATE.last_findings = result.findings


# ---- FastAPI app ------------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    error = validate_startup(MODE, YES_SPEND, REPLAY_URL)
    if error:
        print(f"error: {error}", file=sys.stderr)
        raise RuntimeError(error)  # uvicorn reports a lifespan startup failure and exits non-zero
    print(f"ProbeAI web: mode={MODE}, this run's cap {cap_usd()} USD" +
         (f", replay target {REPLAY_URL}" if MODE == "replay" else ""))
    yield


app = FastAPI(title="ProbeAI", lifespan=lifespan)


class RunRequest(BaseModel):
    url: str
    reset_path: str | None = None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    with STATE.lock:
        return {
            "mode": MODE,
            "cap_usd": cap_usd(),
            "total_budget_usd": total_budget_usd(),
            "replay_url": REPLAY_URL if MODE == "replay" else None,
            "running": STATE.running,
            "run_id": STATE.run_id if STATE.running else None,  # lets a page reload reattach to /api/stream
            "last_run_status": STATE.last_run_status,
            "last_report": STATE.last_report,
            "last_findings": STATE.last_findings,
        }


@app.post("/api/run")
def start_run(body: RunRequest):
    with STATE.lock:
        if STATE.running:
            raise HTTPException(status_code=409, detail="a test is already running")
        run_id = uuid.uuid4().hex[:8]
        q: "queue.Queue" = queue.Queue()
        STATE.run_id = run_id
        STATE.queue = q
        STATE.running = True
        STATE.last_run_status = None
        STATE.last_report = None
        STATE.last_findings = None

    url = REPLAY_URL if MODE == "replay" else body.url
    out_dir = RUNS_DIR / f"web_{run_id}"
    thread = threading.Thread(target=_run_in_background, args=(url, body.reset_path, out_dir, q), daemon=True)
    thread.start()
    profile = run_profile()
    profile_name = profile if isinstance(profile, str) else profile.name
    return {"run_id": run_id, "mode": MODE, "cap_usd": cap_usd(), "profile": profile_name}


@app.get("/api/stream")
def stream(run_id: str):
    with STATE.lock:
        if run_id != STATE.run_id or STATE.queue is None:
            raise HTTPException(status_code=404, detail="no such run")
        q = STATE.queue

    def events():
        while True:
            event = q.get()
            yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            if event["type"] in ("run_finished", "error"):
                break

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/api/runs/{run_id}/shots/{mission_id}/{filename}")
def shot(run_id: str, mission_id: str, filename: str):
    if not MISSION_ID.match(mission_id) or not SHOT_NAME.match(filename):
        raise HTTPException(status_code=404)
    path = (RUNS_DIR / f"web_{run_id}" / mission_id / filename).resolve()
    if RUNS_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)
