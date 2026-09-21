"""Oracles: turn what the browser recorded for one step into signals. Plain rules, no LLM.

Hard signals are things a working app should not do, whatever the user wanted:
  page_error (uncaught JavaScript error), request_failed (network failure, same origin only),
  http_5xx (same origin).
Contextual signals can be perfectly fine behaviour, depending on intent, so they need more support
before they count as a bug:
  http_4xx (same origin, and only when the page stayed silent), no_effect, state_not_reached,
  overflow, console_error.
"""
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from probe.evidence import StepEvidence
from probe.executor import Run, StepRecord, find_entry
from probe.safety import origin_of
from probe.state import PageState

HARD_KINDS = {"page_error", "request_failed", "http_5xx"}

_ID_SEGMENT = re.compile(
    r"/(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?=/|$)")


@dataclass
class Signal:
    kind: str
    strength: str            # "hard" or "contextual"
    step: int
    detail: str              # short sentence for a human
    key: str                 # normalised, so the same problem gets the same key in every run
    reproduced_n: int = 0    # in how many replays it came back; filled in by findings.build_candidates

    def to_dict(self) -> dict:
        return asdict(self)


def _signal(kind: str, step: int, detail: str, key: str) -> Signal:
    return Signal(kind, "hard" if kind in HARD_KINDS else "contextual", step, detail, key)


def _squeeze(text: str | None) -> str:
    return " ".join((text or "").split())


def _message_key(text: str) -> str:
    """First line, numbers replaced, so that counters and timestamps do not make keys differ."""
    first = _squeeze(text.splitlines()[0] if text else "")
    return re.sub(r"\d+", "{n}", first)[:100]


def _request_key(method: str, url: str, outcome: str) -> str:
    """'DELETE /api/tasks/{id} 500': query string dropped, numeric and uuid path segments replaced."""
    path = _ID_SEGMENT.sub("/{id}", urlsplit(url).path)
    return f"{method} {path} {outcome}"


def _target(record: StepRecord) -> str:
    loc = record.locator or {}
    return f'{loc.get("role", "?")} "{loc.get("name", "?")}"'


def _request_signals(evidence: StepEvidence, base_origin: str) -> list[Signal]:
    """Only requests to the app's own origin can raise a signal; a third-party failure stays in the
    evidence. A 4xx counts only when the page stayed silent (same fingerprint before and after the
    step): a 4xx that the page turned into a visible message is normal validation (D8 ruling 1)."""
    step, signals = evidence.step, []
    ui_silent = evidence.fingerprint_before == evidence.fingerprint_after
    for r in evidence.requests:
        method, url, status = r["method"], r["url"], r["status"]
        if origin_of(url) != origin_of(base_origin):
            continue
        where = f"{method} {urlsplit(url).path}"
        if status is None:  # the request never got an answer
            error = r["error"] or ""
            if "aborted" in error.lower() or "cancel" in error.lower():
                continue  # the page moved on or cancelled it itself
            signals.append(_signal("request_failed", step, f"{where} failed: {error}",
                                   _request_key(method, url, "failed")))
        elif status >= 500:
            signals.append(_signal("http_5xx", step, f"{where} answered {status}",
                                   _request_key(method, url, str(status))))
        elif status >= 400 and ui_silent:
            signals.append(_signal("http_4xx", step, f"{where} answered {status} and the page showed nothing",
                                   _request_key(method, url, str(status))))
    return signals


def _no_effect(evidence: StepEvidence, record: StepRecord) -> Signal | None:
    """Fingerprint unchanged, no request, no dialog. Typing only spaces is exempt: the snapshot
    squeezes whitespace away, so the fingerprint cannot show it."""
    if record.action == "type" and not _squeeze(record.text):
        return None
    if (evidence.fingerprint_before == evidence.fingerprint_after
            and not evidence.requests and not evidence.dialogs):
        return _signal("no_effect", evidence.step,
                       f"{record.action} on {_target(record)}: the page did not change and no request was sent",
                       f"{record.action} {_target(record)}")
    return None


def _state_not_reached(evidence: StepEvidence, record: StepRecord, state_after: PageState) -> Signal | None:
    """check / uncheck: the box must be in the requested state afterwards.
    type: the field must hold the typed text afterwards (spaces squeezed), unless a request
    succeeded during the step, because then the page may have taken the text and cleared the field."""
    if record.action not in ("check", "uncheck", "type") or record.locator is None:
        return None
    entry = find_entry(state_after, record.locator)
    if entry is None:
        return None  # the element is gone or renamed; we cannot tell

    if record.action in ("check", "uncheck"):
        want = record.action == "check"
        if entry.checked is None or entry.checked == want:
            return None
        detail = f'{record.action} requested on {_target(record)}, but it is {"checked" if entry.checked else "unchecked"} afterwards'
    else:
        typed, held = _squeeze(record.text)[:200], _squeeze(entry.value)[:200]  # long values: first 200 characters
        if entry.value is None or typed == held:
            return None
        if any(r["status"] is not None and 200 <= r["status"] < 300 for r in evidence.requests):
            return None
        detail = f'typed "{typed[:40]}" into {_target(record)}, but it holds "{held[:40]}" afterwards'
    return _signal("state_not_reached", evidence.step, detail,
                   f"{record.action} {_target(record)}")


def detect_signals(evidence: StepEvidence, record: StepRecord, state_before: PageState,
                   state_after: PageState, base_origin: str) -> list[Signal]:
    """All signals one step raised. A blocked step did nothing, so it raises nothing. A step that
    failed with an error can still show events, but no_effect and state_not_reached need a step that ran."""
    if record.blocked:
        return []
    step = evidence.step
    signals: list[Signal] = []

    for e in evidence.page_errors:
        signals.append(_signal("page_error", step, f'uncaught page error: {e["message"][:120]}',
                               _message_key(e["message"])))
    signals += _request_signals(evidence, base_origin)

    if record.error is None:
        for found in (_no_effect(evidence, record), _state_not_reached(evidence, record, state_after)):
            if found:
                signals.append(found)

    if evidence.scroll_width > evidence.client_width:
        already = " (it was already like that before this step)" if state_before.scroll_width > state_before.client_width else ""
        signals.append(_signal(
            "overflow", step,
            f"page is {evidence.scroll_width}px wide in a {evidence.client_width}px window{already}",
            f"page {urlsplit(state_after.url).path}"))

    for c in evidence.console:
        if c["type"] == "error" and not c["resource_load_error"]:  # those are merged into the http signal
            signals.append(_signal("console_error", step, f'console error: {c["text"][:120]}',
                                   _message_key(c["text"])))

    unique, seen = [], set()
    for s in signals:  # the same problem twice in one step is one signal
        if (s.kind, s.key) not in seen:
            seen.add((s.kind, s.key))
            unique.append(s)
    return unique


def signals_for_run(run: Run, base_origin: str) -> list[Signal]:
    signals: list[Signal] = []
    for r in run.results:
        signals += detect_signals(r.evidence, r.record, r.state_before, r.state_after, base_origin)
    return signals
