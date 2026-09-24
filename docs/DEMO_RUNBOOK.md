# Demo runbook (D17)

One live run, in record mode, on the seeded TaskBoard app; a labelled REPLAY of a real earlier run as the fallback. The code is frozen (D17); this page is what changes if anything does.

## Start (three terminals, from the repo root, PowerShell)
| What | Command | URL |
|---|---|---|
| Demo app (the thing being tested) | `.venv\Scripts\python.exe -m uvicorn demo_app.server:app --port 8765` | http://127.0.0.1:8765/ |
| Live tester (real API, records the run) | `$env:PROBE_LLM_MODE="record"; $env:PROBE_WEB_YES_SPEND="1"; .venv\Scripts\python.exe -m uvicorn web.server:app --port 8000` | http://127.0.0.1:8000/ |
| Fallback tester (REPLAY, costs nothing) | `.venv\Scripts\python.exe demo_fallback\start_replay.py` | http://127.0.0.1:8001/ |

Stop any of them by the PID that owns its port, never by image name (other work runs on this machine):
`Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object OwningProcess` then `Stop-Process -Id <that pid>`. **Never `taskkill /IM python.exe`.**

The fallback replays `runs/web_c5333263/llm_record.jsonl` (rehearsal 5: completed, delete bug Confirmed, the repeat-guard line). sha256 `bda600a35bdf1c475eebf2ae41a92db9e20e187dc037b7f0e047695bfbcd3067`; if `Get-FileHash` differs, do not trust it as the fallback. `start_replay.py` sets `PROBE_REPLAY_MAX_MISSIONS=3` - the server default is one mission and one mission of this recording finds nothing (pinned by `tests/test_d17_fallback.py`).

## Before the talk (10 minutes, on the presentation network)
- [ ] `.venv\Scripts\python.exe -m pytest -q tests/test_d17_fallback.py` passes (the fallback still replays: REPLAY banner, completed, 1 / 2 / 0 / 0).
- [ ] Spend headroom: `.venv\Scripts\python.exe -c "import json;print(sum(json.loads(l)['cost_usd'] for l in open('runs/spend_ledger.jsonl') if l.strip()))"` (self-imposed cap 10 USD, Console limit 15, per-run cap 0.30; one run is about 0.05).
- [ ] Both tabs open: live (8000) and replay (8001). In the **live tab** open *Advanced* and type `/__reset` in the reset-path box (the page does not remember it); URL `http://127.0.0.1:8765/`.
- [ ] The live banner reads **LIVE**; the replay banner reads **REPLAY**. Laptop on power, sleep off, browser zoom so the log and the first card fit.
- [ ] Do not start a run before the talk unless you are rehearsing; a rehearsal run adds a real sample to the record (report it as one).

## During the demo
1. Click **Run test** once. The plan appears at about 7 s: **read the three missions aloud** ("it chose these itself") before results arrive. A run takes about 55 s (range so far 52-67 s).
2. Present **whatever the run shows, as it is.** Branches:
   - **Completed, delete bug Confirmed** -> narrate the Confirmed card first, then the Likely items, then the log line if the repeat guard fired ("it pressed Delete twice, got the same server error twice, and stopped by rule").
   - **Amber "did not finish" banner** -> read the sentence aloud, point at the Confirmed card, which is still valid, and say: *"One check really didn't finish: the AI chose to stop after a single failure, and I don't let the AI declare its own check complete - my stop rule only acts on evidence, the same failure twice, and that run only had one."*
   - **No delete mission ran (no Confirmed card)** -> say so, show what it did find, then switch to the REPLAY tab and say: *"This is a recording of a real earlier run."* Answer: *"It never tried: the AI proposed a delete check in all 26 plans I've recorded, but ranked it fourth in about a quarter of them (6 of 26), and the live mode runs the top three to stay under a minute - a speed-versus-coverage trade-off I chose and can show you."*
   - **Red failed status, API or network error** -> switch to the REPLAY tab immediately.
3. **No silent re-run. Ever.** A second live run is allowed only after a red failure, or if the panel asks, and it is announced as "a second sample" and never replaces the first.
4. **Never say "it always finds the delete bug".** Say ranges, not a single percentage: on the recorded plans roughly 7 runs in 10 look like the clean one, somewhere between about half and nine in ten.
5. The replay is faster (about 11 s) because a recording has no model thinking time; say that when you open it. It is always under the full-width REPLAY banner and introduced as "a recording of a real earlier run".

## What to say about the six real runs
"I ran the finished tool through the real web page six times against my test app. The first three took 58 to 67 seconds; each found the planted delete bug and proved it by repeating it, but each ended with an amber 'did not finish' warning, because the AI kept pressing the same broken Delete button. I added a plain rule - if the same button fails with the same server error twice, stop that check - and ran three more, unchanged in between. Those took 53 to 55 seconds and 12 or 13 AI calls, about five cents each. One was clean: delete bug confirmed, rule fired, no warning. In one the AI gave up after the first failure, so the rule never came into play and the warning showed, correctly. In one the AI ranked the delete check fourth, the live mode runs three, so the delete bug was never tested; it still found two other planted bugs. Across the three post-fix runs, every item it reported was a real planted bug."

## The freeze (D17)
Frozen: everything in `probe/`, `docs/PROMPTS.md`, `web/`, `demo_app/`, both profiles, model ids, prices, caps. Allowed: docs, slides, this runbook, the launcher, tests that only read or replay recordings. Reopen only for: a crash, a red failed status or a UI error on the presentation setup that is not an API outage; a reproducible wrong tier (or any finding the repeat guard created, promoted, dropped or duplicated - revert T14-a first); or the fallback failing to reproduce. An amber banner, no delete mission, S2/S4/S5 misses, or wording taste do **not** reopen it. Last day a reopen fix may land: 2026-10-03; it needs `/decide`, one commit, the full suite and the fallback check.
