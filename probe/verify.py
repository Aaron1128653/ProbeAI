"""Replay: run the recorded steps again in a brand new browser context and see what comes back.

No LLM. Replay uses only the recorded role + name + nth of each step. The reset call (if the app
has one) gives every replay the same starting state as the first run.
"""
import tempfile
from dataclasses import dataclass
from pathlib import Path

from probe.browser import new_context
from probe.evidence import DEFAULT_IGNORE_PATHS, StepEvidence
from probe.executor import Step, StepRecord, run_steps
from probe.oracles import Signal, signals_for_run
from probe.safety import origin_of


@dataclass
class ReplayResult:
    replay: int                     # 1, 2, ...
    evidence: list[StepEvidence]
    signals: list[Signal]


def steps_from_records(records: list[StepRecord]) -> list[Step]:
    """Turn what was recorded back into steps: the locator replaces the ref, which does not survive."""
    return [Step(r.action, r.locator or {}, r.text, r.expect) for r in records]


def reset_app(context, base_url: str, reset_path: str | None):
    """POST the app's reset path (if it has one). It goes through the context, not the page, so it is
    never recorded as evidence. A failed reset stops the run: the start would not be clean."""
    if not reset_path:
        return
    response = context.request.post(origin_of(base_url) + reset_path)
    if not response.ok:
        raise RuntimeError(f"reset {reset_path} answered {response.status}; the run would not start clean")


def replay(browser, base_url: str, steps: list[Step], reset_path: str | None = None, replays: int = 2,
           out_dir=None, ignore_paths=DEFAULT_IGNORE_PATHS, max_steps: int = 15) -> list[ReplayResult]:
    """Run `steps` `replays` times, each in a fresh context, and detect signals on every step."""
    out_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="probe_replay_"))
    results = []
    for n in range(1, replays + 1):
        context = new_context(browser)
        try:
            reset_app(context, base_url, reset_path)
            run = run_steps(context.new_page(), base_url, steps, out_dir / f"replay_{n}", ignore_paths, max_steps)
        finally:
            context.close()
        results.append(ReplayResult(n, [r.evidence for r in run.results], signals_for_run(run, base_url)))
    return results


def reproduced(signal: Signal, results: list[ReplayResult]) -> int:
    """In how many replays the same signal came back: same kind, same normalised key, same step number."""
    return sum(
        1 for result in results
        if any(s.kind == signal.kind and s.key == signal.key and s.step == signal.step for s in result.signals))
