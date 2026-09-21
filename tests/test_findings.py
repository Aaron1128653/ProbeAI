"""Candidates, tiers and the whole scripted pipeline (probe/findings.py, probe/run_script.py)."""
import json
import time

import pytest

from conftest import http, load_steps, make_evidence, make_record, make_run
from probe.browser import new_context
from probe.executor import Step, run_steps
from probe.findings import (CONFIRMED, DROPPED, LIKELY, Candidate, build_candidates, classify,
                            classify_with_reason, describe_step, format_table)
from probe.oracles import HARD_KINDS, Signal, signals_for_run
from probe.run_script import run_and_verify
from probe.verify import ReplayResult, replay

CLEAN = "/?bugs=off"


@pytest.fixture
def page(server, browser):
    context = new_context(browser)
    yield context.new_page()
    context.close()


def signal(kind: str, n: int, step: int = 1, key: str = "k") -> Signal:
    """A signal that came back in n replays."""
    return Signal(kind, "hard" if kind in HARD_KINDS else "contextual", step, "detail", key, reproduced_n=n)


def candidate(*signals: Signal, replays: int = 2) -> Candidate:
    best = max((s.reproduced_n for s in signals), default=0)
    return Candidate("C1", 1, list(signals), best, replays, ['Click button "Go"'], "before.png", "after.png")


# ---- classify(): D7 point 3 ----------------------------------------------------------

@pytest.mark.parametrize("label, signals, judgement, expected", [
    ("hard signal reproduced twice", [signal("http_5xx", 2)], {}, CONFIRMED),
    ("hard signal reproduced once of two", [signal("page_error", 1)], {}, CONFIRMED),
    ("hard signal seen once, not reproduced", [signal("http_5xx", 0)], {}, LIKELY),
    ("contextual reproduced", [signal("http_4xx", 2)], {}, LIKELY),
    ("contextual reproduced, survived the disprove pass", [signal("overflow", 2)], {"disproof_survived": True}, CONFIRMED),
    ("contextual reproduced, refuted", [signal("overflow", 2)], {"disproof_survived": False}, LIKELY),
    ("contextual not reproduced, survived (must be reproduced first)", [signal("overflow", 0)], {"disproof_survived": True}, LIKELY),
    ("flaky hard signal, contextual one reproduced and survived", [signal("http_5xx", 0), signal("overflow", 2)], {"disproof_survived": True}, CONFIRMED),
    ("flaky hard signal, contextual one reproduced, no disproof", [signal("http_5xx", 0), signal("overflow", 2)], {}, LIKELY),
    ("judge says violated, no signal", [], {"judge_violated": True}, LIKELY),
    ("judge says fine but contextual signal reproduced (no effect yet)", [signal("http_4xx", 2)], {"judge_violated": False}, LIKELY),
    ("nothing at all", [], {}, DROPPED),
    ("judge says fine, no signal", [], {"judge_violated": False}, DROPPED),
])
def test_classify_follows_the_tier_rule(label, signals, judgement, expected):
    assert classify(candidate(*signals), **judgement) == expected, label


def test_the_disprove_pass_flips_the_contextual_seeded_bugs_to_confirmed():
    s1 = candidate(signal("http_5xx", 2, key="DELETE /api/tasks/{id} 500"))
    s2 = candidate(signal("state_not_reached", 2, key='uncheck checkbox "Write report"'))
    s4 = candidate(signal("overflow", 2, key="page /"))
    s5 = candidate(signal("http_4xx", 2, key="POST /api/tasks 409"))

    assert [classify(c) for c in (s1, s2, s4, s5)] == [CONFIRMED, LIKELY, LIKELY, LIKELY]
    assert [classify(c, disproof_survived=True) for c in (s1, s2, s4, s5)] == [CONFIRMED] * 4


def test_the_reason_says_what_the_tier_rests_on():
    assert classify_with_reason(candidate(signal("http_5xx", 2)))[1] == "hard signal http_5xx, reproduced 2/2"
    tier, reason = classify_with_reason(candidate(signal("http_5xx", 0)))
    assert tier == LIKELY and "not reproduced (0/2)" in reason
    assert "needs the disprove pass" in classify_with_reason(candidate(signal("overflow", 1)))[1]


# ---- steps in words and the table -------------------------------------------------------

def test_steps_are_described_in_words_from_the_recorded_locator():
    assert describe_step(make_record("click", "button", "Delete Buy milk")) == 'Click button "Delete Buy milk"'
    assert describe_step(make_record("check", "checkbox", "Write report")) == 'Tick checkbox "Write report"'
    assert describe_step(make_record("uncheck", "checkbox", "Write report")) == 'Untick checkbox "Write report"'
    assert describe_step(make_record("type", "textbox", "New task", text="Call Bob")) == 'Type "Call Bob" into textbox "New task"'
    assert describe_step(make_record("type", "textbox", "New task", text="   ")) == 'Type 3 spaces into textbox "New task"'
    assert describe_step(make_record("press", "textbox", "New task", text="Enter")) == 'Press Enter in textbox "New task"'
    assert describe_step(make_record("click", "button", "Same", nth=1)) == 'Click button "Same" (number 2 with that name)'


def test_the_table_shows_tier_step_reproduced_and_signal_key():
    good = candidate(signal("http_5xx", 2, key="DELETE /api/tasks/{id} 500"))
    flaky = Candidate("C2", 3, [signal("http_4xx", 0, step=3, key="POST /api/tasks 409")], 0, 2, [], "", "")
    table = format_table([good, flaky])
    lines = table.splitlines()
    assert lines[0].split() == ["Tier", "Step", "Reproduced", "Signals"]
    assert lines[1].split(maxsplit=3) == ["Confirmed", "1", "2/2", "http_5xx: DELETE /api/tasks/{id} 500"]
    assert "Likely (not reproduced)" in lines[2] and "0/2" in lines[2]


# ---- build_candidates(): grouping, duplicates, audit ------------------------------------

def test_build_candidates_groups_by_step_merges_duplicates_and_audits_everything(tmp_path):
    records = [make_record("click", "button", "Add"), make_record("click", "button", "Add"),
               make_record("click", "button", "Filter"), make_record("click", "button", "Bad", blocked="no way")]
    run = make_run([(r, make_evidence(r, step=i)) for i, r in enumerate(records, start=1)])

    overflow = lambda step: Signal("overflow", "contextual", step, "wide", "page /")  # noqa: E731
    conflict = Signal("http_4xx", "contextual", 2, "409", "POST /api/tasks 409")
    signals = [overflow(1), overflow(2), conflict]
    replays = [ReplayResult(1, [], [overflow(1), conflict]), ReplayResult(2, [], [overflow(1)])]

    candidates = build_candidates(run, signals, replays, tmp_path)

    assert [(c.id, c.step, [s.kind for s in c.signals]) for c in candidates] == [
        ("C1", 1, ["overflow"]), ("C2", 2, ["http_4xx"])]
    assert [(c.reproduced_n, c.replays) for c in candidates] == [(2, 2), (1, 2)]
    assert candidates[1].steps_to_reproduce == ['Click button "Add"', 'Click button "Add"']
    assert (candidates[1].screenshot_before, candidates[1].screenshot_after) == ("step_02_before.png", "step_02_after.png")

    audit = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    assert [c["tier"] for c in audit["candidates"]] == [LIKELY, LIKELY]
    assert audit["merged_duplicates"] == [{
        "step": 2, "kind": "overflow", "key": "page /",
        "reason": "duplicate of C1: same kind and key already seen at step 1"}]
    assert [(q["step"], q["note"]) for q in audit["steps_without_signals"]] == [(3, "no signal"), (4, "blocked: no way")]


def test_build_candidates_does_not_change_the_signals_it_was_given():
    original = Signal("http_5xx", "hard", 1, "d", "k")
    run = make_run([(make_record(), make_evidence())])
    build_candidates(run, [original], [ReplayResult(1, [], [original])])
    assert original.reproduced_n == 0


# ---- flaky: seen once, absent in the replays ---------------------------------------------

def test_a_signal_seen_in_the_first_run_only_is_labelled_not_reproduced(server, browser, tmp_path):
    url = server + CLEAN
    steps = [Step("click", {"role": "button", "name": "Delete Buy milk"})]
    http("POST", server + "/__reset")

    context = new_context(browser)
    page = context.new_page()
    page.route("**/api/tasks/*", lambda route: route.fulfill(status=500, content_type="application/json", body="{}")
               if route.request.method == "DELETE" else route.continue_())  # a one-off server failure
    first = run_steps(page, url, steps, tmp_path / "first")
    context.close()
    signals = signals_for_run(first, url)
    assert [(s.kind, s.key) for s in signals] == [("http_5xx", "DELETE /api/tasks/{id} 500")]

    results = replay(browser, url, steps, "/__reset", 2, tmp_path / "replays")  # the real server is fine
    [c] = build_candidates(first, signals, results, tmp_path)

    assert (c.reproduced_n, c.replays) == (0, 2)
    tier, reason = classify_with_reason(c)
    assert tier == LIKELY and "not reproduced" in reason
    assert "Likely (not reproduced)" in format_table([c])


# ---- the whole scripted pipeline ---------------------------------------------------------

def tiers(outcome, **judgement):
    return [(c.step, c.signals[0].kind, classify(c, **judgement), f"{c.reproduced_n}/{c.replays}")
            for c in outcome["candidates"]]


@pytest.fixture(scope="module")
def buggy(server, browser, tmp_path_factory):
    out = tmp_path_factory.mktemp("buggy")
    outcome = run_and_verify(browser, server + "/", load_steps("steps_taskboard_all.json"), out,
                             reset_path="/__reset", replays=2)
    return out, outcome


def test_scripted_run_on_the_buggy_build_finds_s1_s2_s4_s5_and_nothing_for_s3_and_s6(buggy):
    out, outcome = buggy
    assert tiers(outcome) == [
        (1, "http_5xx", CONFIRMED, "2/2"),            # S1 delete answers 500
        (3, "state_not_reached", LIKELY, "2/2"),      # S2 a completed task cannot be reopened
        (7, "overflow", LIKELY, "2/2"),               # S4 the long title breaks the layout
        (9, "http_4xx", LIKELY, "2/2"),               # S5 duplicate answers 409, UI silent
    ]
    # S3 (blank title accepted, steps 4-5) and S6 (footer count, step 2) have no oracle signal by design
    assert {s.step for s in outcome["signals"]} == {1, 3, 7, 8, 9}


def test_the_disprove_pass_would_confirm_the_contextual_findings(buggy):
    _, outcome = buggy
    assert [t[2] for t in tiers(outcome, disproof_survived=True)] == [CONFIRMED] * 4


def test_the_written_files_tell_the_same_story(buggy):
    out, outcome = buggy
    findings = json.loads((out / "findings.json").read_text(encoding="utf-8"))
    assert [(f["step"], f["tier"], f["reproduced"]) for f in findings] == [
        (1, CONFIRMED, "2/2"), (3, LIKELY, "2/2"), (7, LIKELY, "2/2"), (9, LIKELY, "2/2")]
    assert findings[0]["steps_to_reproduce"] == ['Click button "Delete Buy milk"']
    assert findings[0]["screenshot_after"] == "step_01_after.png" and (out / "step_01_after.png").exists()
    assert (out / "evidence.json").exists() and (out / "replays" / "replay_2" / "step_01_after.png").exists()

    audit = json.loads((out / "audit.json").read_text(encoding="utf-8"))
    assert [q["step"] for q in audit["steps_without_signals"]] == [2, 4, 5, 6]
    assert [(m["step"], m["kind"]) for m in audit["merged_duplicates"]] == [(8, "overflow"), (9, "overflow")]


def test_the_script_really_visits_all_six_seeded_areas(server, page, tmp_path):
    """Ground truth from the app's own trigger log, before any replay resets it."""
    http("POST", server + "/__reset")
    run_steps(page, server + "/", load_steps("steps_taskboard_all.json"), tmp_path)
    wanted = {"S1", "S2", "S3", "S4", "S5", "S6"}
    deadline = time.time() + 3
    while time.time() < deadline:
        _, text = http("GET", server + "/__trigger_log")
        seen = {entry["id"] for entry in json.loads(text)}
        if wanted <= seen:
            break
        time.sleep(0.1)
    assert wanted <= seen, f"not exercised: {sorted(wanted - seen)}"


def test_scripted_run_on_the_clean_build_gives_no_confirmed_and_no_likely(server, browser, tmp_path):
    outcome = run_and_verify(browser, server + CLEAN, load_steps("steps_taskboard_valid.json"), tmp_path,
                             reset_path="/__reset", replays=2)
    assert outcome["signals"] == [] and outcome["candidates"] == []
    assert not (tmp_path / "replays").exists()  # nothing to verify, so nothing was replayed
    assert json.loads((tmp_path / "findings.json").read_text(encoding="utf-8")) == []
    audit = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    assert len(audit["steps_without_signals"]) == 7


def test_clean_build_answers_to_invalid_input_are_4xx_signals_that_become_likely(server, browser, tmp_path):
    """Known consequence of the current rules, see the open question in the T3 report: the clean app
    rejects a blank title (422) and a duplicate (409) correctly and shows a message, yet http_4xx is a
    contextual signal, so without a judge these steps come out as Likely. Never Confirmed."""
    outcome = run_and_verify(browser, server + CLEAN, load_steps("steps_taskboard_all.json"), tmp_path,
                             reset_path="/__reset", replays=1)
    found = [(c.step, c.signals[0].key, classify(c)) for c in outcome["candidates"]]
    assert found == [(5, "POST /api/tasks 422", LIKELY), (9, "POST /api/tasks 409", LIKELY)]
    # the evidence to tell them apart from S5 exists: on the clean build the page reacted
    steps = outcome["run"].results
    assert steps[4].evidence.fingerprint_before != steps[4].evidence.fingerprint_after
    assert steps[8].evidence.fingerprint_before != steps[8].evidence.fingerprint_after
