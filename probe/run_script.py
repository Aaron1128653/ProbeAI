"""Dev tool, no LLM: run a scripted list of steps, replay them, and classify what came out.

    python -m probe.run_script examples/steps_taskboard_all.json --url http://127.0.0.1:8765/ \
        --reset-path /__reset --out runs/taskboard_buggy

Writes to the output folder: evidence.json (the first run), findings.json (Confirmed / Likely
items), audit.json (every candidate, merged duplicate and quiet step, with reasons) and screenshots.
--reset-path is a POST the app offers to get back to a known state (the demo app has /__reset);
it is called before the first run and before every replay. (Git Bash rewrites a leading "/" in
arguments; run `export MSYS_NO_PATHCONV=1` first, or use PowerShell.)
"""
import argparse
import json
from dataclasses import asdict
from pathlib import Path

from playwright.sync_api import sync_playwright

from probe.browser import launch_chromium, new_context
from probe.evidence import DEFAULT_IGNORE_PATHS
from probe.executor import Run, Step, run_steps
from probe.findings import build_candidates, candidate_dict, format_table
from probe.oracles import signals_for_run
from probe.verify import replay, reset_app, steps_from_records


def evidence_dict(run: Run) -> dict:
    """evidence.json holds: "load" (what happened while the page opened), "steps" (one StepEvidence
    each) and "states" (the page state before step 1, before step 2 ... and after the last step)."""
    return {
        "url": run.url,
        "started_at": run.started_at,
        "load": run.load,
        "steps": [r.evidence.to_dict() for r in run.results],
        "states": [asdict(s) for s in run.states],
    }


def run(page, url: str, steps: list[Step], out_dir, ignore_paths=DEFAULT_IGNORE_PATHS,
        max_steps: int = 15) -> dict:
    """Run the steps on `page`, write out_dir/evidence.json (plus screenshots) and return it."""
    result = evidence_dict(run_steps(page, url, steps, out_dir, ignore_paths, max_steps))
    (Path(out_dir) / "evidence.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def run_and_verify(browser, url: str, steps: list[Step], out_dir, reset_path: str | None = None,
                   replays: int = 2, ignore_paths=DEFAULT_IGNORE_PATHS, max_steps: int = 15) -> dict:
    """Run the steps, detect signals, replay in fresh contexts, classify. Writes evidence.json,
    findings.json and audit.json to out_dir. Returns the run, the signals, the replays and the candidates."""
    out_dir = Path(out_dir)
    context = new_context(browser)
    try:
        reset_app(context, url, reset_path)
        first = run_steps(context.new_page(), url, steps, out_dir, ignore_paths, max_steps)
    finally:
        context.close()
    (out_dir / "evidence.json").write_text(json.dumps(evidence_dict(first), indent=2), encoding="utf-8")

    signals = signals_for_run(first, url)
    results = []
    if signals:  # nothing to verify on a quiet run; otherwise replay up to the last step with a signal
        last = max(s.step for s in signals)
        recorded = steps_from_records([r.record for r in first.results])[:last]
        results = replay(browser, url, recorded, reset_path, replays, out_dir / "replays",
                         ignore_paths, max_steps)
    candidates = build_candidates(first, signals, results, out_dir)

    findings = [candidate_dict(c) for c in candidates]
    (out_dir / "findings.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
    return {"run": first, "signals": signals, "replays": results, "candidates": candidates}


def summarize(run: Run) -> str:
    lines = [f"run of {run.url}"]
    for r in run.results:
        s, loc = r.evidence, r.record.locator
        target = f'{loc["role"]} "{loc["name"]}"' if loc else "?"
        outcome = "BLOCKED: " + s.blocked if s.blocked else ("ERROR: " + s.error if s.error else "ok")
        lines.append(f"step {s.step}: {s.action} {target} -> {outcome}")
        for q in s.requests:
            flag = " FAILED" if q["failed"] else ""
            lines.append(f'    {q["method"]} {q["url"]} -> {q["status"]}{flag}')
        for c in s.console:
            lines.append(f'    console {c["type"]}: {c["text"]}')
        for e in s.page_errors:
            lines.append(f'    page error: {e["message"]}')
        for d in s.dialogs:
            lines.append(f'    dialog {d["type"]}: {d["message"]}')
        lines.append(f"    url {s.url_before} -> {s.url_after}, page changed: "
                     f"{s.fingerprint_before != s.fingerprint_after}, width {s.scroll_width}/{s.client_width}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run scripted steps, replay them and classify the result (no LLM).")
    parser.add_argument("steps_file", help="JSON list of steps, see examples/")
    parser.add_argument("--url", required=True, help="page to open, e.g. http://127.0.0.1:8765/?bugs=off")
    parser.add_argument("--out", default="runs/script", help="output folder (default runs/script)")
    parser.add_argument("--reset-path", default=None, help="POST path that resets the app, e.g. /__reset")
    parser.add_argument("--replays", type=int, default=2, help="number of replays in fresh contexts (default 2)")
    args = parser.parse_args(argv)

    steps = [Step(**item) for item in json.loads(Path(args.steps_file).read_text(encoding="utf-8"))]
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        outcome = run_and_verify(browser, args.url, steps, args.out, args.reset_path, args.replays)
        browser.close()

    print(summarize(outcome["run"]))
    print()
    if outcome["candidates"]:
        print(format_table(outcome["candidates"]))
    else:
        print("no signals: nothing to replay, no findings")
    print(f"\nwritten to {Path(args.out)}: evidence.json, findings.json, audit.json")


if __name__ == "__main__":
    main()
