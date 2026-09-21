"""TaskBoard: a small demo app with deliberately seeded issues (see ground_truth.json).

Run:  python -m uvicorn demo_app.server:app --port 8765
Page: /  (buggy build)   or   /?bugs=off  (clean build)

The client adds the same ?bugs=... query to every API call, so each request tells the
server which build it belongs to. State lives in memory; /__reset restores it.
The handlers are `async def` on purpose: they never await, so each one runs to the end
without interruption and no locking is needed.
"""
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="TaskBoard")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

tasks: list[dict] = []        # {"id": int, "title": str, "done": bool}
trigger_log: list[dict] = []  # ground truth: which seeded issue actually ran
next_id = 1


def reset_state():
    """Back to the two seed tasks and an empty trigger log."""
    global next_id
    tasks.clear()
    trigger_log.clear()
    next_id = 1
    for title in ("Buy milk", "Write report"):
        tasks.append({"id": next_id, "title": title, "done": False})
        next_id += 1


def log_trigger(trigger_id: str, detail: str = ""):
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    trigger_log.append({"id": trigger_id, "ts": ts, "detail": detail})


def is_buggy(request: Request) -> bool:
    return request.query_params.get("bugs") != "off"


def find_task(task_id: int) -> dict:
    for task in tasks:
        if task["id"] == task_id:
            return task
    raise HTTPException(status_code=404, detail="Task not found.")


reset_state()


# ---- page ---------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


# ---- API ----------------------------------------------------------------

class NewTask(BaseModel):
    title: str


class TaskUpdate(BaseModel):
    done: bool


@app.get("/api/tasks")
async def list_tasks():
    return tasks


@app.post("/api/tasks", status_code=201)
async def add_task(body: NewTask, request: Request):
    global next_id
    buggy = is_buggy(request)
    title = body.title

    if not title.strip():
        if not buggy:
            raise HTTPException(status_code=422, detail="Task title cannot be blank.")
        # SEEDED S3: blank / whitespace-only title is accepted
        log_trigger("S3", f"blank title accepted: {title!r}")
    else:
        title = title.strip()

    # Duplicate titles (case-insensitive) get 409 in both builds.
    if title.strip() and any(t["title"].strip().lower() == title.lower() for t in tasks):
        if buggy:
            # SEEDED S5: 409 is returned, and the client shows no message (see app.js)
            log_trigger("S5", f"duplicate title rejected: {title!r}")
        raise HTTPException(status_code=409, detail="A task with this title already exists.")

    task = {"id": next_id, "title": title, "done": False}
    next_id += 1
    tasks.append(task)
    return task


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: int, body: TaskUpdate, request: Request):
    task = find_task(task_id)
    if is_buggy(request) and task["done"] and not body.done:
        # SEEDED S2: reopening a completed task answers 200 but keeps done=true
        log_trigger("S2", f"task {task_id} stayed completed")
        return task
    task["done"] = body.done
    return task


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, request: Request):
    if is_buggy(request):
        # SEEDED S1: delete always fails with 500 and the task stays
        log_trigger("S1", f"delete of task {task_id} answered 500")
        return JSONResponse({"detail": "Internal server error"}, status_code=500)
    task = find_task(task_id)
    tasks.remove(task)
    return {"deleted": task_id}


# ---- ground truth plumbing (never linked from the UI) --------------------

class Trigger(BaseModel):
    id: str
    detail: str = ""


@app.get("/__trigger_log")
async def get_trigger_log():
    return trigger_log


@app.post("/__trigger")
async def post_trigger(body: Trigger):
    # used by the client for S4 and S6, which are visible only in the browser
    log_trigger(body.id, body.detail)
    return {"ok": True}


@app.post("/__reset")
async def reset():
    reset_state()
    return {"ok": True}
