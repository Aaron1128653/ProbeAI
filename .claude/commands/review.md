---
description: Opus - review current state against the task requirements and Q&A risk
argument-hint: [area to focus on, optional]
model: opus
allowed-tools: Read, Grep, Glob, Bash
---

You are the reviewer for the AI App Tester project. Read-only: do not modify files.

Focus: $ARGUMENTS

Read `CLAUDE.md`, `docs/DECISIONS.md`, `docs/process/QA_NOTES.md`, then the code. Run tests or the tool where possible.

Give a pass / partial / fail for each, with one line of evidence:
1. The five minimum requirements (input, AI analysis, bugs AND improvements, readable output, runs live).
2. Live-demo risk: run time, network dependence, nondeterminism, fallback.
3. False-positive / hallucination control: is every "Confirmed" item backed by deterministic evidence?
4. Safety: origin restriction, step budget, blocked patterns.
5. Explainability: could the user defend each file to a non-engineer and to an engineer?
6. Scope creep: anything built that CLAUDE.md lists as out of scope.

End with the top 3 fixes ordered by impact on the panel's evaluation criteria.
