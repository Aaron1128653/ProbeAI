# What the Improvement tier has actually produced (hand-read audit, 2026-10-05)

Question the owner asked: "Show me a real Improvement record, like the Confirmed one." Answer: there is no good one, and this
page says why, so it can be said out loud instead of discovered in Q&A.

## What was counted
- Every `report.json` under `runs/` whose model log contains a Claude model: **28 real reports** (of the other 102, 74 are
  fake-mode test fixtures - scripted, not AI output, and 33 of those contain an Improvement item, so they must never be shown as
  findings - and 28 are replays of recordings, which add no new AI output).
- **10 of the 28 real reports contain Improvement items: 14 items in total.** All 10 are *evaluation-mode* runs (22-23 Sep,
  5 checks). **None of the nine live-mode runs through the web page has produced one (Improvement = 0 in all nine).**
- Method: read each item (title, expected, observed, suggestion) and, where a claim could be checked, check it against the run's
  `evidence.json`. The classification below is one reader's judgement.

## The 14 items

| # | Run | Item (title) | Reading |
|---|---|---|---|
| 1 | eval_2026-09-22/buggy_2 m2 | Item count change not confirmed in diff | The AI complaining about its own evidence (22 Sep baseline: the judge only saw diffs) |
| 2 | buggy_2 m5 | Task list content not verified after filter switch | same |
| 3 | buggy_4 m2 | No visible confirmation of 'items left' count change | same - and the footer count IS the real fault S6, which the judge could not see then |
| 4 | buggy_4 m5 | Unable to verify Active filter correctly excludes completed tasks | a note about test design, not about the app |
| 5 | buggy_5 m2 | Cannot confirm strikethrough styling or updated item count from evidence | evidence complaint |
| 6 | buggy_5 m5 | No test data exists for completed tasks | a note about test design |
| 7 | clean_2 m4 | No visible feedback while typing whitespace | trivial and dubious |
| 8 | clean_2 m4 | Validation alert uses generic browser alert dialog | **wrong**: `dialogs` is empty at every step; the in-page error line has `role="alert"` and the AI read that as a browser pop-up |
| 9 | clean_4 m4 | Count label pluralization | says "No action needed" - not an improvement |
| 10 | clean_5 m4 | Validation feedback uses a blocking alert rather than inline message | **wrong**, same misreading as 8 (`dialogs` empty) |
| 11 | injection | Task list contains an unsanitized prompt-injection-style task name | dubious: the page renders titles as plain text |
| 12 | canary/injection | No visible confirmation of textbox focus | trivial |
| 13 | n3/clean_1 | No visible confirmation of textbox focus | trivial |
| 14 | n3/clean_2 | Item count label grammar not perfectly matched to expectation text | says "No fix needed" - not an improvement |

Tally: 6 evidence/test-design complaints (items 1-6), 2 factual misreadings (8, 10), 2 "no action needed" notes (9, 14),
3 trivial (7, 12, 13), 1 dubious (11). **None of the 14 is an item I would show a panel as a good suggestion.**

## What this does and does not mean
- The *mechanism* works as designed: a finding the judge labels "improvement" goes straight to the Improvement tier, is never
  called a defect, and is never Confirmed (`probe/agent.py`, `build_mission_items`). The tier labels are not the problem.
- The *content* is the weakest part of the tool as measured: the judge's improvement suggestions are low value, and two were
  wrong. Nothing was tuned for this (frozen, D17), and it is not claimed otherwise.
- Requirement 3 ("report bugs AND improvements") is met in the narrow sense that the tier exists and works; every finding card
  also carries a concrete suggested fix. It is **not** shown by a good Improvement example, because none exists in the record.

## What to say if asked ("where are the improvements?")
"The tier exists and it's kept separate on purpose: a suggestion is never called a defect. But it's the part I trust least.
I read every improvement it produced in my recordings - fourteen - and none was worth showing you: about half were the AI
complaining that its own evidence was thin, and two were simply wrong. What it did well is the other side: it reports real bugs
with evidence. Making the suggestions useful is the next thing I'd work on."

## Do not
- Do not show any `web_*` run with Improvement > 0: they are scripted fixtures (model `fake`).
- Do not describe "Show a message for duplicate titles" as an AI finding; it comes from a fixture.
- Do not spend money trying to get a better one: nine live runs produced none, the code is frozen, and one more run would not
  change what the record says.
