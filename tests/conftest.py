"""Shared test support: a real TaskBoard server on a free port, one headless Chromium, and builders."""
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from probe.evidence import StepEvidence
from probe.executor import Run, Step, StepRecord, StepResult
from probe.state import PageState, RefEntry

ROOT = Path(__file__).resolve().parent.parent


def load_steps(name: str) -> list[Step]:
    """Steps from examples/<name>."""
    return [Step(**item) for item in json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode()


def fresh_app(base: str):
    """Seed tasks back and an empty trigger log. /__reset alone keeps the log (D8), so tests that
    read the log call this."""
    http("POST", base + "/__reset")
    http("POST", base + "/__trigger_log/clear")


@pytest.fixture(scope="session")
def server():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "demo_app.server:app",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 15
        while True:
            assert proc.poll() is None, "uvicorn exited during start-up"
            try:
                http("GET", base + "/__trigger_log")
                break
            except OSError:
                assert time.time() < deadline, "server did not start in 15 s"
                time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


# ---- builders for hand-made evidence (used by test_oracles.py and test_findings.py) ----------

APP = "http://app.test:8765"


def req(method: str, url: str, status: int | None, error: str | None = None) -> dict:
    """One entry of StepEvidence.requests. status=None means the request failed without an answer."""
    return {"method": method, "url": url, "status": status,
            "failed": status is None or status >= 400, "error": error, "resource_type": "fetch"}


def make_entry(role: str, name: str, nth: int = 0, checked=None, value=None) -> RefEntry:
    return RefEntry(ref="e1", role=role, name=name, nth=nth, checked=checked, disabled=False, value=value)


def make_state(refs=(), url: str = APP + "/", scroll_width: int = 1280, client_width: int = 1280) -> PageState:
    return PageState(url=url, title="t", snapshot_ai="", snapshot_plain="", fingerprint="fp",
                     refs=list(refs), scroll_width=scroll_width, client_width=client_width)


def make_record(action: str = "click", role: str = "button", name: str = "Go", nth: int = 0,
                text: str | None = None, error: str | None = None, blocked: str | None = None) -> StepRecord:
    return StepRecord(action=action, ref=None, locator={"role": role, "name": name, "nth": nth},
                      text=text, expect=None, error=error, blocked=blocked)


def make_evidence(record: StepRecord | None = None, step: int = 1, requests=(), console=(), page_errors=(),
                  dialogs=(), fp_before: str = "fp0", fp_after: str = "fp1",
                  scroll_width: int = 1280, client_width: int = 1280) -> StepEvidence:
    record = record or make_record()
    return StepEvidence(
        step=step, action=record.action, locator=record.locator, text=record.text, expect=None,
        error=record.error, blocked=record.blocked, url_before=APP + "/", url_after=APP + "/",
        fingerprint_before=fp_before, fingerprint_after=fp_after,
        scroll_width=scroll_width, client_width=client_width,
        requests=list(requests), console=list(console), page_errors=list(page_errors), dialogs=list(dialogs),
        screenshot_before=f"step_{step:02d}_before.png", screenshot_after=f"step_{step:02d}_after.png",
        timings={"total_ms": 0, "action_ms": 0, "settle_ms": 0})


def make_run(pairs) -> Run:
    """pairs: list of (StepRecord, StepEvidence). The states are empty stand-ins."""
    state = make_state()
    results = [StepResult(record, evidence, state, state) for record, evidence in pairs]
    return Run(url=APP + "/", started_at="", load={}, states=[state], results=results)
