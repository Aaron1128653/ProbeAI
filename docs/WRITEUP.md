# ProbeAI: an AI tester whose bug reports you can check

Yuanhang Wang · Mechanical Engineering PhD candidate, AUT · October 2026

## 1. What I understood the problem to be

Small teams ship fast, and testing is the first thing cut. Writing automated tests is a project in itself, and manual click-through is slow and inconsistent. An AI tester could remove that set-up cost. But the brief asks the hard question itself: *how do you handle false positives?* A tester that reports things that are not bugs gets ignored. In the one published study I found (WebProber, 120 websites), 85% of an AI tester's reported bugs were false alarms, mostly because the browser automation itself failed, not because the AI reasoned badly. So I treated the real problem as **trust, not coverage**: not "can an AI click around an app?" but "how does a developer know a reported bug is real?"

## 2. Options I considered, and why I chose this one

**Connect.** A URL, the source code, or an upload. I chose a URL: every team has a staging address, it needs no code access and nothing written first. Reading the code does not show that the running app works.

**Find bugs.** (a) AI writes Playwright scripts: gives regression tests, not checked bug reports, and Playwright's own test agents already do it. (b) A free-roaming agent: unbounded, hard to reproduce, hard to show live. (c) Build on Stagehand: a fast-moving dependency with hidden AI calls; I borrowed its ideas instead. (d) Buy a tool such as QA.tech or Momentic: often the right call for a client; building one showed me what to ask them.

**My choice: split the job.** The AI decides *what to test*: it plans three user "missions" and picks one browser action at a time, writing down beforehand what should happen. The browser, not the AI, records *what happened* (every request, error and screenshot). Anything suspicious is replayed from a clean start without the AI. Tiers are set by rule: **Confirmed** = hard browser evidence (for example a server error) that comes back in a clean replay; **Likely** = weaker evidence or the AI's judgement alone, which can never reach Confirmed by itself; **Improvement** = a suggestion. Each finding has steps, screenshots, expected versus observed.

## 3. How I checked it

I built a small to-do app with six planted faults, which logs when each one really fires (the answer key), plus a clean copy as a control. In 5 baseline runs the failing delete and the blank-title bug were found every time. The wrong "items left" counter happened in every run and was reported in none, because the AI was only shown what *changed* on the page. I changed the evidence it sees, not its instructions, and it was reported in 3 of 3 later runs. Three faults were never found: two were never proposed, one ran out of steps. On the clean copy: 1 false alarm in 5 runs, then 0 in 3. Every finding was checked by hand, and with samples this small I report counts, not percentages. A live run takes about a minute and about five US cents. When the AI kept repeating a failed click, I did not add another instruction: I added a rule from browser evidence (same button, same server error, twice, stop that check), tested first on 22 recorded runs.

## 4. What I left out of scope, and why

Reading the code; logins and multi-step authentication; accessibility, speed, mobile and visual-regression scans (good tools exist, different problem); large-site crawling; production sites (staging only; it stays on one site and will not press pay, publish, upload, download or delete-account); a dashboard or database (JSON files and one page are enough). **Known limits:** I tested at length only on my own app, and I know where its faults are; replay needs an app that can be reset; live mode runs only the top three planned checks to stay near a minute, a speed-versus-coverage trade-off.

## 5. How I used AI

Twice. *Inside the tool*, the AI does the part that needs judgement and is never trusted with the evidence. *To build it*, I used Claude Code as a coding partner (one model for design decisions and reviews, another for code). I applied the same rule to it as the tool applies to its AI: verify, do not trust. Every decision and correction is recorded in the repository, including the times the assistant was wrong and I caught it by re-running the numbers.
