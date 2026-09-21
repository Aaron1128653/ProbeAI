"""Candidates and tiers: what the signals mean once the replays are in (D7 point 3). No LLM.

The two model judgements (did the page violate what the agent expected? did the finding survive
an attempt to explain it away?) are parameters that default to None, so a later step can plug them in.
"""
import json
from dataclasses import dataclass, replace
from pathlib import Path

from probe.executor import Run, StepRecord
from probe.oracles import Signal
from probe.verify import ReplayResult, reproduced

CONFIRMED, LIKELY, DROPPED = "Confirmed", "Likely", "Dropped"


@dataclass
class Candidate:
    id: str
    step: int
    signals: list[Signal]           # each carries its own reproduced_n
    reproduced_n: int               # replays in which the best-reproduced signal came back
    replays: int                    # replays that were run
    steps_to_reproduce: list[str]   # human sentences, from the recorded locators, steps 1..step
    screenshot_before: str          # file names inside the run folder
    screenshot_after: str


def describe_step(record: StepRecord) -> str:
    loc = record.locator or {}
    target = f'{loc.get("role") or "element"} "{loc.get("name", "?")}"'
    if loc.get("nth"):
        target += f" (number {loc['nth'] + 1} with that name)"
    text = record.text or ""
    if record.action == "click":
        return f"Click {target}"
    if record.action == "check":
        return f"Tick {target}"
    if record.action == "uncheck":
        return f"Untick {target}"
    if record.action == "type":
        return f"Type {len(text)} spaces into {target}" if not text.strip() else f'Type "{text}" into {target}'
    return f'Press {text} in {target}'


def classify_with_reason(candidate: Candidate, judge_violated: bool | None = None,
                         disproof_survived: bool | None = None) -> tuple[str, str]:
    n = f"{candidate.reproduced_n}/{candidate.replays}"
    hard = [s for s in candidate.signals if s.strength == "hard" and s.reproduced_n > 0]
    contextual = [s for s in candidate.signals if s.strength == "contextual" and s.reproduced_n > 0]

    if hard:
        return CONFIRMED, f"hard signal {hard[0].kind}, reproduced {n}"
    if contextual and disproof_survived is True:
        return CONFIRMED, f"contextual signal {contextual[0].kind}, reproduced {n}, survived the disprove pass"
    if contextual:
        return LIKELY, f"contextual signal {contextual[0].kind} only, reproduced {n}; needs the disprove pass to become Confirmed"
    if candidate.signals:
        return LIKELY, f"not reproduced ({n}): the signal was seen in the first run only"
    if judge_violated is True:
        return LIKELY, "the judge says the expectation was violated; no signal to back it"
    return DROPPED, "no signal and not reproduced"


def classify(candidate: Candidate, judge_violated: bool | None = None,
             disproof_survived: bool | None = None) -> str:
    """Confirmed = reproduced AND (a hard signal OR a contextual signal that survived the disprove pass).
    Likely = reproduced with contextual signals only (or the judge says violated), or seen but not
    reproduced. Dropped = no signal and not reproduced."""
    return classify_with_reason(candidate, judge_violated, disproof_survived)[0]


def build_candidates(run: Run, signals: list[Signal], results: list[ReplayResult],
                     out_dir=None) -> list[Candidate]:
    """One candidate per step that raised signals. A signal already seen at an earlier step (same
    kind and key, for example the page staying too wide) is not a new candidate. Writes audit.json
    to out_dir when given: every candidate, every merged duplicate, every step without a signal."""
    replays = len(results)
    signals = [replace(s, reproduced_n=reproduced(s, results)) for s in signals]

    first_seen: dict[tuple[str, str], tuple[str, int]] = {}  # (kind, key) -> (candidate id, step)
    candidates: list[Candidate] = []
    merged: list[dict] = []
    for step in sorted({s.step for s in signals}):
        kept = []
        for s in (s for s in signals if s.step == step):
            if (s.kind, s.key) in first_seen:
                cid, at = first_seen[(s.kind, s.key)]
                merged.append({"step": step, "kind": s.kind, "key": s.key,
                               "reason": f"duplicate of {cid}: same kind and key already seen at step {at}"})
            else:
                kept.append(s)
        if not kept:
            continue
        cid = f"C{len(candidates) + 1}"
        for s in kept:
            first_seen[(s.kind, s.key)] = (cid, step)
        shots = run.results[step - 1].evidence
        candidates.append(Candidate(
            id=cid, step=step, signals=kept,
            reproduced_n=max(s.reproduced_n for s in kept), replays=replays,
            steps_to_reproduce=[describe_step(r.record) for r in run.results[:step]],
            screenshot_before=shots.screenshot_before, screenshot_after=shots.screenshot_after))

    if out_dir is not None:
        with_signals = {s.step for s in signals}
        quiet = []
        for r in run.results:
            step = r.evidence.step
            if step in with_signals:
                continue
            note = f"blocked: {r.record.blocked}" if r.record.blocked else \
                   f"error: {r.record.error}" if r.record.error else "no signal"
            quiet.append({"step": step, "action": describe_step(r.record), "note": note})
        audit = {"url": run.url, "replays": replays,
                 "candidates": [candidate_dict(c) for c in candidates],
                 "merged_duplicates": merged, "steps_without_signals": quiet}
        (Path(out_dir) / "audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    return candidates


def candidate_dict(c: Candidate, judge_violated: bool | None = None,
                   disproof_survived: bool | None = None) -> dict:
    tier, reason = classify_with_reason(c, judge_violated, disproof_survived)
    return {
        "id": c.id, "tier": tier, "reason": reason, "step": c.step,
        "reproduced": f"{c.reproduced_n}/{c.replays}",
        "signals": [s.to_dict() for s in c.signals],
        "steps_to_reproduce": c.steps_to_reproduce,
        "screenshot_before": c.screenshot_before, "screenshot_after": c.screenshot_after,
    }


def format_table(candidates: list[Candidate]) -> str:
    """Compact table: tier, step, reproduced n/n, signals."""
    rows = [("Tier", "Step", "Reproduced", "Signals")]
    for c in candidates:
        tier = classify(c)
        if all(s.reproduced_n == 0 for s in c.signals):
            tier += " (not reproduced)"
        rows.append((tier, str(c.step), f"{c.reproduced_n}/{c.replays}",
                     "; ".join(f"{s.kind}: {s.key}" for s in c.signals)))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    lines = []
    for row in rows:
        left = "  ".join(cell.ljust(width) for cell, width in zip(row, widths))
        lines.append(f"{left}  {row[3]}")
    return "\n".join(lines)
