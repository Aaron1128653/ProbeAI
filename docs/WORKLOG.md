# Work log

Newest day first. Updated after every finished task (see CLAUDE.md working rules). `/wrapup` writes the final entry of a session.

## 2026-09-21

**Done**
- Read brief and requirements; reviewed CV and PhD documents to fix constraints (Python-strong, must defend all code).
- Researched existing work: Playwright Test Agents, autonomous-test-agent, qa-agent, WebProber paper, Stagehand Python SDK (`docs/RESEARCH.md`).
- Recorded decisions D1-D5 in `docs/DECISIONS.md`: mission-based pipeline, Python stack without Stagehand, verify-before-report tiers, seeded-bug evaluation protocol, process rules.
- Set up project files: `CLAUDE.md`, `/decide`, `/build`, `/review`, `/wrapup`, `builder` agent, git.
- T1 done: demo app "TaskBoard" (`demo_app/server.py`, `static/`, `ground_truth.json`) with seeded issues S1-S6, `?bugs=off` clean build, `/__trigger_log`, `/__trigger`, `/__reset`. `.venv` created (fastapi, uvicorn, playwright, pytest pinned in `requirements.txt`), Playwright chromium installed, `.env.example` added.

**Evidence**
- `python -m pip install --dry-run playwright anthropic fastapi uvicorn[standard]` resolves on Python 3.14 (nothing installed yet).
- T1: `pytest -q` -> `15 passed in 11.57s` (6 buggy-build tests: S1-S6 each appear in the trigger log; 8 clean-build tests: delete, reopen, blank rejected with message, long titles wrap without overflow, duplicate shows message, counter correct, add/filters, page loads with `bugs=off`; 1 reset test). Sanity check: the 8 clean tests pointed at the buggy build all fail.
- Measured: in the buggy build the page only overflows horizontally from about 90 characters at a 1280 px viewport, although S4 is logged from 61 characters.

**Next**
- T2 harness: page-state extractor with numbered elements, step executor by index, evidence recorder, safety policy (see `docs/DECISIONS.md`).

**Blockers / decisions needed**
- API key not funded yet. Needed before the first LLM-dependent task (~2026-09-25). User will top up when the app is ready to use it.
