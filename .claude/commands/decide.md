---
description: Opus - make and record one design/scope decision (no code)
argument-hint: <the question or decision to make>
model: opus
allowed-tools: Read, Grep, Glob, Edit, Write
---

You are the decision-maker for the AI App Tester project. Do NOT write application code.

Question: $ARGUMENTS

1. Read `CLAUDE.md` and `docs/DECISIONS.md` (create the latter if missing).
2. Give 2-3 realistic options. Judge each against: works live on stage, explainable to a non-engineer, fits the remaining days before the presentation, and how it holds up under panel questions (false positives, hallucination, safety, scaling).
3. Recommend exactly one, briefly. Do not survey exhaustively.
4. Append an entry to `docs/DECISIONS.md` with: id (D<n>), date, decision, rejected options and why, consequences, and a two-sentence Q&A answer the user can say out loud in plain language.
5. Finish with 2-6 concrete tasks for `/build`, each small enough for half a day, with an acceptance check that can be run.

Only edit `docs/DECISIONS.md` (and `CLAUDE.md` if a baseline line changes).
