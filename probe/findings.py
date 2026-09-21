"""Candidates and tiers: what the signals mean once the replays are in (D7 point 3, D8 rulings). No LLM.

The two model judgements (did the page violate what the agent expected? did the finding survive
an attempt to explain it away?) are parameters that default to None, so a later step can plug them in.
"""
import json
from dataclasses import dataclass, replace
from pathlib import Path

from probe.executor import Run, StepRecord
from probe.oracles import Signal
from probe.verify import ReplayResult, reproduced, reproduced_by_outcome

CONFIRMED, LIKELY, DROPPED = "Confirmed", "Likely", "Dropped"


@dataclass
class Candidate:
    id: str
    step: int
    signals: list[Signal]           # each carries its own reproduced_n; empty for a judge-only finding
    reproduced_n: int               # replays in which the best-reproduced signal (judge-only: the outcome) came back
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
    """Tier and the reason for it. Rules (D7 point 3, D8 rulings 2, 3, 4 and 9):
    - Confirmed: a hard signal that came back in ALL replays, or a contextual signal that came back in
      all replays and survived the disprove pass.
    - A candidate with contextual signals only is Dropped when the judge says the expectation was not
      violated, or when the disprove pass found a harmless explanation. A candidate with a hard signal
      is never demoted and needs no disprove pass.
    - Likely: everything else that came back at least once, labelled "flaky (k/n)" when it did not
      come back every time, or "not reproduced" when it never came back.
    - Judge-only candidate (no signals): the same outcome came back -> Likely, otherwise Dropped (D3)."""
    replays = candidate.replays
    hard = [s for s in candidate.signals if s.strength == "hard"]
    contextual = [s for s in candidate.signals if s.strength == "contextual"]

    def every_time(s: Signal) -> bool:
        return replays > 0 and s.reproduced_n == replays

    if not candidate.signals:  # only the judge saw it; "reproduced" means the same visible outcome
        n = f"{candidate.reproduced_n}/{replays}"
        if judge_violated is False:
            return DROPPED, "the judge says the expectation was not violated"
        if candidate.reproduced_n == 0:
            return DROPPED, f"judge-only finding and the same outcome did not come back in the replays ({n})"
        if candidate.reproduced_n < replays:
            return LIKELY, f"flaky ({n}): judge-only finding, the same outcome came back in some replays only"
        return LIKELY, f"judge-only finding, the same outcome came back in every replay ({n})"

    for s in hard:
        if every_time(s):
            return CONFIRMED, f"hard signal {s.kind}, reproduced {s.reproduced_n}/{replays}"

    if not hard:  # contextual only: the judge and the disprove pass may still say no
        if judge_violated is False:
            return DROPPED, f"contextual signal {contextual[0].kind} only and the judge says the expectation was not violated"
        if disproof_survived is False:
            return DROPPED, f"contextual signal {contextual[0].kind} only and the disprove pass found a harmless explanation"

    for s in contextual:
        if every_time(s) and disproof_survived is True:
            return CONFIRMED, f"contextual signal {s.kind}, reproduced {s.reproduced_n}/{replays}, survived the disprove pass"

    best = max(candidate.signals, key=lambda s: s.reproduced_n)
    n = f"{best.reproduced_n}/{replays}"
    if best.reproduced_n == 0:
        return LIKELY, f"not reproduced ({n}): the signal was seen in the first run only"
    if best.reproduced_n < replays:
        return LIKELY, f"flaky ({n}): the signal {best.kind} came back in some replays only"
    return LIKELY, f"contextual signal {best.kind} only, reproduced {n}; needs the disprove pass to become Confirmed"


def classify(candidate: Candidate, judge_violated: bool | None = None,
             disproof_survived: bool | None = None) -> str:
    return classify_with_reason(candidate, judge_violated, disproof_survived)[0]


def _candidate(cid: str, run: Run, step: int, signals: list[Signal], reproduced_n: int, replays: int) -> Candidate:
    evidence = run.results[step - 1].evidence
    return Candidate(
        id=cid, step=step, signals=signals, reproduced_n=reproduced_n, replays=replays,
        steps_to_reproduce=[describe_step(r.record) for r in run.results[:step]],
        screenshot_before=evidence.screenshot_before, screenshot_after=evidence.screenshot_after)


def judge_only_candidate(cid: str, run: Run, step: int, results: list[ReplayResult]) -> Candidate:
    """A candidate for something only the judge saw at `step`: no signals. It counts as reproduced in
    a replay when that step ends in the same visible page (the same fingerprint_after). The replays
    must have run at least up to `step`."""
    return _candidate(cid, run, step, [], reproduced_by_outcome(run.results[step - 1].evidence, results),
                      len(results))


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
        candidates.append(_candidate(cid, run, step, kept, max(s.reproduced_n for s in kept), replays))

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
        if tier == LIKELY and c.reproduced_n == 0:
            tier += " (not reproduced)"
        elif tier == LIKELY and c.reproduced_n < c.replays:
            tier += " (flaky)"
        rows.append((tier, str(c.step), f"{c.reproduced_n}/{c.replays}",
                     "; ".join(f"{s.kind}: {s.key}" for s in c.signals) or "(judge only)"))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    lines = []
    for row in rows:
        left = "  ".join(cell.ljust(width) for cell, width in zip(row, widths))
        lines.append(f"{left}  {row[3]}")
    return "\n".join(lines)
