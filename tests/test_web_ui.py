"""End-to-end UI tests for web/server.py + web/static/index.html: a real subprocess server,
driven by an actual Playwright browser loading the served page and clicking through it - not
just the JSON a TestClient would see (tests/test_web.py already covers that). Every scenario here
is replay or fake mode, so none of it costs API money (D10's T6-b acceptance, plus the amendment's
positive-fixture and run_status-prominence requirements).
"""
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import free_port, fresh_app, http
from probe.state import capture_state

ROOT = Path(__file__).resolve().parent.parent
RECORDING = ROOT / "runs" / "first_real_2026-09-22" / "llm_record.jsonl"


@contextmanager
def start_web(env_overrides: dict, wait_path: str = "/api/status"):
    """Starts web.server:app as a real subprocess on a free port (mirrors conftest.py's `server`
    fixture for demo_app). Yields the base URL; always terminates the process afterward."""
    port = free_port()
    env = dict(os.environ)
    env.pop("PROBE_LLM_SOURCE", None)  # this machine's own .env must never leak into these tests
    env.pop("PROBE_REPLAY_URL", None)
    env.update(env_overrides)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "web.server:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 15
        while True:
            assert proc.poll() is None, "uvicorn (web) exited during start-up - check env_overrides"
            try:
                http("GET", base + wait_path)
                break
            except OSError:
                assert time.time() < deadline, "web server did not start in 15 s"
                time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def ref_for(browser, url: str, role: str, name: str, nth: int = 0) -> str:
    """One read-only capture_state() call to discover the ref an agent-style fake script needs -
    refs are only known once the page is actually loaded (D7), same technique test_agent.py uses."""
    context = browser.new_context()
    try:
        page = context.new_page()
        page.goto(url)
        state = capture_state(page)
        entry = next(e for e in state.refs if e.role == role and e.name == name and e.nth == nth)
        return entry.ref
    finally:
        context.close()


def decision(action="click", ref="e1", text=None, expect="something changes") -> dict:
    return {"action": action, "ref": ref, "text": text, "expect": expect, "reasoning": "because"}


# ---- 1. Replay the real 2026-09-22 recording: the "found nothing, completed" path -------------

def test_replay_run_shows_no_confirmed_issues(server, browser):
    if not RECORDING.exists():
        pytest.skip("the 2026-09-22 real recording is machine-local (runs/ is git-ignored)")
    fresh_app(server)
    with start_web({"PROBE_LLM_MODE": "replay", "PROBE_LLM_SOURCE": str(RECORDING),
                    "PROBE_REPLAY_URL": server}) as base:
        context = browser.new_context()
        page = context.new_page()
        page.goto(base)

        page.wait_for_selector("#banner:not([hidden])")
        assert "REPLAY" in page.locator("#banner").inner_text()
        assert page.locator("#url").is_disabled()
        assert page.locator("#url").input_value() == server

        page.locator("#runBtn").click()
        page.wait_for_selector("#report:not([hidden])", timeout=30000)

        assert "No confirmed issues" in page.locator(".verdict").inner_text()
        assert page.locator("#statusLine").is_hidden()  # run_status completed: no status line at all
        assert page.locator(".finding").count() == 0    # the one signal found is Dropped, not a card
        dropped = page.locator("#droppedBox")
        assert dropped.is_visible()
        assert "1 item" in dropped.locator("#droppedSummary").inner_text()
        context.close()


# ---- 2. A deterministic Confirmed + Improvement + Dropped fixture, fake mode -------------------

def test_tier_mix_fixture_renders_confirmed_improvement_and_dropped(server, browser):
    fresh_app(server)
    field = ref_for(browser, server, "textbox", "New task")
    delete_btn = ref_for(browser, server, "button", "Delete Buy milk")
    add_btn = ref_for(browser, server, "button", "Add")

    plan = {
        "app_type": "task list", "capabilities": ["add", "tick", "delete"],
        "missions": [
            {"id": "m1", "goal": "Delete an existing task", "category": "core_flow",
             "priority": "critical", "why": "Users could not manage their list."},
            {"id": "m2", "goal": "Reject a duplicate task", "category": "input_validation",
             "priority": "medium", "why": "Duplicate entries would confuse users."},
        ],
    }
    script = {
        "plan": [plan],
        "step": [
            # mission 1: click the New task field (no_effect -> judge says fine -> Dropped),
            # then delete Buy milk (http_5xx -> Confirmed, no disprove needed).
            decision("click", field, expect="the field is focused, nothing else changes"),
            decision("click", delete_btn, expect="the task disappears"),
            decision("done", None, expect=""),
            # mission 2: add a title that duplicates the seeded "Buy milk" task.
            decision("type", field, text="Buy milk", expect="the box holds the text I typed"),
            decision("click", add_btn, expect="a duplicate is rejected with a visible message"),
            decision("done", None, expect=""),
        ],
        "judge": [
            {"step_verdicts": [{"step": 1, "violated": False, "reason": "focusing a field changes nothing by design"},
                              {"step": 2, "violated": True, "reason": "the task is still there"}],
             "findings": [{"step": 2, "kind": "bug", "title": "Delete fails", "severity": "high",
                          "impact": "Users cannot remove tasks.", "expected": "the task disappears",
                          "observed": "the task remains", "suggestion": "check the DELETE handler"}]},
            {"step_verdicts": [],
             "findings": [{"step": 2, "kind": "improvement", "title": "Show a message for duplicate titles",
                          "severity": "low", "impact": "Users get no feedback.",
                          "expected": "a visible error", "observed": "nothing happened",
                          "suggestion": "show the server's error message"}]},
        ],
    }
    script_path = ROOT / "runs" / "_web_ui_fixture_script.json"
    script_path.parent.mkdir(exist_ok=True)
    script_path.write_text(json.dumps(script), encoding="utf-8")

    with start_web({"PROBE_LLM_MODE": "fake", "PROBE_LLM_SOURCE": str(script_path)}) as base:
        context = browser.new_context()
        page = context.new_page()
        page.goto(base)
        page.wait_for_selector("#banner:not([hidden])")
        assert "LIVE" in page.locator("#banner").inner_text()

        page.locator("#url").fill(server)
        page.locator("#runBtn").click()
        page.wait_for_selector("#report:not([hidden])", timeout=30000)

        assert page.locator("#statusLine").is_hidden()  # completed
        assert "Review before release" in page.locator(".verdict").inner_text()

        cards = page.locator(".finding")
        assert cards.count() == 2
        titles = cards.locator(".title").all_inner_texts()
        assert "Delete fails" in titles
        assert "Show a message for duplicate titles" in titles

        # .badge is styled text-transform: uppercase, which Playwright's inner_text() (like the
        # browser's own innerText) reflects - compare case-insensitively, the rendering is by design.
        confirmed_card = page.locator(".finding", has_text="Delete fails")
        assert "confirmed" == confirmed_card.locator(".badge").inner_text().strip().lower()
        improvement_card = page.locator(".finding", has_text="Show a message")
        assert "improvement" == improvement_card.locator(".badge").inner_text().strip().lower()

        # both screenshots for the Confirmed finding actually resolve (not 404)
        shot_srcs = confirmed_card.locator("img").evaluate_all("els => els.map(e => e.getAttribute('src'))")
        assert len(shot_srcs) == 2
        for src in shot_srcs:
            resp = page.request.get(base + src)
            assert resp.status == 200, src

        dropped = page.locator("#droppedBox")
        assert dropped.is_visible()
        assert "1 item" in dropped.locator("#droppedSummary").inner_text()
        context.close()

    script_path.unlink(missing_ok=True)


# ---- 3. run_status rendering: at least as prominent as the verdict, and drawn above it ---------

def test_partial_status_line_is_at_least_as_prominent_as_the_verdict_and_appears_above_it(server, browser):
    with start_web({"PROBE_LLM_MODE": "fake"}) as base:
        context = browser.new_context()
        page = context.new_page()
        page.goto(base)
        page.wait_for_selector("#banner:not([hidden])")

        page.evaluate("""() => {
            renderStatusLine('partial', null);
            renderReport({counts: {Confirmed: 0, Likely: 0, Improvement: 0, Dropped: 0},
                         elapsed_s: 12.3, llm_calls: 3, estimated_cost_usd: 0.01});
        }""")

        status_el = page.locator("#statusLine")
        assert status_el.is_visible()
        assert "did not finish" in status_el.inner_text()
        assert "No confirmed issues" in page.locator(".verdict").inner_text()

        status_size = page.eval_on_selector("#statusLine", "el => parseFloat(getComputedStyle(el).fontSize)")
        verdict_size = page.eval_on_selector(".verdict", "el => parseFloat(getComputedStyle(el).fontSize)")
        assert status_size >= verdict_size, "the run_status line must be at least as prominent as the verdict"

        above = page.evaluate("""() => {
            const rel = document.querySelector('#statusLine').compareDocumentPosition(document.querySelector('#report'));
            return !!(rel & Node.DOCUMENT_POSITION_FOLLOWING);
        }""")
        assert above, "the status line must come before the Release Check card in the document"
        context.close()


def test_failed_status_shows_no_report_card(browser):
    with start_web({"PROBE_LLM_MODE": "fake"}) as base:
        context = browser.new_context()
        page = context.new_page()
        page.goto(base)
        page.wait_for_selector("#banner:not([hidden])")
        page.evaluate("() => { renderStatusLine('failed', 'the app could not be reached'); }")
        status_el = page.locator("#statusLine")
        assert "could not finish" in status_el.inner_text()
        assert "the app could not be reached" in status_el.inner_text()
        assert page.locator("#report").is_hidden()
        context.close()


# ---- 4. D15/T13-b: the precise partial wording, same banner, same prominence rule ---------------

SPECIFIC_WORDING = ("One check stopped after repeated server failures. "
                    "The findings below are still valid, but the full sweep did not complete.")
GENERIC_WORDING = ("This scan did not finish. The findings below are real, but absence of a Confirmed "
                   "issue does not mean the app passed — the sweep was cut short.")
MODEL_STOPPED = "model_stopped_after_server_failures"


def _visible_text(page):
    """The status line as a reader sees it, with the ⚠ glyph and whitespace differences stripped."""
    return " ".join(page.locator("#statusLine").inner_text().replace("⚠", "").split())


def test_specific_partial_wording_is_exactly_the_reviewed_sentence_and_is_neutral(server, browser):
    """The sentence states what happened and that the sweep did not complete. It must not credit the
    model's stop as right (D15: the audit found the stop to be a vocabulary miscall, so "correctly
    stopped" would re-do in prose what the decision refuses to do in code) and must not read as a pass."""
    with start_web({"PROBE_LLM_MODE": "fake"}) as base:
        context = browser.new_context()
        page = context.new_page()
        page.goto(base)
        page.wait_for_selector("#banner:not([hidden])")
        page.evaluate(f"() => renderStatusLine('partial', null, '{MODEL_STOPPED}')")
        text = _visible_text(page)
        assert text == SPECIFIC_WORDING
        lowered = text.lower()
        for word in ("correct", "appropriate", "efficient", "right call", "justified", "sensible",
                     "passed", "success"):
            assert word not in lowered, word
        assert "did not complete" in lowered  # states that the sweep did not finish
        context.close()


def test_any_other_partial_reason_keeps_the_generic_wording_exactly(server, browser):
    with start_web({"PROBE_LLM_MODE": "fake"}) as base:
        context = browser.new_context()
        page = context.new_page()
        page.goto(base)
        page.wait_for_selector("#banner:not([hidden])")
        for reason in ("null", "undefined", "'something_else'", "''"):
            page.evaluate(f"() => renderStatusLine('partial', null, {reason})")
            assert _visible_text(page) == GENERIC_WORDING, reason
        # the reason only refines "partial": it can never turn a failed run or a completed one into anything
        page.evaluate(f"() => renderStatusLine('failed', 'boom', '{MODEL_STOPPED}')")
        assert "could not finish" in page.locator("#statusLine").inner_text()
        page.evaluate(f"() => renderStatusLine('completed', null, '{MODEL_STOPPED}')")
        assert page.locator("#statusLine").is_hidden()
        context.close()


def test_specific_partial_status_line_keeps_the_prominence_rule(server, browser):
    """The D10-amendment prominence test above, unmodified in its own right, only ever exercised the
    generic sentence. Same assertions, run against the specific one: this decision must not weaken it."""
    with start_web({"PROBE_LLM_MODE": "fake"}) as base:
        context = browser.new_context()
        page = context.new_page()
        page.goto(base)
        page.wait_for_selector("#banner:not([hidden])")
        page.evaluate(f"""() => {{
            renderStatusLine('partial', null, '{MODEL_STOPPED}');
            renderReport({{counts: {{Confirmed: 0, Likely: 0, Improvement: 0, Dropped: 0}},
                         elapsed_s: 12.3, llm_calls: 3, estimated_cost_usd: 0.01}});
        }}""")
        assert page.locator("#statusLine").is_visible()
        assert "No confirmed issues" in page.locator(".verdict").inner_text()
        status_size = page.eval_on_selector("#statusLine", "el => parseFloat(getComputedStyle(el).fontSize)")
        verdict_size = page.eval_on_selector(".verdict", "el => parseFloat(getComputedStyle(el).fontSize)")
        assert status_size >= verdict_size
        above = page.evaluate("""() => {
            const rel = document.querySelector('#statusLine').compareDocumentPosition(document.querySelector('#report'));
            return !!(rel & Node.DOCUMENT_POSITION_FOLLOWING);
        }""")
        assert above
        # the same class as the generic partial line, so colour and size come from the same CSS rule
        assert page.eval_on_selector("#statusLine", "el => el.className") == "status-line partial"
        context.close()


def _scripted_run(server, browser, steps, judge=None):
    """Drives the real page through a fake-mode run whose only mission ends the way `steps` say.
    Zero API cost. Returns what a viewer sees and what the API says: the status line (text and
    whether it is visible), the on-screen log, the finding cards' titles in DOM order, and
    /api/status. `judge` is the one judge answer (default: nothing to report)."""
    fresh_app(server)
    refs = {"click": ref_for(browser, server, "button", "Delete Buy milk"),
            # a DIFFERENT element: D16's repeat guard ends a mission when the same click fails the same
            # way twice, so a script that wants two server failures without the guard needs two buttons
            "click_other": ref_for(browser, server, "button", "Delete Write report")}
    plan = {"app_type": "task list", "capabilities": ["delete"],
            "missions": [{"id": "m1", "goal": "Delete an existing task", "category": "core_flow",
                          "priority": "critical", "why": "Users could not manage their list."}]}
    script = {"plan": [plan],
              "step": [decision("click" if a == "click_other" else a, refs.get(a), expect="the task disappears")
                       for a in steps],
              "judge": [judge or {"step_verdicts": [], "findings": []}]}
    script_path = ROOT / "runs" / "_web_ui_partial_script.json"
    script_path.parent.mkdir(exist_ok=True)
    script_path.write_text(json.dumps(script), encoding="utf-8")
    try:
        with start_web({"PROBE_LLM_MODE": "fake", "PROBE_LLM_SOURCE": str(script_path)}) as base:
            context = browser.new_context()
            page = context.new_page()
            page.goto(base)
            page.wait_for_selector("#banner:not([hidden])")
            page.locator("#url").fill(server)
            page.locator("#runBtn").click()
            page.wait_for_selector("#report:not([hidden])", timeout=30000)
            seen = {"text": _visible_text(page), "status_visible": page.locator("#statusLine").is_visible(),
                    "log": page.locator("#log").inner_text(),
                    "titles": page.locator(".finding .title").all_inner_texts(),
                    "status": json.loads(http("GET", base + "/api/status")[1])}
            context.close()
            return seen
    finally:
        script_path.unlink(missing_ok=True)


def _scripted_partial_run(server, browser, steps):
    """(visible status-line text, /api/status json) - the shape the T13 tests below were written for."""
    seen = _scripted_run(server, browser, steps)
    return seen["text"], seen["status"]


def test_rehearsal_1_shape_end_to_end_gets_the_specific_sentence_and_stays_partial(server, browser):
    """Zero-cost replay of rehearsal 1's shape through the real agent, server and page: the model
    itself says "stuck" after two real 500s from the seeded delete bug (on two different Delete
    buttons since D16: the same button twice is now ended by the repeat guard, see T14-b)."""
    text, status = _scripted_partial_run(server, browser, ["click", "click_other", "stuck"])
    assert text == SPECIFIC_WORDING
    assert status["last_run_status"] == "partial"           # not reclassified, not softened
    assert status["last_partial_reason"] == MODEL_STOPPED


def test_model_declared_stuck_without_server_failures_keeps_the_generic_sentence(server, browser):
    """"stuck" straight away, no request ever failed: the browser has no server failure to name, so
    the page must not name one."""
    text, status = _scripted_partial_run(server, browser, ["stuck"])
    assert text == GENERIC_WORDING
    assert status["last_run_status"] == "partial"
    assert status["last_partial_reason"] is None


# ---- 5. D16/T14-b: the repeat guard's log line, and tier-sorted finding cards ---------------------

GUARD_JUDGE = {"step_verdicts": [{"step": 1, "violated": True, "reason": "the task is still there"}],
               "findings": [{"step": 1, "kind": "bug", "title": "Delete fails", "severity": "high",
                             "impact": "Users cannot remove tasks.", "expected": "the task disappears",
                             "observed": "the task remains", "suggestion": "check the DELETE handler"}]}


def test_rehearsal_1_shape_with_the_same_button_now_completes_with_one_plain_log_line(server, browser):
    """The exact shape that ended `partial` in all three real rehearsals, minus the model's extra
    retries: the same Delete clicked twice, two identical 500s. D16's guard ends the mission as done
    from the browser's own evidence, so the run is `completed` (no amber line at all), the finding is
    still there, and one neutral log line says why the mission stopped early. The script holds no
    `stuck` entry: a third step call would fail on the exhausted fake script."""
    seen = _scripted_run(server, browser, ["click", "click"], judge=GUARD_JUDGE)
    assert seen["status"]["last_run_status"] == "completed"
    assert seen["status_visible"] is False
    assert seen["status"]["last_partial_reason"] is None
    assert ("Finished: m1 (1 found) - stopped after the same action failed the same way twice "
            "(steps 1 and 2)") in seen["log"]
    assert seen["titles"] == ["Delete fails"]                     # still reported, still Confirmed below
    assert [f["tier"] for f in seen["status"]["last_findings"]] == ["Confirmed"]


def test_a_mission_that_ends_normally_gets_no_guard_text_in_the_log(server, browser):
    seen = _scripted_run(server, browser, ["click", "done"])
    assert "Finished: m1" in seen["log"]
    assert "stopped after" not in seen["log"]


def test_finding_cards_are_sorted_by_tier_live_and_after_a_reload_without_touching_the_data(server, browser):
    """m1 yields a Likely and an Improvement, m2 (the later mission) yields the Confirmed delete. They
    ARRIVE in that order - the same shape as rehearsals 2 and 3, where the one proven finding sat at
    the bottom. The page must show Confirmed, Likely, Improvement, both as the run streams in and
    after a reload, while the data the API returns keeps its arrival order."""
    fresh_app(server)
    field = ref_for(browser, server, "textbox", "New task")
    add_btn = ref_for(browser, server, "button", "Add")
    delete_btn = ref_for(browser, server, "button", "Delete Buy milk")
    plan = {"app_type": "task list", "capabilities": ["add", "delete"],
            "missions": [{"id": "m1", "goal": "Reject a duplicate task", "category": "input_validation",
                          "priority": "medium", "why": "Duplicates would confuse users."},
                         {"id": "m2", "goal": "Delete an existing task", "category": "core_flow",
                          "priority": "critical", "why": "Users could not manage their list."}]}
    script = {
        "plan": [plan],
        "step": [decision("type", field, text="Buy milk", expect="the box holds the text I typed"),
                 decision("click", add_btn, expect="a duplicate is rejected with a visible message"),
                 decision("done", None, expect=""),
                 decision("click", delete_btn, expect="the task disappears"),
                 decision("done", None, expect="")],
        "judge": [
            {"step_verdicts": [],
             "findings": [{"step": 1, "kind": "bug", "title": "Likely thing from m1", "severity": "medium",
                           "impact": "x", "expected": "e", "observed": "o", "suggestion": "s"},
                          {"step": 2, "kind": "improvement", "title": "Improvement thing from m1",
                           "severity": "low", "impact": "x", "expected": "e", "observed": "o", "suggestion": "s"}]},
            GUARD_JUDGE],
    }
    script_path = ROOT / "runs" / "_web_ui_order_script.json"
    script_path.parent.mkdir(exist_ok=True)
    script_path.write_text(json.dumps(script), encoding="utf-8")
    try:
        with start_web({"PROBE_LLM_MODE": "fake", "PROBE_LLM_SOURCE": str(script_path)}) as base:
            context = browser.new_context()
            page = context.new_page()
            page.goto(base)
            page.wait_for_selector("#banner:not([hidden])")
            page.locator("#url").fill(server)
            page.locator("#runBtn").click()
            page.wait_for_selector("#report:not([hidden])", timeout=30000)

            def badges():
                return [b.strip().lower() for b in page.locator(".finding .badge").all_inner_texts()]

            live = badges()
            api = json.loads(http("GET", base + "/api/status")[1])["last_findings"]
            arrival = [f["tier"] for f in api if f["tier"] != "Dropped"]
            assert set(arrival) == {"Confirmed", "Likely", "Improvement"}, arrival  # the fixture gave all three
            assert arrival[-1] == "Confirmed", "fixture must deliver the Confirmed item LAST, like rehearsals 2-3"
            assert live == ["confirmed", "likely", "improvement"], live

            page.reload()
            page.wait_for_selector("#report:not([hidden])", timeout=30000)
            assert badges() == ["confirmed", "likely", "improvement"]
            assert [f["tier"] for f in json.loads(http("GET", base + "/api/status")[1])["last_findings"]
                    if f["tier"] != "Dropped"] == arrival  # the data is untouched by the display sort
            context.close()
    finally:
        script_path.unlink(missing_ok=True)


def test_finding_order_within_a_tier_stays_arrival_order(server, browser):
    """Sorting is by tier only: two Likely cards keep the order they arrived in."""
    with start_web({"PROBE_LLM_MODE": "fake"}) as base:
        context = browser.new_context()
        page = context.new_page()
        page.goto(base)
        page.wait_for_selector("#banner:not([hidden])")
        order = page.evaluate("""() => {
            for (const [tier, title] of [["Likely", "first likely"], ["Improvement", "an improvement"],
                                         ["Likely", "second likely"], ["Confirmed", "the confirmed"]]) {
              handleFinding({tier, title});
            }
            return Array.from(document.querySelectorAll('#findings .finding .title')).map(e => e.textContent);
        }""")
        assert order == ["the confirmed", "first likely", "second likely", "an improvement"]
        context.close()
