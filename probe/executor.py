"""Carry out one step in the browser and record the semantic locator that was used.

A step names its target either by the ref the model picked ({"ref": "e8"}, live runs) or by role,
name and nth ({"role": "button", "name": "Delete Buy milk", "nth": 0}, scripts and replay). Either
way the record keeps role + name + nth, because refs do not survive a reload and replay needs them.
"""
import time
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError

from probe.state import PageState, RefEntry

ACTION_TIMEOUT_MS = 5000  # how long Playwright may wait for an element to become clickable
ACTIONS = ("click", "type", "check", "uncheck", "press")


@dataclass
class Step:
    action: str                 # click | type | check | uncheck | press
    target: dict                # {"ref": "e8"}  or  {"role": ..., "name": ..., "nth": 0}
    text: str | None = None     # what to type, or the key to press (e.g. "Enter")
    expect: str | None = None   # what the agent expects to change


@dataclass
class StepRecord:
    action: str
    ref: str | None             # the ref the agent picked, if it did
    locator: dict | None        # {"role", "name", "nth"}
    text: str | None
    expect: str | None
    error: str | None = None    # set when the step could not be carried out
    blocked: str | None = None  # set when the safety policy refused it (nothing was executed)
    action_ms: int = 0
    settle_ms: int = 0


def find_entry(state: PageState, target: dict) -> RefEntry | None:
    """The actionable element in `state` that the target points at, or None."""
    if "ref" in target:
        return next((r for r in state.refs if r.ref == target["ref"]), None)
    wanted = (target.get("role"), target.get("name"), target.get("nth", 0))
    return next((r for r in state.refs if (r.role, r.name, r.nth) == wanted), None)


def _locator_of(step: Step, entry: RefEntry | None) -> dict | None:
    if entry is not None:
        return {"role": entry.role, "name": entry.name, "nth": entry.nth}
    if "role" in step.target and "name" in step.target:
        return {"role": step.target["role"], "name": step.target["name"], "nth": step.target.get("nth", 0)}
    return None


def _resolve(page, step: Step, locator: dict):
    if "ref" in step.target:
        return page.locator(f"aria-ref={step.target['ref']}")  # live run: valid for the current snapshot
    return page.get_by_role(locator["role"], name=locator["name"], exact=True).nth(locator["nth"])


def _blocked_field(locator, step: Step, policy) -> str:
    """The policy's second look at a field we are about to type into. Returns the reason, or ""."""
    if policy is None or step.action not in ("type", "press"):
        return ""
    allowed, reason = policy.check_field(step, locator.get_attribute("type", timeout=ACTION_TIMEOUT_MS))
    return "" if allowed else reason


def _perform(locator, step: Step):
    if step.action == "click":
        locator.click(timeout=ACTION_TIMEOUT_MS)
    elif step.action == "type":
        locator.fill(step.text, timeout=ACTION_TIMEOUT_MS)
    elif step.action == "press":
        locator.press(step.text, timeout=ACTION_TIMEOUT_MS)
    else:
        # check / uncheck: click only when the state differs. Playwright's own check() verifies the
        # box afterwards and fails on pages that refuse or undo the change; we want to record that, not fail.
        want = step.action == "check"
        if locator.is_checked(timeout=ACTION_TIMEOUT_MS) != want:
            locator.click(timeout=ACTION_TIMEOUT_MS)


def execute_step(page, state: PageState, step: Step, recorder=None, policy=None) -> StepRecord:
    """Run one step against `state`, which must be the latest capture_state(page). Never raises
    for problems with the step itself: they end up in the record.

    policy   optional SafetyPolicy; a blocked step is recorded with its reason and not executed.
    recorder optional EvidenceRecorder; used to wait until no request is in flight after the action.
    """
    entry = find_entry(state, step.target)
    record = StepRecord(action=step.action, ref=step.target.get("ref"),
                        locator=_locator_of(step, entry), text=step.text, expect=step.expect)

    if policy is not None:
        allowed, reason = policy.check(step, entry, state)
        if not allowed:
            record.blocked = reason
            return record

    if step.action not in ACTIONS:
        record.error = f"unknown action {step.action!r}"
    elif step.action in ("type", "press") and step.text is None:
        record.error = f"{step.action} needs text"
    elif "ref" in step.target and entry is None:
        record.error = f"ref {step.target['ref']!r} is not an actionable element on the current page"
    elif record.locator is None:
        record.error = f"no element for target {step.target}"
    else:
        started = time.monotonic()
        try:
            locator = _resolve(page, step, record.locator)
            if locator.count() == 0:
                record.error = f"element not found: {record.locator}"
            else:
                record.blocked = _blocked_field(locator, step, policy) or None
                if record.blocked is None:
                    _perform(locator, step)
        except PlaywrightError as exc:
            record.error = str(exc).splitlines()[0]  # first line only, the rest is Playwright's call log
        record.action_ms = round((time.monotonic() - started) * 1000)
        if record.blocked:
            return record  # nothing was done, nothing to wait for

    started = time.monotonic()
    if recorder is not None:
        recorder.wait_until_settled()
    else:
        page.wait_for_timeout(300)
    record.settle_ms = round((time.monotonic() - started) * 1000)
    return record
