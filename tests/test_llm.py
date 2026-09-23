"""The LLM client (probe/llm.py): modes, usage log, cost cap, errors, validate_decision.

No API key and no network anywhere: the SDK is replaced by a stub that mimics `messages.parse`,
and one test runs the real SDK against a canned HTTP transport.
"""
import json
import os
import socket
from dataclasses import replace
from types import SimpleNamespace

import anthropic
import pytest

from conftest import make_entry, make_state
from probe.llm import (DEFAULT_MAX_COST_USD, DEFAULT_MODELS, DEFAULT_TOTAL_BUDGET_USD, MAX_TOKENS,
                       MODES, NO_THINKING, LLMClient, LLMError, load_dotenv, validate_decision)
from probe.schemas import (AppPlan, Disproof, JudgedFinding, Judgement, Mission, StepDecision,
                           StepVerdict)

DECISION = StepDecision(action="click", ref="e3", text=None, expect="The task disappears", reasoning="Delete it.")
PLAN = AppPlan(app_type="task list", capabilities=["add", "tick", "delete"], missions=[
    Mission(id="m1", goal="Add a task", category="core_flow", priority="critical", why="Users lose their list.")])
JUDGEMENT = Judgement(
    step_verdicts=[StepVerdict(step=1, violated=True, reason="The task is still there.")],
    findings=[JudgedFinding(step=2, kind="bug", title="Footer count is wrong", severity="medium",
                            impact="Users misread their workload.", expected="1 item left",
                            observed="2 items left", suggestion="Count active tasks only.")])
DISPROOF = Disproof(refuted=False, reason="No harmless explanation fits.")

ANSWER_FOR_ROLE = {"plan": PLAN, "step": DECISION, "judge": JUDGEMENT, "disprove": DISPROOF}
SCHEMA_FOR_ROLE = {"plan": AppPlan, "step": StepDecision, "judge": Judgement, "disprove": Disproof}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Whatever is set on this machine must not change the tests."""
    for name in ("ANTHROPIC_API_KEY", "PROBE_LLM_MODE", "PROBE_MAX_COST_USD", "PROBE_TOTAL_BUDGET_USD",
                 "PROBE_SPEND_LEDGER", "PROBE_LLM_SOURCE",
                 *(f"PROBE_MODEL_{role.upper()}" for role in DEFAULT_MODELS)):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Belt and braces: if any test in this file tried to reach a server, it would fail right here."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)


class StubSDK:
    """Mimics anthropic.Anthropic() as far as the client uses it: sdk.messages.parse(**kwargs)."""

    def __init__(self, *answers, input_tokens=100, output_tokens=50, error=None):
        self.answers = list(answers)
        self.input_tokens, self.output_tokens, self.error = input_tokens, output_tokens, error
        self.calls = []  # the keyword arguments of every parse() call
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        answer = self.answers.pop(0) if self.answers else None
        return SimpleNamespace(
            parsed_output=answer, stop_reason="end_turn" if answer else "max_tokens",
            usage=SimpleNamespace(input_tokens=self.input_tokens, output_tokens=self.output_tokens))


def client_for(tmp_path, mode="real", **kwargs) -> LLMClient:
    return LLMClient(tmp_path, mode=mode, env_file=None, **kwargs)


def log_lines(tmp_path, name="llm_log.jsonl") -> list[dict]:
    path = tmp_path / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def http_error(cls, status, message):
    return cls(message, response=SimpleNamespace(status_code=status, headers={}, request=None), body=None)


# ---- real mode: what is sent, what comes back, what is logged -------------------------

def test_real_mode_sends_the_request_returns_the_parsed_object_and_logs_the_usage(tmp_path):
    sdk = StubSDK(DECISION, input_tokens=100, output_tokens=50)
    client = client_for(tmp_path, sdk=sdk)

    answer, usage = client.call("step", "SYSTEM TEXT", "USER TEXT", StepDecision)

    assert answer is DECISION
    [sent] = sdk.calls
    assert sent["model"] == "claude-haiku-4-5" and sent["max_tokens"] == 400
    assert sent["system"] == "SYSTEM TEXT"
    assert sent["messages"] == [{"role": "user", "content": "USER TEXT"}]
    assert sent["output_format"] is StepDecision
    assert sent["thinking"] == {"type": "disabled"}

    assert (usage.role, usage.model, usage.input_tokens, usage.output_tokens) == ("step", "claude-haiku-4-5", 100, 50)
    assert usage.cost_usd == pytest.approx((100 * 1.0 + 50 * 5.0) / 1_000_000)  # haiku: 1 / 5 USD per million
    assert usage.latency_ms >= 0
    [line] = log_lines(tmp_path)
    assert line == {"role": "step", "model": "claude-haiku-4-5", "input_tokens": 100, "output_tokens": 50,
                    "latency_ms": usage.latency_ms, "cost_usd": usage.cost_usd}
    assert client.spent_usd == pytest.approx(usage.cost_usd)


def test_each_role_gets_its_model_token_limit_and_thinking_setting(tmp_path):
    sdk = StubSDK(PLAN, DECISION, JUDGEMENT, DISPROOF)
    client = client_for(tmp_path, sdk=sdk)
    for role in ("plan", "step", "judge", "disprove"):
        client.call(role, "s", "u", SCHEMA_FOR_ROLE[role])

    by_role = dict(zip(("plan", "step", "judge", "disprove"), sdk.calls))
    assert {r: c["model"] for r, c in by_role.items()} == {
        "plan": "claude-sonnet-5", "step": "claude-haiku-4-5", "judge": "claude-sonnet-5", "disprove": "claude-sonnet-5"}
    assert {r: c["max_tokens"] for r, c in by_role.items()} == {"plan": 1500, "step": 400, "judge": 1500, "disprove": 1500}
    assert {r: c.get("thinking") for r, c in by_role.items()} == {
        "plan": None, "step": {"type": "disabled"}, "judge": {"type": "disabled"}, "disprove": None}
    assert all(c["output_format"] is SCHEMA_FOR_ROLE[r] for r, c in by_role.items())


def test_every_thinking_enabled_role_has_room_for_thinking_plus_an_answer():
    """T9-a: the invariant that was actually violated. disprove ran with thinking ENABLED (it is
    not in NO_THINKING) on a 300-token budget - and thinking tokens count against max_tokens, so
    the budget was gone before the answer was emitted. It went unseen because no real disprove
    call had ever been made; the first one, on 2026-09-23, failed and aborted a paid 7-run batch."""
    for role, limit in MAX_TOKENS.items():
        if role not in NO_THINKING:
            assert limit >= 1500, f"{role} thinks but only has {limit} tokens for thinking AND the answer"


def test_models_come_from_environment_variables_and_an_empty_one_means_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_MODEL_STEP", "claude-sonnet-5")
    monkeypatch.setenv("PROBE_MODEL_JUDGE", "")  # as in a copied .env.example
    sdk = StubSDK(DECISION, JUDGEMENT)
    client = client_for(tmp_path, sdk=sdk)
    client.call("step", "s", "u", StepDecision)
    client.call("judge", "s", "u", Judgement)
    assert [c["model"] for c in sdk.calls] == ["claude-sonnet-5", "claude-sonnet-5"]
    assert DEFAULT_MODELS["step"] == "claude-haiku-4-5"


def test_a_model_without_a_known_price_is_refused_before_any_call(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_MODEL_STEP", "some-future-model")
    sdk = StubSDK(DECISION)
    with pytest.raises(LLMError, match="no price known for model 'some-future-model'"):
        client_for(tmp_path, sdk=sdk).call("step", "s", "u", StepDecision)
    assert sdk.calls == []


def test_an_unknown_role_or_mode_is_an_error(tmp_path):
    with pytest.raises(LLMError, match="unknown role 'boss'"):
        client_for(tmp_path, sdk=StubSDK()).call("boss", "s", "u", StepDecision)
    with pytest.raises(LLMError, match="PROBE_LLM_MODE must be one of"):
        client_for(tmp_path, mode="banana")


def test_the_mode_has_no_default_and_must_be_set_explicitly(tmp_path, monkeypatch):
    # D9: no default, so a run never spends money by accident.
    with pytest.raises(LLMError, match="PROBE_LLM_MODE .* is not set") as error:
        LLMClient(tmp_path, env_file=None)
    assert "no default" in str(error.value)
    for name in MODES:
        assert name in str(error.value)
    monkeypatch.setenv("PROBE_LLM_MODE", "fake")
    assert LLMClient(tmp_path, source={}, env_file=None).mode == "fake"


# ---- cost accounting and the cap ------------------------------------------------------

def test_cost_accumulates_and_the_cap_aborts_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_MAX_COST_USD", "0.01")
    sdk = StubSDK(PLAN, PLAN, PLAN, input_tokens=1000, output_tokens=500)  # sonnet-5: 0.007 USD per call
    client = client_for(tmp_path, sdk=sdk)

    client.call("plan", "s", "u", AppPlan)
    assert client.spent_usd == pytest.approx(0.007)

    with pytest.raises(LLMError, match=r"0\.0140 USD is over PROBE_MAX_COST_USD \(0\.01\)"):
        client.call("plan", "s", "u", AppPlan)
    assert client.spent_usd == pytest.approx(0.014)
    assert len(log_lines(tmp_path)) == 2  # the call that crossed the cap was paid for, so it is logged

    with pytest.raises(LLMError, match="PROBE_MAX_COST_USD"):  # and nothing more is sent
        client.call("plan", "s", "u", AppPlan)
    assert len(sdk.calls) == 2


def test_spending_exactly_the_cap_is_allowed_and_only_more_than_the_cap_aborts(tmp_path):
    sdk = StubSDK(PLAN, PLAN, input_tokens=250_000, output_tokens=0)  # sonnet-5: exactly 0.50 USD per call
    client = client_for(tmp_path, sdk=sdk, max_cost_usd=0.50)
    client.call("plan", "s", "u", AppPlan)         # 0.50 of 0.50: "exceeds" means strictly more
    assert client.spent_usd == 0.50
    with pytest.raises(LLMError, match="PROBE_MAX_COST_USD"):
        client.call("plan", "s", "u", AppPlan)     # 1.00 of 0.50


def test_the_cap_defaults_to_thirty_cents_and_can_be_set(tmp_path, monkeypatch):
    assert DEFAULT_MAX_COST_USD == 0.30  # D9: lowered from 1.00 so a run cannot spend much by itself
    assert client_for(tmp_path, sdk=StubSDK()).max_cost_usd == 0.30
    monkeypatch.setenv("PROBE_MAX_COST_USD", "2.5")
    assert client_for(tmp_path, sdk=StubSDK()).max_cost_usd == 2.5
    assert client_for(tmp_path, sdk=StubSDK(), max_cost_usd=0.25).max_cost_usd == 0.25
    monkeypatch.setenv("PROBE_MAX_COST_USD", "a lot")
    with pytest.raises(LLMError, match="PROBE_MAX_COST_USD must be a number"):
        client_for(tmp_path, sdk=StubSDK())


# ---- the cumulative spend ledger (D9 item 3) -------------------------------------------

def test_without_a_ledger_path_there_is_no_cumulative_check(tmp_path):
    # Existing callers (and every other test in this file) pass no ledger_path: unaffected.
    sdk = StubSDK(PLAN, PLAN, input_tokens=1_000_000, output_tokens=1_000_000)  # 12 USD each, no per-run cap set low
    client = client_for(tmp_path, sdk=sdk, max_cost_usd=100)
    client.call("plan", "s", "u", AppPlan)
    client.call("plan", "s", "u", AppPlan)
    assert client.spent_usd == pytest.approx(24)
    assert client.ledger_path is None


def test_the_ledger_is_shared_across_clients_and_blocks_once_it_is_already_over_budget(tmp_path):
    ledger = tmp_path / "spend_ledger.jsonl"
    sdk1 = StubSDK(PLAN, input_tokens=1_000_000, output_tokens=1_000_000)  # sonnet-5: 12 USD
    first = client_for(tmp_path / "run1", sdk=sdk1, max_cost_usd=100, ledger_path=ledger, total_budget_usd=8)
    first.call("plan", "s", "u", AppPlan)  # spent from THIS client's own cap; the ledger now holds 12

    sdk2 = StubSDK(PLAN)
    second = client_for(tmp_path / "run2", sdk=sdk2, max_cost_usd=100, ledger_path=ledger, total_budget_usd=8)
    with pytest.raises(LLMError, match=r"already totals 12\.0000 USD, at or over PROBE_TOTAL_BUDGET_USD \(8\.00\)"):
        second.call("plan", "s", "u", AppPlan)
    assert sdk2.calls == []  # refused before any request was sent
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1  # only the first client's call is on it


def test_the_ledger_refuses_once_it_is_at_or_over_the_total_budget(tmp_path):
    # Checked BEFORE a call, when its cost is not yet known: landing exactly on the budget still
    # blocks the NEXT call (unlike the per-run cap, which allows a call that lands exactly on it).
    ledger = tmp_path / "spend_ledger.jsonl"
    sdk = StubSDK(PLAN, PLAN, input_tokens=250_000, output_tokens=0)  # sonnet-5: exactly 0.50 USD per call
    client = client_for(tmp_path, sdk=sdk, max_cost_usd=100, ledger_path=ledger, total_budget_usd=0.50)
    client.call("plan", "s", "u", AppPlan)      # ledger was empty before this call: allowed
    assert client._ledger_total() == pytest.approx(0.50)
    with pytest.raises(LLMError, match="PROBE_TOTAL_BUDGET_USD"):
        client.call("plan", "s", "u", AppPlan)  # ledger already at 0.50 of 0.50: refused


def test_fake_and_replay_calls_never_touch_the_ledger(tmp_path):
    ledger = tmp_path / "spend_ledger.jsonl"
    client_for(tmp_path, mode="fake", source={"plan": [PLAN.model_dump()]}, ledger_path=ledger, total_budget_usd=0).call(
        "plan", "s", "u", AppPlan)
    assert not ledger.exists()  # a cap of 0 would refuse a real call instantly; fake mode never checks


def test_the_total_budget_defaults_to_eight_dollars_and_can_be_set(tmp_path, monkeypatch):
    assert DEFAULT_TOTAL_BUDGET_USD == 8.00
    assert client_for(tmp_path, sdk=StubSDK(), ledger_path=tmp_path / "l.jsonl").total_budget_usd == 8.00
    monkeypatch.setenv("PROBE_TOTAL_BUDGET_USD", "3")
    assert client_for(tmp_path, sdk=StubSDK(), ledger_path=tmp_path / "l.jsonl").total_budget_usd == 3.0


def test_ledger_path_falls_back_to_probe_spend_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_SPEND_LEDGER", str(tmp_path / "shared.jsonl"))
    client = client_for(tmp_path, sdk=StubSDK())
    assert client.ledger_path == tmp_path / "shared.jsonl"


# ---- PROBE_LLM_SOURCE (D9 ruling 6) ----------------------------------------------------

def test_fake_source_falls_back_to_probe_llm_source(tmp_path, monkeypatch):
    path = tmp_path / "script.json"
    path.write_text(json.dumps({"plan": [PLAN.model_dump()]}), encoding="utf-8")
    monkeypatch.setenv("PROBE_LLM_SOURCE", str(path))
    client = client_for(tmp_path, mode="fake")
    assert client.call("plan", "s", "u", AppPlan)[0] == PLAN


def test_the_price_table_is_sonnet_2_10_and_haiku_1_5_per_million(tmp_path):
    sdk = StubSDK(PLAN, DECISION, input_tokens=1_000_000, output_tokens=1_000_000)
    client = client_for(tmp_path, sdk=sdk, max_cost_usd=100)  # a million tokens each way would trip the default cap
    assert client.call("plan", "s", "u", AppPlan)[1].cost_usd == pytest.approx(2 + 10)
    assert client.call("step", "s", "u", StepDecision)[1].cost_usd == pytest.approx(1 + 5)


# ---- fake mode ---------------------------------------------------------------------

def test_fake_mode_serves_the_script_by_role_and_call_index(tmp_path):
    other = DECISION.model_copy(update={"ref": "e5"})
    script = {"plan": [PLAN.model_dump()], "step": [DECISION.model_dump(), other.model_dump()]}
    client = client_for(tmp_path, mode="fake", source=script)

    assert client.call("step", "s", "u", StepDecision)[0] == DECISION
    assert client.call("plan", "s", "u", AppPlan)[0] == PLAN   # each role has its own counter
    answer, usage = client.call("step", "s", "u", StepDecision)
    assert answer == other and isinstance(answer, StepDecision)
    assert (usage.model, usage.input_tokens, usage.output_tokens, usage.cost_usd) == ("fake", 0, 0, 0.0)
    assert [(line["role"], line["model"]) for line in log_lines(tmp_path)] == [
        ("step", "fake"), ("plan", "fake"), ("step", "fake")]  # fake calls are logged too
    assert client.spent_usd == 0.0


def test_fake_mode_says_clearly_when_the_script_runs_out(tmp_path):
    client = client_for(tmp_path, mode="fake", source={"step": [DECISION.model_dump()]})
    client.call("step", "s", "u", StepDecision)
    with pytest.raises(LLMError, match=r"fake mode has no answer left for role 'step'.*holds 1.*call number 2"):
        client.call("step", "s", "u", StepDecision)
    with pytest.raises(LLMError, match=r"no answer left for role 'judge'.*holds 0.*call number 1"):
        client.call("judge", "s", "u", Judgement)


def test_fake_mode_reads_a_json_file_and_rejects_bad_scripts(tmp_path):
    path = tmp_path / "script.json"
    path.write_text(json.dumps({"disprove": [DISPROOF.model_dump()]}), encoding="utf-8")
    assert client_for(tmp_path, mode="fake", source=path).call("disprove", "s", "u", Disproof)[0] == DISPROOF

    with pytest.raises(LLMError, match="unknown role"):
        client_for(tmp_path, mode="fake", source={"steps": []})
    with pytest.raises(LLMError, match="needs a source"):
        client_for(tmp_path, mode="fake")
    with pytest.raises(LLMError, match="cannot read the fake script"):
        client_for(tmp_path, mode="fake", source=tmp_path / "missing.json")

    wrong_shape = client_for(tmp_path, mode="fake", source={"step": [{"action": "hover"}]})
    with pytest.raises(LLMError, match=r"answer number 1 for role 'step' does not fit StepDecision"):
        wrong_shape.call("step", "s", "u", StepDecision)


# ---- record and replay ----------------------------------------------------------------

def test_record_then_replay_gives_the_same_answers_without_an_sdk(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    sdk = StubSDK(PLAN, DECISION, DECISION.model_copy(update={"action": "done", "ref": None}))
    recorder = client_for(first, mode="record", sdk=sdk)
    recorded = [recorder.call("plan", "SYS-plan", "USER-1", AppPlan)[0],
                recorder.call("step", "SYS-step", "USER-2", StepDecision)[0],
                recorder.call("step", "SYS-step", "USER-3", StepDecision)[0]]

    lines = log_lines(first, "llm_record.jsonl")
    assert [(l["role"], l["system"], l["user"]) for l in lines] == [
        ("plan", "SYS-plan", "USER-1"), ("step", "SYS-step", "USER-2"), ("step", "SYS-step", "USER-3")]
    assert lines[0]["answer"] == PLAN.model_dump(mode="json")
    assert len(log_lines(first)) == 3  # record mode also writes the usage log

    replayer = client_for(second, mode="replay", source=first / "llm_record.jsonl")  # no sdk, no key
    assert replayer.sdk is None
    replayed = [replayer.call("plan", "x", "x", AppPlan)[0],
                replayer.call("step", "x", "x", StepDecision)[0],
                replayer.call("step", "x", "x", StepDecision)[0]]
    assert replayed == recorded
    assert [line["model"] for line in log_lines(second)] == ["replay"] * 3

    with pytest.raises(LLMError, match="replay mode has no answer left for role 'step'"):
        replayer.call("step", "x", "x", StepDecision)


def test_replay_needs_a_record_that_exists(tmp_path):
    with pytest.raises(LLMError, match="cannot read the record"):
        client_for(tmp_path, mode="replay", source=tmp_path / "nothing.jsonl")
    with pytest.raises(LLMError, match="needs a source"):
        client_for(tmp_path, mode="replay")


def test_an_answer_that_cannot_be_parsed_is_an_error_but_still_logged(tmp_path):
    client = client_for(tmp_path, mode="record", sdk=StubSDK(None))  # parsed_output is None, stop reason max_tokens
    with pytest.raises(LLMError, match=r"could not be parsed as StepDecision \(stop reason: max_tokens\)"):
        client.call("step", "s", "u", StepDecision)
    assert len(log_lines(tmp_path)) == 1          # paid for, so logged
    assert log_lines(tmp_path, "llm_record.jsonl") == []  # nothing usable to record


# ---- the API key ---------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["real", "record"])
def test_a_missing_api_key_gives_a_clear_message(tmp_path, mode):
    with pytest.raises(LLMError) as error:
        client_for(tmp_path, mode=mode)
    message = str(error.value)
    assert "ANTHROPIC_API_KEY is not set" in message and ".env" in message and mode in message


def test_fake_and_replay_need_no_key_and_an_injected_sdk_needs_none_either(tmp_path):
    client_for(tmp_path, mode="fake", source={})
    client_for(tmp_path, mode="real", sdk=StubSDK())


def test_with_a_key_the_real_sdk_object_is_built_without_calling_anything(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    client = client_for(tmp_path, mode="real")
    assert isinstance(client.sdk, anthropic.Anthropic)
    assert client.sdk.max_retries == 2  # the SDK's own retries are what we rely on


def test_the_key_never_shows_up_in_errors_logs_or_records(tmp_path, monkeypatch):
    secret = "sk-ant-secret-value-123"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    ok = client_for(tmp_path / "ok", mode="record", sdk=StubSDK(DECISION))
    ok.call("step", "s", "u", StepDecision)
    bad = client_for(tmp_path / "bad", sdk=StubSDK(error=http_error(anthropic.AuthenticationError, 401, "invalid x-api-key")))
    with pytest.raises(LLMError) as error:
        bad.call("step", "s", "u", StepDecision)

    assert secret not in str(error.value) and secret not in repr(error.value)
    for folder in (tmp_path / "ok", tmp_path / "bad"):
        for file in folder.iterdir():
            assert secret not in file.read_text(encoding="utf-8")


def test_dotenv_fills_missing_variables_only(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nANTHROPIC_API_KEY=sk-from-file\nPROBE_MODEL_STEP=\n"
                        "PROBE_MAX_COST_USD = \"0.5\"\nPROBE_LLM_MODE=fake\nno equals sign here\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")           # empty counts as not set, and is restored afterwards
    monkeypatch.setenv("PROBE_MAX_COST_USD", "")
    monkeypatch.setenv("PROBE_MODEL_STEP", "")
    monkeypatch.setenv("PROBE_LLM_MODE", "replay")        # already set: the file must not override it

    load_dotenv(env_file)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-from-file"
    assert os.environ["PROBE_MAX_COST_USD"] == "0.5"      # quotes and spaces around the value are stripped
    assert os.environ["PROBE_MODEL_STEP"] == ""           # an empty value in the file is skipped
    assert os.environ["PROBE_LLM_MODE"] == "replay"
    load_dotenv(tmp_path / "no-such-file")                # a missing .env is fine


def test_the_client_reads_the_env_file_it_is_given(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("PROBE_MAX_COST_USD=0.42\n", encoding="utf-8")
    monkeypatch.setenv("PROBE_MAX_COST_USD", "")
    client = LLMClient(tmp_path / "run", mode="fake", source={}, env_file=env_file)
    assert client.max_cost_usd == 0.42


# ---- errors from the SDK -----------------------------------------------------------------

@pytest.mark.parametrize("error, expected", [
    (http_error(anthropic.AuthenticationError, 401, "invalid x-api-key"), r"rejected the key \(HTTP 401\)"),
    (http_error(anthropic.RateLimitError, 429, "slow down"), r"Rate limit reached \(HTTP 429\).*slow down"),
    (http_error(anthropic.BadRequestError, 400, "Your credit balance is too low"), "HTTP 400: Your credit balance is too low"),
    (http_error(anthropic.InternalServerError, 500, "boom"), "HTTP 500: boom"),
    (anthropic.APIConnectionError(request=None), "Could not reach the API"),
    (anthropic.APITimeoutError(request=None), "Could not reach the API"),
])
def test_sdk_errors_become_llm_errors_with_the_original_attached(tmp_path, error, expected):
    client = client_for(tmp_path, sdk=StubSDK(error=error))
    with pytest.raises(LLMError, match=expected) as caught:
        client.call("step", "s", "u", StepDecision)
    assert caught.value.__cause__ is error
    assert log_lines(tmp_path) == [] and client.spent_usd == 0.0  # no answer, no cost


# ---- the real SDK against a canned HTTP answer (still no network) -----------------------------

def test_the_real_sdk_builds_the_expected_request_and_parses_the_answer(tmp_path):
    httpx2 = pytest.importorskip("httpx2")
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": DECISION.model_dump_json()}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 123, "output_tokens": 45}})

    sdk = anthropic.Anthropic(api_key="sk-test-not-a-real-key",
                              http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    answer, usage = client_for(tmp_path, sdk=sdk).call("step", "SYSTEM TEXT", "USER TEXT", StepDecision)

    assert answer == DECISION and (usage.input_tokens, usage.output_tokens) == (123, 45)
    body = seen["body"]
    assert seen["path"] == "/v1/messages"
    assert (body["model"], body["max_tokens"], body["system"]) == ("claude-haiku-4-5", 400, "SYSTEM TEXT")
    assert body["messages"] == [{"role": "user", "content": "USER TEXT"}]
    assert body["thinking"] == {"type": "disabled"}
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["title"] == "StepDecision"


# ---- validate_decision ---------------------------------------------------------------------

def page_state():
    refs = [replace(make_entry("textbox", "New task", value=""), ref="e1"),
            replace(make_entry("button", "Add"), ref="e2"),
            replace(make_entry("checkbox", "Buy milk", checked=False), ref="e3")]
    return make_state(refs)


def decision(action, ref=None, text=None) -> StepDecision:
    return StepDecision(action=action, ref=ref, text=text, expect="something changes", reasoning="because")


@pytest.mark.parametrize("d", [
    decision("click", "e2"),
    decision("check", "e3"),
    decision("uncheck", "e3"),
    decision("type", "e1", "Call Bob"),
    decision("type", "e1", ""),          # typing nothing (clearing the field) is allowed
    decision("type", "e1", "   "),
    decision("press", "e1", "Enter"),
    decision("done"),
    decision("stuck"),
    decision("done", "e2"),              # done / stuck are not checked: the loop ignores ref and text
])
def test_valid_decisions_pass(d):
    assert validate_decision(d, page_state()) is None


def test_a_ref_must_be_one_of_the_pages_interactive_refs():
    error = validate_decision(decision("click", "e99"), page_state())
    assert "'e99'" in error and "not an interactive element" in error
    assert "e1, e2, e3" in error                                        # says what to choose from, for the retry
    assert "needs a ref" in validate_decision(decision("click"), page_state())
    assert "needs a ref" in validate_decision(decision("type", None, "hi"), page_state())
    assert "'e2'" in validate_decision(decision("click", "e2"), make_state([]))  # a page with no refs at all
    assert "no interactive element" in validate_decision(decision("click", "e2"), make_state([]))


def test_type_and_press_need_text_and_click_check_uncheck_take_none():
    assert "type needs text" in validate_decision(decision("type", "e1"), page_state())
    assert "press needs text" in validate_decision(decision("press", "e1"), page_state())
    assert "press needs text" in validate_decision(decision("press", "e1", ""), page_state())
    for action in ("click", "check", "uncheck"):
        error = validate_decision(decision(action, "e3", "hello"), page_state())
        assert f"{action} takes no text" in error and "action type" in error
