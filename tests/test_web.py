"""web/server.py: endpoints, run_status computation, the startup gate, and the screenshot route's
path safety - all via fastapi.testclient.TestClient. No browser, no real API call anywhere: every
run in this file is driven by a monkeypatched run_test / LLMClient, not the real background thread
against a real page. The real end-to-end behaviour (an actual run, an actual browser, an actual
served page) is covered in tests/test_web_ui.py against the replay recording and a fake fixture.
"""
import json
import threading

import pytest
from fastapi.testclient import TestClient

import web.server as server
from probe.agent import RunResult
from probe.llm import LLMError


class StubLLMClient:
    """_run_in_background always constructs an LLMClient before calling run_test(), even though
    these tests replace run_test() itself and do not care what LLM client it receives. The real
    LLMClient(mode="fake") needs a source (a script) or it raises - irrelevant noise for tests
    that are about run_status, not about LLMClient - so this stands in for it."""
    def __init__(self, *args, **kwargs):
        pass


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Every test gets its own RunState and its own runs/ directory, and mode=fake so the
    lifespan's startup gate always passes without needing a key or --yes-spend equivalent."""
    monkeypatch.setattr(server, "STATE", server.RunState(lock=threading.Lock()))
    monkeypatch.setattr(server, "MODE", "fake")
    monkeypatch.setattr(server, "REPLAY_URL", None)
    monkeypatch.setattr(server, "YES_SPEND", False)
    monkeypatch.setattr(server, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(server, "LLMClient", StubLLMClient)


def result_of(missions, timed_out=False, findings=None, counts=None) -> RunResult:
    counts = counts or {"Confirmed": 0, "Likely": 0, "Improvement": 0, "Dropped": 0}
    report = {"url": "http://x/", "profile": "live", "verdict": "no confirmed issues", "counts": counts,
             "elapsed_s": 1.0, "timed_out": timed_out, "llm_calls": 0, "input_tokens": 0,
             "output_tokens": 0, "estimated_cost_usd": 0.0}
    return RunResult(findings=findings or [], report=report, missions=missions, timed_out=timed_out)


def fake_run_test(events, result=None, error=None):
    """Builds a stand-in for probe.agent.run_test: plays `events` through on_event, then returns
    `result` or raises `error`. Mirrors the real function's contract closely enough for the
    server's own logic (which only depends on on_event and the return value/exception)."""
    def run_test(url, profile, llm, out_dir, on_event=None, reset_path=None, browser=None):
        for event in events:
            if on_event:
                on_event(event)
        if error:
            raise error
        return result
    return run_test


def start_and_drain(client: TestClient) -> tuple[dict, list[dict]]:
    """POST /api/run, then read every SSE event to completion. Returns (post response json, events)."""
    posted = client.post("/api/run", json={"url": "http://example.test/"})
    assert posted.status_code == 200
    run_id = posted.json()["run_id"]
    with client.stream("GET", f"/api/stream?run_id={run_id}") as resp:
        lines = [l for l in resp.iter_lines() if l.startswith("data: ")]
    events = [json.loads(l[len("data: "):]) for l in lines]
    return posted.json(), events


# ---- validate_startup() and the lifespan gate --------------------------------------------------

def test_validate_startup_blocks_real_without_yes_spend():
    assert server.validate_startup("real", False, None) is not None
    assert server.validate_startup("record", False, None) is not None


def test_validate_startup_allows_real_with_yes_spend():
    assert server.validate_startup("real", True, None) is None


def test_validate_startup_requires_replay_url_in_replay_mode():
    msg = server.validate_startup("replay", True, None)
    assert msg is not None and "PROBE_REPLAY_URL" in msg
    assert server.validate_startup("replay", True, "http://x/") is None


def test_validate_startup_rejects_an_unset_or_unknown_mode():
    assert server.validate_startup(None, True, None) is not None
    assert server.validate_startup("banana", True, None) is not None


def test_the_app_refuses_to_start_when_the_gate_fails(monkeypatch):
    monkeypatch.setattr(server, "MODE", "real")
    monkeypatch.setattr(server, "YES_SPEND", False)
    with pytest.raises(RuntimeError, match="spends real API money"):
        with TestClient(server.app):
            pass  # the lifespan startup runs on entering the context manager


def test_the_app_starts_fine_in_fake_mode():
    with TestClient(server.app) as client:
        assert client.get("/api/status").json()["mode"] == "fake"


# ---- /api/run and /api/stream: run_status computation -------------------------------------------

def test_a_completed_run_produces_run_status_completed(monkeypatch):
    events = [{"type": "run_started", "t": 0.0, "url": "http://x/", "profile": "live"},
             {"type": "run_finished", "t": 1.0, "report": {"counts": {}, "timed_out": False}}]
    result = result_of(missions=[{"id": "m1", "goal": "g", "status": "done", "steps": 1}])
    monkeypatch.setattr(server, "run_test", fake_run_test(events, result=result))
    with TestClient(server.app) as client:
        _, seen = start_and_drain(client)
    assert seen[-1]["type"] == "run_finished"
    assert seen[-1]["run_status"] == "completed"


def test_a_timed_out_run_produces_run_status_partial(monkeypatch):
    events = [{"type": "run_finished", "t": 1.0, "report": {"counts": {}, "timed_out": True}}]
    result = result_of(missions=[{"id": "m1", "goal": "g", "status": "done", "steps": 1}], timed_out=True)
    monkeypatch.setattr(server, "run_test", fake_run_test(events, result=result))
    with TestClient(server.app) as client:
        _, seen = start_and_drain(client)
    assert seen[-1]["run_status"] == "partial"


def test_a_stuck_mission_produces_run_status_partial_even_without_a_timeout(monkeypatch):
    events = [{"type": "run_finished", "t": 1.0, "report": {"counts": {}, "timed_out": False}}]
    result = result_of(missions=[{"id": "m1", "goal": "g", "status": "stuck", "steps": 0}], timed_out=False)
    monkeypatch.setattr(server, "run_test", fake_run_test(events, result=result))
    with TestClient(server.app) as client:
        _, seen = start_and_drain(client)
    assert seen[-1]["run_status"] == "partial"


def test_a_raised_exception_produces_one_error_event_with_run_status_failed(monkeypatch):
    events = [{"type": "run_started", "t": 0.0, "url": "http://x/", "profile": "live"}]
    error = LLMError("estimated cost is over PROBE_MAX_COST_USD")
    monkeypatch.setattr(server, "run_test", fake_run_test(events, error=error))
    with TestClient(server.app) as client:
        _, seen = start_and_drain(client)
    assert seen[-1]["type"] == "error"
    assert seen[-1]["run_status"] == "failed"
    assert "PROBE_MAX_COST_USD" in seen[-1]["message"]
    assert seen[-1]["kind"] == "LLMError"


def test_a_multiline_exception_only_shows_its_first_line_to_the_page(monkeypatch):
    """Playwright's own errors are multi-line: the real reason, then a blank line, then a verbose
    'Call log:' retry trace - found by actually watching a failed run (T6-c). The page is not the
    place for that trace; only the first line (the actual reason) should reach it."""
    events = [{"type": "run_started", "t": 0.0, "url": "http://x/", "profile": "live"}]
    error = RuntimeError("Page.goto: net::ERR_CONNECTION_REFUSED at http://x/\nCall log:\n - navigating to \"http://x/\"")
    monkeypatch.setattr(server, "run_test", fake_run_test(events, error=error))
    with TestClient(server.app) as client:
        _, seen = start_and_drain(client)
    assert seen[-1]["message"] == "Page.goto: net::ERR_CONNECTION_REFUSED at http://x/"
    assert "Call log" not in seen[-1]["message"]


# ---- T12: the live UI is a spend-capable entry point and must share the cross-run ledger -------
# It was the only one not wired through default_ledger_path(): the three CLIs were done in T7-0c,
# and with PROBE_SPEND_LEDGER empty in .env that left the cumulative cap OFF for the one path that
# actually runs in front of people.

def ledger_path_the_web_path_would_use(monkeypatch, mode):
    """Drive _run_in_background far enough to capture what it hands LLMClient, without running a
    real agent: run_test is stubbed out, so only the client construction matters."""
    seen = {}

    def capture(out_dir, mode=None, ledger_path=None, **kwargs):
        seen["ledger_path"] = ledger_path
        return StubLLMClient()
    monkeypatch.setattr(server, "MODE", mode)
    # satisfy the startup gate for the mode under test - it is not what is being checked here, and
    # it is correct to refuse real/record without these (see the validate_startup tests above)
    monkeypatch.setattr(server, "YES_SPEND", mode in ("real", "record"))
    monkeypatch.setattr(server, "REPLAY_URL", "http://x/" if mode == "replay" else None)
    monkeypatch.setattr(server, "LLMClient", capture)
    monkeypatch.setattr(server, "run_test", fake_run_test(
        [], result=result_of(missions=[{"id": "m1", "goal": "g", "status": "done", "steps": 0}])))
    with TestClient(server.app) as client:
        start_and_drain(client)
    return seen["ledger_path"]


@pytest.mark.parametrize("mode", ["real", "record"])
def test_the_web_path_gets_the_default_ledger_when_it_can_spend(mode, monkeypatch):
    monkeypatch.delenv("PROBE_SPEND_LEDGER", raising=False)
    assert ledger_path_the_web_path_would_use(monkeypatch, mode) == "runs/spend_ledger.jsonl"


@pytest.mark.parametrize("mode", ["fake", "replay"])
def test_the_web_path_gets_no_ledger_when_it_cannot_spend(mode, monkeypatch):
    monkeypatch.delenv("PROBE_SPEND_LEDGER", raising=False)
    assert ledger_path_the_web_path_would_use(monkeypatch, mode) is None


def test_an_explicit_spend_ledger_env_value_still_wins_on_the_web_path(monkeypatch):
    monkeypatch.setenv("PROBE_SPEND_LEDGER", "custom/ledger.jsonl")
    assert ledger_path_the_web_path_would_use(monkeypatch, "record") == "custom/ledger.jsonl"


def test_every_spend_capable_entry_point_uses_the_same_ledger_convention():
    """The invariant that was actually violated: three entry points wired it, one did not, and the
    one that did not is the demo. Reads the source so a new entry point cannot quietly skip it."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for name in ("probe/agent.py", "probe/evaluate.py", "probe/check_disprove.py", "web/server.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "ledger_path=" in source, f"{name} constructs an LLMClient without wiring a ledger"
        assert "default_ledger_path" in source, f"{name} does not use the shared ledger convention"


def test_status_remembers_the_last_run_after_it_finishes(monkeypatch):
    events = [{"type": "run_finished", "t": 1.0, "report": {"counts": {}, "timed_out": False}}]
    finding = {"id": "C1", "tier": "Confirmed", "title": "Delete fails"}
    result = result_of(missions=[{"id": "m1", "goal": "g", "status": "done", "steps": 1}], findings=[finding])
    monkeypatch.setattr(server, "run_test", fake_run_test(events, result=result))
    with TestClient(server.app) as client:
        start_and_drain(client)
        status = client.get("/api/status").json()
    assert status["running"] is False
    assert status["run_id"] is None  # only exposed while a run is in progress
    assert status["last_run_status"] == "completed"
    assert status["last_report"] is not None
    assert status["last_findings"] == [finding]  # so a page reload can rebuild the finding cards


# ---- concurrency: one run at a time -------------------------------------------------------------

def test_a_second_run_is_refused_with_409_while_one_is_in_progress(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_run_test(url, profile, llm, out_dir, on_event=None, reset_path=None, browser=None):
        started.set()
        release.wait(timeout=5)
        return result_of(missions=[{"id": "m1", "goal": "g", "status": "done", "steps": 0}])
    monkeypatch.setattr(server, "run_test", slow_run_test)

    with TestClient(server.app) as client:
        first = client.post("/api/run", json={"url": "http://x/"})
        assert first.status_code == 200
        started.wait(timeout=5)
        second = client.post("/api/run", json={"url": "http://x/"})
        assert second.status_code == 409
        release.set()
        # drain so the background thread finishes before the test (and the fixture) tears down
        run_id = first.json()["run_id"]
        with client.stream("GET", f"/api/stream?run_id={run_id}") as resp:
            list(resp.iter_lines())


def test_stream_404s_for_a_run_id_that_is_not_the_current_run():
    with TestClient(server.app) as client:
        resp = client.get("/api/stream?run_id=not-a-real-run")
    assert resp.status_code == 404


# ---- screenshot route: filename and mission-id safety --------------------------------------------

def test_shot_serves_a_real_screenshot():
    with TestClient(server.app) as client:
        run_id = "abcd1234"
        mission_dir = server.RUNS_DIR / f"web_{run_id}" / "m1"
        mission_dir.mkdir(parents=True)
        (mission_dir / "step_01_before.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
        resp = client.get(f"/api/runs/{run_id}/shots/m1/step_01_before.png")
    assert resp.status_code == 200
    assert resp.content.startswith(b"\x89PNG")


@pytest.mark.parametrize("mission_id,filename", [
    ("m1", "../../../secrets.txt"),
    ("m1", "step_01_before.png/../../secret"),
    ("../secret", "step_01_before.png"),
    ("m1", "not_a_screenshot.png"),
    ("m1", "step_1_before.png"),  # missing the zero-padding the real filenames always have
    ("mission-one", "step_01_before.png"),
])
def test_shot_refuses_anything_that_is_not_exactly_a_screenshot_path(mission_id, filename):
    with TestClient(server.app) as client:
        run_id = "abcd1234"
        mission_dir = server.RUNS_DIR / f"web_{run_id}" / "m1"
        mission_dir.mkdir(parents=True)
        (mission_dir / "step_01_before.png").write_bytes(b"fake")
        resp = client.get(f"/api/runs/{run_id}/shots/{mission_id}/{filename}")
    assert resp.status_code == 404


def test_shot_404s_for_a_file_that_does_not_exist():
    with TestClient(server.app) as client:
        resp = client.get("/api/runs/no-such-run/shots/m1/step_01_before.png")
    assert resp.status_code == 404


def test_shot_pattern_check_rejects_a_wrongly_named_file_that_actually_exists():
    """Isolates the regex's own contribution: a file that DOES exist on disk, just not under a
    name the pattern allows, must still be refused - otherwise the earlier not-found-style test
    cases could all be passing only because the file happens not to exist, not because the name
    was checked (confirmed by a mutation check: weakening SHOT_NAME to match anything left every
    other test in this file green, since they all used names for files that were never created)."""
    with TestClient(server.app) as client:
        run_id = "abcd1234"
        mission_dir = server.RUNS_DIR / f"web_{run_id}" / "m1"
        mission_dir.mkdir(parents=True)
        (mission_dir / "not_a_screenshot.png").write_bytes(b"fake")
        resp = client.get(f"/api/runs/{run_id}/shots/m1/not_a_screenshot.png")
    assert resp.status_code == 404


# ---- run_status pure function, unit-level ---------------------------------------------------

def test_compute_run_status_directly():
    done = result_of(missions=[{"id": "m1", "goal": "g", "status": "done", "steps": 3}])
    assert server.compute_run_status(done) == "completed"

    timed_out = result_of(missions=[{"id": "m1", "goal": "g", "status": "done", "steps": 1}], timed_out=True)
    assert server.compute_run_status(timed_out) == "partial"

    stuck = result_of(missions=[{"id": "m1", "goal": "g", "status": "stuck", "steps": 0}])
    assert server.compute_run_status(stuck) == "partial"

    no_missions = result_of(missions=[], timed_out=True)
    assert server.compute_run_status(no_missions) == "partial"


# ---- D15/T13-b: compute_partial_reason() - the precise status sentence's gate -----------------

def _mission(status="done", stuck_reason=None, failures=0, mid="m1"):
    return {"id": mid, "goal": "g", "status": status, "steps": 2,
            "stuck_reason": stuck_reason, "server_failure_signals": failures}


def test_partial_reason_is_set_only_for_model_declared_stops_with_repeated_server_failures():
    """Rehearsal 1's shape: m3 ended by the model's own "stuck" after two recorded server failures,
    the other missions finished. run_status stays "partial" (never softened); only the reason is set."""
    rehearsal_1 = result_of([_mission("done", mid="m1"), _mission("done", mid="m2"),
                             _mission("stuck", "model_declared", failures=2, mid="m3")])
    assert server.compute_run_status(rehearsal_1) == "partial"
    assert server.compute_partial_reason(rehearsal_1) == server.MODEL_STOPPED_AFTER_SERVER_FAILURES


def test_partial_reason_is_none_for_a_completed_run():
    assert server.compute_partial_reason(result_of([_mission("done", failures=5)])) is None


def test_partial_reason_needs_at_least_two_server_failures():
    """One failure is not "repeated", and zero means the sentence would name something the browser
    never recorded - the model saying "stuck" alone does not tell us why it stopped."""
    for failures in (0, 1):
        r = result_of([_mission("stuck", "model_declared", failures=failures)])
        assert server.compute_run_status(r) == "partial"
        assert server.compute_partial_reason(r) is None, failures
    r = result_of([_mission("stuck", "model_declared", failures=server.MIN_SERVER_FAILURES)])
    assert server.compute_partial_reason(r) == server.MODEL_STOPPED_AFTER_SERVER_FAILURES


def test_partial_reason_is_none_when_any_real_failure_is_mixed_in():
    """A validation failure, a timeout or a budget stop anywhere in the run keeps today's generic
    wording - the presence of any of those must not be softened by the specific sentence."""
    declared = _mission("stuck", "model_declared", failures=3, mid="m1")
    for other in (_mission("stuck", "validation_failed", failures=3, mid="m2"),
                  _mission("budget_exceeded", None, failures=3, mid="m2"),
                  _mission("stuck", None, failures=3, mid="m2")):  # a stuck that carries no reason
        r = result_of([declared, other])
        assert server.compute_run_status(r) == "partial"
        assert server.compute_partial_reason(r) is None, other
    timed_out = result_of([declared], timed_out=True)
    assert server.compute_run_status(timed_out) == "partial"
    assert server.compute_partial_reason(timed_out) is None


def test_partial_reason_tolerates_a_mission_summary_without_the_new_keys():
    """Older report/summary shapes have no server_failure_signals: that must read as 0, not crash."""
    old = {"id": "m1", "goal": "g", "status": "stuck", "steps": 1, "stuck_reason": "model_declared"}
    assert server.compute_partial_reason(result_of([old])) is None


def _finish_with(monkeypatch, result):
    events = [{"type": "run_started", "t": 0.0, "url": "http://x/", "profile": "live"},
              {"type": "run_finished", "t": 1.0, "report": result.report}]
    monkeypatch.setattr(server, "run_test", fake_run_test(events, result=result))


def test_partial_reason_reaches_the_run_finished_event_and_api_status(monkeypatch):
    result = result_of([_mission("done", mid="m1"), _mission("stuck", "model_declared", failures=2, mid="m2")])
    _finish_with(monkeypatch, result)
    with TestClient(server.app) as client:
        _posted, events = start_and_drain(client)
        finished = events[-1]
        assert finished["run_status"] == "partial"  # unchanged by T13
        assert finished["partial_reason"] == server.MODEL_STOPPED_AFTER_SERVER_FAILURES
        status = client.get("/api/status").json()
        assert status["last_run_status"] == "partial"
        assert status["last_partial_reason"] == server.MODEL_STOPPED_AFTER_SERVER_FAILURES


def test_partial_reason_is_null_on_the_wire_for_a_generic_partial_and_a_completed_run(monkeypatch):
    with TestClient(server.app) as client:
        _finish_with(monkeypatch, result_of([_mission("stuck", "validation_failed", failures=4)]))
        _posted, events = start_and_drain(client)
        assert events[-1]["run_status"] == "partial" and events[-1]["partial_reason"] is None
        assert client.get("/api/status").json()["last_partial_reason"] is None

        _finish_with(monkeypatch, result_of([_mission("done", failures=4)]))
        _posted, events = start_and_drain(client)
        assert events[-1]["run_status"] == "completed" and events[-1]["partial_reason"] is None


def test_partial_reason_is_cleared_when_the_next_run_starts_and_when_a_run_fails(monkeypatch):
    with TestClient(server.app) as client:
        _finish_with(monkeypatch, result_of([_mission("stuck", "model_declared", failures=2)]))
        start_and_drain(client)
        assert client.get("/api/status").json()["last_partial_reason"] is not None

        monkeypatch.setattr(server, "run_test", fake_run_test([], error=RuntimeError("boom")))
        _posted, events = start_and_drain(client)
        assert events[-1]["run_status"] == "failed"
        assert client.get("/api/status").json()["last_partial_reason"] is None
