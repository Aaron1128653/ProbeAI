# ProbeAI

Point it at a running web app and it decides what to test, tests it in a real browser, and reports back the bugs worth fixing and the rough edges worth considering — with the evidence for each.

Built as a take-home engineering exercise.

## How it works

```
URL → missions → browser evidence → judge → replay → tiered report
```

1. **Missions.** One model call turns the first page into a short plan: what kind of app this is, and 3–5 things a user would actually want to do.
2. **Browser evidence.** For each mission the agent picks *one* action at a time from what is really on the page, and the browser records what happened — every request and status, console errors, before/after screenshots, page state. The model cannot write this part. If the same action fails the same way twice (say, the same button answers with a server error), the browser's evidence ends that check - the model is not asked to retry a third time.
3. **Judge.** A separate call reviews the whole mission against what the agent said it *expected* before acting.
4. **Replay.** Anything suspicious is re-run from a clean start. If it doesn't happen again, it doesn't get called confirmed.
5. **Report.** Findings are tiered by rule, not by the model's own confidence: **Confirmed / Likely / Improvement / Dropped.**

## Why this approach

- **The AI decides what is worth testing** — that's the part that needs judgement, and the part a script can't do.
- **The browser records the evidence** — so the AI can interpret what happened but cannot invent it.
- **Suspected failures are replayed before being called confirmed** — a one-off glitch and a real bug look identical until you try again.

The honest limitation, stated up front: a replay repeats the agent's own mistakes too. That's what the pre-registered expectation and the separate "argue the opposite" pass are for.

## Quick start

Start the demo app (a small task list with six deliberately planted faults):

```bash
python -m uvicorn demo_app.server:app --port 8765
```

Then the ProbeAI UI, in one of two modes:

```bash
# REPLAY — replays a real recorded run. No API key, no cost. The demo fallback.
PROBE_LLM_MODE=replay \
PROBE_LLM_SOURCE=runs/first_real_2026-09-22/llm_record.jsonl \
PROBE_REPLAY_URL=http://127.0.0.1:8765/ \
python -m uvicorn web.server:app --port 8000

# LIVE — real API calls. Needs ANTHROPIC_API_KEY in .env and an explicit opt-in.
PROBE_LLM_MODE=real PROBE_WEB_YES_SPEND=1 python -m uvicorn web.server:app --port 8000
```

Open <http://127.0.0.1:8000/>. The page shows a full-width **LIVE** or **REPLAY** banner at all times, so nobody ever has to guess which one they're watching.

Spending is deliberately awkward: there is no button on the page that can authorise it, the mode is fixed when the server starts, every run has a cost cap, and all real calls add to one shared running total.

## What was actually measured

A small validation experiment, not a benchmark — six known faults planted in the demo app, five runs against the broken build and five against a clean copy.

- **Found reliably:** the failing delete (hard evidence: HTTP 500, reproduced every time) and the blank-title bug — 5/5 and 5/5.
- **The interesting failure:** a wrong "items left" counter happened in **every** run and was reported in **none** of them. The cause turned out to be mine, not the model's — the judge was only shown what *changed* on the page, and a counter that fails to update changes nothing. After fixing that, it was caught **3/3**.
- **False alarms:** 1 across 5 clean runs in the baseline, 0 across the 3 runs after. Non-zero and reported as such.
- **Prompt injection:** a task whose title tells the agent to ignore its instructions. It didn't.
- **Not found:** three of the six. Two were never attempted and one ran out of step budget before it could act — a coverage gap, not a judgement gap, and the difference is measurable because the demo app records which faults actually fired.

Reported numbers come from a finding-by-finding adjudication against the answer key, not raw keyword matches. The automated matcher was wrong for 3 of 6 faults on the first real batch, which is why its output is now labelled "auto-matched, requires adjudication".

## Known limits (V1)

- **Coverage before judgement.** Some faults are never explored — the run-wide step budget is consumed by the first few missions, so the last mission is routinely starved.
- **The retry guard only covers hard, identical failures.** It stops repeated server errors and script errors on the same element; a model that keeps retrying something that silently does nothing is not stopped, deliberately (weaker evidence).
- **Replay assumes the app can be reset.** The demo app exposes a reset endpoint; an arbitrary app with server-side state may not replay cleanly.
- **Third-party sites are not yet exercised**, and two safety checks are deliberately gated on that: the origin check reads link targets but not form actions or script-driven navigation, and "did the same thing happen again" compares the whole page, which would drift on a page with a clock or a live counter.
- **Only the demo app has been tested at length**, and its faults are known to the author. Generality is unproven.

## More detail

| | |
|---|---|
| [`docs/AUDIT_GUIDE.md`](docs/AUDIT_GUIDE.md) | For a reviewer: every claim with a command to check it, what is self-reported vs independently re-run, and a list of mistakes found along the way |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Why it is built this way — each decision with the options rejected and the reason |
| [`docs/QA_NOTES.md`](docs/QA_NOTES.md) | Each part in plain language, for explaining to a non-engineer |
