"""The LLM client (D8 "LLM client"): one entry point, four modes, a usage log and a cost cap.

    client = LLMClient(run_dir)
    answer, usage = client.call("step", SYSTEM["step"], user_text, StepDecision)

Modes, chosen by PROBE_LLM_MODE (default real):
    real     ask the Anthropic API
    record   like real, and also save every prompt and answer to run_dir/llm_record.jsonl
    replay   serve the answers of such a record, by role and call number (the offline fallback)
    fake     serve a hand-written JSON script the same way (for tests)
Every call is appended to run_dir/llm_log.jsonl. The API key is read from the environment only
(ANTHROPIC_API_KEY, filled from .env when present) and is never printed or logged.
"""
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import anthropic
from pydantic import ValidationError

from probe.schemas import StepDecision
from probe.state import PageState

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
MODES = ("real", "record", "replay", "fake")
DEFAULT_MAX_COST_USD = 1.00

# role -> model. Override with PROBE_MODEL_PLAN / _STEP / _JUDGE / _DISPROVE. No date suffixes.
DEFAULT_MODELS = {
    "plan": "claude-sonnet-5",
    "step": "claude-haiku-4-5",
    "judge": "claude-sonnet-5",
    "disprove": "claude-sonnet-5",
}
MAX_TOKENS = {"plan": 1500, "step": 400, "judge": 1500, "disprove": 300}
NO_THINKING = ("step", "judge")  # these calls must be fast, so thinking is switched off

# USD per million tokens (input, output). Prices change: check the console for current prices.
PRICES = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


class LLMError(Exception):
    """Anything that stops an LLM call, with a message a person can act on."""


@dataclass
class Usage:
    role: str
    model: str            # "fake" or "replay" when no API was used
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: float       # estimate from the price table


def load_dotenv(path) -> None:
    """Copy KEY=VALUE lines of a .env file into os.environ. Variables that are already set (and
    not empty) win, and empty values are skipped, so a copy of .env.example changes nothing."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value


def model_for(role: str) -> str:
    return os.environ.get(f"PROBE_MODEL_{role.upper()}") or DEFAULT_MODELS[role]


def _append_line(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


class LLMClient:
    def __init__(self, run_dir, mode: str | None = None, sdk=None, source=None,
                 max_cost_usd: float | None = None, env_file=ENV_FILE):
        """run_dir  the run's folder (llm_log.jsonl and llm_record.jsonl go there)
        mode     real | record | replay | fake; default PROBE_LLM_MODE, else real
        sdk      an object with .messages.parse(...); tests inject a stub, otherwise anthropic.Anthropic()
        source   fake: the script (a dict, or the path of a JSON file {"plan": [...], "step": [...], ...});
                 replay: the path of an llm_record.jsonl
        env_file the .env to read first; None reads nothing"""
        if env_file:
            load_dotenv(env_file)
        self.mode = mode or os.environ.get("PROBE_LLM_MODE") or "real"
        if self.mode not in MODES:
            raise LLMError(f"PROBE_LLM_MODE must be one of {', '.join(MODES)}, not {self.mode!r}")

        if max_cost_usd is None:
            text = os.environ.get("PROBE_MAX_COST_USD") or str(DEFAULT_MAX_COST_USD)
            try:
                max_cost_usd = float(text)
            except ValueError:
                raise LLMError(f"PROBE_MAX_COST_USD must be a number of US dollars, not {text!r}") from None
        self.max_cost_usd = max_cost_usd
        self.spent_usd = 0.0

        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.run_dir / "llm_log.jsonl"
        self.record_path = self.run_dir / "llm_record.jsonl"

        self.sdk = None
        self._answers: dict[str, list[dict]] = {}  # fake / replay: role -> answers in order
        self._used: dict[str, int] = {}            # fake / replay: role -> answers served so far
        if self.mode in ("real", "record"):
            self.sdk = sdk or self._make_sdk()
        else:
            self._answers = self._load_answers(source)

    # ---- set-up -----------------------------------------------------------

    def _make_sdk(self):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError(f"{self.mode} mode needs an API key, but ANTHROPIC_API_KEY is not set. "
                           "Put it in .env (see .env.example) or in the environment. "
                           "To run without a key use PROBE_LLM_MODE=fake or replay.")
        return anthropic.Anthropic()  # reads the key from the environment; retries 2 times by itself

    def _load_answers(self, source) -> dict[str, list[dict]]:
        if source is None:
            raise LLMError(f"{self.mode} mode needs a source: a script for fake, a llm_record.jsonl for replay")
        if self.mode == "fake":
            if isinstance(source, dict):
                script = source
            else:
                try:
                    script = json.loads(Path(source).read_text(encoding="utf-8"))
                except OSError as exc:
                    raise LLMError(f"cannot read the fake script {source}: {exc}") from exc
            unknown = sorted(set(script) - set(DEFAULT_MODELS))
            if unknown:
                raise LLMError(f"the fake script has unknown role(s) {unknown}; the roles are {', '.join(DEFAULT_MODELS)}")
            return script

        try:  # replay
            lines = Path(source).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise LLMError(f"cannot read the record {source}: {exc}") from exc
        answers: dict[str, list[dict]] = {}
        for line in lines:
            if line.strip():
                entry = json.loads(line)
                answers.setdefault(entry["role"], []).append(entry["answer"])
        return answers

    # ---- the one entry point ----------------------------------------------

    def call(self, role: str, system: str, user: str, schema):
        """Ask the model (or serve a scripted answer). Returns (an instance of `schema`, a Usage)."""
        if role not in DEFAULT_MODELS:
            raise LLMError(f"unknown role {role!r}; the roles are {', '.join(DEFAULT_MODELS)}")
        self._check_cap()
        if self.mode in ("fake", "replay"):
            return self._serve(role, schema)
        return self._ask_api(role, system, user, schema)

    def _check_cap(self) -> None:
        if self.spent_usd > self.max_cost_usd:
            raise LLMError(f"estimated cost {self.spent_usd:.4f} USD is over PROBE_MAX_COST_USD "
                           f"({self.max_cost_usd:.2f}); the run is aborted")

    def _serve(self, role: str, schema):
        index = self._used.get(role, 0)
        answers = self._answers.get(role, [])
        if index >= len(answers):
            raise LLMError(f"{self.mode} mode has no answer left for role '{role}': the source holds "
                           f"{len(answers)} for it and call number {index + 1} was made")
        self._used[role] = index + 1
        try:
            parsed = schema.model_validate(answers[index])
        except ValidationError as exc:
            raise LLMError(f"{self.mode} answer number {index + 1} for role '{role}' does not fit "
                           f"{schema.__name__}: {exc}") from exc
        usage = Usage(role, self.mode, 0, 0, 0, 0.0)
        _append_line(self.log_path, asdict(usage))
        return parsed, usage

    def _ask_api(self, role: str, system: str, user: str, schema):
        model = model_for(role)
        price = PRICES.get(model)
        if price is None:
            raise LLMError(f"no price known for model {model!r}: add it to PRICES in probe/llm.py "
                           "so that the cost cap keeps working")
        request = dict(model=model, max_tokens=MAX_TOKENS[role], system=system,
                       messages=[{"role": "user", "content": user}], output_format=schema)
        if role in NO_THINKING:
            request["thinking"] = {"type": "disabled"}

        started = time.monotonic()
        try:
            response = self.sdk.messages.parse(**request)
        except anthropic.AuthenticationError as exc:
            raise LLMError("The API rejected the key (HTTP 401). Check ANTHROPIC_API_KEY.") from exc
        except anthropic.RateLimitError as exc:  # before APIStatusError: it is a subclass
            raise LLMError(f"Rate limit reached (HTTP 429), even after the SDK's own retries: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"The API answered HTTP {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise LLMError(f"Could not reach the API (network problem or timeout): {exc}") from exc
        latency_ms = round((time.monotonic() - started) * 1000)

        tokens_in, tokens_out = response.usage.input_tokens, response.usage.output_tokens
        cost = (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000
        usage = Usage(role, model, tokens_in, tokens_out, latency_ms, round(cost, 6))
        self.spent_usd += cost
        _append_line(self.log_path, asdict(usage))  # the call was paid for, so it is logged even if unusable

        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(f"The model's answer for role '{role}' could not be parsed as {schema.__name__} "
                           f"(stop reason: {response.stop_reason}). If that is max_tokens, the answer was cut off.")
        if self.mode == "record":
            _append_line(self.record_path, {"role": role, "model": model, "system": system, "user": user,
                                            "answer": parsed.model_dump(mode="json")})
        self._check_cap()
        return parsed, usage


# ---- checking what the model chose ------------------------------------------------

ACTIONS_WITH_REF = ("click", "type", "check", "uncheck", "press")


def validate_decision(decision: StepDecision, state: PageState) -> str | None:
    """None if the decision can be carried out on `state`. Otherwise one sentence for the retry:
    the ref must be one of the page's interactive refs, type / press need text, and click / check /
    uncheck take none. done and stuck are always fine."""
    action = decision.action
    if action in ACTIONS_WITH_REF:
        refs = [entry.ref for entry in state.refs]
        choices = ", ".join(refs) or "(the page has no interactive element)"
        if not decision.ref:
            return f"Action {action} needs a ref, but ref is empty. Choose one of: {choices}."
        if decision.ref not in refs:
            return f"Ref {decision.ref!r} is not an interactive element on the current page. Choose one of: {choices}."
    if action == "type" and decision.text is None:
        return "Action type needs text, but text is empty."
    if action == "press" and not decision.text:
        return "Action press needs text (the key, for example Enter), but text is empty."
    if action in ("click", "check", "uncheck") and decision.text:
        return f"Action {action} takes no text, but text is {decision.text!r}. To enter text use action type."
    return None
