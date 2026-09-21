# Audit guide (for an independent reviewer)

Project: ProbeAI, an AI web-app tester built as a take-home engineering exercise. Written 2026-09-21 (end of day 1), to be updated as work proceeds. Nothing here asks you to trust the author: every claim has a command or a file you can check.

## 1. How the work was produced
- The owner (Yuanhang Wang) sets goals and approves direction; they are a mechanical-engineering PhD student, not a professional software engineer.
- **Claude Opus** made design decisions and wrote them into `docs/DECISIONS.md` (D1-D9), `docs/PROMPTS.md`, `docs/RESEARCH.md`.
- **Claude Sonnet** (the `builder` subagent, `.claude/agents/builder.md`) wrote the code from those specs and reported back. Its reports are self-reported.
- Opus **re-ran the test suite and the scripted pipeline itself** before accepting each task (see section 4).
- Important limit: the same model family wrote and reviewed the work. An outside reader should re-run things and read the code, not just this file.
- The git history is the trace: one commit per task, decisions committed before the code that implements them.

## 2. Reading order
1. `CLAUDE.md` (standing rules, direction) 2. `docs/DECISIONS.md` (why; later entries amend earlier ones, see its header) 3. `docs/PROMPTS.md` 4. `docs/RESEARCH.md` (sources, what was and was not verified) 5. `docs/WORKLOG.md` (what happened, with evidence per task) 6. `docs/QA_NOTES.md` (plain-language explanation per module) 7. code: `demo_app/`, `probe/`, `tests/`, `examples/`.

## 3. Commit map
| Commit | What | Tests after |
|---|---|---|
| 315b392 | Setup, D1-D5, research, Claude commands | - |
| 1eabe27 | T1 TaskBoard demo app, six seeded issues, clean mode, trigger log | 15 |
| b860ce8 | Decisions amended after T1 and market check (D6) | - |
| 662cd54 | T1 follow-ups: S4 logged only when overflow shows; `signature_any` | 17 |
| ff9c830 | D7: ARIA-snapshot state, hard vs contextual signals, injection defence | - |
| 804c2b2 | T2 browser harness (state, executor, evidence, safety) | 39 |
| d844475 | Rulings after T2 | - |
| a50e95d | T2 fixes (password guard, regex blocklist, S2 signal) | 42 (subset) |
| b3969c4 | T3 oracles, replay verifier, tiers, scripted pipeline | 90 |
| a4c4ef2 | D8 + PROMPTS: rulings after T3, LLM contracts, run loop | - |
| eae5490 | T3b: apply D8 rulings | 112 |
| 56df798 | T4: LLM client, schemas, prompts, fake/record/replay | 167 |
(later commits: D9 budget policy and this guide, then T5 onward)

## 4. Claims and how to check them
Environment: Windows, Python 3.14, `.venv` at the repo root (`requirements.txt` pins versions; Playwright Chromium installed).
- **"167 tests pass"** -> `.venv\Scripts\pytest.exe -q` (about 80 s). Opus re-ran it after T2, T3 and T4 (39 / 90 / 167).
- **"Buggy build: S1 Confirmed, S2/S4/S5 Likely; clean build: nothing"** -> start `python -m uvicorn demo_app.server:app --port 8765`, then `python -m probe.run_script examples/steps_taskboard_all.json --url http://127.0.0.1:8765/ --out runs/check --reset-path /__reset` and the same with `--url "http://127.0.0.1:8765/?bugs=off"`. (Git Bash: `export MSYS_NO_PATHCONV=1` first.) Opus ran the buggy and clean case itself after T3; the clean case then showed two false Likely, which D8 fixed and T3b removed.
- **"Mutation checks caught 11/11 and 21/21"** -> self-reported by Sonnet in `docs/WORKLOG.md`. Not re-run by Opus. To spot-check: break one rule (for example make `http_4xx` ignore the silent-page condition in `probe/oracles.py`) and confirm a test fails.
- **"No real API call was made"** -> the LLM tests (`tests/test_llm.py`) fail on any socket open via an autouse fixture (the browser tests legitimately use localhost); `runs/spend_ledger.jsonl` does not exist yet. Check that `.env` is git-ignored and absent from history: `git log --all -- .env`.
- **Decision traceability** -> each D-entry lists options rejected and a Q&A answer; grep the code for the behaviour named in the entry.

## 5. Corrections and problems found during the work (transparency)
1. A ChatGPT-sourced claim that Momentic's headline was "Point Mo at your app, get real bugs back" did not match Momentic's homepage as fetched; recorded in `docs/RESEARCH.md`. Funding claims and all Reddit claims are unverified (Reddit is blocked for the search tool).
2. The T1 brief and D4 disagreed on what S5 and S6 were; the code followed the brief, D4 was corrected.
3. D1's first `no_effect` definition was too weak (client-only state changes) -> redefined in D7.
4. T2 was redesigned mid-task after D7 (custom JS extractor replaced by Playwright's ARIA snapshot); the builder adapted.
5. After T3 the clean build produced two false "Likely" findings (correct 422/409 validation). Found by Opus's re-run, not by the builder's acceptance script. Fixed by D8.
6. Process mistake by Opus: a broad `taskkill /IM python.exe` to stop a test server, which ends every Python process on the machine. Disclosed to the owner; the rule against it is in `CLAUDE.md`.
7. The builder's `run_and_verify` has no judge yet and replays only up to the last signal step; T5 must extend that (D9).

## 6. What is NOT verified yet (as of end of day 1)
- **No model has been called.** Prompts (`docs/PROMPTS.md`), the schemas' behaviour on the real API, `thinking` disabled on Haiku 4.5, structured outputs on Haiku 4.5, and every cost figure are untested. Prices are from an SDK reference table cached 2026-06-24.
- **There is no end-to-end AI run yet.** The agent loop (T5), UI and report (T6), evaluation (T7) do not exist. The task's requirement "runs live with AI in front of the panel" is therefore not yet met.
- **The injection defence is designed (D7) but untested** (`?inject=on` canary is planned in T7).
- **Generality is unproven:** only the author-built TaskBoard has been exercised, and the author knows its bugs. A third-party demo site is planned for T7.
- Replay assumes the target can be reset (TaskBoard exposes `/__reset`); arbitrary apps with server-side state may not replay cleanly.

## 7. Questions worth asking the author
- Is a "Confirmed" item defensible, and does an independent replay really remove agent mistakes? (D3, D7 point 3: the disprove pass exists for that; it is not built until T5.)
- Are the evaluation numbers fair when the bugs are author-chosen and the prompts were written knowing the demo app? (Prompts are generic by design; check `docs/PROMPTS.md` for app-specific words. The plan has a clean-build control and a third-party app.)
- What leaves the machine? Page text and accessibility trees go to the Anthropic API; screenshots stay local (D2).
- Is the safety model adequate? (Staging assumption, same-origin only, blocklist, password guard, model returns only a schema object; see D1, D7 point 4, `probe/safety.py`.)
- Budget: 20 USD funded, 10 USD planned cap with layered enforcement (D9).
