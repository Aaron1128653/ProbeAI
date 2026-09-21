"""Dev tool, no LLM: run a scripted list of steps against a URL and write evidence.json.

    python -m probe.run_script examples/steps_delete.json --url http://127.0.0.1:8765/ --out runs/delete_demo

Reset the demo app first (POST /__reset) if you need a known starting state.
"""
import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from probe.browser import launch_chromium, new_context
from probe.evidence import DEFAULT_IGNORE_PATHS, EvidenceRecorder
from probe.executor import Step, execute_step
from probe.safety import SafetyPolicy
from probe.state import capture_state


def run(page, url: str, steps: list[Step], out_dir, ignore_paths=DEFAULT_IGNORE_PATHS,
        max_steps: int = 15) -> dict:
    """Open `url`, run the steps, write out_dir/evidence.json (plus screenshots) and return it.

    evidence.json holds: "load" (what happened while the page opened), "steps" (one StepEvidence
    each) and "states" (the page state before step 1, before step 2 ... and after the last step).
    """
    recorder = EvidenceRecorder(page, out_dir, ignore_paths)
    policy = SafetyPolicy(allowed_origin=url, max_steps=max_steps)

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    page.goto(url)
    recorder.wait_until_settled()
    load = {"requests": list(recorder.requests), "console": list(recorder.console),
            "page_errors": list(recorder.page_errors), "dialogs": list(recorder.dialogs)}

    state = capture_state(page)
    states = [state]
    step_evidence = []
    for n, step in enumerate(steps, start=1):
        recorder.begin_step(n, state)
        record = execute_step(page, state, step, recorder, policy)
        state = capture_state(page)
        step_evidence.append(recorder.end_step(record, state).to_dict())
        states.append(state)

    result = {
        "url": url,
        "started_at": started_at,
        "load": load,
        "steps": step_evidence,
        "states": [asdict(s) for s in states],
    }
    (Path(out_dir) / "evidence.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def summarize(result: dict) -> str:
    lines = [f"run of {result['url']}"]
    for s in result["steps"]:
        loc = s["locator"]
        target = f'{loc["role"]} "{loc["name"]}"' if loc else "?"
        outcome = "BLOCKED: " + s["blocked"] if s["blocked"] else ("ERROR: " + s["error"] if s["error"] else "ok")
        lines.append(f'step {s["step"]}: {s["action"]} {target} -> {outcome}')
        for r in s["requests"]:
            flag = " FAILED" if r["failed"] else ""
            lines.append(f'    {r["method"]} {r["url"]} -> {r["status"]}{flag}')
        for c in s["console"]:
            lines.append(f'    console {c["type"]}: {c["text"]}')
        for e in s["page_errors"]:
            lines.append(f'    page error: {e["message"]}')
        for d in s["dialogs"]:
            lines.append(f'    dialog {d["type"]}: {d["message"]}')
        lines.append(f'    url {s["url_before"]} -> {s["url_after"]}, '
                     f'page changed: {s["fingerprint_before"] != s["fingerprint_after"]}, '
                     f'width {s["scroll_width"]}/{s["client_width"]}')
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a scripted list of steps and write evidence.json (no LLM).")
    parser.add_argument("steps_file", help="JSON list of steps, see examples/steps_delete.json")
    parser.add_argument("--url", required=True, help="page to open, e.g. http://127.0.0.1:8765/?bugs=off")
    parser.add_argument("--out", default="runs/script", help="output folder (default runs/script)")
    args = parser.parse_args(argv)

    steps = [Step(**item) for item in json.loads(Path(args.steps_file).read_text(encoding="utf-8"))]
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = new_context(browser)
        result = run(context.new_page(), args.url, steps, args.out)
        browser.close()
    print(summarize(result))
    print(f"evidence written to {Path(args.out) / 'evidence.json'}")


if __name__ == "__main__":
    main()
