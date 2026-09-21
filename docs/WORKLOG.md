# Work log

Newest day first. Updated after every finished task (see CLAUDE.md working rules). `/wrapup` writes the final entry of a session.

## 2026-09-21

**Done**
- Read brief and requirements; reviewed CV and PhD documents to fix constraints (Python-strong, must defend all code).
- Researched existing work: Playwright Test Agents, autonomous-test-agent, qa-agent, WebProber paper, Stagehand Python SDK (`docs/RESEARCH.md`).
- Recorded decisions D1-D5 in `docs/DECISIONS.md`: mission-based pipeline, Python stack without Stagehand, verify-before-report tiers, seeded-bug evaluation protocol, process rules.
- Set up project files: `CLAUDE.md`, `/decide`, `/build`, `/review`, `/wrapup`, `builder` agent, git.
- T2 fixes after review (D7 addendum): password guard reads the real `type` attribute before `type`/`press` steps (name check kept, now also covers `press`); blocklist is regexes (word gaps, inflections); plain snapshot capped at 20000 chars in the state and evidence (fingerprint still from the whole text); `ground_truth.json` S2 expected_signal = `state_not_reached`.
- T2 done: browser harness in `probe/` (`browser.py`, `state.py`, `executor.py`, `evidence.py`, `safety.py`, `run_script.py`), no LLM. Page state is Playwright's ARIA snapshot (D7): the model-facing text has `[ref=eN]` tags, each actionable ref also gets a semantic locator (role, name, nth) for replay. Dev CLI `python -m probe.run_script examples/steps_delete.json --url ... --out runs/...` writes `evidence.json` and screenshots. `pytest.ini` (pythonpath) and `tests/conftest.py` (shared server and browser fixtures) added.
- T1 done: demo app "TaskBoard" (`demo_app/server.py`, `static/`, `ground_truth.json`) with seeded issues S1-S6, `?bugs=off` clean build, `/__trigger_log`, `/__trigger`, `/__reset`. `.venv` created (fastapi, uvicorn, playwright, pytest pinned in `requirements.txt`), Playwright chromium installed, `.env.example` added.

**Evidence**
- `python -m pip install --dry-run playwright anthropic fastapi uvicorn[standard]` resolves on Python 3.14 (nothing installed yet).
- T1: `pytest -q` -> `15 passed in 11.57s` (6 buggy-build tests: S1-S6 each appear in the trigger log; 8 clean-build tests: delete, reopen, blank rejected with message, long titles wrap without overflow, duplicate shows message, counter correct, add/filters, page loads with `bugs=off`; 1 reset test). Sanity check: the 8 clean tests pointed at the buggy build all fail.
- Measured: in the buggy build the page only overflows horizontally from about 90 characters at a 1280 px viewport, although S4 was logged from 61 characters (fixed by the follow-up below).
- T1 follow-ups (owner decisions on the T1 report): S4 is now logged only when `scrollWidth > clientWidth` after rendering; `ground_truth.json` uses `signature_any` (11-12 lowercase synonyms per issue). `pytest -q` -> `17 passed in 12.66s` (new: 69-char title does not log S4; ground truth file is well formed).
- T2: `pytest -q` -> `39 passed in 27.00s` (17 T1 + 22 harness). Harness tests cover: state parsing on TaskBoard (expected refs and roles), every ref reaches the same DOM element by `aria-ref` and by role+name+nth, scripted delete on the buggy build shows `DELETE /api/tasks/<id>` status 500 and no `/__` requests, the same script on `?bugs=off` shows 200 and no failed request, a recorded step replays in a fresh context (500 again), a client-only checkbox tick changes the fingerprint and the server-undone tick (S2) does not, safety rules including password fields, step budget, dialogs recorded, blocked step not executed. Mutation checks: taking the plain snapshot after the AI one (which invalidates refs) fails 3 tests; breaking `ignore_paths` or the step budget fails the tests that guard them.
- T2 CLI on the buggy build: `DELETE http://127.0.0.1:8765/api/tasks/1 -> 500 FAILED`, console "Failed to load resource ... 500", page fingerprint unchanged, trigger log has S1. On the clean build: `DELETE ...?bugs=off -> 200`, fingerprint changed, trigger log empty.
- T2 fixes: `pytest -q tests/test_harness.py tests/test_demo_app.py` -> `42 passed in 24.53s` (new: 26 blocked / 11 allowed names incl. "Delete my account", "Uploads", "Downloading"; password field blocked by real `type` for `type` and `press`, search box still typeable; snapshot caps and fingerprint over the whole page; ground truth S2 = `state_not_reached`).
- Playwright behaviour worth remembering: any `page.aria_snapshot()` call makes the refs of an earlier `mode="ai"` snapshot stop resolving, so `capture_state` takes the plain snapshot first and the AI one last.

**Next**
- T3 oracles + replay verifier: turn evidence into hard/contextual signals (D7), replay recorded steps in a fresh context, compute tiers.

**Blockers / decisions needed**
- API key not funded yet. Needed before the first LLM-dependent task (~2026-09-25). User will top up when the app is ready to use it.
