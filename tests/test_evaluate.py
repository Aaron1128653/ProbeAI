"""probe/evaluate.py: T7's evaluation runner (D4, D7 point 4). Pure-function tests for the
matching/scoring logic, plus one small real-browser, fake-LLM smoke test of the whole pipeline
against the real TaskBoard (mirrors tests/test_agent.py's end-to-end style; --n stays at 1 here,
a full N=5 protocol run is what the CLI is for, not what a fast test suite needs).
"""
from conftest import fresh_app
from probe.evaluate import (add_query, check_injection_safety, evaluate_buggy, evaluate_clean,
                            evaluate_injection, finding_text, matches_any, summarize)
from probe.llm import LLMClient
from probe.schemas import AppPlan, JudgedFinding, Judgement, Mission, StepDecision, StepVerdict

MISSION = Mission(id="m1", goal="Delete an existing task", category="core_flow",
                  priority="critical", why="Users could no longer manage their list.")


def fake_client(tmp_path, script: dict) -> LLMClient:
    return LLMClient(tmp_path, mode="fake", source=script, env_file=None)


def decision(action="click", ref="e1", text=None, expect="something changes") -> dict:
    return StepDecision(action=action, ref=ref, text=text, expect=expect, reasoning="because").model_dump()


def plan_with(*missions: Mission) -> dict:
    return AppPlan(app_type="task list", capabilities=["add", "tick", "delete"], missions=list(missions)).model_dump()


def ref_for(browser, url: str, role: str, name: str, nth: int = 0) -> str:
    """Duplicated from tests/test_agent.py's own pattern (see tests/test_web_ui.py's ref_for for
    the same note): a read-only capture_state() call is the only way to know a ref before a run."""
    from probe.state import capture_state
    context = browser.new_context()
    try:
        page = context.new_page()
        page.goto(url)
        state = capture_state(page)
        entry = next(e for e in state.refs if e.role == role and e.name == name and e.nth == nth)
        return entry.ref
    finally:
        context.close()


# ---- pure functions: matching, the injection check, summarizing -------------------------------

def test_add_query_appends_with_the_right_separator():
    assert add_query("http://x/", "bugs=off") == "http://x/?bugs=off"
    assert add_query("http://x/?a=1", "bugs=off") == "http://x/?a=1&bugs=off"


def test_finding_text_concatenates_the_readable_fields():
    text = finding_text({"title": "Delete fails", "impact": "cannot remove", "expected": "",
                         "observed": "", "suggestion": "", "reason": ""})
    assert "Delete fails" in text and "cannot remove" in text


def test_finding_text_tolerates_missing_or_none_fields():
    assert finding_text({}) == " " * 5  # six fields, five separating spaces, all empty


def test_matches_any_is_case_insensitive_substring_match():
    assert matches_any("The DELETE handler is broken", ["delete", "remove"])
    assert not matches_any("everything is fine", ["delete", "remove"])


def test_check_injection_safety_passes_when_nothing_was_left_unblocked():
    evidence = {"url": "http://x/", "steps": [
        {"step": 1, "blocked": None, "locator": {"name": "Delete Buy milk"}, "url_after": "http://x/"},
        {"step": 2, "blocked": "matches blocked pattern", "locator": {"name": "Delete my account"}, "url_after": "http://x/"},
    ]}
    assert check_injection_safety(evidence) == []


def test_check_injection_safety_flags_an_unblocked_match_to_a_blocked_pattern():
    evidence = {"url": "http://x/", "steps": [
        {"step": 1, "blocked": None, "locator": {"name": "Delete my account"}, "url_after": "http://x/"},
    ]}
    problems = check_injection_safety(evidence)
    assert len(problems) == 1 and "step 1" in problems[0]


def test_check_injection_safety_flags_an_unblocked_cross_origin_navigation():
    evidence = {"url": "http://x/", "steps": [
        {"step": 1, "blocked": None, "locator": {"name": "A link"}, "url_after": "http://evil.example/"},
    ]}
    problems = check_injection_safety(evidence)
    assert len(problems) == 1 and "evil.example" in problems[0]


def test_summarize_counts_exercised_and_confirmed_per_bug_and_false_positives():
    ground_truth = [{"id": "S1", "title": "Delete fails"}]
    buggy_runs = [
        {"exercised": ["S1"], "confirmed": ["S1"], "report": {"estimated_cost_usd": 0.01, "elapsed_s": 1.0}},
        {"exercised": ["S1"], "confirmed": [], "report": {"estimated_cost_usd": 0.01, "elapsed_s": 1.0}},
    ]
    clean_runs = [{"false_positives": 0, "report": {"estimated_cost_usd": 0.01, "elapsed_s": 1.0}},
                 {"false_positives": 1, "report": {"estimated_cost_usd": 0.01, "elapsed_s": 1.0}}]
    summary = summarize(ground_truth, buggy_runs, clean_runs, injection=None, third_party=None)
    assert summary["per_bug"]["S1"] == {"title": "Delete fails", "exercised": "2/2", "confirmed": "1/2"}
    assert summary["false_positives_on_clean"] == {"per_run": [0, 1], "total": 1}
    assert summary["injection_canary"] is None and summary["third_party"] is None


# ---- real browser, fake LLM: the whole pipeline against the real TaskBoard ---------------------

def test_evaluate_buggy_matches_the_seeded_delete_bug(server, browser, tmp_path):
    fresh_app(server)
    ref = ref_for(browser, server, "button", "Delete Buy milk")
    script = {
        "plan": [plan_with(MISSION)],
        "step": [decision("click", ref, expect="the task disappears"), decision("done", None, expect="")],
        "judge": [Judgement(step_verdicts=[StepVerdict(step=1, violated=True, reason="task remains")],
                            findings=[JudgedFinding(step=1, kind="bug", title="Delete fails",
                                                    severity="high", impact="i", expected="e",
                                                    observed="o", suggestion="s")]).model_dump()],
    }
    ground_truth = [{"id": "S1", "title": "Delete fails", "signature_any": ["delete"]}]

    def make_llm(out_dir):
        return fake_client(out_dir, script)

    runs = evaluate_buggy(server, ground_truth, make_llm, browser, tmp_path / "buggy", n=1)
    assert runs[0]["exercised"] == ["S1"]
    assert runs[0]["confirmed"] == ["S1"]
    assert runs[0]["report"]["counts"]["Confirmed"] == 1


def test_evaluate_clean_finds_no_false_positives_on_the_clean_build(server, browser, tmp_path):
    fresh_app(server)
    clean_url = server + "/?bugs=off"
    ref = ref_for(browser, clean_url, "button", "Delete Buy milk")
    script = {"plan": [plan_with(MISSION)],
             "step": [decision("click", ref, expect="the task disappears"), decision("done", None, expect="")],
             "judge": [Judgement(step_verdicts=[], findings=[]).model_dump()]}

    def make_llm(out_dir):
        return fake_client(out_dir, script)

    runs = evaluate_clean(clean_url, make_llm, browser, tmp_path / "clean", n=1)
    assert runs[0]["false_positives"] == 0


def test_evaluate_injection_passes_when_the_mission_ignores_the_canary_task(server, browser, tmp_path):
    """The model, in this script, does exactly its assigned mission and never touches the
    injected task - the realistic "resisted the injection" outcome, and the only one worth
    scripting in fake mode (fake mode cannot simulate an LLM actually being fooled)."""
    fresh_app(server)
    inject_url = server + "/?inject=on"
    ref = ref_for(browser, inject_url, "button", "Delete Buy milk")
    script = {"plan": [plan_with(MISSION)],
             "step": [decision("click", ref, expect="the task disappears"), decision("done", None, expect="")],
             "judge": [Judgement(step_verdicts=[], findings=[]).model_dump()]}

    def make_llm(out_dir):
        return fake_client(out_dir, script)

    result = evaluate_injection(inject_url, make_llm, browser, tmp_path / "injection")
    assert result["passed"] is True
    assert result["problems"] == []
