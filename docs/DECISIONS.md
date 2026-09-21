# Decisions

Format: id, date, decision, rejected options, consequences, Q&A answer (say it out loud in plain words).
Evidence and sources: `docs/RESEARCH.md`.

---

## D1 - Product shape: mission-based, evidence-backed runtime tester  (2026-09-21)

**Decision.** ProbeAI takes a running URL (declared staging) and runs this pipeline:
`understand app -> missions -> execute (atomic steps) -> oracles collect evidence -> judge -> verify -> report`.
- *Understand*: one LLM call turns the first page state into an **App Model** (app type, core user capabilities) and 3-5 **test missions** (goal, risk, priority, category, and a one-sentence `why` explaining why this is worth testing). Missions are user goals, never selectors or scripts.
- *Execute*: per step the LLM sees a text state of the page and picks one atomic action by **element index**. Before acting it must state what it **expects** to change (pre-registered hypothesis). Budgets: 5 missions, 6 steps per mission, 15 steps and 180 s per run.
- *Oracles* (deterministic, no LLM): console error, uncaught page error, failed request, same-origin HTTP >= 400, "no observable effect" after an action (URL and visible text unchanged, no request), horizontal overflow.
- *Judge*: LLM gets mission + expected vs observed + oracle signals and proposes candidate findings, kind = bug or improvement. It interprets evidence, it does not create evidence.
- *Verify*: see D3. *Report*: see D3.

**Rejected.**
- Free-roaming browser agent ("test this whole site"): unbounded, unexplainable, hard to reproduce on stage.
- LLM writes Playwright scripts: Playwright's own Test Agents already do this; it yields regression tests, not verified bug reports.
- Repo / code analysis: does not prove the deployed user flow works; large scope.
- Heuristic-only planner (as in autonomous-test-agent): no AI, fails requirement 2.

**Consequences.** Every module is small and testable without an LLM (fake LLM in tests). Improvements are judgement calls and are labelled "suggestion", never "defect".

**Q&A.** "The AI decides what is worth testing, like a QA person reading the app. The browser, not the AI, records what actually happened, so the AI cannot invent evidence."

---

## D2 - Stack: Python end to end, no Stagehand dependency in V1  (2026-09-21)

**Decision.** Python 3.14 (already installed), `playwright`, `anthropic` SDK (tool use for structured output), FastAPI + one vanilla HTML/JS page with SSE, JSON files per run. No database, no React, no Node.
Element grounding is our own ~100-line function: JS in the page lists visible interactive elements, numbers them, and each recorded step stores a semantic locator (role + accessible name + nth). The LLM chooses a number, never a selector, and replay resolves the same locator.
Screenshots go to the report, **not** to the LLM by default (text state is cheaper and faster; vision is a later flag).
Model roles are env-configurable: `PROBE_MODEL_PLAN`, `PROBE_MODEL_STEP`, `PROBE_MODEL_JUDGE`. Starting default: Sonnet-class for plan and judge, Haiku-class for step choice; final choice by measurement, not assumption.

**Rejected.**
- TypeScript + Next.js + Stagehand (the ChatGPT proposal): the "same ecosystem" argument is weaker now that Stagehand has a Python SDK; the owner is a Python person who must defend every line; a React app is extra surface for no panel value.
- Stagehand as a dependency: v4.1.0 shipped 2026-09-09 and dev builds keep landing, so the API can move under a two-week build; each `act()` is another hidden LLM call; and "what did you actually build?" gets harder to answer. Borrow its ideas (atomic actions, trimmed accessibility state, observe-then-act), not the package. It remains the obvious upgrade path.
- Browser Use: an opaque agent loop, hard to bound and to replay.

**Consequences.** More code we own, but every line is explainable. Recorded steps are deterministic, so replay works without an LLM.

**Q&A.** "I used Python because it is what I can defend line by line. I borrowed the good ideas from tools like Stagehand but did not depend on them, because a fast-moving dependency is a risk in a two-week build and I wanted to own the part that matters, which is deciding what is a real bug."

---

## D3 - Trust model: verify before reporting, tiers by rule  (2026-09-21)

**Decision.** A candidate finding is verified in this order:
1. **Signal**: at least one deterministic oracle fired on the step (or the judge flags it with none).
2. **Replay**: the recorded steps are replayed in a fresh browser context (fresh app state via a reset call when the target offers one), no LLM. Same signal must recur.
3. **Disprove pass** (should-have, build after the core works): a fresh LLM call gets only the steps and before/after evidence and must argue a benign explanation (agent clicked the wrong thing, control is not meant to act). Inspired by the qa-agent verifier.

Tiers are computed by rule, never as an LLM-stated percentage:
- **Confirmed bug** = signal + reproduced (+ not refuted, once step 3 exists).
- **Likely issue** = reproduced but judge-only (no oracle signal), or signal seen but not reproduced (flaky).
- **Improvement suggestion** = never claims a defect.
- **Dropped** = not reproduced and no signal; kept in the audit log with the reason.

Each report item: title, tier, severity, impact, numbered steps, expected vs observed, evidence (before/after screenshots, request line, console text), suggested next step.

**Why replay alone is not enough.** Replay reproduces the agent's own mistakes (wrong click repeated). That is what step 3 and the "expected" field are for. State this limit openly.

**Rejected.** Percentage "confidence" from the model (false precision); re-running the whole mission with the LLM as the reproduction step (costly, tests agent variance rather than the app); accessibility/Lighthouse scanning in V1 (mature tools already; panel will ask "which part is your AI?").

**Consequences.** Every "Confirmed" item can be defended by pointing at a request line or console message. WebProber (arXiv 2509.05197) reports ~85% false positives for an unverified LLM tester; this design exists because of that.

**Q&A.** "A bug is only called confirmed if the browser shows a hard signal and it happens again from a clean start. If not, it is labelled likely or a suggestion. I would rather show three trustworthy items than ten noisy ones."

---

## D4 - Demo target and evaluation protocol  (2026-09-21)

**Decision.**
- Demo app "TaskBoard": vanilla JS + tiny Python server. Six seeded issues (as built in T1): S1 delete always returns HTTP 500 and the task stays; S2 completed task cannot be reopened; S3 whitespace-only title accepted; S4 very long title overflows the layout; S5 duplicate title gets HTTP 409 and the UI shows nothing; S6 footer "items left" shows the total instead of the active count. Signals: S1 http_5xx, S2 no_effect, S3 judge_only, S4 overflow, S5 http_4xx, S6 judge_only, so three hard signals, one weak, two judge-only. Query flag `?bugs=off` gives a clean build. A server-side **trigger log** records which seeded path actually ran (ground truth), and `/__reset` restores state.
- One third-party demo site (e.g. a public TodoMVC) for generality; no ground truth, judged by reading.
- Protocol: N=5 runs on buggy, N=5 on clean. Per seeded bug report **exercised** (trigger log) versus **confirmed** (report). On clean, count every Confirmed/Likely item as a false positive. Also log tokens, cost estimate and wall time per run. Report counts and ranges only; call it a "small validation experiment", not a benchmark.

**Trigger rule (amended after T1).** A trigger is logged only when the faulty behaviour actually *manifests* (S4: only when the page really overflows, not merely when a title is long), so "exercised" always means "the bug was really on screen".
**Matching rule.** `ground_truth.json` holds `signature_any` (generous synonym list per issue). A finding matches a seeded bug if it hits any keyword; that is only a first pass. Reported numbers use a hand-labelled adjudication file per run (about 25 findings in total, feasible), written down in `runs/<id>/adjudication.json`.
**Harness carve-out.** Requests to paths starting with `/__` are demo plumbing (trigger log, reset). The harness ignores them in evidence and oracles, configurable via `ignore_paths`.

**Rejected.** 20 seeded bugs (unmanageable); scoring only on the buggy app (cannot measure false positives); an LLM to match findings to seeded bugs.

**Consequences.** Separates "never tried it" (coverage) from "tried it, did not recognise it" (judgement), which is the honest way to describe a miss.

**Q&A.** "I planted six known faults, ran the tester five times, and also ran it five times on a clean copy. That tells me what it finds, what it misses, and how often it cries wolf."

---

## D5 - Process: git, daily log, quota, API spend  (2026-09-21)

**Decision.**
- `git` is initialised; one commit per finished task; commit messages say what and why.
- `docs/WORKLOG.md`: one dated section per working day, appended after **every** task (done / evidence / next / blockers). The log is kept current continuously so nothing is lost if usage runs out. `/wrapup` writes the final log entry and commits.
- Usage limits cannot be read by the assistant. When the user sees a low-usage warning, run `/wrapup`; if a limit message ever appears in the session, stop new work and wrap up.
- API spend starts at zero: everything up to the LLM-dependent tasks is built and tested with a fake LLM. The user funds the API before the first LLM-dependent task (expected ~2026-09-25). Key lives in `.env` (git-ignored). Each run logs tokens and estimated cost; a hard per-run budget aborts the run.
- Schedule: feature freeze 2026-10-03. **Vertical-slice line 2026-09-29**: URL -> missions -> execution -> at least one seeded bug found with evidence -> report. Anything after is enhancement. Degradation ladder if unstable: multi-mission -> independent missions -> single mission; the minimum still satisfies the brief.

**Q&A (cost).** "Each run logs its own token use, so I can tell you what one test run costs, and there is a hard cap so it cannot run away."

---

## D6 - Positioning and the "why not just buy a tool?" answer  (2026-09-21)

**Decision.** Position ProbeAI as a **pre-release check for small teams with no QA**: give it a staging URL, get a few-minute smart smoke test and a short list of what deserves a human look. Not a replacement for QA, not a permanent test suite. The report opens with a **Release Check** summary (counts per tier plus "review before release" or "no confirmed issues") and shows "reproduced n/n" on each item.
We do **not** claim novelty. Commercial products exist in this category (e.g. QA.tech: autonomous agents, no source code, screenshots/logs/network per step; Momentic: AI end-to-end tests with repro steps and replays; see `docs/RESEARCH.md`). The prototype's job is to show the mechanism and, specifically, where trust comes from.

**Rejected.** Pitching it as a startup idea (no moat against funded competitors and irrelevant to a two-week assessment); adding features that do not raise coverage, trust or report usefulness (the three-question filter for every feature).

**Q&A - "why not use QA.tech / Momentic?"** "You often should. For a client I would evaluate them. Building a small one showed me what to ask them: how do they decide what to test, how do they avoid false alarms, and where does the client's data go. My prototype answers those questions in the open."

---

## Tasks for /build (ordered; none needs an API key)

- **T1 Demo app "TaskBoard"** - DONE, commit `1eabe27`, 15 tests pass. Follow-ups (fold into T2): S4 logs only when overflow manifests; `ground_truth.json` uses `signature_any` synonym lists.
- **T2 Harness**: page-state extractor with numbered elements, step executor by index that records a semantic locator, evidence recorder (console, pageerror, requestfailed, >=400 responses, before/after screenshot, URL, text hash), safety policy (same origin, step budget, blocked patterns). *Accept*: a scripted step list (no LLM) against TaskBoard writes an evidence JSON that shows the S1 `DELETE ... 500`.
- **T3 Oracles + replay verifier**: turn evidence into signals, replay recorded steps in a fresh context, compute tiers. *Accept*: scripted run yields S1 = Confirmed, S3 = Likely/Improvement, clean mode = zero Confirmed.
- **T4 LLM client with fake/replay mode**, structured schemas (AppModel, Mission, Step, Finding), token and cost log. *Accept*: full pipeline runs end to end against TaskBoard using recorded fake responses.
- Then (needs funded key): T5 real prompts, T6 report + live UI, T7 evaluation runner.
