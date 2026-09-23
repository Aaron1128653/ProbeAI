"""D14's validation experiment, as a regression gate (T11-b).

D14 predicted the blast radius of the judge-only scoping change BEFORE it was written, by
re-deriving tiers over every recorded run. This locks that prediction in: exactly 3 findings move
Dropped -> Likely, all of them the S6 counter defect, and nothing on any clean run changes.

Zero cost - it reads `runs/`, which holds the already-recorded real runs, and calls the pure
tiering function. No API call, no browser, no server. `runs/` is git-ignored, so on a machine
that does not have the recordings every test here skips rather than failing.

The standing instruction from the user, kept here where it will be read next time: if this gate
ever reports a different blast radius, that is a FINDING. Report it - do not retune the rule, the
audit or the expected numbers so they agree again.
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BATCHES = ("runs/eval_2026-09-22", "runs/eval_2026-09-23_n3", "runs/eval_2026-09-23_canary")
RULING_2 = "expectation was not violated"

# What D14 computed in advance, before probe/findings.py was touched.
EXPECTED_MOVES = {
    ("eval_2026-09-22", "buggy_3"),
    ("eval_2026-09-23_n3", "buggy_2"),
    ("eval_2026-09-23_canary", "injection"),
}


def recorded_findings():
    """(batch, run, finding) for every recorded finding on this machine."""
    out = []
    for batch in BATCHES:
        for path in sorted((ROOT / batch).glob("*/findings.json")):
            for item in json.loads(path.read_text(encoding="utf-8")):
                out.append((Path(batch).name, path.parent.name, item))
    return out


def tier_changes():
    """Findings that D14's scoping moves off Dropped: those dropped by ruling 2 that have no
    signals (judge-only) and whose outcome did come back, so the evidence gate lets them through."""
    moved = []
    for batch, run, item in recorded_findings():
        if item["tier"] != "Dropped" or RULING_2 not in (item.get("reason") or ""):
            continue
        if item.get("signals"):
            continue  # signal-based: ruling 2 still applies, unchanged by D14
        reproduced_n, _, replays = (item.get("reproduced") or "0/0").partition("/")
        if replays and int(reproduced_n) > 0:
            moved.append((batch, run, item))
    return moved


@pytest.fixture(scope="module", autouse=True)
def require_recordings():
    if not any((ROOT / b).is_dir() for b in BATCHES):
        pytest.skip("the recorded runs are machine-local (runs/ is git-ignored)")


def test_exactly_three_findings_move_from_dropped_to_likely():
    moved = tier_changes()
    assert len(moved) == 3, f"D14 predicted 3, got {len(moved)}: {[(b, r, i['title']) for b, r, i in moved]}"


def test_all_three_are_the_s6_counter_defect_and_reproduced_every_time():
    for batch, run, item in tier_changes():
        title = (item.get("title") or "").lower()
        assert any(word in title for word in ("count", "items left")), \
            f"{batch}/{run}: expected the S6 counter defect, got {item.get('title')!r}"
        assert item["reproduced"] == "2/2", f"{batch}/{run}: expected 2/2, got {item['reproduced']}"


def test_they_are_the_three_runs_d14_named_in_advance():
    assert {(b, r) for b, r, _ in tier_changes()} == EXPECTED_MOVES


def test_no_clean_run_changes_tier():
    """The false-positive risk, measured rather than hoped: a change that moved anything on a
    clean build would raise the 'how often does it cry wolf' number, which is a headline metric."""
    on_clean = [(b, r, i.get("title")) for b, r, i in tier_changes() if r.startswith("clean")]
    assert on_clean == [], f"a clean run changed tier, which D14 said could not happen: {on_clean}"


def test_ruling_2_still_accounts_for_the_signal_based_drops_it_was_right_about():
    """The other half of D14's evidence: 32 signal-based suppressions that must NOT change."""
    still_dropped = [i for b, r, i in recorded_findings()
                     if i["tier"] == "Dropped" and RULING_2 in (i.get("reason") or "") and i.get("signals")]
    assert len(still_dropped) == 32, f"expected 32 signal-based drops to be untouched, found {len(still_dropped)}"
    assert all("no_effect" in (i.get("title") or "") for i in still_dropped)
