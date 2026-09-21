# AI App Tester — take-home engineering exercise

Source material: `Project background/` (brief, requirements, invitation). Background of the user: `Mybackground/`.
Presentation: in person on 2026-10-07, 08 or 09 (slot TBC). 10 min talk + Q&A. Panel includes a non-engineer.
Deliverables: (1) prototype that runs LIVE, (2) one-page write-up (problem / options / out-of-scope), (3) short slide deck.

## Model split (standing rule)
- **Opus decides**: architecture, scope cuts, agent prompts and output schemas, evaluation protocol, slide narrative, code review. Commands: `/decide`, `/review`.
- **Sonnet executes**: scaffolding, Playwright glue, API/UI code, demo app, benchmark scripts, tests. Commands: `/build`, `/wrapup`, or the `builder` subagent when delegating from an Opus session.
- Sonnet never makes a design decision that is not already recorded in `docs/DECISIONS.md`. If one is missing, stop and ask for `/decide`.

## The user
Mechanical-engineering PhD (AUT). Strong in Python/MATLAB, experimental design, validation, uncertainty. Not a web developer.
The user must be able to explain every line in Q&A, so: **Python end to end, few dependencies, no framework the user cannot explain, plain readable code.**

## Direction
The source of truth is `docs/DECISIONS.md` (D1-D7; D7 amends D2/D3); evidence in `docs/RESEARCH.md`. One-line summary:
ProbeAI takes a staging URL -> LLM builds an app model and 3-5 missions -> executes atomic steps chosen by ARIA-snapshot ref -> deterministic oracles record evidence -> judge proposes findings -> verify by clean-context replay -> tiered report (Confirmed / Likely / Improvement).
Stack: Python, playwright, anthropic SDK, FastAPI + one static HTML page, JSON files. No Stagehand, no React, no database.
Core sentences: the AI decides what is worth testing; the browser provides the evidence; suspected failures are reproduced before being called confirmed.
Out of scope V1: repo analysis, accessibility/Lighthouse scanning, auth-heavy flows, multi-page crawling at scale, performance, mobile, visual regression, database, agent frameworks.

## Hard requirements (from Task_Requirements)
1 accept input pointing at an app - 2 analyse with AI, not fixed test cases - 3 report bugs AND improvements - 4 readable output for developers - 5 run live in front of the panel.
Narrow-but-working beats broad-but-broken. Feature freeze: 2026-10-03. Then slides, write-up, rehearsal, fallback.

## Live-demo rules
Whole run should finish in about 2 minutes; UI streams every step as it happens. Keep an API-outage fallback (clearly labelled replay of a real earlier run) but the primary demo is live. Rehearse on the real network/hotspot.

## Working rules
- Verify by running the thing; show real output before saying "done".
- Keep `docs/DECISIONS.md` (why) and `docs/QA_NOTES.md` (how to explain each part in Q&A) current.
- Git is in use: one commit per finished task. Commit message = what and why. Never commit `.env`, `.venv/`, `runs/`.
- After every finished task append to `docs/WORKLOG.md` (today's section: done / evidence / next / blockers). Do it continuously, not only at the end of the day.
- Usage limits are not visible to the assistant. If a limit or low-usage message appears, or the user says so, stop new work and run `/wrapup`.
- API money: the user funded 20 USD and wants most of it left (D9). Total real spend cap 10 USD; per-run cap 0.30; `PROBE_LLM_MODE` has no default and real/record runs need `--yes-spend`. Build and test with fake or replay; a real call needs an explicit go from the user. Never hard-code keys; the user creates `.env` locally; never print, log or commit the key.
- Process safety: stop servers by PID or port, never `taskkill /IM python.exe` (other work runs on this machine).
- Audit trail: everything is committed; `docs/AUDIT_GUIDE.md` maps commits to claims and lists what is self-reported versus independently re-run. Keep it current when a claim changes.
