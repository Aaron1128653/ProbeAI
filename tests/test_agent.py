"""The agent loop (probe/agent.py): prompts, deciding one step, one mission, judging, and the
whole run. See docs/DECISIONS.md D9 (the T5 specification) and the "D9 addendum" after this task.

No real API call anywhere: every LLMClient here is mode="fake". Two kinds of tests:
  - pure / synthetic: prompt builders, decide()'s retry logic, build_mission_items() with
    hand-built Candidates (conftest-style, as test_findings.py does), Meter and build_report().
    No browser.
  - real-browser integration: run_mission() against the real TaskBoard app (a fresh reset each
    time), and the full replay_mission -> build_mission_items -> classify pipeline reusing the
    same steps script T3b already proved the oracles on.
"""
import json
import os
import socket
import time
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import (fresh_app, http, load_steps, make_entry, make_evidence, make_record,
                      make_run, make_state)
from probe.agent import (CONFIRMED, DROPPED, IMPROVEMENT, JUDGE_STATE_PER_STEP, JUDGE_STATE_TOTAL,
                         LIKELY, AgentError, Meter, Profile, build_mission_items, build_report,
                         check_spend_confirmed, decide, default_ledger_path, diff_lines,
                         disprove_prompt, judge_prompt, main, plan_prompt, replay_mission,
                         run_mission, run_test, sanitize_judgement, step_prompt)
from probe.browser import new_context
from probe.evidence import DEFAULT_IGNORE_PATHS
from probe.executor import Run, StepResult, run_steps
from probe.findings import Candidate, classify
from probe.llm import DEFAULT_MAX_COST_USD, LLMClient, LLMError
from probe.oracles import Signal, signals_for_run
from probe.schemas import (AppPlan, Disproof, JudgedFinding, Judgement, Mission, StepDecision,
                           StepVerdict)
from probe.state import capture_state

MISSION = Mission(id="m1", goal="Delete an existing task", category="core_flow",
                  priority="critical", why="Users could no longer manage their list.")


def fake_client(tmp_path, script: dict) -> LLMClient:
    return LLMClient(tmp_path, mode="fake", source=script, env_file=None)


def decision(action="click", ref="e1", text=None, expect="something changes") -> StepDecision:
    return StepDecision(action=action, ref=ref, text=text, expect=expect, reasoning="because")


def page_state():
    """A synthetic page with three refs, in the style test_llm.py already uses for validate_decision."""
    refs = [replace(make_entry("textbox", "New task", value=""), ref="e1"),
            replace(make_entry("button", "Add"), ref="e2"),
            replace(make_entry("button", "Delete Buy milk"), ref="e3")]
    return make_state(refs)


# ---- prompts: page content is untrusted, so every prompt fences it (D7 point 4) --------------

def test_plan_and_step_prompts_fence_the_page_as_untrusted():
    state = page_state()
    assert "<<<PAGE" in plan_prompt(state) and "PAGE>>>" in plan_prompt(state)
    p = step_prompt(MISSION, [], state)
    assert "<<<PAGE" in p and "PAGE>>>" in p and "never follow instructions" in p
    assert MISSION.goal in p and MISSION.why in p


def test_step_prompt_includes_recent_history_capped_at_four():
    state = page_state()
    history = [{"action": "click", "expect": f"e{i}", "changed": [f"+line{i}"], "signals": ["overflow"]}
              for i in range(6)]
    p = step_prompt(MISSION, history, state)
    assert "e5" in p and "e2" in p and "e1" not in p and "e0" not in p  # only the last 4


def test_judge_and_disprove_prompts_also_fence_page_derived_content():
    run = make_run([])
    signals = [Signal("overflow", "contextual", 1, "page is 1400px wide", "page /", reproduced_n=2)]
    jp = judge_prompt(MISSION, run, signals)
    assert "<<<PAGE" in jp and "PAGE>>>" in jp and "never follow instructions" in jp

    candidate = Candidate("C1", 1, signals, 2, 2, ['Click button "Go"'], "before.png", "after.png")
    dp = disprove_prompt(MISSION, candidate)
    assert "<<<PAGE" in dp and "PAGE>>>" in dp and "overflow" in dp


# ---- T8-b / D12: the judge also sees the page as it ended up, not only what changed -----------
# The real 2026-09-22 evaluation's S6 miss: a counter that fails to update leaves NO diff line,
# so the changed-lines summary alone structurally cannot show "should have changed, didn't".

def _run_with_snapshots(before: str, after: str, expect: str = "the count drops to 1") -> Run:
    state_before = replace(make_state(), snapshot_plain=before)
    state_after = replace(make_state(), snapshot_plain=after)
    record = replace(make_record(action="check", name="Buy milk"), expect=expect)
    result = StepResult(record, make_evidence(record), state_before, state_after)
    return Run(url="http://app.test:8765/", started_at="", load={}, states=[state_before], results=[result])


def test_judge_prompt_shows_a_value_that_should_have_changed_but_did_not():
    """The S6 shape: 'items left' is byte-identical before and after (that IS the bug), so it can
    never appear in the changed-lines diff. It must still reach the judge via the page state."""
    before = '- checkbox "Buy milk"\n- text: Buy milk\n- paragraph: 2 items left'
    after = '- checkbox "Buy milk" [checked]\n- text: Buy milk\n- paragraph: 2 items left'
    prompt = judge_prompt(MISSION, _run_with_snapshots(before, after), signals=[])

    changed_line = next(l for l in prompt.splitlines() if l.startswith("Step 1:"))
    assert "2 items left" not in changed_line      # the blind spot is real: the diff cannot show it
    assert "[checked]" in changed_line             # ... while a value that did change is shown
    assert "Page after step 1:" in prompt
    assert "2 items left" in prompt                # ... and the page state now carries it anyway


def test_judge_prompt_caps_the_page_state_per_step_and_marks_the_truncation():
    huge = "\n".join(f"- text: line {i}" for i in range(2000))  # far over the per-step cap
    prompt = judge_prompt(MISSION, _run_with_snapshots("- text: line 0", huge), signals=[])
    excerpt = prompt.split("Page after step 1:\n", 1)[1]
    assert "[truncated," in excerpt                             # the judge is told it was cut
    assert len(excerpt) < len(huge)
    assert len(excerpt) <= JUDGE_STATE_PER_STEP + 200           # cap honoured, plus the marker


def test_judge_prompt_stops_adding_page_state_once_the_mission_budget_is_used_up():
    big = "\n".join(f"- text: line {i}" for i in range(500))
    state_before = replace(make_state(), snapshot_plain="- text: start")
    state_after = replace(make_state(), snapshot_plain=big)
    record = replace(make_record(), expect="something")
    results = [StepResult(record, make_evidence(record), state_before, state_after) for _ in range(8)]
    run = Run(url="http://app.test:8765/", started_at="", load={}, states=[state_before], results=results)

    prompt = judge_prompt(MISSION, run, signals=[])
    assert "the page-state budget for this mission is used up" in prompt  # said out loud, not silent
    page_state_chars = sum(len(p) for p in prompt.split("Page after step ")[1:])
    assert page_state_chars < JUDGE_STATE_TOTAL * 2  # bounded no matter how many steps a mission has


def test_diff_lines_shows_only_added_and_removed_lines_capped():
    before, after = "a\nb\nc", "a\nX\nc\nY"
    assert diff_lines(before, after) == ["-b", "+X", "+Y"]
    assert diff_lines("same", "same") == []
    long_before = "\n".join(f"l{i}" for i in range(50))
    long_after = "\n".join(f"m{i}" for i in range(50))
    assert len(diff_lines(long_before, long_after, cap=10)) == 10


# ---- decide(): the model's answer, validated, with one retry (D8) ---------------------------

def test_decide_returns_a_valid_first_answer(tmp_path):
    client = fake_client(tmp_path, {"step": [decision("click", "e3").model_dump()]})
    result, error = decide(client, MISSION, [], page_state(), Meter())
    assert error is None and result.action == "click" and result.ref == "e3"


def test_decide_retries_once_on_an_invalid_ref_then_continues(tmp_path):
    script = {"step": [decision("click", "e99").model_dump(),  # invalid: no such ref
                       decision("click", "e3").model_dump()]}  # the retry: valid
    client = fake_client(tmp_path, script)
    result, error = decide(client, MISSION, [], page_state(), Meter())
    assert error is None and result.ref == "e3"
    logged = (tmp_path / "llm_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(logged) == 2  # both the invalid answer and the retry are logged


def test_decide_gives_up_after_one_retry_and_returns_the_error(tmp_path):
    script = {"step": [decision("click", "e99").model_dump(),   # invalid
                       decision("type", "e3").model_dump()]}    # still invalid: type needs text
    client = fake_client(tmp_path, script)
    result, error = decide(client, MISSION, [], page_state(), Meter())
    assert result is None
    assert "needs text" in error


def test_decide_updates_the_meter_for_every_call_including_retries(tmp_path):
    script = {"step": [decision("click", "e99").model_dump(), decision("click", "e3").model_dump()]}
    client = fake_client(tmp_path, script)
    meter = Meter()
    decide(client, MISSION, [], page_state(), meter)
    assert meter.calls == 2  # fake calls cost nothing, but they still count


# ---- build_mission_items(): tiering, judge text, the disprove pass, improvements ------------

def signal(kind: str, n: int, step: int = 1, key: str = "k", strength=None) -> Signal:
    from probe.oracles import HARD_KINDS
    return Signal(kind, strength or ("hard" if kind in HARD_KINDS else "contextual"), step, "detail", key,
                 reproduced_n=n)


def candidate(*signals: Signal, step: int = 1, replays: int = 2, cid: str = "C1") -> Candidate:
    best = max((s.reproduced_n for s in signals), default=0)
    return Candidate(cid, step, list(signals), best, replays, ['Click button "Go"'], "b.png", "a.png")


def judgement(step_verdicts=(), findings=()) -> Judgement:
    return Judgement(step_verdicts=list(step_verdicts), findings=list(findings))


def bug(step, title="Bug", severity="high") -> JudgedFinding:
    return JudgedFinding(step=step, kind="bug", title=title, severity=severity, impact="i",
                         expected="e", observed="o", suggestion="s")


def improvement(step, title="Improvement") -> JudgedFinding:
    return JudgedFinding(step=step, kind="improvement", title=title, severity="low", impact="i",
                         expected="e", observed="o", suggestion="s")


def test_a_hard_signal_needs_no_disprove_pass_and_is_confirmed(tmp_path):
    client = fake_client(tmp_path, {})  # the disprove role is never called
    c = candidate(signal("http_5xx", 2))
    items = build_mission_items(MISSION, make_run([]), [c], judgement(), client, Meter())
    assert [i["tier"] for i in items] == [CONFIRMED]
    assert items[0]["title"].startswith("http_5xx:")  # no judge text: built from the signal


def test_judge_text_attaches_to_a_signal_based_candidate(tmp_path):
    client = fake_client(tmp_path, {})  # hard signal: no disprove call is made
    c = candidate(signal("http_5xx", 2), step=3)
    j = judgement(findings=[bug(3, title="Delete fails", severity="high")])
    items = build_mission_items(MISSION, make_run([]), [c], j, client, Meter())
    assert items[0]["title"] == "Delete fails" and items[0]["severity"] == "high"


def test_contextual_only_candidate_needs_disprove_and_survives_to_confirmed(tmp_path):
    client = fake_client(tmp_path, {"disprove": [Disproof(refuted=False, reason="no benign explanation").model_dump()]})
    c = candidate(signal("overflow", 2))
    items = build_mission_items(MISSION, make_run([]), [c], judgement(), client, Meter())
    assert items[0]["tier"] == CONFIRMED and "survived the disprove pass" in items[0]["reason"]


def test_contextual_only_candidate_is_dropped_when_disprove_refutes_it(tmp_path):
    client = fake_client(tmp_path, {"disprove": [Disproof(refuted=True, reason="wrong element clicked").model_dump()]})
    c = candidate(signal("overflow", 2))
    items = build_mission_items(MISSION, make_run([]), [c], judgement(), client, Meter())
    assert items[0]["tier"] == DROPPED


def test_a_failed_disprove_call_leaves_the_candidate_at_likely_instead_of_killing_the_run(tmp_path):
    """T9-a: on 2026-09-23 an unusable disprove answer (cut off by max_tokens) propagated out of
    here and aborted a paid 7-run batch on its last run; on stage it would end a live demo. The
    empty script makes the real LLMError ("no answer left for role 'disprove'") - not a stub
    pretending to be one. Degrading is safe by D3's own rule: without a disprove pass the
    candidate cannot reach Confirmed, so this can only ever under-claim, never over-claim."""
    client = fake_client(tmp_path, {})  # no disprove answers at all -> the call raises LLMError
    c = candidate(signal("overflow", 2))
    items = build_mission_items(MISSION, make_run([]), [c], judgement(), client, Meter())

    assert items[0]["tier"] == LIKELY          # not Confirmed (no disprove ran) and not a crash
    assert "no answer left" in items[0]["disprove_error"]
    assert "the disprove pass could not be used" in items[0]["reason"]  # visible, not swallowed


def test_judge_says_not_violated_drops_a_contextual_candidate_without_calling_disprove(tmp_path):
    client = fake_client(tmp_path, {})  # disprove has no answers: calling it would raise
    c = candidate(signal("http_4xx", 2), step=2)
    j = judgement(step_verdicts=[StepVerdict(step=2, violated=False, reason="expected validation")])
    items = build_mission_items(MISSION, make_run([]), [c], j, client, Meter())
    assert items[0]["tier"] == DROPPED


def test_a_flaky_contextual_candidate_stays_likely_without_a_disprove_call(tmp_path):
    client = fake_client(tmp_path, {})  # not fully reproduced: no disprove call is made
    c = candidate(signal("overflow", 1))  # 1 of 2 replays
    items = build_mission_items(MISSION, make_run([]), [c], judgement(), client, Meter())
    assert items[0]["tier"] == LIKELY and "flaky" in items[0]["reason"]


def test_judge_only_candidate_needs_no_disprove_pass(tmp_path):
    client = fake_client(tmp_path, {})  # no signals at all: disprove must not be called
    c = Candidate("J5", 5, [], 2, 2, ["step a", "step b"], "b.png", "a.png")
    j = judgement(findings=[bug(5, title="Footer count wrong")])
    items = build_mission_items(MISSION, make_run([]), [c], j, client, Meter())
    assert items[0]["tier"] == LIKELY and items[0]["title"] == "Footer count wrong"


def test_an_improvement_kind_finding_overrides_tiering_even_with_a_hard_signal(tmp_path):
    # D9 addendum: kind="improvement" always wins, no disprove pass, no Confirmed/Likely/Dropped.
    client = fake_client(tmp_path, {})  # disprove must not be called
    c = candidate(signal("http_4xx", 2), step=9)
    j = judgement(findings=[improvement(9, title="Show a message for duplicates")])
    items = build_mission_items(MISSION, make_run([]), [c], j, client, Meter())
    assert len(items) == 1
    assert items[0]["tier"] == IMPROVEMENT and items[0]["title"] == "Show a message for duplicates"
    assert items[0]["screenshot_before"] == "b.png"  # the candidate's own evidence is kept


def test_a_pure_improvement_finding_with_no_candidate_becomes_its_own_item(tmp_path):
    from conftest import make_evidence, make_record
    pairs = [(make_record(action="type", name="New task"), make_evidence(step=i)) for i in range(1, 6)]
    run = make_run(pairs)  # 5 steps, no signals anywhere
    client = fake_client(tmp_path, {})  # no candidates at all: disprove must not be called

    j = judgement(findings=[improvement(5, title="Reject whitespace-only titles")])
    items = build_mission_items(MISSION, run, [], j, client, Meter())

    assert len(items) == 1
    item = items[0]
    assert item["tier"] == IMPROVEMENT and item["title"] == "Reject whitespace-only titles"
    assert item["signals"] == [] and item["reproduced"] == "-"
    assert len(item["steps_to_reproduce"]) == 5
    assert item["screenshot_before"] == run.results[4].evidence.screenshot_before


def test_findings_with_no_step_are_kept_out_of_the_report(tmp_path):
    client = fake_client(tmp_path, {})
    j = judgement(findings=[JudgedFinding(step=None, kind="improvement", title="General note",
                                          severity="low", impact="i", expected="e", observed="o", suggestion="s")])
    items = build_mission_items(MISSION, make_run([]), [], j, client, Meter())
    assert items == []  # nowhere to anchor it; not silently invented as step 0


def test_multiple_bug_findings_on_different_steps_each_get_their_own_item(tmp_path):
    client = fake_client(tmp_path, {"disprove": [Disproof(refuted=False, reason="x").model_dump()]})
    c1 = candidate(signal("http_5xx", 2), step=1, cid="C1")
    c2 = candidate(signal("overflow", 2), step=7, cid="C2")
    j = judgement(findings=[bug(1, title="Delete fails"), bug(7, title="Layout overflows")])
    items = build_mission_items(MISSION, make_run([]), [c1, c2], j, client, Meter())
    by_title = {i["title"]: i["tier"] for i in items}
    assert by_title == {"Delete fails": CONFIRMED, "Layout overflows": CONFIRMED}


# ---- Meter and build_report ------------------------------------------------------------------

def test_meter_adds_up_calls_tokens_and_cost():
    from types import SimpleNamespace
    meter = Meter()
    usage = SimpleNamespace(input_tokens=100, output_tokens=50, cost_usd=0.002)
    meter.record(usage)
    meter.record(usage)
    assert (meter.calls, meter.input_tokens, meter.output_tokens) == (2, 200, 100)
    assert meter.cost_usd == pytest.approx(0.004)


def test_build_report_counts_per_tier_and_the_release_check_verdict():
    findings = [{"tier": CONFIRMED}, {"tier": LIKELY}, {"tier": IMPROVEMENT}, {"tier": IMPROVEMENT}]
    report = build_report("http://x/", Profile("eval", 5, 15, 6, 180, 2), findings, time.monotonic() - 1.5,
                          timed_out=False, meter=Meter())
    assert report["counts"] == {CONFIRMED: 1, LIKELY: 1, IMPROVEMENT: 2, DROPPED: 0}
    assert report["verdict"] == "review before release"
    assert report["profile"] == "eval" and report["timed_out"] is False
    assert report["elapsed_s"] >= 1.0


def test_build_report_says_no_confirmed_issues_when_nothing_stuck():
    findings = [{"tier": DROPPED}, {"tier": IMPROVEMENT}]
    report = build_report("http://x/", Profile("live", 3, 10, 6, 90, 1), findings, time.monotonic(), False, Meter())
    assert report["verdict"] == "no confirmed issues"


# ---- Profile validation (T7-0b, docs/DECISIONS.md D11 item 3): a bad hand-built Profile used to
# fail silently and wrong (e.g. replays=0 quietly makes Confirmed unreachable), not loudly --------

def test_profile_accepts_the_built_in_values():
    Profile("live", 3, 10, 6, 90, 1)
    Profile("eval", 5, 15, 6, 180, 2)


def test_profile_accepts_wall_clock_s_zero():
    """A deliberately-used value (an already-expired profile, for testing the immediate-timeout
    path) - must stay valid, not get swept up by validation meant for the other fields."""
    Profile("instant", max_missions=5, max_total_steps=10, max_steps_per_mission=6,
           wall_clock_s=0, replays=1)


@pytest.mark.parametrize("field,value", [
    ("max_missions", 0), ("max_total_steps", 0), ("max_steps_per_mission", 0),
    ("replays", 0), ("wall_clock_s", -1),
])
def test_profile_rejects_an_invalid_field(field, value):
    kwargs = dict(name="bad", max_missions=3, max_total_steps=10, max_steps_per_mission=6,
                 wall_clock_s=90, replays=1)
    kwargs[field] = value
    with pytest.raises(AgentError, match=field):
        Profile(**kwargs)


# ---- check_spend_confirmed(): the CLI's --yes-spend gate (D9 item 4) -------------------------

@pytest.mark.parametrize("mode", ["real", "record"])
def test_real_and_record_need_yes_spend(mode):
    message = check_spend_confirmed(mode, yes_spend=False)
    assert message is not None and "--yes-spend" in message and mode in message
    assert check_spend_confirmed(mode, yes_spend=True) is None


@pytest.mark.parametrize("mode", ["fake", "replay", None])
def test_fake_and_replay_never_need_yes_spend(mode):
    assert check_spend_confirmed(mode, yes_spend=False) is None


def test_the_spend_message_states_the_per_run_cap(monkeypatch):
    monkeypatch.delenv("PROBE_MAX_COST_USD", raising=False)
    assert str(DEFAULT_MAX_COST_USD) in check_spend_confirmed("real", False)


# ---- default_ledger_path() (T7-0c, docs/DECISIONS.md D11 item 4): real/record used to leave the
# cross-run cap off unless PROBE_SPEND_LEDGER was set by hand - which is what the one real run so
# far actually relied on, not a CLI default. ----------------------------------------------------

@pytest.mark.parametrize("mode", ["real", "record"])
def test_default_ledger_path_is_on_for_real_and_record(mode, monkeypatch):
    monkeypatch.delenv("PROBE_SPEND_LEDGER", raising=False)
    assert default_ledger_path(mode) == "runs/spend_ledger.jsonl"


@pytest.mark.parametrize("mode", ["fake", "replay", None])
def test_default_ledger_path_is_off_for_fake_and_replay(mode, monkeypatch):
    monkeypatch.delenv("PROBE_SPEND_LEDGER", raising=False)
    assert default_ledger_path(mode) is None


def test_default_ledger_path_respects_an_explicit_env_value(monkeypatch):
    monkeypatch.setenv("PROBE_SPEND_LEDGER", "custom/ledger.jsonl")
    assert default_ledger_path("real") == "custom/ledger.jsonl"
    assert default_ledger_path("fake") is None  # still off regardless of the env value
    monkeypatch.setenv("PROBE_MAX_COST_USD", "0.05")
    assert "0.05" in check_spend_confirmed("real", False)


# ================================================================================================
# Real-browser integration: run_mission() against TaskBoard, and the full pipeline end to end.
# Refs are only known once a page is actually loaded (D7), so a short read-only discovery pass
# finds the ref a fake script needs, against the SAME fresh-reset state the real call will see.
# ================================================================================================

def _ref_for(browser, url: str, role: str, name: str, nth: int = 0) -> str:
    context = new_context(browser)
    try:
        page = context.new_page()
        page.goto(url)
        state = capture_state(page)
        entry = next(e for e in state.refs if e.role == role and e.name == name and e.nth == nth)
        return entry.ref
    finally:
        context.close()


# ---- run_mission(): one mission, decided step by step against the real app ------------------

def test_run_mission_does_a_sensible_two_step_mission_and_ends_done(server, browser, tmp_path):
    fresh_app(server)
    field = _ref_for(browser, server, "textbox", "New task")
    add = _ref_for(browser, server, "button", "Add")
    script = {"step": [
        decision("type", field, text="Buy bread", expect="the box holds the text I typed").model_dump(),
        decision("click", add, expect="a new task 'Buy bread' appears").model_dump(),
        decision("done", None, expect="").model_dump(),
    ]}
    client = fake_client(tmp_path, script)
    mission_run, signals, status, stuck_reason = run_mission(browser, server, "/__reset", MISSION, client, 6,
                                               time.monotonic() + 30, tmp_path / "m", DEFAULT_IGNORE_PATHS, Meter())
    assert status == "done" and stuck_reason is None
    assert len(mission_run.results) == 2  # "done" itself takes no step
    assert signals == []  # adding a normal task raises no signal


def test_run_mission_recovers_from_one_invalid_ref_via_retry(server, browser, tmp_path):
    fresh_app(server)
    field = _ref_for(browser, server, "textbox", "New task")
    script = {"step": [
        decision("click", "e999", expect="nothing: this ref does not exist").model_dump(),  # invalid
        decision("type", field, text="Recovered", expect="the box holds the text I typed").model_dump(),
        decision("done", None, expect="").model_dump(),
    ]}
    client = fake_client(tmp_path, script)
    mission_run, signals, status, stuck_reason = run_mission(browser, server, "/__reset", MISSION, client, 6,
                                               time.monotonic() + 30, tmp_path / "m", DEFAULT_IGNORE_PATHS, Meter())
    assert status == "done"
    assert len(mission_run.results) == 1  # the invalid attempt spent no step budget


def test_run_mission_ends_stuck_when_the_retry_also_fails(server, browser, tmp_path):
    fresh_app(server)
    script = {"step": [decision("click", "e999").model_dump(), decision("click", "e998").model_dump()]}
    client = fake_client(tmp_path, script)
    mission_run, signals, status, stuck_reason = run_mission(browser, server, "/__reset", MISSION, client, 6,
                                               time.monotonic() + 30, tmp_path / "m", DEFAULT_IGNORE_PATHS, Meter())
    assert status == "stuck" and mission_run.results == [] and signals == []
    assert stuck_reason == "validation_failed"  # D15: two invalid answers in a row - code-verified, not opinion


def test_run_mission_records_a_model_declared_stuck_separately_from_a_validation_failure(server, browser, tmp_path):
    """D15: the model itself choosing action="stuck" is a different exit from decide() giving up
    after two invalid answers, but both used to collapse into the same "stuck" string. status is
    unchanged (still "stuck", still an incomplete sweep to run_status); only the reason is new."""
    fresh_app(server)
    script = {"step": [decision("stuck", None, expect="I cannot proceed").model_dump()]}
    client = fake_client(tmp_path, script)
    mission_run, signals, status, stuck_reason = run_mission(
        browser, server, "/__reset", MISSION, client, 6, time.monotonic() + 30, tmp_path / "m",
        DEFAULT_IGNORE_PATHS, Meter())
    assert status == "stuck" and stuck_reason == "model_declared"
    assert mission_run.results == [] and signals == []


def test_run_mission_times_out_before_taking_any_step(server, browser, tmp_path):
    fresh_app(server)
    client = fake_client(tmp_path, {})  # no answers needed: the deadline is already past
    mission_run, signals, status, stuck_reason = run_mission(browser, server, "/__reset", MISSION, client, 6,
                                               time.monotonic() - 1, tmp_path / "m", DEFAULT_IGNORE_PATHS, Meter())
    assert status == "timed_out" and mission_run.results == []
    assert stuck_reason is None  # a timeout is not a kind of "stuck"


def test_run_mission_stops_at_the_step_budget(server, browser, tmp_path):
    fresh_app(server)
    box = _ref_for(browser, server, "checkbox", "Write report")
    script = {"step": [decision("check", box, expect="it becomes ticked").model_dump()]}
    client = fake_client(tmp_path, script)
    mission_run, signals, status, stuck_reason = run_mission(browser, server, "/__reset", MISSION, client, 1,
                                               time.monotonic() + 30, tmp_path / "m", DEFAULT_IGNORE_PATHS, Meter())
    assert status == "budget_exceeded" and len(mission_run.results) == 1
    assert stuck_reason is None


# ---- replay_mission() + build_mission_items() against a real mission_run --------------------

def test_full_pipeline_on_the_buggy_taskboard_matches_the_seeded_bugs(server, browser, tmp_path):
    """Reuses the exact script T3b proved the oracles on. The kind the judge gives each finding
    decides its fate (D9 addendum): S1/S2/S4 (real bugs) survive disprove to Confirmed; S6
    (judge-only bug) is Likely by outcome; S3 and S5 (their true kind is "improvement" in
    ground_truth.json) are reported as Improvement, never tiered as a defect."""
    fresh_app(server)
    context = new_context(browser)
    mission_run = run_steps(context.new_page(), server, load_steps("steps_taskboard_all.json"),
                            tmp_path / "run", DEFAULT_IGNORE_PATHS)
    context.close()
    signals = signals_for_run(mission_run, server)

    j = judgement(
        step_verdicts=[StepVerdict(step=1, violated=True, reason="delete failed"),
                      StepVerdict(step=3, violated=True, reason="reopen failed"),
                      StepVerdict(step=7, violated=True, reason="page overflows")],
        findings=[bug(1, title="Delete fails"),
                 bug(2, title="Footer count is wrong once a task is completed"),
                 bug(3, title="Completed task cannot be reopened"),
                 improvement(5, title="Reject whitespace-only titles"),
                 bug(7, title="Long title overflows the page"),
                 improvement(9, title="Show a message for duplicate titles")])

    candidates, replays = replay_mission(browser, server, "/__reset", mission_run, signals, j,
                                         replays=2, out_dir=tmp_path / "verify", ignore_paths=DEFAULT_IGNORE_PATHS)
    disprove_script = {"disprove": [Disproof(refuted=False, reason="no benign explanation").model_dump()] * 2}
    client = fake_client(tmp_path, disprove_script)
    items = build_mission_items(MISSION, mission_run, candidates, j, client, Meter())

    assert {i["title"]: i["tier"] for i in items} == {
        "Delete fails": CONFIRMED,
        "Completed task cannot be reopened": CONFIRMED,
        "Long title overflows the page": CONFIRMED,
        "Footer count is wrong once a task is completed": LIKELY,
        "Reject whitespace-only titles": IMPROVEMENT,
        "Show a message for duplicate titles": IMPROVEMENT,
    }


def test_full_pipeline_on_the_clean_taskboard_finds_nothing(server, browser, tmp_path):
    fresh_app(server)
    clean_url = server + "/?bugs=off"
    context = new_context(browser)
    mission_run = run_steps(context.new_page(), clean_url, load_steps("steps_taskboard_all.json"),
                            tmp_path / "run", DEFAULT_IGNORE_PATHS)
    context.close()
    signals = signals_for_run(mission_run, clean_url)
    assert signals == []  # T3b already proved this; the base this test builds on

    j = judgement()  # a well-behaved judge: nothing wrong here
    candidates, _replays = replay_mission(browser, clean_url, "/__reset", mission_run, signals, j,
                                          replays=2, out_dir=tmp_path / "verify", ignore_paths=DEFAULT_IGNORE_PATHS)
    assert candidates == []
    items = build_mission_items(MISSION, mission_run, candidates, j, fake_client(tmp_path, {}), Meter())
    assert items == []


# ---- run_test(): the whole thing, end to end -------------------------------------------------

def _plan_with(*missions: Mission) -> AppPlan:
    return AppPlan(app_type="task list", capabilities=["add", "tick", "delete"], missions=list(missions))


def test_run_test_end_to_end_confirms_the_delete_bug(server, browser, tmp_path):
    fresh_app(server)
    ref = _ref_for(browser, server, "button", "Delete Buy milk")
    script = {
        "plan": [_plan_with(MISSION).model_dump()],
        "step": [decision("click", ref, expect="the task Buy milk disappears").model_dump(),
                decision("done", None, expect="").model_dump()],
        "judge": [judgement(step_verdicts=[StepVerdict(step=1, violated=True, reason="task remains")],
                            findings=[bug(1, title="Delete fails")]).model_dump()],
        "disprove": [],  # a hard signal (http_5xx) needs no disprove call
    }
    llm = LLMClient(tmp_path, mode="fake", source=script, env_file=None)
    result = run_test(server, "live", llm, tmp_path / "out", reset_path="/__reset", browser=browser)

    assert result.report["counts"] == {CONFIRMED: 1, LIKELY: 0, IMPROVEMENT: 0, DROPPED: 0}
    assert result.report["verdict"] == "review before release"
    assert [f["title"] for f in result.findings] == ["Delete fails"]
    assert result.timed_out is False
    for name in ("report.json", "findings.json", "evidence.json", "events.jsonl"):
        assert (tmp_path / "out" / name).exists()
    events = [json.loads(l) for l in (tmp_path / "out" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0]["type"] == "run_started" and events[-1]["type"] == "run_finished"
    assert [e["t"] for e in events] == sorted(e["t"] for e in events)  # in order


def test_run_test_end_to_end_on_a_clean_build_finds_nothing(server, browser, tmp_path):
    fresh_app(server)
    clean_url = server + "/?bugs=off"
    ref = _ref_for(browser, clean_url, "button", "Delete Buy milk")
    script = {
        "plan": [_plan_with(MISSION).model_dump()],
        "step": [decision("click", ref, expect="the task disappears").model_dump(),
                decision("done", None, expect="").model_dump()],
        "judge": [judgement().model_dump()],
    }
    llm = LLMClient(tmp_path, mode="fake", source=script, env_file=None)
    result = run_test(clean_url, "live", llm, tmp_path / "out", reset_path="/__reset", browser=browser)
    assert result.report["counts"] == {CONFIRMED: 0, LIKELY: 0, IMPROVEMENT: 0, DROPPED: 0}
    assert result.report["verdict"] == "no confirmed issues"


def test_run_test_trims_missions_to_the_profile_maximum(server, browser, tmp_path):
    fresh_app(server)
    plan = _plan_with(*(Mission(id=f"m{i}", goal=f"g{i}", category="core_flow", priority="low", why="w")
                        for i in range(1, 4)))
    tiny = Profile("tiny", max_missions=1, max_total_steps=10, max_steps_per_mission=6, wall_clock_s=30, replays=1)
    script = {"plan": [plan.model_dump()], "step": [decision("done", None, expect="").model_dump()]}
    llm = LLMClient(tmp_path, mode="fake", source=script, env_file=None)
    result = run_test(server, tiny, llm, tmp_path / "out", reset_path="/__reset", browser=browser)
    assert [m["id"] for m in result.missions] == ["m1"]  # m2 and m3 were never started


def test_run_test_marks_timed_out_when_the_wall_clock_is_already_spent(server, browser, tmp_path):
    fresh_app(server)
    plan = _plan_with(Mission(id="m1", goal="g", category="core_flow", priority="low", why="w"),
                      Mission(id="m2", goal="g2", category="core_flow", priority="low", why="w"))
    instant = Profile("instant", max_missions=5, max_total_steps=10, max_steps_per_mission=6,
                      wall_clock_s=0, replays=1)
    llm = LLMClient(tmp_path, mode="fake", source={"plan": [plan.model_dump()]}, env_file=None)
    result = run_test(server, instant, llm, tmp_path / "out", reset_path="/__reset", browser=browser)
    assert result.timed_out is True and result.report["timed_out"] is True
    assert result.missions == []  # the deadline was already gone before the first mission started


# ================================================================================================
# Fixes from the 2026-09-22 /review (Opus). Each test below is written to fail against the
# pre-fix code, matching the review's own reproduction, so these are regression tests, not just
# feature tests.
# ================================================================================================

# ---- Fix 1: main() used to read PROBE_LLM_MODE before .env was loaded, silently skipping the
# --yes-spend gate whenever the mode came from .env rather than the shell (the documented way to
# configure it). Verified in the review by an isolated subprocess; here as a proper regression test.

def test_main_reads_dotenv_before_the_yes_spend_gate(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("PROBE_LLM_MODE=real\nPROBE_MAX_COST_USD=0.10\n", encoding="utf-8")
    monkeypatch.setattr("probe.agent.ENV_FILE", env_file)
    monkeypatch.delenv("PROBE_LLM_MODE", raising=False)
    monkeypatch.delenv("PROBE_MAX_COST_USD", raising=False)
    try:
        with pytest.raises(SystemExit) as exc:
            main(["--url", "http://example.invalid/", "--profile", "live", "--out", str(tmp_path / "out")])
        # Before the fix this was None (the gate never saw "real"), so it fell through and tried a
        # real API call instead of exiting 2 here.
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "--yes-spend" in err and "0.10" in err  # the cap is stated before spending, per D9 item 4
    finally:
        os.environ.pop("PROBE_LLM_MODE", None)  # load_dotenv writes os.environ directly: clean up
        os.environ.pop("PROBE_MAX_COST_USD", None)


def test_main_with_yes_spend_passes_the_gate_and_reaches_llmclient(tmp_path, monkeypatch, capsys):
    """Once --yes-spend is given, main() proceeds past the gate to build the real client. Checked
    by stubbing LLMClient itself to raise unconditionally, rather than relying on ANTHROPIC_API_KEY
    being absent: this machine may have a real .env with a real key (it does, once funded), and
    this test must give the same answer either way, not depend on what happens to be on disk."""
    env_file = tmp_path / ".env"
    env_file.write_text("PROBE_LLM_MODE=real\n", encoding="utf-8")
    monkeypatch.setattr("probe.agent.ENV_FILE", env_file)
    monkeypatch.delenv("PROBE_LLM_MODE", raising=False)

    def boom(*args, **kwargs):
        raise LLMError("stub reached: the gate did not block this call")
    monkeypatch.setattr("probe.agent.LLMClient", boom)

    try:
        with pytest.raises(SystemExit) as exc:
            main(["--url", "http://example.invalid/", "--profile", "live",
                 "--out", str(tmp_path / "out"), "--yes-spend"])
        assert exc.value.code == 1  # past the gate, but something still went wrong (here: our stub)
        out, err = capsys.readouterr()
        assert "Spending real API money" in out  # the gate really was passed, not skipped
        assert "stub reached" in err
    finally:
        os.environ.pop("PROBE_LLM_MODE", None)


def test_main_wires_the_default_ledger_path_into_llmclient(tmp_path, monkeypatch):
    """Not just that default_ledger_path() itself returns the right value (tested above) - that
    main() actually passes it to LLMClient. The same class of gap as sanitize_judgement's own
    wiring test elsewhere in this file: a correct helper function nobody calls is no protection."""
    env_file = tmp_path / ".env"
    env_file.write_text("PROBE_LLM_MODE=record\n", encoding="utf-8")
    monkeypatch.setattr("probe.agent.ENV_FILE", env_file)
    monkeypatch.delenv("PROBE_LLM_MODE", raising=False)
    monkeypatch.delenv("PROBE_SPEND_LEDGER", raising=False)

    seen = {}

    def capture(out_dir, source=None, ledger_path=None, **kwargs):
        seen["ledger_path"] = ledger_path
        raise LLMError("stub reached")
    monkeypatch.setattr("probe.agent.LLMClient", capture)

    try:
        with pytest.raises(SystemExit):
            main(["--url", "http://example.invalid/", "--profile", "live",
                 "--out", str(tmp_path / "out"), "--yes-spend"])
        assert seen["ledger_path"] == "runs/spend_ledger.jsonl"
    finally:
        os.environ.pop("PROBE_LLM_MODE", None)


def test_main_leaves_ledger_path_none_for_fake_mode(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("PROBE_LLM_MODE=fake\n", encoding="utf-8")
    monkeypatch.setattr("probe.agent.ENV_FILE", env_file)
    monkeypatch.delenv("PROBE_LLM_MODE", raising=False)

    seen = {}

    def capture(out_dir, source=None, ledger_path=None, **kwargs):
        seen["ledger_path"] = ledger_path
        raise LLMError("stub reached")
    monkeypatch.setattr("probe.agent.LLMClient", capture)

    try:
        with pytest.raises(SystemExit):
            main(["--url", "http://example.invalid/", "--profile", "live", "--out", str(tmp_path / "out")])
        assert seen["ledger_path"] is None
    finally:
        os.environ.pop("PROBE_LLM_MODE", None)


# ---- Fix 2: a judge finding citing a step outside the mission's real range used to crash
# (IndexError, step too large) or silently misattribute evidence (step<=0, Python's negative
# indexing) once it reached judge_only_candidate's mission_run.results[step-1]. Reproduced in the
# review directly against replay_mission; sanitize_judgement now runs before that.

def test_sanitize_judgement_drops_out_of_range_steps_and_keeps_valid_ones():
    j = judgement(
        step_verdicts=[StepVerdict(step=1, violated=True, reason="r"),
                      StepVerdict(step=99, violated=True, reason="out of range too")],
        findings=[bug(1, title="valid"), bug(0, title="zero"), bug(5, title="too far"),
                 improvement(1, title="also valid")])
    clean, dropped = sanitize_judgement(j, valid_steps=2, mission=MISSION)
    assert [f.title for f in clean.findings] == ["valid", "also valid"]
    assert [v.step for v in clean.step_verdicts] == [1]
    assert {d["step"] for d in dropped} == {0, 5}
    assert all(d["tier"] == DROPPED and d["mission"] == MISSION.id for d in dropped)
    assert {d["title"] for d in dropped} == {"zero", "too far"}  # the judge's text is kept, for the record


def test_sanitize_judgement_keeps_findings_with_no_step():
    j = judgement(findings=[JudgedFinding(step=None, kind="improvement", title="general", severity="low",
                                          impact="i", expected="e", observed="o", suggestion="s")])
    clean, dropped = sanitize_judgement(j, valid_steps=3, mission=MISSION)
    assert len(clean.findings) == 1 and dropped == []


def test_replay_mission_no_longer_crashes_on_a_hallucinated_step(tmp_path):
    from conftest import make_evidence, make_record
    pairs = [(make_record(action="click", name="Add"), make_evidence(step=1)),
            (make_record(action="click", name="Add"), make_evidence(step=2))]
    run = make_run(pairs)  # only 2 steps were actually taken
    raw = judgement(findings=[bug(5, title="hallucinated step")])  # the judge miscounted

    clean, dropped = sanitize_judgement(raw, valid_steps=len(run.results), mission=MISSION)
    assert dropped and dropped[0]["step"] == 5

    candidates, results = replay_mission(None, "http://x/", None, run, [], clean, replays=0,
                                         out_dir=tmp_path, ignore_paths=("/__",))
    assert candidates == []  # no crash, and nothing invented in place of the dropped finding


def test_run_test_wires_sanitize_judgement_in_end_to_end(server, browser, tmp_path):
    """Not just that sanitize_judgement works standalone: that run_test() actually calls it before
    handing the judgement to replay_mission. Without that wiring this crashes with an IndexError
    (confirmed by temporarily removing the call while writing this fix) instead of finishing with
    a Dropped item for the hallucinated step."""
    fresh_app(server)
    ref = _ref_for(browser, server, "button", "Delete Buy milk")
    script = {
        "plan": [_plan_with(MISSION).model_dump()],
        "step": [decision("click", ref, expect="the task disappears").model_dump(),
                decision("done", None, expect="").model_dump()],
        # The mission only takes 1 real step, but the judge cites step 7 - hallucinated/miscounted.
        "judge": [judgement(findings=[bug(7, title="hallucinated"), bug(1, title="Delete fails")]).model_dump()],
        "disprove": [],  # step 1's http_5xx is a hard signal: no disprove call is made
    }
    llm = LLMClient(tmp_path, mode="fake", source=script, env_file=None)
    result = run_test(server, "live", llm, tmp_path / "out", reset_path="/__reset", browser=browser)

    by_title = {f["title"]: f["tier"] for f in result.findings}
    assert by_title["Delete fails"] == CONFIRMED
    assert by_title["hallucinated"] == DROPPED
    dropped = next(f for f in result.findings if f["title"] == "hallucinated")
    assert "outside this mission's actual range" in dropped["reason"]


# ---- Fix 3: judging, replay and the disprove pass used to run with no deadline check at all, so
# a mission that used up the wall clock taking actions could still add a full replay and several
# disprove calls afterward. Skipping them now can only leave a candidate at Likely, never wrongly
# promote it to Confirmed (the existing tier rule already treats replays=0 / no disprove
# conservatively) - these tests confirm the skip actually happens, not just that it would be safe.

def test_replay_mission_skips_replay_once_the_deadline_has_passed(server, browser, tmp_path):
    fresh_app(server)
    context = new_context(browser)
    mission_run = run_steps(context.new_page(), server, load_steps("steps_delete.json"),
                            tmp_path / "run", DEFAULT_IGNORE_PATHS)
    context.close()
    signals = signals_for_run(mission_run, server)
    assert signals  # the delete bug does raise a hard signal

    candidates, results = replay_mission(browser, server, "/__reset", mission_run, signals, None,
                                         replays=2, out_dir=tmp_path / "v", ignore_paths=DEFAULT_IGNORE_PATHS,
                                         deadline=time.monotonic() - 1)
    assert results == []  # no replay was attempted
    assert candidates and candidates[0].replays == 0
    assert classify(candidates[0]) != CONFIRMED  # unreplayed, so it cannot be Confirmed


def test_build_mission_items_skips_disprove_once_the_deadline_has_passed(tmp_path):
    client = fake_client(tmp_path, {})  # disprove has no scripted answer: calling it would raise
    c = candidate(signal("overflow", 2))  # fully reproduced contextual signal: would normally trigger disprove
    items = build_mission_items(MISSION, make_run([]), [c], judgement(), client, Meter(),
                                deadline=time.monotonic() - 1)
    assert items[0]["tier"] == LIKELY  # no disprove call was made, so it stays short of Confirmed


def test_run_test_still_produces_a_report_when_time_runs_out_during_verification(server, browser, tmp_path):
    """A mission that finishes its actions with no time left must still report what it found,
    not skip judging/replay/disprove silently or crash - it should just report less confidently."""
    fresh_app(server)
    ref = _ref_for(browser, server, "button", "Delete Buy milk")
    tight = Profile("tight", max_missions=1, max_total_steps=10, max_steps_per_mission=6,
                    wall_clock_s=0.01, replays=2)
    script = {
        "plan": [_plan_with(MISSION).model_dump()],
        "step": [decision("click", ref, expect="the task disappears").model_dump(),
                decision("done", None, expect="").model_dump()],
    }
    llm = LLMClient(tmp_path, mode="fake", source=script, env_file=None)
    result = run_test(server, tight, llm, tmp_path / "out", reset_path="/__reset", browser=browser)
    assert result.timed_out is True
    assert (tmp_path / "out" / "report.json").exists()  # a report is still written, not lost
