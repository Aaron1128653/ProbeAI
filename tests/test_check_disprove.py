"""probe/check_disprove.py: the one-real-call disprove contract check (D13).

No real API call anywhere here - the check itself is an explicitly invoked script, and these
tests only cover the parts that decide WHAT it would send and WHAT it claims afterwards: picking
a genuine contextual-only fully-reproduced candidate out of a recorded run, and reporting
honestly which of D13's criteria a given mode actually verified.
"""
import json

import pytest

from probe.check_disprove import criteria_report, load_recorded_candidate

ROOT_RUN = "runs/eval_2026-09-23_canary/injection"


def write_run(tmp_path, findings, mission_goal="Delete an existing task"):
    (tmp_path / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(json.dumps(
        {"type": "mission_started", "t": 0.1,
         "mission": {"id": "m1", "goal": mission_goal, "category": "core_flow",
                     "priority": "high", "why": "w"}}) + "\n", encoding="utf-8")
    return tmp_path


def finding(fid="C1", signals=(("no_effect", "contextual"),), reproduced="2/2", step=1):
    return {"id": fid, "step": step, "mission": "m1", "reproduced": reproduced,
            "signals": [{"kind": k, "strength": s, "step": step, "detail": f"{k} detail",
                         "key": f"{k}/key", "reproduced_n": 2} for k, s in signals],
            "steps_to_reproduce": ['Click textbox "New task"'],
            "screenshot_before": "step_01_before.png", "screenshot_after": "step_01_after.png"}


# ---- picking the candidate that would actually reach the disprove gate ------------------------

def test_picks_a_contextual_only_fully_reproduced_candidate(tmp_path):
    run = write_run(tmp_path, [finding()])
    mission, candidate = load_recorded_candidate(run)
    assert candidate.id == "C1" and candidate.reproduced_n == 2 and candidate.replays == 2
    assert [s.kind for s in candidate.signals] == ["no_effect"]
    assert mission.goal == "Delete an existing task"       # read from events.jsonl, not invented
    assert candidate.steps_to_reproduce == ['Click textbox "New task"']


def test_skips_hard_signal_candidates_because_they_never_reach_the_disprove_gate(tmp_path):
    """build_mission_items returns early for a hard signal - it needs no disprove pass - so
    replaying one would be checking a path the real pipeline never takes."""
    run = write_run(tmp_path, [finding("C1", signals=(("http_5xx", "hard"),)),
                               finding("C2", signals=(("no_effect", "contextual"),))])
    _, candidate = load_recorded_candidate(run)
    assert candidate.id == "C2"


def test_skips_a_candidate_that_did_not_reproduce_every_time(tmp_path):
    """Same reason: the gate requires every signal back in every replay, so a flaky one (1/2)
    would never get there either."""
    run = write_run(tmp_path, [finding("C1", reproduced="1/2"), finding("C2", reproduced="2/2")])
    _, candidate = load_recorded_candidate(run)
    assert candidate.id == "C2"


def test_says_so_clearly_when_a_run_holds_nothing_eligible(tmp_path):
    run = write_run(tmp_path, [finding("C1", signals=(("http_5xx", "hard"),))])
    with pytest.raises(SystemExit, match="no contextual-only, fully-reproduced finding"):
        load_recorded_candidate(run)


def test_judge_only_candidates_are_not_eligible_either(tmp_path):
    """A judge-only finding has no signals at all, and build_mission_items skips those too."""
    run = write_run(tmp_path, [finding("C1", signals=())])
    with pytest.raises(SystemExit):
        load_recorded_candidate(run)


# ---- the honesty mechanism: the report must not claim a real call it did not make -------------

@pytest.mark.parametrize("mode", ["fake", "replay", None])
def test_criteria_report_refuses_to_claim_live_verification_without_a_real_call(mode):
    """Found by running the script in fake mode and reading its own output: the criteria block
    was hardcoded to "[live]" and so claimed a real call had been made when none had. That is
    precisely the overclaim this whole script exists to avoid."""
    text = criteria_report(mode)
    assert "NO REAL CALL WAS MADE" in text
    assert "[live]" not in text
    assert "[NOT real]" in text


@pytest.mark.parametrize("mode", ["real", "record"])
def test_criteria_report_marks_1_to_4_live_only_in_a_spending_mode(mode):
    text = criteria_report(mode)
    assert "[live]" in text and "NO REAL CALL WAS MADE" not in text
    assert "[NOT run] 5." in text          # criterion 5 is never claimed, in any mode
    assert "mutation-checked (T9-a)" in text


def test_criterion_five_is_never_claimed_as_verified():
    for mode in ("real", "record", "fake", "replay", None):
        assert "[NOT run] 5." in criteria_report(mode)


# ---- against the real recorded run, when it is present on this machine -----------------------

def test_the_real_recorded_run_still_holds_an_eligible_candidate():
    """runs/ is git-ignored, so this is machine-local; it guards the claim in D13 that a genuine
    contextual-only fully-reproduced candidate exists to replay, rather than assuming it."""
    from pathlib import Path
    run = Path(ROOT_RUN)
    if not (run / "findings.json").is_file():
        pytest.skip(f"{ROOT_RUN} is machine-local (runs/ is git-ignored)")
    mission, candidate = load_recorded_candidate(run)
    assert candidate.signals and all(s.strength == "contextual" for s in candidate.signals)
    assert candidate.reproduced_n == candidate.replays > 0
    assert mission.goal and candidate.steps_to_reproduce
