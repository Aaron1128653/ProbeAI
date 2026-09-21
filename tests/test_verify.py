"""Replay (probe/verify.py): fresh contexts, reset, and what counts as "reproduced"."""
import pytest

from conftest import http, make_record
from probe.browser import new_context
from probe.executor import Step, run_steps
from probe.oracles import Signal
from probe.verify import ReplayResult, replay, reproduced, steps_from_records

CLEAN = "/?bugs=off"
DELETE_STEP = Step("click", {"role": "button", "name": "Delete Buy milk"})


def signal(kind="http_5xx", step=1, key="DELETE /api/tasks/{id} 500") -> Signal:
    return Signal(kind, "hard", step, "detail", key)


def result(n: int, *signals: Signal) -> ReplayResult:
    return ReplayResult(n, [], list(signals))


# ---- reproduced() -------------------------------------------------------------

def test_reproduced_counts_replays_with_the_same_kind_key_and_step():
    original = signal()
    assert reproduced(original, [result(1, signal()), result(2, signal())]) == 2
    assert reproduced(original, [result(1, signal()), result(2)]) == 1
    assert reproduced(original, []) == 0


def test_reproduced_needs_the_same_kind_the_same_key_and_the_same_step():
    original = signal()
    other_key = signal(key="DELETE /api/tasks/{id} 404")
    other_step = signal(step=2)
    other_kind = signal(kind="http_4xx")
    assert reproduced(original, [result(1, other_key)]) == 0
    assert reproduced(original, [result(1, other_step)]) == 0
    assert reproduced(original, [result(1, other_kind)]) == 0
    assert reproduced(original, [result(1, other_key, other_step, other_kind, signal())]) == 1


def test_a_replay_that_raises_the_signal_twice_still_counts_once():
    assert reproduced(signal(), [result(1, signal(), signal())]) == 1


# ---- steps_from_records() --------------------------------------------------------

def test_recorded_steps_are_rebuilt_from_their_locators_not_their_refs():
    record = make_record("type", "textbox", "New task", nth=1, text="hello")
    record.ref = "e9"  # what the agent picked; it dies with the page
    [step] = steps_from_records([record])
    assert step == Step("type", {"role": "textbox", "name": "New task", "nth": 1}, "hello", None)

    unlocatable = make_record()
    unlocatable.locator = None
    assert steps_from_records([unlocatable])[0].target == {}


# ---- replay() with a real browser ---------------------------------------------------

def test_replaying_the_seeded_delete_bug_in_fresh_contexts_reproduces_it_2_of_2(server, browser, tmp_path):
    results = replay(browser, server + "/", [DELETE_STEP], reset_path="/__reset", replays=2, out_dir=tmp_path)

    assert [r.replay for r in results] == [1, 2]
    original = signal()
    assert reproduced(original, results) == 2
    for n in (1, 2):
        assert (tmp_path / f"replay_{n}" / "step_01_after.png").stat().st_size > 0


def test_replay_starts_from_the_reset_state_and_a_failed_reset_stops_the_run(server, browser, tmp_path):
    url = server + CLEAN
    http("POST", server + "/__reset")
    context = new_context(browser)
    run_steps(context.new_page(), url, [DELETE_STEP], tmp_path / "first")  # deletes "Buy milk" for good
    context.close()

    [stale] = replay(browser, url, [DELETE_STEP], replays=1, out_dir=tmp_path / "a")  # no reset
    assert "not found" in stale.evidence[0].error  # the first run's changes are still there

    [fresh] = replay(browser, url, [DELETE_STEP], reset_path="/__reset", replays=1, out_dir=tmp_path / "b")
    assert fresh.evidence[0].error is None
    assert [r["status"] for r in fresh.evidence[0].requests if r["method"] == "DELETE"] == [200]

    with pytest.raises(RuntimeError, match="reset"):
        replay(browser, url, [DELETE_STEP], reset_path="/no-such-path", replays=1, out_dir=tmp_path / "c")
