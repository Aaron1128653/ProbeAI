# Q&A notes

For each part of the system: what it does, why it is built this way, how to say it to a non-engineer. Appended by `/build` after each task. Decision-level answers are in `docs/DECISIONS.md`.

## T1 - Demo app "TaskBoard" (`demo_app/`)
- **What it is:** a tiny to-do list (add, tick, delete, filter, "N items left"): Python server plus plain HTML/JS. It is the thing my tester is pointed at, so I control exactly what is broken. Open `/` for the buggy build and `/?bugs=off` for a clean copy.
- **Six planted faults (S1-S6):** delete returns HTTP 500, a completed task cannot be reopened, a blank title is accepted, a very long title overflows the page, a duplicate title fails silently, the footer counts wrong. Each is marked `# SEEDED S<n>` in the code and described in `ground_truth.json`.
- **Trigger log:** every time a planted fault really runs, the app writes it into a list (`/__trigger_log`). That is the answer key: it tells me whether the tester never tried something (coverage) or tried it and did not recognise it (judgement). `/__reset` puts the app back to a known state before each run.
- **Clean mode:** the same app with all faults off (proper validation, error messages shown). Running the tester on it tells me how often it reports problems that are not there.
- **Tests:** `pytest` starts the server and drives the page with hand-written Playwright steps, only to prove the faults fire in buggy mode and none fire in clean mode. That test code is not the product.
