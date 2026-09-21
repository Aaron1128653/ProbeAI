# Research notes (2026-09-21)

What was actually fetched and read (not just seen in search results):

| Source | What it shows | What we take from it |
|---|---|---|
| [Playwright Test Agents](https://playwright.dev/docs/test-agents) | Official planner / generator / healer agents. Planner explores an app and writes a Markdown test plan; generator turns it into Playwright tests; healer repairs failing tests. | The "AI writes regression tests" route already exists and is maintained by Playwright. Our angle is different: find and *verify* bugs now, not maintain a suite. Exporting the trace as a Playwright test is our V2 story. |
| [prashanth-sams/autonomous-test-agent](https://github.com/prashanth-sams/autonomous-test-agent) | TypeScript + Playwright exploratory agent. Deterministic planner (no LLM). Every candidate defect is **replayed from a clean state** with a fresh request log; fingerprint + dedupe; SafetyPolicy (allowed hosts, blocked actions, step/time budgets); oracles for 5xx, exceptions, unresponsive controls. Demo app with 6 seeded defects, reports all 6 found. | Validates our replay-verification, safety policy and seeded-defect benchmark. Its planner is heuristic, so "AI decides what is worth testing" is where we add value. |
| [msilvera-howdy/qa-agent](https://github.com/msilvera-howdy/qa-agent) | Two-session design: an executor finds candidates, a second **independent verifier session is told to disprove each finding**. Only survivors are reported; dropped findings and reasons are kept in the audit trail. | Adversarial verification is worth having. We keep an audit log of dropped candidates. |
| [WebProber, arXiv 2509.05197](https://arxiv.org/html/2509.05197v1) | LLM/vision web-testing prototype on 120 sites: 29 usability issues found, ~85% of reported bugs were false positives (mostly browser-automation artefacts), 59.4% coverage on a manually inspected subset; shallow exploration and dynamic content are weak points. | Best available argument that unverified LLM bug reports are noise. Cite it for "why verify before reporting". |
| [Stagehand Python SDK](https://pypi.org/project/stagehand/) | Python >= 3.11, local mode works without Node or Browserbase; v4.1.0 released 2026-09-09, dev builds of 4.2.0 as of 2026-09-19. | It is usable from Python, but the API is moving fast. See D2. |

Market check (homepages fetched 2026-09-21):

| Source | What it says | Note |
|---|---|---|
| [QA.tech](https://www.qa.tech) | "AI QA Testing Tool for Web & Mobile - Autonomous QA Agents". Tests without touching your code, plain-language goals, screenshots + logs + network activity for every step, agent reasoning at the point of failure. | Closest to our brief. The page does not say whether findings are independently reproduced; do not claim they are not. |
| [Momentic](https://momentic.ai) | "Catch real bugs before they ship." Plain-English end-to-end tests, hosted browsers/devices, auto-update on UI change, repro steps and session replays. Tests are specified (YAML) and it learns from docs, code and Jira; it is not just "paste a URL". | The ChatGPT write-up quoted a different headline ("Point Mo at your app...") that I could not find; treat its "Momentic is almost this exact task" claim as unverified. |

Not verified: funding claims (Momentic Series A etc.), and: the Reddit / r/softwaretesting claims in the ChatGPT write-up. Reddit is blocked for this tool, so treat those as anecdotes.
Seen in search results only (not read): Midscene, Browser Use, Bug0 roundups.
