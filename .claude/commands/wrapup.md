---
description: Write today's work-log entry and commit (use when usage is running low or at end of day)
argument-hint: [optional note]
model: sonnet
allowed-tools: Read, Edit, Write, Bash, Grep, Glob
---

Wrap up the current working session. Note from the user: $ARGUMENTS

1. Run `git status` and `git diff --stat` to see what changed since the last commit.
2. Open `docs/WORKLOG.md`. Find or create today's section (`## YYYY-MM-DD`). Fill in, from real evidence (files, test output), not memory:
   - **Done**: finished tasks and where they live.
   - **Evidence**: the command that was run and its result (pass/fail, counts).
   - **In progress**: anything half-finished and its exact state.
   - **Next**: the very next task, so a fresh session can start cold.
   - **Blockers / decisions needed**: including anything waiting on the user (e.g. API funding).
3. If any design question is open, list it under Blockers so `/decide` can pick it up.
4. `git add` the relevant files (never `.env`, `.venv/`, `runs/`), commit with a message that says what and why. If work is half-done, commit it on the current branch with "WIP:" in the message.
5. Reply with a 5-line summary and the commit hash. Do not start new work.
