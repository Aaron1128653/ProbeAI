"""Oracle rules (probe/oracles.py). Most tests use hand-made evidence and need no browser."""
import pytest

from conftest import APP, http, make_entry, make_evidence, make_record, make_state, req
from probe.browser import new_context
from probe.executor import Step, run_steps
from probe.oracles import HARD_KINDS, detect_signals, signals_for_run

DELETE_URL = APP + "/api/tasks/7?bugs=off"


@pytest.fixture
def page(server, browser):
    """A fresh context and page."""
    context = new_context(browser)
    yield context.new_page()
    context.close()


def detect(evidence, record=None, before=None, after=None):
    record = record or make_record()
    return detect_signals(evidence, record, before or make_state(), after or make_state(), APP)


def kinds(signals):
    return [s.kind for s in signals]


# ---- hard signals -------------------------------------------------------------

def test_page_error_is_a_hard_signal():
    ev = make_evidence(page_errors=[{"message": "TypeError: x is not a function (line 42)"}])
    [signal] = [s for s in detect(ev) if s.kind == "page_error"]
    assert signal.strength == "hard" and signal.step == 1
    assert signal.key == "TypeError: x is not a function (line {n})"  # numbers normalised


def test_network_failure_is_hard_but_aborted_requests_are_ignored():
    failed = make_evidence(requests=[req("GET", APP + "/api/tasks", None, "net::ERR_CONNECTION_REFUSED")])
    [signal] = detect(failed)
    assert (signal.kind, signal.strength, signal.key) == ("request_failed", "hard", "GET /api/tasks failed")

    aborted = make_evidence(requests=[req("GET", APP + "/api/tasks", None, "net::ERR_ABORTED"),
                                      req("GET", APP + "/x", None, "cancelled")])
    assert "request_failed" not in kinds(detect(aborted))


def test_a_failed_request_to_somebody_elses_server_is_no_signal():
    """D8 ruling 6: a hard signal must be the app's own doing. The failure stays in the evidence only."""
    other = make_evidence(requests=[req("GET", "https://cdn.example/lib.js", None, "net::ERR_CONNECTION_REFUSED"),
                                    req("GET", "https://tracker.example/p.gif", None, "net::ERR_BLOCKED_BY_CLIENT")])
    assert kinds(detect(other)) == []
    assert len(other.requests) == 2  # still recorded

    mixed = make_evidence(requests=[req("GET", "https://cdn.example/lib.js", None, "net::ERR_FAILED"),
                                    req("GET", APP + "/api/tasks", None, "net::ERR_FAILED")])
    assert [(s.kind, s.key) for s in detect(mixed)] == [("request_failed", "GET /api/tasks failed")]


def test_5xx_is_hard_on_the_same_origin_only():
    same = make_evidence(requests=[req("DELETE", DELETE_URL, 500)])
    [signal] = [s for s in detect(same) if s.kind == "http_5xx"]
    assert signal.strength == "hard"
    assert signal.key == "DELETE /api/tasks/{id} 500"  # id replaced, query string dropped
    assert signal.detail == "DELETE /api/tasks/7 answered 500"

    other = make_evidence(requests=[req("GET", "https://cdn.example/lib.js", 503)])
    assert detect(other) == []  # somebody else's server


def test_keys_replace_numeric_and_uuid_path_segments():
    uuid = "3f2b8c1e-9a4d-4c6b-8e1f-0a1b2c3d4e5f"
    ev = make_evidence(requests=[req("PATCH", f"{APP}/api/users/{uuid}/tasks/12", 500),
                                 req("GET", f"{APP}/api/v2/tasks", 500)])
    assert [s.key for s in detect(ev) if s.kind == "http_5xx"] == [
        "PATCH /api/users/{id}/tasks/{id} 500", "GET /api/v2/tasks 500"]  # "v2" is not an id segment


# ---- contextual signals ----------------------------------------------------------

SILENT = dict(fp_before="same", fp_after="same")  # the page did not react to the step


def test_4xx_is_contextual_and_same_origin_only():
    ev = make_evidence(requests=[req("POST", APP + "/api/tasks?bugs=off", 409),
                                 req("GET", "https://cdn.example/x", 404)], **SILENT)
    signals = [s for s in detect(ev) if s.kind == "http_4xx"]
    assert [(s.strength, s.key) for s in signals] == [("contextual", "POST /api/tasks 409")]


def test_4xx_fires_only_when_the_page_stayed_silent():
    """D8 ruling 1: a 4xx the page turned into a visible message is normal validation (clean build);
    a swallowed 4xx is the defect (S5)."""
    request = req("POST", APP + "/api/tasks", 409)
    silent = detect(make_evidence(requests=[request], **SILENT))
    assert [(s.kind, s.key) for s in silent] == [("http_4xx", "POST /api/tasks 409")]

    reacted = detect(make_evidence(requests=[request], fp_before="before", fp_after="after"))
    assert reacted == []

    # 5xx is hard and does not depend on what the page did
    crashed = make_evidence(requests=[req("DELETE", DELETE_URL, 500)], fp_before="before", fp_after="after")
    assert kinds(detect(crashed)) == ["http_5xx"]


def test_hard_and_contextual_split():
    """Which kinds are hard is a rule of the design (D7 point 3), so pin it down."""
    ev = make_evidence(
        page_errors=[{"message": "boom"}],
        requests=[req("GET", APP + "/a", None, "net::ERR_FAILED"), req("GET", APP + "/b", 500),
                  req("GET", APP + "/c", 404)],
        console=[{"type": "error", "text": "oops", "resource_load_error": False, "url": None}],
        scroll_width=1500, client_width=1280, **SILENT)
    strengths = {s.kind: s.strength for s in detect(ev)}
    assert strengths == {"page_error": "hard", "request_failed": "hard", "http_5xx": "hard",
                         "http_4xx": "contextual", "overflow": "contextual", "console_error": "contextual"}
    assert HARD_KINDS == {"page_error", "request_failed", "http_5xx"}


def test_no_effect_needs_no_change_no_request_no_dialog_and_a_step_that_ran():
    same = dict(fp_before="x", fp_after="x")
    [signal] = detect(make_evidence(**same))
    assert (signal.kind, signal.strength, signal.key) == ("no_effect", "contextual", 'click button "Go"')

    assert detect(make_evidence(fp_before="x", fp_after="y")) == []                  # the page changed
    assert detect(make_evidence(**same, requests=[req("GET", APP + "/a", 200)])) == []  # a request went out
    assert detect(make_evidence(**same, dialogs=[{"type": "alert", "message": "hi"}])) == []
    assert detect(make_evidence(**same), record=make_record(error="element not found")) == []
    assert detect(make_evidence(**same), record=make_record(blocked="not allowed")) == []


def test_typing_only_spaces_is_not_reported_as_no_effect():
    """The snapshot squeezes whitespace, so the fingerprint cannot show a field holding only spaces."""
    record = make_record("type", "textbox", "New task", text="   ")
    assert detect(make_evidence(record, fp_before="x", fp_after="x"), record,
                  after=make_state([make_entry("textbox", "New task", value="")])) == []


def test_checkbox_that_is_not_in_the_requested_state_afterwards():
    record = make_record("uncheck", "checkbox", "Write report")
    evidence = make_evidence(record, requests=[req("PATCH", APP + "/api/tasks/2", 200)])
    stuck = make_state([make_entry("checkbox", "Write report", checked=True)])
    [signal] = detect(evidence, record, after=stuck)
    assert (signal.kind, signal.strength) == ("state_not_reached", "contextual")
    assert signal.key == 'uncheck checkbox "Write report"'
    assert "checked afterwards" in signal.detail

    done = make_state([make_entry("checkbox", "Write report", checked=False)])
    assert detect(evidence, record, after=done) == []
    assert detect(evidence, record, after=make_state()) == []  # element gone: cannot tell

    tick = make_record("check", "checkbox", "Write report")
    still_off = make_state([make_entry("checkbox", "Write report", checked=False)])
    assert kinds(detect(make_evidence(tick, requests=[req("PATCH", APP + "/api/tasks/2", 200)]), tick,
                        after=still_off)) == ["state_not_reached"]


def test_textbox_that_does_not_hold_the_typed_text_afterwards():
    record = make_record("type", "textbox", "New task", text="Call  Bob")
    evidence = make_evidence(record)
    empty = make_state([make_entry("textbox", "New task", value="")])
    [signal] = detect(evidence, record, after=empty)
    assert signal.kind == "state_not_reached" and signal.key == 'type textbox "New task"'

    holds = make_state([make_entry("textbox", "New task", value="Call Bob")])  # spaces are squeezed
    assert detect(evidence, record, after=holds) == []

    cleared_by_page = make_evidence(record, requests=[req("POST", APP + "/api/tasks", 201)])
    assert detect(cleared_by_page, record, after=empty) == []  # a request succeeded: the page took the text
    failed_request = make_evidence(record, requests=[req("POST", APP + "/api/tasks", 500)])
    assert kinds(detect(failed_request, record, after=empty)) == ["http_5xx", "state_not_reached"]


def test_the_typed_text_is_compared_on_its_first_200_characters_only():
    """D8 ruling 7: a field or a snapshot may cut a long value; the start must still match."""
    typed = "word " * 80  # 400 characters
    record = make_record("type", "textbox", "New task", text=typed)
    evidence = make_evidence(record)

    cut_short = make_state([make_entry("textbox", "New task", value=typed.strip()[:230])])
    assert detect(evidence, record, after=cut_short) == []  # same first 200 characters, ends differ

    wrong_start = make_state([make_entry("textbox", "New task", value="oops " + typed[5:].strip())])
    assert kinds(detect(evidence, record, after=wrong_start)) == ["state_not_reached"]

    beyond_200 = make_state([make_entry("textbox", "New task", value=typed.strip()[:200] + " changed later")])
    assert detect(evidence, record, after=beyond_200) == []  # a difference after character 200 is not looked at


def test_overflow_signal_and_its_note_when_it_was_there_before():
    ev = make_evidence(scroll_width=1414, client_width=1280, fp_before="a", fp_after="b")
    [signal] = detect(ev, after=make_state(scroll_width=1414))
    assert (signal.kind, signal.strength, signal.key) == ("overflow", "contextual", "page /")
    assert signal.detail == "page is 1414px wide in a 1280px window"

    [again] = detect(ev, before=make_state(scroll_width=1414), after=make_state(scroll_width=1414))
    assert "already" in again.detail
    assert detect(make_evidence(scroll_width=1280, client_width=1280, fp_before="a", fp_after="b")) == []


def test_console_errors_count_but_resource_load_errors_are_merged_into_the_http_signal():
    console = [
        {"type": "error", "text": "Failed to load resource: the server responded with a status of 500",
         "resource_load_error": True, "url": DELETE_URL},
        {"type": "error", "text": "Uncaught thing 42", "resource_load_error": False, "url": None},
        {"type": "warning", "text": "just a warning", "resource_load_error": False, "url": None},
    ]
    ev = make_evidence(console=console, requests=[req("DELETE", DELETE_URL, 500)])
    signals = detect(ev)
    assert kinds(signals) == ["http_5xx", "console_error"]
    assert signals[1].key == "Uncaught thing {n}"


def test_a_blocked_step_raises_nothing_and_repeats_within_a_step_collapse():
    boom = {"message": "boom"}
    assert detect(make_evidence(page_errors=[boom]), make_record(blocked="no")) == []
    assert len(detect(make_evidence(page_errors=[boom, boom, boom]))) == 1


# ---- with a real browser ---------------------------------------------------------

CLIENT_ONLY_PAGE = """
<label><input type="checkbox"> Remember me</label>
<button>Does nothing</button>
"""


def serve_page(page, server, path="/client-only", html=CLIENT_ONLY_PAGE) -> str:
    page.route(f"**{path}", lambda route: route.fulfill(body=html, content_type="text/html"))
    return server + path


def test_a_client_only_tick_raises_no_signal_but_a_dead_button_does(server, page, tmp_path):
    url = serve_page(page, server)
    steps = [Step("check", {"role": "checkbox", "name": "Remember me"}),   # no request, page changes
             Step("uncheck", {"role": "checkbox", "name": "Remember me"}),
             Step("click", {"role": "button", "name": "Does nothing"})]
    run = run_steps(page, url, steps, tmp_path)
    signals = signals_for_run(run, url)

    assert [(s.step, s.kind) for s in signals] == [(3, "no_effect")]  # only the dead button
    assert signals[0].key == 'click button "Does nothing"'


def test_the_seeded_delete_bug_gives_a_5xx_signal_and_no_console_duplicate(server, page, tmp_path):
    http("POST", server + "/__reset")
    steps = [Step("click", {"role": "button", "name": "Delete Buy milk"})]
    run = run_steps(page, server + "/", steps, tmp_path)
    signals = signals_for_run(run, server)

    assert [(s.kind, s.strength, s.key) for s in signals] == [("http_5xx", "hard", "DELETE /api/tasks/{id} 500")]
    assert any(c["resource_load_error"] for c in run.results[0].evidence.console)  # it was there, and merged
