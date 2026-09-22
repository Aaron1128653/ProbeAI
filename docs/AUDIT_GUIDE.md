# Audit guide (for an independent reviewer)

Project: ProbeAI, an AI web-app tester built as a take-home engineering exercise. Written 2026-09-21 (end of day 1), updated 2026-09-22 (day 2: T5, an independent review of it, three fixes, and the first real API call). Nothing here asks you to trust the author: every claim has a command or a file you can check.

## 1. How the work was produced
- The owner (Yuanhang Wang) sets goals and approves direction; they are a mechanical-engineering PhD student, not a professional software engineer.
- **Claude Opus** made design decisions and wrote them into `docs/DECISIONS.md` (D1-D9), `docs/PROMPTS.md`, `docs/RESEARCH.md`.
- **Claude Sonnet** wrote the code from those specs. Through T4 (commits up to `56df798`) this ran as a separate `builder` subagent (`.claude/agents/builder.md`), reporting back self-reported results that Opus then independently re-ran (see section 4). **T5-0 and T5 (`bd860d9`, `613ac45`) were different**: the `/build` command routed the same conversation to run as Sonnet directly, with no separate Opus pass at the time - the review below is exactly that missing pass, done afterward at the owner's explicit request.
- **The owner then asked for an independent `/review` before any real API money was spent.** Opus read `probe/agent.py` and `probe/llm.py` fresh (not the WORKLOG self-report), reproduced three real bugs directly (not just by inspection), and gave a prioritized fix list; the owner approved the top three, and Sonnet fixed them with regression tests, each mutation-confirmed against the pre-fix code (commit `0b4662c`). This is the pattern the project is meant to follow, and it worked as intended: a same-turn self-check had already produced 213 passing tests without ever exercising the actual `.env`-loading order, the out-of-range step path, or the missing deadline check - a second, independent, adversarial read caught all three before they could cost money or crash a live demo.
- Important limit: the same model family wrote and reviewed the work throughout. An outside reader should re-run things and read the code, not just this file.
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
| d479e4e | End of day 1: D9 budget policy and T5 spec, this guide, WORKLOG handoff | - |
| bd860d9 | T5-0: budget guard (no default LLM mode, lower per-run cap, ledger) | 174 |
| 613ac45 | T5: agent loop (missions, judge, replay, disprove, tiered report) | 213 |
| c3f4da1 | Update this guide for T5-0/T5 | - |
| 0b4662c | Fix 3 issues from an independent `/review` on T5, before any real call | 222 |

**No commit for the first real API run** (2026-09-22): it produced only files under `runs/`, which is git-ignored on purpose (it can hold real request/response content). Its outcome is recorded in `docs/DECISIONS.md`'s D9 addendum and `docs/WORKLOG.md` instead: mode `record`, $0.10 cap, cost $0.020443, 6 calls, no errors.

## 4. Claims and how to check them
Environment: Windows, Python 3.14, `.venv` at the repo root (`requirements.txt` pins versions; Playwright Chromium installed).
- **"222 tests pass"** -> `.venv\Scripts\pytest.exe -q` (about 105-115 s). Re-run after every commit through T4 by Opus; T5-0/T5 themselves were re-run only by the same Sonnet turn that wrote them, but **the three fixes in `0b4662c` came from an independent Opus `/review`** that read the code fresh and reproduced each issue before Sonnet fixed it (see section 1) - 39 / 90 / 167 / 174 / 213 / 222.
- **"Buggy build: Confirmed S1/S2/S4, Likely S6, Improvement S3/S5; clean build: nothing"** -> current numbers, superseding the T3b-era "S2/S4/S5 Likely" line (D9 addendum explains why). Reproduce via `tests/test_agent.py::test_full_pipeline_on_the_buggy_taskboard_matches_the_seeded_bugs` and its clean-build twin, or live: start `python -m uvicorn demo_app.server:app --port 8765`, discover a ref with one `capture_state()` call (see the test module's `_ref_for` helper - refs are only known once the page is loaded, D7), then `python -m probe.agent --url http://127.0.0.1:8765/ --profile live --out runs/check --reset-path /__reset --llm-source <a fake script>` (PROBE_LLM_MODE=fake; Git Bash: `export MSYS_NO_PATHCONV=1` first). A CLI run like this was done once, live, to check the entry point itself works, not only pytest.
- **"Mutation checks caught N/N"** -> for T1-T4, self-reported by the builder subagent in `docs/WORKLOG.md`, not re-run by Opus. For T5-0/T5's own mutation checks (four of them), and for all three `0b4662c` review-fix regression tests (also mutation-checked - each one reverted and confirmed to fail, including a second-pass catch when the first version of the sanitize_judgement test turned out not to cover `_run_test`'s wiring), these are visible as reverted edits in the conversation, not as a separate file. To spot-check any of them, break the named rule yourself (e.g. change `_check_ledger`'s `>=` to `>` in `probe/llm.py`, or remove `load_dotenv(ENV_FILE)` from `main()` in `probe/agent.py`) and confirm a test fails.
- **"No real API call was made [until 2026-09-22]"** -> true through `0b4662c`: the LLM tests (`tests/test_llm.py`) fail on any socket open via an autouse fixture (the browser tests legitimately use localhost). **One deliberate real call was then made**, after the user set a $15 Console spend limit and approved it: mode `record`, single-mission profile, `PROBE_MAX_COST_USD=0.10`, ledger enabled at `runs/spend_ledger.jsonl`. Its output lives only under `runs/` (git-ignored, present only on the machine it ran on) and in `docs/DECISIONS.md`'s D9 addendum. Check `.env` itself is still git-ignored and absent from history: `git log --all -- .env`.
- **Decision traceability** -> each D-entry lists options rejected and a Q&A answer; grep the code for the behaviour named in the entry.

## 5. Corrections and problems found during the work (transparency)
1. A ChatGPT-sourced claim that Momentic's headline was "Point Mo at your app, get real bugs back" did not match Momentic's homepage as fetched; recorded in `docs/RESEARCH.md`. Funding claims and all Reddit claims are unverified (Reddit is blocked for the search tool).
2. The T1 brief and D4 disagreed on what S5 and S6 were; the code followed the brief, D4 was corrected.
3. D1's first `no_effect` definition was too weak (client-only state changes) -> redefined in D7.
4. T2 was redesigned mid-task after D7 (custom JS extractor replaced by Playwright's ARIA snapshot); the builder adapted.
5. After T3 the clean build produced two false "Likely" findings (correct 422/409 validation). Found by Opus's re-run, not by the builder's acceptance script. Fixed by D8.
6. Process mistake by Opus: a broad `taskkill /IM python.exe` to stop a test server, which ends every Python process on the machine. Disclosed to the owner; the rule against it is in `CLAUDE.md`. Not repeated on day 2 (a smoke-test server was stopped by finding its PID on its port).
7. The T4-era `run_and_verify` had no judge and replayed only up to the last signal step; fixed by T5's `replay_mission`, which also covers judge-only steps.
8. `replay_mission` did not create its own output directory, so a mission with no signals to replay crashed writing `audit.json`. Found by the day-2 clean-build test, fixed the same session (`613ac45`).
9. D9's own acceptance wording for the T5 test ("Likely S2, S4, S5") turned out to be stale once the disprove pass it also specifies was actually built - see the D9 addendum in `docs/DECISIONS.md` and section 1's note that this correction was made by the same Sonnet turn that wrote the code, not caught by a separate reviewer.
10. An independent Opus `/review` of T5, requested by the user specifically to catch what a same-turn self-check would miss, found three real, reproduced bugs before any real API money was spent: `main()` reading `PROBE_LLM_MODE` before `.env` was loaded (silently skipped the `--yes-spend` gate whenever the mode was set the documented way, in `.env`); an out-of-range judge step number crashing or silently misattributing evidence; no deadline check at all in the judge/replay/disprove phase. All three fixed in `0b4662c`, each with a regression test proven to fail pre-fix. Six further findings were explicitly deferred to before T7 (listed in the D9 addendum) rather than silently dropped.
11. While writing the fix for finding 10's step-2 issue, the first version of its test only checked `sanitize_judgement()` standalone and would have passed even if `_run_test` never called it - caught before committing by mutating the wiring out and watching the test suite (wrongly) stay green; a proper end-to-end test was added and confirmed to fail on the same mutation.

## 6. What is verified, and what is not (as of the first real run, 2026-09-22)
**Now verified by one real, deliberate, user-approved call** (mode `record`, $0.10 cap, cost $0.020443, 6 calls): `claude-haiku-4-5` does support `output_format=StepDecision` with `thinking: disabled` - D7 point 5's open question is resolved, no fallback to Sonnet 5 for steps is needed. The `--yes-spend` gate and the cross-run ledger were both exercised for real (not simulated) and behaved as designed. `llm_record.jsonl` from that run exists for T6 to replay at zero cost.

**Still NOT verified:**
- **Only one narrow real run exists** (one mission, 3 steps, one quiet non-finding). The prompts (`docs/PROMPTS.md`) have not been tuned or stress-tested against a real judge/disprove call on an actual bug - whether the judge would really notice something subtle like the S6 footer bug from a text diff (see the D9 addendum's discussion of step 2 in the buggy-build fake-mode test) remains open for T5b.
- **There is an end-to-end AI run mechanism (`probe/agent.py`, T5), but it has never produced a report from a *bug-finding* real run** - the one real run so far found nothing (correctly). The report/UI (T6) and evaluation runner (T7) do not exist yet, so the task's requirement "runs live with AI in front of the panel" is still not met.
- **The injection defence is designed (D7) but untested** (`?inject=on` canary is planned in T7).
- **Generality is unproven:** only the author-built TaskBoard has been exercised, and the author knows its bugs. A third-party demo site is planned for T7.
- Replay assumes the target can be reset (TaskBoard exposes `/__reset`); arbitrary apps with server-side state may not replay cleanly.
- The 6 deferred review findings (section 5, item 10) are unfixed: safety blocklist gaps, form-action/JS-navigation origin check, `Profile` validation, ledger not auto-wired by the CLI (it was manually enabled for the real run above, not by default), thin disprove evidence, `reproduced_by_outcome`'s false-negative risk.

## 7. Questions worth asking the author
- Is a "Confirmed" item defensible, and does an independent replay really remove agent mistakes? (D3, D7 point 3: the disprove pass is built and now real-call-verified to run without errors, but not yet exercised on an actual bug with a real disprove call; see section 6.)
- Are the evaluation numbers fair when the bugs are author-chosen and the prompts were written knowing the demo app? (Prompts are generic by design; check `docs/PROMPTS.md` for app-specific words. The plan has a clean-build control and a third-party app.)
- What leaves the machine? Page text and accessibility trees go to the Anthropic API; screenshots stay local (D2).
- Is the safety model adequate? (Staging assumption, same-origin only, blocklist, password guard, model returns only a schema object; see D1, D7 point 4, `probe/safety.py`; six gaps deferred to before T7, section 5 item 10.)
- Budget: 20 USD funded, Console limit set to 15, 10 USD planned internal cap with layered enforcement (D9); 0.02 USD spent so far.
- Why was a `/review` requested specifically before the first real call, and what did it change? (This is a good one for the panel: it shows the process caught real bugs - see section 5, items 10-11 - before they could cost real money or crash a live demo.)
