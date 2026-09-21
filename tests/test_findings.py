"""Candidates, tiers and the whole scripted pipeline (probe/findings.py, probe/run_script.py)."""
import json
import time

import pytest

from conftest import fresh_app, http, load_steps, make_evidence, make_record, make_run
from probe.browser import new_context
from probe.executor import Step, run_steps
from probe.findings import (CONFIRMED, DROPPED, LIKELY, Candidate, build_candidates, classify,
                            classify_with_reason, describe_step, format_table, judge_only_candidate)
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


def candidate(*signals: Signal, replays: int = 2, outcome_n: int = 0) -> Candidate:
    """outcome_n: how often the outcome came back, only used when there are no signals (judge-only)."""
    best = max((s.reproduced_n for s in signals), default=outcome_n)
    return Candidate("C1", 1, list(signals), best, replays, ['Click button "Go"'], "before.png", "after.png")


# ---- classify(): D7 point 3 and the D8 rulings -------------------------------------------

@pytest.mark.parametrize("label, signals, judgement, expected", [
    # ruling 3: Confirmed needs the signal in ALL replays
    ("hard signal reproduced twice", [signal("http_5xx", 2)], {}, CONFIRMED),
    ("hard signal reproduced once of two is flaky", [signal("page_error", 1)], {}, LIKELY),
    ("hard signal seen once, not reproduced", [signal("http_5xx", 0)], {}, LIKELY),
    ("contextual reproduced", [signal("http_4xx", 2)], {}, LIKELY),
    ("contextual reproduced once of two, survived: still flaky", [signal("overflow", 1)], {"disproof_survived": True}, LIKELY),
    # ruling 4: the disprove pass decides contextual candidates, in both directions
    ("contextual reproduced, survived the disprove pass", [signal("overflow", 2)], {"disproof_survived": True}, CONFIRMED),
    ("contextual reproduced, refuted by the disprove pass", [signal("overflow", 2)], {"disproof_survived": False}, DROPPED),
    ("contextual not reproduced, survived (must be reproduced first)", [signal("overflow", 0)], {"disproof_survived": True}, LIKELY),
    ("flaky hard signal, contextual one reproduced and survived", [signal("http_5xx", 0), signal("overflow", 2)], {"disproof_survived": True}, CONFIRMED),
    ("flaky hard signal, contextual one reproduced, no disproof", [signal("http_5xx", 0), signal("overflow", 2)], {}, LIKELY),
    ("hard signals are never demoted by the disprove pass", [signal("http_5xx", 2)], {"disproof_survived": False}, CONFIRMED),
    ("a flaky hard signal is not dropped by the disprove pass either", [signal("http_5xx", 1), signal("overflow", 2)], {"disproof_survived": False}, LIKELY),
    # ruling 2: the judge can demote a contextual-only candidate, never a hard one
    ("judge says fine, contextual signal reproduced", [signal("http_4xx", 2)], {"judge_violated": False}, DROPPED),
    ("judge says fine, contextual signal not reproduced", [signal("http_4xx", 0)], {"judge_violated": False}, DROPPED),
    ("judge says violated, contextual signal reproduced", [signal("http_4xx", 2)], {"judge_violated": True}, LIKELY),
    ("judge says fine, hard signal reproduced", [signal("http_5xx", 2)], {"judge_violated": False}, CONFIRMED),
    ("judge says fine, hard signal flaky", [signal("http_5xx", 1)], {"judge_violated": False}, LIKELY),
    ("judge says fine, hard flaky and contextual reproduced", [signal("http_5xx", 0), signal("http_4xx", 2)], {"judge_violated": False}, LIKELY),
    # candidates without any signal (judge-only findings are reproduced by outcome, ruling 9)
    ("nothing at all", [], {}, DROPPED),
    ("judge-only, same outcome in both replays", [], {"outcome_n": 2}, LIKELY),
    ("judge-only, same outcome in one of two replays", [], {"outcome_n": 1}, LIKELY),
    ("judge-only, outcome did not come back", [], {"outcome_n": 0}, DROPPED),
    ("judge-only, judge says fine", [], {"outcome_n": 2, "judge_violated": False}, DROPPED),
])
def test_classify_follows_the_tier_rule(label, signals, judgement, expected):
    judgement = dict(judgement)
    outcome_n = judgement.pop("outcome_n", 0)
    assert classify(candidate(*signals, outcome_n=outcome_n), **judgement) == expected, label


def test_a_hard_signal_reproduced_in_only_some_replays_is_likely_and_labelled_flaky():
    tier, reason = classify_with_reason(candidate(signal("http_5xx", 1)))
    assert tier == LIKELY and reason.startswith("flaky (1/2)")
    one_of_three = classify_with_reason(candidate(signal("http_5xx", 1), replays=3))
    assert one_of_three[0] == LIKELY and one_of_three[1].startswith("flaky (1/3)")
    assert classify(candidate(signal("http_5xx", 3), replays=3)) == CONFIRMED  # n/n whatever n is
    assert classify(candidate(signal("http_5xx", 0), replays=0)) == LIKELY     # never replayed: cannot be Confirmed


def test_the_judge_and_the_disprove_pass_leave_their_reason_for_a_dropped_candidate():
    tier, reason = classify_with_reason(candidate(signal("http_4xx", 2)), judge_violated=False)
    assert tier == DROPPED and "judge says the expectation was not violated" in reason
    tier, reason = classify_with_reason(candidate(signal("overflow", 2)), disproof_survived=False)
    assert tier == DROPPED and "harmless explanation" in reason


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
    assert "needs the disprove pass" in classify_with_reason(candidate(signal("overflow", 2)))[1]
    assert classify_with_reason(candidate(signal("overflow", 1)))[1].startswith("flaky (1/2)")


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
    half = Candidate("C3", 4, [signal("http_5xx", 1, step=4, key="GET /api/x 500")], 1, 2, [], "", "")
    only_judge = candidate(outcome_n=2)
    table = format_table([good, flaky, half, only_judge])
    lines = table.splitlines()
    assert lines[0].split() == ["Tier", "Step", "Reproduced", "Signals"]
    assert lines[1].split(maxsplit=3) == ["Confirmed", "1", "2/2", "http_5xx: DELETE /api/tasks/{id} 500"]
    assert "Likely (not reproduced)" in lines[2] and "0/2" in lines[2]
    assert "Likely (flaky)" in lines[3] and "1/2" in lines[3]
    assert lines[4].split(maxsplit=3) == ["Likely", "1", "2/2", "(judge only)"]


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


def test_a_signal_that_came_back_in_one_replay_of_two_gives_a_likely_flaky_candidate():
    """D8 ruling 3, through build_candidates and reproduced(): 1/2 is not Confirmed even for a hard signal."""
    run = make_run([(make_record(), make_evidence())])
    original = Signal("http_5xx", "hard", 1, "d", "DELETE /api/tasks/{id} 500")
    [c] = build_candidates(run, [original], [ReplayResult(1, [], [original]), ReplayResult(2, [], [])])
    assert (c.reproduced_n, c.replays) == (1, 2)
    tier, reason = classify_with_reason(c)
    assert tier == LIKELY and reason.startswith("flaky (1/2)")
    assert "Likely (flaky)" in format_table([c])


# ---- judge-only findings: reproduced by outcome (D8 ruling 9) -----------------------------

def test_a_judge_only_candidate_has_no_signals_and_counts_replays_that_end_in_the_same_page():
    records = [make_record("click", "button", "Add"), make_record("click", "button", "Filter")]
    run = make_run([(r, make_evidence(r, step=i, fp_after=f"page{i}")) for i, r in enumerate(records, start=1)])
    same = ReplayResult(1, [make_evidence(step=1, fp_after="page1"), make_evidence(step=2, fp_after="page2")], [])
    different = ReplayResult(2, [make_evidence(step=1, fp_after="page1"), make_evidence(step=2, fp_after="other")], [])

    c = judge_only_candidate("C7", run, 2, [same, different])
    assert (c.id, c.step, c.signals, c.reproduced_n, c.replays) == ("C7", 2, [], 1, 2)
    assert c.steps_to_reproduce == ['Click button "Add"', 'Click button "Filter"']
    assert (c.screenshot_before, c.screenshot_after) == ("step_02_before.png", "step_02_after.png")
    assert classify(c) == LIKELY and classify_with_reason(c)[1].startswith("flaky (1/2)")

    both = judge_only_candidate("C8", run, 2, [same, same])
    assert (both.reproduced_n, classify(both)) == (2, LIKELY)
    never = judge_only_candidate("C9", run, 2, [different, different])
    assert (never.reproduced_n, classify(never)) == (0, DROPPED)


def test_s3_and_s6_are_judge_only_and_their_outcome_recurs_on_the_buggy_build(server, browser, tmp_path):
    """No oracle sees a blank task being accepted (step 5) or the footer showing the total (step 2)."""
    steps = load_steps("steps_taskboard_all.json")
    fresh_app(server)
    context = new_context(browser)
    first = run_steps(context.new_page(), server + "/", steps, tmp_path / "first")
    context.close()
    assert signals_for_run(first, server + "/") != []  # other steps do raise signals; steps 2 and 5 do not
    assert not [s for s in signals_for_run(first, server + "/") if s.step in (2, 5)]

    results = replay(browser, server + "/", steps[:5], "/__reset", 2, tmp_path / "replays")
    s6 = judge_only_candidate("C1", first, 2, results)
    s3 = judge_only_candidate("C2", first, 5, results)
    for c in (s6, s3):
        assert (c.signals, c.reproduced_n, c.replays) == ([], 2, 2)
        assert classify(c) == LIKELY


def test_a_judge_only_outcome_that_does_not_come_back_is_dropped(server, browser, tmp_path):
    """The first run met a page a clean start does not give (an extra task), so the replays end differently."""
    steps = load_steps("steps_taskboard_all.json")[:2]
    fresh_app(server)
    http("POST", server + "/api/tasks?bugs=off", {"title": "Leftover"})  # only the first run sees this task
    context = new_context(browser)
    first = run_steps(context.new_page(), server + "/", steps, tmp_path / "first")
    context.close()

    results = replay(browser, server + "/", steps, "/__reset", 2, tmp_path / "replays")
    c = judge_only_candidate("C1", first, 2, results)
    assert (c.reproduced_n, c.replays) == (0, 2)
    assert classify(c) == DROPPED


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
    fresh_app(server)
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


def test_full_script_on_the_clean_build_gives_zero_confirmed_and_zero_likely(server, browser, tmp_path):
    """D8 ruling 1: the clean app answers a blank title with 422 and a duplicate with 409, and shows a
    message each time. Those 4xx answers are correct behaviour, so they raise no signal."""
    outcome = run_and_verify(browser, server + CLEAN, load_steps("steps_taskboard_all.json"), tmp_path,
                             reset_path="/__reset", replays=2)
    assert outcome["signals"] == [] and outcome["candidates"] == []
    assert json.loads((tmp_path / "findings.json").read_text(encoding="utf-8")) == []

    steps = outcome["run"].results
    for index, status in ((4, 422), (8, 409)):  # the 4xx answers really happened ...
        assert [r["status"] for r in steps[index].evidence.requests if r["method"] == "POST"] == [status]
        assert steps[index].evidence.fingerprint_before != steps[index].evidence.fingerprint_after  # ... and the page reacted
