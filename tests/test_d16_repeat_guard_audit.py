"""D16's zero-cost counterfactual audit, as a regression gate (T14-c).

D16 predicted the repeat guard's blast radius BEFORE any code was written by re-running the real
oracles over every recorded real run and applying the rule mechanically. This locks that prediction
in: the guard fires on exactly 13 missions, every one of them the seeded S1 delete, never on a clean
build, and everything it would have cut was the identical failing click again.

How it is derived (the same way D16 did): per-step signals come from running the real
`probe.oracles.detect_signals` on each run's recorded `evidence.json` (step evidence AND page states
are stored there), not from the prompt text the step model happened to be shown. Then
`repeated_hard_failure` is applied step by step, exactly as `run_mission` does.

The corpus is a FIXED list (the recorded evaluation batches, the first real run, and the three real
web rehearsals), not "whatever is under runs/": a future post-fix rehearsal recording would itself
contain a guard firing and would silently change the audited number.

Limit, stated plainly: this measures what the recorded traces show would have been cut. A real re-run
diverges after the cut (the model is never asked again), so it is evidence that the rule removes only
redundant retries in everything recorded - not proof about future runs.

Zero cost: reads `runs/`, calls pure functions. No API call, no browser, no server. `runs/` is
git-ignored, so on a machine without the recordings every test here skips rather than failing.

The standing instruction, kept here where it will be read next time: if this gate ever reports
different numbers, that is a FINDING. Report it - do not retune the rule, the audit or the expected
numbers so they agree again.
"""
import json
from pathlib import Path

import pytest

from probe.agent import repeated_hard_failure
from probe.evidence import StepEvidence
from probe.executor import StepRecord
from probe.oracles import HARD_KINDS, detect_signals
from probe.state import PageState, RefEntry

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"

BATCHES = ("eval_2026-09-22", "eval_2026-09-23_n3", "eval_2026-09-23_canary")
SINGLE_RUNS = ("first_real_2026-09-22", "web_e9ab64af", "web_44bd99e2", "web_2690feac")
REHEARSALS = ("web_e9ab64af", "web_44bd99e2", "web_2690feac")
DELETE_500 = "DELETE /api/tasks/{id} 500"

# What D16 computed in advance, before probe/agent.py was touched.
EXPECTED_RUNS = 22
EXPECTED_MISSIONS = 100
EXPECTED_FIRINGS = 13
EXPECTED_CUT_STEPS = 26


def corpus() -> list[Path]:
    """Every run directory of the fixed corpus that has a recorded evidence.json on this machine."""
    dirs = []
    for batch in BATCHES:
        dirs += sorted(p.parent for p in (RUNS / batch).glob("*/evidence.json"))
    dirs += [RUNS / name for name in SINGLE_RUNS if (RUNS / name / "evidence.json").exists()]
    return dirs


def _state(d: dict) -> PageState:
    return PageState(**{**d, "refs": [RefEntry(**r) for r in d["refs"]]})


def _step_pairs(mission: dict) -> list:
    """(StepRecord, [Signal]) for every executed step of one recorded mission, oracles re-run on the
    recorded evidence."""
    base = mission["url"]
    states = [_state(s) for s in mission["states"]]
    pairs = []
    for i, raw in enumerate(mission["steps"]):
        ev = StepEvidence(**raw)
        record = StepRecord(action=ev.action, ref=None, locator=ev.locator, text=ev.text, expect=ev.expect,
                            error=ev.error, blocked=ev.blocked)
        pairs.append((record, detect_signals(ev, record, states[i], states[i + 1], base)))
    return pairs


def audit() -> dict:
    """Apply the guard step by step, as run_mission does, to every mission in the corpus."""
    firings, missions, runs = [], 0, 0
    for run_dir in corpus():
        runs += 1
        findings = {}
        fpath = run_dir / "findings.json"
        if fpath.exists():
            for item in json.loads(fpath.read_text(encoding="utf-8")):
                findings.setdefault(item.get("mission"), []).append(item)
        evidence = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
        for mission_id, mission in sorted(evidence.items()):
            missions += 1
            pairs = _step_pairs(mission)
            for n in range(1, len(pairs) + 1):
                guard = repeated_hard_failure(pairs[:n])
                if guard:
                    cut = pairs[n:]
                    firings.append({"run": run_dir.relative_to(RUNS).as_posix(), "name": run_dir.name, "mission": mission_id, "guard": guard, "fired_at": n,
                                    "cut": cut, "kept": pairs[:n], "findings": findings.get(mission_id, [])})
                    break
    return {"runs": runs, "missions": missions, "firings": firings}


@pytest.fixture(scope="module")
def result():
    if not (RUNS / "eval_2026-09-22").is_dir():
        pytest.skip("the recorded runs are machine-local (runs/ is git-ignored)")
    return audit()


def test_the_corpus_is_the_one_d16_audited(result):
    assert (result["runs"], result["missions"]) == (EXPECTED_RUNS, EXPECTED_MISSIONS), \
        f"D16 audited {EXPECTED_RUNS} runs / {EXPECTED_MISSIONS} missions, found {result['runs']} / {result['missions']}"


def test_it_fires_on_exactly_thirteen_missions_all_the_s1_delete_at_step_two(result):
    firings = result["firings"]
    assert len(firings) == EXPECTED_FIRINGS, f"D16 predicted {EXPECTED_FIRINGS}, got {len(firings)}"
    for f in firings:
        g = f["guard"]
        assert (g["first_step"], g["repeat_step"]) == (1, 2), f
        assert g["signal_kind"] == "http_5xx" and g["signal_key"] == DELETE_500, f
        assert g["action"] == "click" and g["target"]["name"].startswith("Delete "), f


def test_it_never_fires_on_a_clean_build_or_on_the_first_real_run(result):
    wrong = [(f["run"], f["mission"]) for f in result["firings"]
             if f["name"].startswith("clean") or f["name"].startswith("first_real")]
    assert wrong == [], f"the guard fired where nothing can fail: {wrong}"


def test_everything_it_would_have_cut_is_the_identical_click_with_the_identical_signal(result):
    """The real risk of ending a mission early is throwing away a tail that mattered. On everything
    recorded, no discarded step used a different action, a different target or raised a different signal."""
    cut_total = 0
    for f in result["firings"]:
        first_record = f["kept"][0][0]
        for record, signals in f["cut"]:
            cut_total += 1
            assert (record.action, record.locator, record.text) == (first_record.action, first_record.locator,
                                                                    first_record.text), f
            hard = {(s.kind, s.key) for s in signals if s.kind in HARD_KINDS}
            assert ("http_5xx", DELETE_500) in hard and len(signals) == len(hard), f
    assert cut_total == EXPECTED_CUT_STEPS, f"D16 counted {EXPECTED_CUT_STEPS} cut steps, got {cut_total}"


def test_no_recorded_finding_cites_a_step_after_the_cut(result):
    """So no finding changes tier or existence on the recorded corpus: the tail never produced a
    candidate of its own (build_candidates already merges a signal seen at an earlier step)."""
    for f in result["firings"]:
        for item in f["findings"]:
            step = item.get("step")
            assert step is None or step <= f["fired_at"], f"{f['run']}/{f['mission']}: {item.get('title')!r} cites step {step}"


def test_the_three_rehearsals_would_all_have_been_completed(result):
    """Each rehearsal's m3 fires at step 2; m1 and m2 were done, nothing timed out, so with m3 done
    the run reads completed (compute_run_status is unchanged: done + done + done)."""
    hit = {(f["run"], f["mission"]) for f in result["firings"] if f["run"] in REHEARSALS}
    assert hit == {(r, "m3") for r in REHEARSALS}
    for run in REHEARSALS:
        others = [f for f in result["firings"] if f["run"] == run and f["mission"] != "m3"]
        assert others == []  # the guard changed nothing about m1 / m2
