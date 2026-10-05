"""D17 / T15-a: the labelled API-outage fallback, and the plan facts the stage strategy rests on.

Zero cost. Nothing here calls the API: the fallback tests run the web server in REPLAY mode (which
has no client at all; the key given to it is deliberately a bogus one, so a stray real call would
fail loudly), and the corpus tests only read recordings.

The replay tests use the COMMITTED fixture (D18: `demo_fallback/fixture/`, pinned by hash) and FAIL,
not skip, if it is missing - a fallback that silently vanishes is the failure this exists to catch.
The corpus tests and the byte-identity test read `runs/`, which is git-ignored, so on a machine (or a
fresh clone) without the recordings they skip.

What the fallback is: rehearsal 5 (`runs/web_c5333263`), the clean post-fix run - completed, the
delete bug Confirmed, the repeat guard's log line, Confirmed card first. Its recording replays only
if the replay server is told to run all three missions: the server default is ONE (see
web/server.py run_profile), and a one-mission replay of this recording runs `m1` only, which finds
nothing - a misleading fallback. Test 2 pins that, so nobody "simplifies" the variable away.

Numbers pinned by the corpus tests were derived by the tests themselves from the recordings. D17's
first version said position 3 in 19 / position 4 in 7 and 15 of 16 delete missions retried; the
recordings said 20 / 6 and 14 of 15 (a D17 amendment), and with the owner's runs 7, 8 and 9 added (D23) 20 / 9 of 29
plans, still 14 of 15 retried. If they ever differ again that is a
FINDING: report it, do not retune the numbers so they agree.
"""
import json
import shutil
from pathlib import Path

import pytest

from conftest import fresh_app
from test_web_ui import start_web

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
LEDGER = RUNS / "spend_ledger.jsonl"
FALLBACK = ROOT / "demo_fallback" / "fixture" / "rehearsal5_llm_record.jsonl"
ORIGINAL = RUNS / "web_c5333263" / "llm_record.jsonl"
PINNED_SHA256 = "bda600a35bdf1c475eebf2ae41a92db9e20e187dc037b7f0e047695bfbcd3067"
GUARD_LINE = "Finished: m3 (1 found) - stopped after the same action failed the same way twice (steps 1 and 2)"
BOGUS_KEY = "sk-ant-not-a-real-key-replay-must-never-use-it"

PLAN_BATCHES = ("eval_2026-09-22", "eval_2026-09-23_n3", "eval_2026-09-23_canary")
SINGLE_RUNS = ("first_real_2026-09-22", "web_e9ab64af", "web_44bd99e2", "web_2690feac",
               "web_d9c0496c", "web_c5333263", "web_ef484e45",
               "web_c9331033", "web_e63befc0", "web_6b6833cf")   # D23: the owner's own live runs 7 and 8 (25 Sep) and 9 (5 Oct)


def _ledger_lines() -> int:
    return len(LEDGER.read_text(encoding="utf-8").splitlines()) if LEDGER.exists() else 0


def _replay_env(server: str, max_missions: str) -> dict:
    return {"PROBE_LLM_MODE": "replay", "PROBE_LLM_SOURCE": str(FALLBACK), "PROBE_REPLAY_URL": server,
            "PROBE_REPLAY_MAX_MISSIONS": max_missions, "ANTHROPIC_API_KEY": BOGUS_KEY}


def _replay_through_the_page(browser, server: str, max_missions: str) -> dict:
    """Drive the served page exactly as a presenter would (Advanced -> reset path -> Run) against the
    replay server, and return what was on screen plus what the API says. Removes the run folder the
    server created, and checks nothing was added to the spend ledger."""
    assert FALLBACK.exists(), "the committed fallback fixture is missing (D18)"
    fresh_app(server)
    before_dirs, before_ledger = {p.name for p in RUNS.glob("web_*")}, _ledger_lines()
    try:
        with start_web(_replay_env(server, max_missions)) as base:
            context = browser.new_context()
            page = context.new_page()
            page.goto(base)
            page.wait_for_selector("#banner:not([hidden])")
            banner = page.locator("#banner").inner_text()
            page.locator("details.advanced summary").click()
            page.locator("#resetPath").fill("/__reset")
            page.locator("#runBtn").click()
            page.wait_for_selector("#report:not([hidden])", timeout=60000)
            page.wait_for_timeout(500)
            import urllib.request
            status = json.loads(urllib.request.urlopen(base + "/api/status", timeout=5).read().decode())
            seen = {"banner": banner, "status": status,
                    "status_line_visible": page.locator("#statusLine").is_visible(),
                    "log": page.locator("#log").inner_text(),
                    "report": " ".join(page.locator("#report").inner_text().split()),
                    "badges": [b.strip().lower() for b in page.locator(".finding .badge").all_inner_texts()]}
            context.close()
    finally:
        for p in RUNS.glob("web_*"):
            if p.name not in before_dirs:
                shutil.rmtree(p, ignore_errors=True)
    assert _ledger_lines() == before_ledger, "a replay must never add a line to the spend ledger"
    return seen


def test_the_labelled_fallback_replays_rehearsal_5_through_the_real_page(server, browser):
    seen = _replay_through_the_page(browser, server, max_missions="3")
    assert "REPLAY" in seen["banner"] and "no API call" in seen["banner"]
    st = seen["status"]
    assert st["last_run_status"] == "completed" and seen["status_line_visible"] is False
    assert st["last_report"]["counts"] == {"Confirmed": 1, "Likely": 2, "Improvement": 0, "Dropped": 0}
    assert seen["badges"] == ["confirmed", "likely", "likely"]          # Confirmed card first
    assert GUARD_LINE in seen["log"]                                     # the repeat guard, on m3
    assert st["last_report"]["llm_calls"] == 12 and "12 model call(s)" in seen["report"]
    assert "~$0.0000" in seen["report"]                                  # a replay costs nothing
    assert st["last_partial_reason"] is None


def test_without_the_three_mission_variable_the_fallback_replays_only_m1(server, browser):
    """Why PROBE_REPLAY_MAX_MISSIONS=3 is mandatory: the server default is one mission, and one mission
    of this recording finds nothing - a fallback that would silently show "no confirmed issues"."""
    seen = _replay_through_the_page(browser, server, max_missions="")
    assert "Finished: m1" in seen["log"] and "Finished: m2" not in seen["log"] and "Finished: m3" not in seen["log"]
    assert seen["status"]["last_report"]["counts"]["Confirmed"] == 0
    assert seen["badges"] != ["confirmed", "likely", "likely"]


# ---- the committed fixture itself (D18): pinned, clean, and the real run's own record ----------------

def test_the_committed_fixture_is_pinned_and_clean():
    import hashlib
    import re
    raw = FALLBACK.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PINNED_SHA256   # -text in .gitattributes keeps this true on a clone
    text = raw.decode("utf-8")
    lines = [l for l in text.splitlines() if l.strip()]
    records = [json.loads(l) for l in lines]
    assert len(records) == 12
    assert all(set(r) == {"role", "model", "system", "user", "answer"} for r in records)
    roles = [r["role"] for r in records]
    assert (roles.count("plan"), roles.count("step"), roles.count("judge")) == (1, 8, 3)
    for needle in ("sk-ant", "ANTHROPIC_API_KEY", ".env"):
        assert needle not in text, needle
    for pattern in (r"sk-[A-Za-z0-9]{8}", r"[\w.+-]+@[\w-]+\.[\w.]+", r"(?i)users[\\/]"):
        assert re.search(pattern, text) is None, pattern
    for r in records:
        for field in ("system", "user"):
            assert re.search(r"[A-Za-z]:\\[A-Za-z]", r[field]) is None, "a Windows path in a prompt"
    assert all(u.startswith("http://127.0.0.1:8765/") for u in re.findall(r"https?://\S+", text))


def test_the_fixture_is_byte_identical_to_the_run_it_came_from():
    if not ORIGINAL.exists():
        pytest.skip("the original run is machine-local (runs/ is git-ignored)")
    assert FALLBACK.read_bytes() == ORIGINAL.read_bytes()


# ---- the plan facts: how often does the live profile's three-mission cut drop the delete check? ----

def _plan_dirs() -> list:
    dirs = []
    for batch in PLAN_BATCHES:
        dirs += sorted(p.parent for p in (RUNS / batch).glob("*/llm_record.jsonl"))
    dirs += [RUNS / name for name in SINGLE_RUNS if (RUNS / name / "llm_record.jsonl").exists()]
    return dirs


def _first_plan(run_dir: Path) -> dict:
    for line in (run_dir / "llm_record.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            if rec.get("role") == "plan":
                return rec["answer"] if isinstance(rec["answer"], dict) else json.loads(rec["answer"])
    raise AssertionError(f"no plan answer in {run_dir}")


@pytest.fixture(scope="module")
def recordings():
    if not (RUNS / "eval_2026-09-22").is_dir():
        pytest.skip("the recorded runs are machine-local (runs/ is git-ignored)")
    return _plan_dirs()


def test_the_planner_proposed_delete_in_every_plan_and_the_live_cut_drops_it_in_nine_of_29(recordings):
    """The AI proposed a delete check in 29 of 29 recorded plans: at list position 3 in 20 (kept by the
    live profile's `plan.missions[:3]`) and at position 4 in 9 (dropped by it). Rehearsal 6 and the owner's
    runs 7, 8 and 9 are four of the 9."""
    assert len(recordings) == 29
    positions = {}
    for run_dir in recordings:
        missions = _first_plan(run_dir)["missions"]
        assert len(missions) == 5, f"{run_dir.name}: {len(missions)} missions"
        hits = [i for i, m in enumerate(missions, start=1) if "delet" in m["goal"].lower()]
        assert hits, f"{run_dir.name}: no delete mission proposed"
        assert missions[hits[0] - 1]["id"] == f"m{hits[0]}"             # list order and ids agree
        positions[hits[0]] = positions.get(hits[0], 0) + 1
    assert positions == {3: 20, 4: 9}, positions
    six = _first_plan(RUNS / "web_ef484e45")["missions"]
    assert "delet" in six[3]["goal"].lower() and all("delet" not in m["goal"].lower() for m in six[:3])


def test_on_the_seeded_build_the_model_retried_a_failing_delete_in_14_of_15_missions(recordings):
    """Missions where a Delete click got a 500 at least once: 15. The model clicked again in 14; the one
    exception is rehearsal 4, where it said `stuck` after a single failure (so the repeat guard never had
    a second failure to act on). Derived from the recorded step evidence, not from the model's text."""
    delete_missions = []
    for run_dir in recordings:
        path = run_dir / "evidence.json"
        if not path.exists():
            continue
        for mission_id, mission in sorted(json.loads(path.read_text(encoding="utf-8")).items()):
            clicks = [s for s in mission["steps"]
                      if s["action"] == "click" and (s["locator"] or {}).get("name", "").startswith("Delete")]
            failed = [s for s in clicks if any(r["method"] == "DELETE" and (r["status"] or 0) >= 500
                                               for r in s["requests"])]
            if failed:
                delete_missions.append((run_dir.name, mission_id, len(clicks)))
    assert len(delete_missions) == 15, delete_missions
    assert sum(1 for _r, _m, clicks in delete_missions if clicks >= 2) == 14
    assert [(r, m) for r, m, clicks in delete_missions if clicks == 1] == [("web_d9c0496c", "m3")]
