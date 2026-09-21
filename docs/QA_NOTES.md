# Q&A notes

For each part of the system: what it does, why it is built this way, how to say it to a non-engineer. Appended by `/build` after each task. Decision-level answers are in `docs/DECISIONS.md`.

## T1 - Demo app "TaskBoard" (`demo_app/`)

- **What it is:** a tiny to-do list (add, tick, delete, filter, "N items left"): Python server plus plain HTML/JS. It is the thing my tester is pointed at, so I control exactly what is broken. Open `/` for the buggy build and `/?bugs=off` for a clean copy.
- **Six planted faults (S1-S6):** delete returns HTTP 500, a completed task cannot be reopened, a blank title is accepted, a very long title overflows the page, a duplicate title fails silently, the footer counts wrong. Each is marked `# SEEDED S<n>` in the code and described in `ground_truth.json`.
- **Trigger log:** every time a planted fault really runs, the app writes it into a list (`/__trigger_log`). That is the answer key: it tells me whether the tester never tried something (coverage) or tried it and did not recognise it (judgement). `/__reset` puts the app back to a known state before each run. A fault is logged only when it really shows on screen (for the long-title fault: only when the page actually scrolls sideways), so "exercised" means "the bug was visible".
- **Answer key for matching:** `ground_truth.json` lists, per fault, a generous set of synonym words (`signature_any`). A tester finding that contains any of them is only a first-pass match to that fault; the numbers I report come from checking the matches by hand.
- **Clean mode:** the same app with all faults off (proper validation, error messages shown). Running the tester on it tells me how often it reports problems that are not there.
- **Tests:** `pytest` starts the server and drives the page with hand-written Playwright steps, only to prove the faults fire in buggy mode and none fire in clean mode. That test code is not the product.

## T2 - Browser harness (`probe/`)

**browser.py**
- Starts a headless Chrome-family browser and hands out a *fresh context* for every run: its own cookies and storage, like a brand new browser profile. That is what makes a replay a fair second try.

**state.py** (what the tester "sees")
- Uses the browser's accessibility tree, the same information screen readers use: a short outline of buttons, links, text boxes and their names, for example `button "Delete Buy milk"`. I did not write my own page scanner; Playwright produces this outline.
- In the outline every clickable thing gets a short tag like `e8`. The AI can only answer "click e8"; it never writes a selector or a URL.
- A tag stops working after a reload, so I also store role + name + position (`button "Delete Buy milk", 1st one`). That is what a replay uses.
- Fingerprint = hash of the URL and the outline. If it does not change after an action, the page did not visibly react. The page text is fenced and labelled untrusted before any AI reads it.

**executor.py** (doing one step)
- Takes one action (click, type, tick, untick, press a key), waits until the page has been quiet for 0.3 s, and records which element it used in the replayable form.
- If something is wrong (element gone, button disabled, unknown tag) the step is written down with the error text; the run never crashes.

**evidence.py** (what really happened)
- Listens to the browser itself: every request and its status, console messages, uncaught errors, pop-up dialogs, before/after screenshots, page fingerprint before/after, page width. The AI cannot influence any of this.
- Requests to the demo app's own bookkeeping paths (`/__...`) are ignored so they never look like app behaviour. Chrome's "Failed to load resource" console line is marked so it is not counted twice next to the failed request.

**safety.py** (guard rails outside the AI)
- Plain code that says no to: leaving the declared site, going over the step budget, buttons like "Delete account", "Pay", "Publish", "Upload", "Download", and typing into password fields. Deleting a task inside the app ("Delete Buy milk") is allowed on purpose because the target is a declared test app.
- A blocked step is written down with the reason and never executed.

**run_script.py** (developer tool, no AI)
- Runs a hand-written list of steps and writes `evidence.json` plus screenshots. It proves the harness works before any AI is involved: the delete step shows `DELETE /api/tasks/1 -> 500` on the buggy build and `200` on the clean one.

## T2 fixes after review (`probe/safety.py`, `probe/executor.py`, `probe/state.py`)

- **Passwords:** the accessibility outline hides that a box is a password box, so before typing (or pressing a key) the tester also reads the box's real `type` attribute from the page and refuses if it says password. The name check ("Password") stays as a second net. This covers typing and key presses.
- **Dangerous buttons:** the block list is now a list of patterns, not exact words. Words may sit in between ("Delete my account") and endings may vary ("Uploads", "Downloading"). It deliberately over-blocks: a task called "Pay rent" would have its own buttons blocked. Safer to skip a control than to press the wrong one; the demo tasks do not collide.
- **Evidence size:** the outline stored in `evidence.json` is cut at 20000 characters. The fingerprint is still computed from the whole page, so a change far down the page is not missed.
- **Accepted limits, on purpose:** a clock or counter on the page can only make "nothing happened" fire *less* often, never falsely. A clickable box with no role (a bare `div`) is not seen by the outline; that is a known limit of V1. Every safety check uses up one step of the budget, including refused or invalid steps, so a model that keeps choosing bad targets runs out of steps.
- **Answer key:** for S2 (a completed task cannot be reopened) the expected signal is now "state not reached": the box was asked to become unticked and is still ticked. It is not "nothing happened", because a request did go out.
