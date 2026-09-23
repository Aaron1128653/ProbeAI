"""One real `disprove` call against a real recorded finding - an API contract check (D13).

    python -m probe.check_disprove --run runs/eval_2026-09-23_canary/injection --out runs/disprove_check

**This is not an evaluation result and must never be quoted next to the N=3/N=5 numbers.** It
answers one narrow question - does a real disprove call work end to end - and says nothing about
whether the disprove pass judges *well*. One call on one candidate is not a measurement.

Why it exists: the disprove pass (D3 step 3, the call that decides whether a reproduced
contextual signal becomes Confirmed or Dropped) had three real opportunities across this project
and executed **zero** times - 0 calls in the 2026-09-22 N=5 batch, 0 in the N=3 batch, 0 in the
canary. Every candidate hit one of `build_mission_items`'s four earlier `continue` branches first.
Its `max_tokens` was also set to a value it could never have worked at until T9-a. Rather than pay
for repeated full runs hoping one happens to reach the gate (three tries, zero calls, ~$0.09 a
run), this replays one genuine recorded candidate through the real `disprove_prompt()` and makes
exactly one call.

The candidate is read from a real run directory, never hand-written: a contextual-only,
fully-reproduced finding from `findings.json` plus its mission goal from `events.jsonl`. Nothing
about the prompt is reconstructed by hand - `disprove_prompt()` itself builds it, because the
whole point is to exercise the real contract.

Mode comes from PROBE_LLM_MODE, exactly like probe/agent.py and probe/evaluate.py; real and record
need --yes-spend, and the shared spend ledger and per-run cap apply unchanged.
"""
import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from probe.agent import check_spend_confirmed, default_ledger_path, disprove_prompt
from probe.findings import Candidate
from probe.llm import DEFAULT_MAX_COST_USD, ENV_FILE, LLMClient, LLMError, load_dotenv
from probe.oracles import Signal
from probe.prompts import SYSTEM
from probe.schemas import Disproof

def criteria_report(mode: str | None) -> str:
    """D13's five criteria and which this particular run actually checked. The marker depends on
    the mode on purpose: in fake or replay mode no real call happens, so printing "[live]" there
    would be exactly the kind of overclaim this script exists to prevent (found by running it in
    fake mode and reading its own output)."""
    real = mode in ("real", "record")
    tag = "[live]    " if real else "[NOT real]"
    header = ("D13's success criteria, and which this run actually checked:" if real else
              f"D13's success criteria. PROBE_LLM_MODE={mode!r}, so NO REAL CALL WAS MADE - this\n"
              "run only exercises the plumbing. Criteria 1-4 need real or record mode:")
    return f"""{header}
  {tag} 1. a real disprove call was made
  {tag} 2. it returned a valid parsed Disproof
  {tag} 3. it did not hit max_tokens - by construction: probe/llm.py raises when the answer
               cannot be parsed and names the stop reason, so a parsed Disproof IS the proof it
               was not cut off (stop_reason is not surfaced on the success path, and exposing it
               would mean changing the Usage record written to the shared spend ledger)
  {tag} 4. token usage, latency and cost were recorded (below, and in this run's llm_log.jsonl)
  [NOT run] 5. a failed disprove degrades instead of aborting - NOT checked here in any mode,
               because forcing a real API failure cannot be done cheaply or honestly. It is
               covered by tests/test_agent.py::
               test_a_failed_disprove_call_leaves_the_candidate_at_likely_instead_of_killing_the_run,
               which is mutation-checked (T9-a)."""


@dataclass(frozen=True)
class _Mission:
    """disprove_prompt() reads only .goal; a recorded run gives us the goal and nothing else is
    needed, so this stands in rather than rebuilding a full schemas.Mission from partial data."""
    goal: str


def load_recorded_candidate(run_dir: Path) -> tuple[_Mission, Candidate]:
    """The first contextual-only, fully-reproduced candidate in a recorded run - i.e. exactly the
    shape that reaches the disprove gate in build_mission_items (signals present, none hard, and
    every signal came back in every replay). Raises if the run holds no such finding."""
    findings = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    for f in findings:
        signals = f.get("signals") or []
        if not signals or any(s["strength"] == "hard" for s in signals):
            continue
        reproduced_n, _, replays = (f.get("reproduced") or "0/0").partition("/")
        if not replays or int(reproduced_n) != int(replays):
            continue
        candidate = Candidate(
            id=f["id"], step=f["step"], signals=[Signal(**s) for s in signals],
            reproduced_n=int(reproduced_n), replays=int(replays),
            steps_to_reproduce=f.get("steps_to_reproduce") or [],
            screenshot_before=f.get("screenshot_before") or "",
            screenshot_after=f.get("screenshot_after") or "")
        return _Mission(goal=_mission_goal(run_dir, f.get("mission"))), candidate
    raise SystemExit(f"error: {run_dir} holds no contextual-only, fully-reproduced finding - "
                     "that is the only shape that reaches the disprove gate, so there is nothing "
                     "here to replay. Point --run at a different recorded run.")


def _mission_goal(run_dir: Path, mission_id: str | None) -> str:
    events_path = run_dir / "events.jsonl"
    if mission_id and events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line) if line.strip() else {}
            if event.get("type") == "mission_started" and event.get("mission", {}).get("id") == mission_id:
                return event["mission"]["goal"]
    return "(the recorded run did not name this mission)"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="One real disprove call against a recorded finding - a contract check, not an evaluation (D13).")
    parser.add_argument("--run", required=True,
                        help="a recorded run directory holding findings.json and events.jsonl")
    parser.add_argument("--out", required=True, help="where this check's own log and report go")
    parser.add_argument("--yes-spend", action="store_true",
                        help="required when PROBE_LLM_MODE is real or record, since those spend money")
    parser.add_argument("--llm-source", default=None, help="fake: a JSON script; replay: a llm_record.jsonl")
    args = parser.parse_args(argv)

    load_dotenv(ENV_FILE)  # before reading the mode, for the same reason probe/agent.py's main() does
    mode = os.environ.get("PROBE_LLM_MODE")
    message = check_spend_confirmed(mode, args.yes_spend)
    if message:
        print(message, file=sys.stderr)
        raise SystemExit(2)
    if mode in ("real", "record"):
        cap = os.environ.get("PROBE_MAX_COST_USD") or str(DEFAULT_MAX_COST_USD)
        print(f"Spending real API money: PROBE_LLM_MODE={mode}, ONE disprove call, cap {cap} USD.")

    mission, candidate = load_recorded_candidate(Path(args.run))
    prompt = disprove_prompt(mission, candidate)  # the real builder, never a hand-written string
    out_dir = Path(args.out)

    try:
        llm = LLMClient(out_dir, source=args.llm_source, ledger_path=default_ledger_path(mode))
        answer, usage = llm.call("disprove", SYSTEM["disprove"], prompt, Disproof)  # exactly one call
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("\nCriterion 1-2 FAILED: the call did not return a usable Disproof. The pipeline "
              "itself is unaffected - T9-a makes a failed disprove degrade to Likely rather than "
              "abort a run - but this contract check did not pass.", file=sys.stderr)
        raise SystemExit(1) from exc

    report = {
        "check": "disprove API contract (D13) - NOT an evaluation result",
        "mode": mode,
        "real_call_made": mode in ("real", "record"),
        "source_run": str(args.run),
        "candidate": {"id": candidate.id, "step": candidate.step,
                      "reproduced": f"{candidate.reproduced_n}/{candidate.replays}",
                      "signals": [f"{s.kind} ({s.strength})" for s in candidate.signals]},
        "mission_goal": mission.goal,
        "answer": {"refuted": answer.refuted, "reason": answer.reason},
        "stop_reason": "not surfaced on success; a parsed Disproof proves it was not cut off",
        "usage": {"model": usage.model, "input_tokens": usage.input_tokens,
                  "output_tokens": usage.output_tokens, "latency_ms": usage.latency_ms,
                  "estimated_cost_usd": usage.cost_usd},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "disprove_check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print("DISPROVE API CONTRACT CHECK - NOT an evaluation result.")
    print("Do not quote this next to the N=3/N=5 numbers: it shows the call works, not that the")
    print("disprove pass judges well. One call on one candidate is not a measurement.")
    print("=" * 78)
    print(f"\nReplayed candidate {candidate.id} from {args.run}")
    print(f"  mission: {mission.goal}")
    print(f"  signals: {', '.join(f'{s.kind} ({s.strength})' for s in candidate.signals)}"
          f"  reproduced {candidate.reproduced_n}/{candidate.replays}")
    print(f"\nAnswer: refuted={answer.refuted}")
    print(f"  reason: {answer.reason}")
    print(f"\nUsage: model={usage.model}, {usage.input_tokens} in / {usage.output_tokens} out, "
          f"{usage.latency_ms} ms, ${usage.cost_usd}")
    print(f"\n{criteria_report(mode)}")
    print(f"\nWritten to {out_dir / 'disprove_check.json'}")


if __name__ == "__main__":
    main()
