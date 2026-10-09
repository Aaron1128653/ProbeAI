# ProbeAI

**An AI tester whose bug reports you can check.**

Point it at a running web app. An AI decides what is worth testing, a real browser does the testing and records the evidence, and anything suspicious is replayed from a clean start before it is called confirmed. The report is tiered by rule — **Confirmed / Likely / Improvement / Dropped** — not by how sure the AI sounds.

A two-week prototype (September–October 2026), built as a take-home engineering exercise. This repository holds the prototype, the one-page write-up, the slide deck, and the record of how it was built. Author: Yuanhang Wang.

![A real run on the demo app: a Confirmed finding and two Likely ones](deck/assets/rehearsal_5.png)

*A real live run against the demo app (24 September 2026): 53.8 s, 12 model calls, about 5 US cents. The Confirmed finding is the failing delete: the browser recorded a server error, then recorded it again in a clean replay.*

## How it works

```
URL → missions → browser evidence → judge → replay → tiered report
```

1. **Missions.** One model call turns the first page into a short plan: what kind of app this is, and 3–5 things a user would actually want to do. (Live mode runs the first three, to stay near a minute.)
2. **Browser evidence.** For each mission the agent picks *one* action at a time from what is really on the page, and the browser records what happened — every request and status, console errors, before/after screenshots, page state. The model cannot write this part. If the same action fails the same way twice (say, the same button answers with a server error), the browser's evidence ends that check — the model is not asked to retry a third time.
3. **Judge.** A separate call reviews the whole mission against what the agent said it *expected* before acting.
4. **Replay.** Anything suspicious is re-run from a clean browser and a reset app. If it doesn't happen again, it doesn't get called confirmed.
5. **Report.** Findings are tiered by rule, not by the model's own confidence.

## What the four tiers mean

| Tier | Meaning |
|---|---|
| **Confirmed** | The browser recorded **hard evidence** — a server error, an uncaught page error, a failed request — and the same evidence came back in every clean replay. An AI opinion alone can never reach this tier. |
| **Likely** | Seen and reproduced, but without hard browser evidence (the AI saw it on the page), or hard evidence that did not come back every time. A human should look. |
| **Improvement** | The judge calls it a suggestion, not a defect. Never Confirmed. |
| **Dropped** | Looked at and dismissed, with the reason shown (it did not come back, or a soft signal the judge says is expected behaviour). |

Confirmed is a statement about the *evidence*, not about whether the bug exists: a wrong number on screen can be a real bug and still be only Likely, because the machine measured nothing. (A rarer second path to Confirmed: a soft signal that is reproduced every time and survives an "argue the opposite" pass.)

## Why this approach

- **The AI decides what is worth testing** — that's the part that needs judgement, and the part a script can't do.
- **The browser records the evidence** — so the AI can interpret what happened but cannot invent it.
- **Suspected failures are replayed before being called confirmed** — a one-off glitch and a real bug look identical until you try again.

The honest limitation, stated up front: a replay repeats the agent's own mistakes too. That's what the pre-registered expectation and the separate "argue the opposite" pass are for.

## Quick start

Developed and tested on Python 3.14 (Windows 11).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

**1. Start the demo app** — a small task list with six deliberately planted faults:

```bash
python -m uvicorn demo_app.server:app --port 8765
```

Open <http://127.0.0.1:8765/>. Add `?bugs=off` to the address for the fault-free control copy.

**2. Run ProbeAI in REPLAY mode** — plays back a recording of a real run. No API key, no cost:

```bash
python demo_fallback/start_replay.py
```

Open <http://127.0.0.1:8001/>, expand **Advanced**, type `/__reset` (two underscores) as the reset path, and click **Run test**. It takes about 11 seconds. The page shows a full-width **REPLAY** banner at all times, so nobody has to guess which mode they are watching.

**3. Run it LIVE** (optional; real API calls, about 5 US cents a run). Copy `.env.example` to `.env`, put your key in `ANTHROPIC_API_KEY`, then:

```bash
# macOS / Linux
PROBE_LLM_MODE=record PROBE_WEB_YES_SPEND=1 python -m uvicorn web.server:app --port 8000
# Windows PowerShell
$env:PROBE_LLM_MODE="record"; $env:PROBE_WEB_YES_SPEND="1"; python -m uvicorn web.server:app --port 8000
```

Open <http://127.0.0.1:8000/> (banner: **LIVE**), enter `http://127.0.0.1:8765/` as the URL and `/__reset` as the reset path, and click **Run test**. A run takes about a minute. `record` mode also saves the model calls, so the run can be replayed later for free.

Spending is deliberately awkward: there is no button on the page that can authorise it, the mode is fixed when the server starts, every run has a cost cap, and all real calls add to one shared running total.

On Windows, `start_demo.bat` starts the demo app and the free REPLAY page in one click, `start_demo_live.bat` adds the LIVE page, and `stop_demo.bat` stops them.

**Tests:** `python -m pytest -q` runs 363 tests. None of them calls the paid API. A few need local recordings under `runs/` (not published) and skip without them.

## What was actually measured

Small validation experiments, not a benchmark. The demo app records which of its six faults really fired, so a miss can be told apart as "never reached" or "reached but not seen".

**Evaluation mode** (5 checks per run, 22–23 September): six known faults planted in the demo app, five runs against the broken build and five against a clean copy.

- **Found reliably:** the failing delete (hard evidence: HTTP 500, reproduced every time) and the blank-title bug — 5/5 and 5/5.
- **The interesting failure:** a wrong "items left" counter happened in **every** run and was reported in **none** of them. The cause turned out to be mine, not the model's — the judge was only shown what *changed* on the page, and a counter that fails to update changes nothing. After changing what it is shown (not its instructions), it was reported in 3 of 3 later runs.
- **False alarms:** 1 across 5 clean runs in the baseline, 0 across the 3 runs after. Non-zero and reported as such.
- **Prompt injection:** a task whose title tells the agent to ignore its instructions. It didn't.
- **Not found:** three of the six. In the baseline two were never proposed by the planner and one ran out of step budget — a coverage gap, not a judgement gap.

**Live mode** (3 checks per run, about a minute), through the web page, on the demo app — ten runs, 23 September to 6 October:

- The first three all ended *partial*: the model kept pressing the same broken Delete button. I added a rule from browser evidence (same button, same server error, twice: stop that check) rather than another instruction.
- After the rule, 6 of 7 runs completed. The delete bug was Confirmed in 2 of the 7; in 5 of the 7 the three-check limit cut the delete check, so those runs reported only Likely findings (the blank title and the counter). Every item reported in those runs was a real planted bug. A live run took roughly a minute (45–75 s) and about 5 US cents.
- One live run on the clean copy reported nothing — one run, three checks, which does not show the app has no bugs.
- With samples this small the numbers are counts, not percentages.

Reported evaluation numbers come from a finding-by-finding adjudication against the answer key, not raw keyword matches. The automated matcher was wrong for 3 of 6 faults on the first real batch, which is why its output is labelled "auto-matched, requires adjudication".

## Known limits (V1)

- **Live mode runs the first three planned checks.** The planner proposed a delete check in every recorded plan, but ranked it fourth often enough that a live run frequently ends with no Confirmed finding. A speed-versus-coverage trade-off, not hidden.
- **Only the demo app has been tested at length**, and its faults are known to the author. Third-party sites are not yet exercised, and two safety checks are deliberately gated on that: the origin check reads link targets but not form actions or script-driven navigation, and "did the same thing happen again" compares the whole page, which would drift on a page with a clock or a live counter. Generality is unproven.
- **Replay assumes the app can be reset.** The demo app exposes a reset endpoint; an arbitrary app with server-side state may not replay cleanly.
- **The retry guard only covers hard, identical failures.** A model that keeps retrying something that silently does nothing is not stopped, deliberately (weaker evidence).
- **The Improvement tier is the least validated part.** Of the 14 suggestions in the recorded evaluation runs, none was worth showing (see [`docs/IMPROVEMENT_AUDIT.md`](docs/IMPROVEMENT_AUDIT.md)).
- **Out of scope on purpose:** reading the code; logins (the tool would have to hold passwords, and typing into password fields is blocked); accessibility, speed, mobile and visual-regression scans; large-site crawling; production sites (staging only, one site; it will not press pay, publish, upload, download or delete-account).
- **The run folders behind these numbers are not published** (`runs/` is git-ignored). The write-up, the decisions, the audit guide and one recorded run (the replay fixture) are.

## How it was built

The code was written with AI assistance, under a review loop rather than trust in any one model: Claude Opus made and reviewed the design decisions, Claude Sonnet wrote the code, and at key decision points the plan was taken to ChatGPT as an outside auditor, with Opus ruling on each of its points (some adopted, some rejected with a written reason). The author did the final check. Decisions are written down before the code, a daily work log keeps the trail, and git has one commit per task, so any later session could pick up from the record.

The honest limit: the code was written and reviewed within one model family, so re-run it rather than trust it. [`docs/AUDIT_GUIDE.md`](docs/AUDIT_GUIDE.md) lists every claim with a command to check it, what is self-reported versus independently re-run, and the mistakes found along the way. The commands in [`.claude/`](.claude/) are the Claude Code commands the workflow used.

## Deliverables

- **One-page write-up:** [`docs/WRITEUP.pdf`](docs/WRITEUP.pdf) (also [`.md`](docs/WRITEUP.md) and [`.docx`](docs/WRITEUP.docx)): the problem, the options considered, and what was left out.
- **Slide deck:** [`deck/ProbeAI_deck.pptx`](deck/ProbeAI_deck.pptx), 10 talk slides plus backups, generated by `deck/build_deck.py`.

## Repository map

| | |
|---|---|
| [`probe/`](probe/) | The tester: agent loop, browser harness, oracles, replay, tiering rules, LLM client with spend caps |
| [`web/`](web/) | FastAPI server and the one static page |
| [`demo_app/`](demo_app/) | TaskBoard, the app under test, with six planted faults and the answer key (`ground_truth.json`) |
| [`demo_fallback/`](demo_fallback/) | The pinned replay fixture and the launchers |
| [`tests/`](tests/) | The test suite |
| [`examples/`](examples/) | Scripted step files for running the harness without a model |
| [`deck/`](deck/) | The slide deck and the scripts that build and check it |
| [`docs/`](docs/) | Decisions, audit guide, Q&A notes, research, prompts, work log, runbook |

More detail:

| | |
|---|---|
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Why it is built this way — each decision with the options rejected and the reason |
| [`docs/AUDIT_GUIDE.md`](docs/AUDIT_GUIDE.md) | For a reviewer: claims, how to check them, and corrections found along the way |
| [`docs/QA_NOTES.md`](docs/QA_NOTES.md) | Each part in plain language, for explaining to a non-engineer |
| [`docs/PROMPTS.md`](docs/PROMPTS.md) | The model prompts |
| [`docs/RESEARCH.md`](docs/RESEARCH.md) | The research behind the choices |
| [`docs/WORKLOG.md`](docs/WORKLOG.md) | The daily log |

## License

MIT — see [`LICENSE`](LICENSE).
