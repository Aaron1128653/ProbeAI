"""Checks for the browser harness in probe/ (no LLM anywhere).

Real TaskBoard server and real Chromium, see conftest.py. Steps are scripted by hand.
"""
import json
import re
from urllib.parse import urlsplit

import pytest

from conftest import fresh_app, http, load_steps
from probe import executor
from probe.browser import new_context
from probe.evidence import DEFAULT_IGNORE_PATHS, EvidenceRecorder
from probe.executor import Step, execute_step
from probe.run_script import run
from probe.safety import SafetyPolicy
from probe.state import (PLAIN_CAP, SNAPSHOT_CAP, PageState, RefEntry, capture_state, parse_refs,
                         render_for_llm)

BUGGY = "/"
CLEAN = "/?bugs=off"


@pytest.fixture
def page(server, browser):
    """A fresh context and page on a freshly reset app with an empty trigger log."""
    fresh_app(server)
    context = new_context(browser)
    yield context.new_page()
    context.close()


def paths(requests: list[dict]) -> list[str]:
    return [urlsplit(r["url"]).path for r in requests]


def trigger_ids(server: str) -> list[str]:
    _, text = http("GET", server + "/__trigger_log")
    return [entry["id"] for entry in json.loads(text)]


def ref_of(state: PageState, role: str, name: str) -> str:
    return next(r.ref for r in state.refs if r.role == role and r.name == name)


# ---- browser.py -------------------------------------------------------------

def test_new_context_is_isolated_and_uses_the_viewport(server, browser):
    a = new_context(browser, viewport=(800, 600))
    b = new_context(browser)
    page_a, page_b = a.new_page(), b.new_page()
    page_a.goto(server + "/")
    page_b.goto(server + "/")
    page_a.evaluate("localStorage.setItem('k', 'v')")
    assert page_a.evaluate("innerWidth") == 800
    assert page_b.evaluate("innerWidth") == 1280
    assert page_b.evaluate("localStorage.getItem('k')") is None  # nothing shared between contexts
    a.close()
    b.close()


# ---- state.py ---------------------------------------------------------------

def test_capture_state_finds_the_taskboard_refs_and_roles(server, page):
    page.goto(server + CLEAN)
    page.get_by_role("listitem").first.wait_for()  # the page's own JS loads the tasks
    state = capture_state(page)

    assert [(r.role, r.name) for r in state.refs] == [
        ("textbox", "New task"), ("button", "Add"), ("button", "All"),
        ("button", "Active"), ("button", "Completed"),
        ("checkbox", "Buy milk"), ("button", "Delete Buy milk"),
        ("checkbox", "Write report"), ("button", "Delete Write report"),
    ]
    assert all(re.fullmatch(r"e\d+", r.ref) for r in state.refs)
    assert len({r.ref for r in state.refs}) == 9
    assert all(r.nth == 0 and not r.disabled for r in state.refs)
    assert [r.checked for r in state.refs if r.role == "checkbox"] == [False, False]
    assert state.title == "TaskBoard" and state.url == server + CLEAN
    assert state.scroll_width <= state.client_width == 1280
    assert len(state.fingerprint) == 40

    assert '- button "Add" [ref=' in state.snapshot_ai
    assert "[ref=" not in state.snapshot_plain and "2 items left" in state.snapshot_plain

    text = render_for_llm(state)
    assert "untrusted data" in text and "<<<PAGE" in text and text.endswith("PAGE>>>")
    assert state.snapshot_ai in text


def test_parse_refs_reads_text_field_values():
    snapshot = "\n".join([
        '- textbox "Plain" [ref=e1]: hello there',
        '- textbox "Holder" [ref=e2]:',
        '  - /placeholder: Type here',
        '  - text: typed text',
        '- textbox "Empty holder" [ref=e3]:',
        '  - /placeholder: Type here',
        '- textbox "Empty" [ref=e4]',
        '- text: a sibling text node, not a value',
        '- searchbox "Find" [ref=e5]: "123"',
        '- textbox "Last" [ref=e6]',
    ])
    values = {r.name: r.value for r in parse_refs(snapshot, "http://app.test/")}
    assert values == {"Plain": "hello there", "Holder": "typed text", "Empty holder": "",
                      "Empty": "", "Find": "123", "Last": ""}


def test_snapshots_are_capped_but_the_fingerprint_sees_the_whole_page(server, page):
    def state_with_tail(tail: str) -> PageState:
        page.goto(server + CLEAN)
        page.set_content("".join(f"<p>filler line {i}</p>" for i in range(2500)) + f"<p>{tail}</p>")
        return capture_state(page)

    a, b = state_with_tail("tail A"), state_with_tail("tail B")
    marker = "(snapshot truncated)"
    assert a.snapshot_plain.endswith(marker) and len(a.snapshot_plain) <= PLAIN_CAP + 40
    assert a.snapshot_ai.endswith(marker) and len(a.snapshot_ai) <= SNAPSHOT_CAP + 40
    assert "tail A" not in a.snapshot_plain  # cut off
    assert a.fingerprint != b.fingerprint    # but the fingerprint still saw it


def test_parse_refs_reads_the_snapshot_forms():
    snapshot = "\n".join([
        '- main [ref=e1]:',
        '  - heading "Demo" [level=1] [ref=e2]',
        '  - link "About us" [ref=e3] [cursor=pointer]:',
        '    - /url: /about',
        '  - link "Jump" [ref=e4]:',
        '    - /url: "#top"',
        '  - checkbox "Buy milk" [checked] [ref=e5]',
        '  - checkbox [ref=e6]',
        '  - button "Say \\"hi\\" \\\\ there" [ref=e7]: x',
        '  - button "Off" [disabled] [ref=e8]',
        '  - button "Same" [ref=e9]',
        '  - button "Same" [ref=e10]',
        '  - textbox "Email" [ref=e11]: a@b.c',
        '  - combobox "Colour" [ref=e12]:',
        '    - option "Red" [selected]',
        '  - button "No ref here"',
        '  - generic [ref=e13]: Clickable div',
        '- ... (snapshot truncated)',
    ])
    refs = parse_refs(snapshot, "http://app.test/dir/page")
    assert [(r.ref, r.role, r.name, r.nth) for r in refs] == [
        ("e3", "link", "About us", 0), ("e4", "link", "Jump", 0),
        ("e5", "checkbox", "Buy milk", 0), ("e6", "checkbox", "", 0),
        ("e7", "button", 'Say "hi" \\ there', 0), ("e8", "button", "Off", 0),
        ("e9", "button", "Same", 0), ("e10", "button", "Same", 1),
        ("e11", "textbox", "Email", 0), ("e12", "combobox", "Colour", 0),
    ]
    by_ref = {r.ref: r for r in refs}
    assert by_ref["e3"].href == "http://app.test/about"          # relative link made absolute
    assert by_ref["e4"].href == "http://app.test/dir/page#top"   # quoted YAML string
    assert by_ref["e5"].checked is True and by_ref["e6"].checked is False
    assert by_ref["e8"].disabled is True and by_ref["e9"].disabled is False
    assert by_ref["e11"].checked is None  # only checkbox / radio / switch have a checked state


NAMING_PAGE = """
<h1>Demo</h1>
<a href="/about">About us</a>
<a href="https://evil.example/x">Docs</a>
<input type="password" aria-label="Password">
<label>Email <input type="email" value="a@b.c"></label>
<select aria-label="Colour"><option>Red</option><option>Blue</option></select>
<input type="radio" id="r1"><label for="r1">Option one</label>
<div role="switch" aria-checked="true" tabindex="0">Dark mode</div>
<div role="tab" tabindex="0">Tab A</div>
<button disabled>Off</button>
<button aria-label='Say "hi" \\ there'>x</button>
<div onclick="void 0">Clickable div</div>
<button>Same</button><button>Same</button>
<input type="range" aria-label="Volume">
<a href="#top">Jump</a>
"""


def test_every_ref_reaches_the_same_element_by_ref_and_by_role_name_nth(server, page):
    page.goto(server + CLEAN)  # a real URL, so that relative links can be made absolute
    page.set_content(NAMING_PAGE)
    state = capture_state(page)

    assert [(r.role, r.name, r.nth) for r in state.refs] == [
        ("link", "About us", 0), ("link", "Docs", 0), ("textbox", "Password", 0),
        ("textbox", "Email", 0), ("combobox", "Colour", 0), ("radio", "Option one", 0),
        ("switch", "Dark mode", 0), ("tab", "Tab A", 0), ("button", "Off", 0),
        ("button", 'Say "hi" \\ there', 0), ("button", "Same", 0), ("button", "Same", 1),
        ("slider", "Volume", 0), ("link", "Jump", 0),
    ]  # headings, options and the role-less clickable div are not actionable refs
    by_name = {(r.name, r.nth): r for r in state.refs}
    assert by_name[("Docs", 0)].href == "https://evil.example/x"
    assert by_name[("About us", 0)].href == server + "/about"
    assert by_name[("Dark mode", 0)].checked is True
    assert by_name[("Option one", 0)].checked is False
    assert by_name[("Off", 0)].disabled is True
    assert by_name[("Email", 0)].value == "a@b.c"  # value on the same line

    for r in state.refs:
        by_ref = page.locator(f"aria-ref={r.ref}").element_handle()
        by_semantics = page.get_by_role(r.role, name=r.name, exact=True).nth(r.nth).element_handle()
        assert by_ref.evaluate("(a, b) => a === b", by_semantics), f"{r.role} {r.name!r} nth {r.nth}"


def test_fingerprint_is_stable_and_a_client_only_checkbox_tick_changes_it(server, page):
    page.goto(server + CLEAN)
    page.set_content('<label><input type="checkbox"> Remember me</label><p>static text</p>')
    first = capture_state(page).fingerprint
    assert capture_state(page).fingerprint == first  # nothing happened, nothing changed

    page.get_by_role("checkbox", name="Remember me").click()  # no request, no visible text change
    ticked = capture_state(page)
    assert ticked.fingerprint != first
    assert ticked.refs[0].checked is True


def test_fingerprint_shows_that_the_server_undid_a_tick_on_the_buggy_build(server, page, tmp_path):
    recorder = EvidenceRecorder(page, tmp_path)
    page.goto(server + BUGGY)
    tick = Step("check", {"role": "checkbox", "name": "Buy milk"})
    untick = Step("uncheck", {"role": "checkbox", "name": "Buy milk"})

    execute_step(page, capture_state(page), tick, recorder)
    before = capture_state(page)
    assert before.refs[5].checked is True

    execute_step(page, before, untick, recorder)  # the PATCH goes out, the server says 200 and keeps done=true
    after = capture_state(page)
    assert any(r["method"] == "PATCH" and r["status"] == 200 for r in recorder.requests)
    assert after.fingerprint == before.fingerprint  # so, to the eye, nothing changed


# ---- executor.py ------------------------------------------------------------

def test_type_press_check_and_uncheck(server, page, tmp_path):
    recorder = EvidenceRecorder(page, tmp_path)
    page.goto(server + CLEAN)
    box = {"role": "textbox", "name": "New task"}

    record = execute_step(page, capture_state(page), Step("type", box, text="Call Bob"), recorder)
    assert record.error is None
    typed = capture_state(page)
    assert "Call Bob" in typed.snapshot_plain  # typed text is part of the page state
    assert typed.refs[0].value == "Call Bob"   # and readable per field (this field has a placeholder)

    execute_step(page, capture_state(page), Step("press", box, text="Enter"), recorder)
    state = capture_state(page)
    assert "Call Bob" in [r.name for r in state.refs if r.role == "checkbox"]

    tick = {"role": "checkbox", "name": "Call Bob"}
    for action, expected in (("check", True), ("check", True), ("uncheck", False)):
        execute_step(page, capture_state(page), Step(action, tick), recorder)
        state = capture_state(page)
        assert next(r for r in state.refs if r.name == "Call Bob").checked is expected


def test_problems_with_a_step_become_records_not_exceptions(server, page, monkeypatch):
    page.goto(server + CLEAN)
    state = capture_state(page)

    unknown_ref = execute_step(page, state, Step("click", {"ref": "e999"}))
    assert unknown_ref.locator is None and "not an actionable element" in unknown_ref.error

    heading = re.search(r'heading "TaskBoard" \[level=1\] \[ref=(e\d+)\]', state.snapshot_ai).group(1)
    not_actionable = execute_step(page, state, Step("click", {"ref": heading}))
    assert "not an actionable element" in not_actionable.error

    missing = execute_step(page, state, Step("click", {"role": "button", "name": "Nope"}))
    assert missing.locator == {"role": "button", "name": "Nope", "nth": 0}
    assert "not found" in missing.error

    ref = ref_of(state, "checkbox", "Buy milk")
    assert "needs text" in execute_step(page, state, Step("type", {"ref": ref})).error
    assert "unknown action" in execute_step(page, state, Step("hover", {"ref": ref})).error

    # a ref that went stale because the page redrew itself after the snapshot
    page.evaluate("document.getElementById('task-list').replaceChildren()")
    stale = execute_step(page, state, Step("click", {"ref": ref}))
    assert stale.locator == {"role": "checkbox", "name": "Buy milk", "nth": 0}
    assert "not found" in stale.error

    monkeypatch.setattr(executor, "ACTION_TIMEOUT_MS", 300)
    page.set_content("<button disabled>Off</button>")
    off_state = capture_state(page)
    assert off_state.refs[0].disabled is True
    off = execute_step(page, off_state, Step("click", {"ref": off_state.refs[0].ref}))
    assert "Timeout" in off.error


def test_recorded_step_replays_through_its_semantic_locator_in_a_fresh_context(server, browser, tmp_path):
    records, delete_statuses = [], []
    for name in ("agent", "replay"):
        http("POST", server + "/__reset")
        context = new_context(browser)  # a brand new context each time, refs cannot carry over
        page = context.new_page()
        recorder = EvidenceRecorder(page, tmp_path / name)
        page.goto(server + BUGGY)
        recorder.wait_until_settled()

        state = capture_state(page)
        if name == "agent":
            ref = ref_of(state, "button", "Delete Write report")
            step = Step("click", {"ref": ref})  # how the agent picks: by ref
        else:
            step = Step(records[0].action, records[0].locator)  # how replay works: by locator

        recorder.begin_step(1, state)  # takes a screenshot; the refs must survive that
        record = execute_step(page, state, step, recorder)
        evidence = recorder.end_step(record, capture_state(page))
        records.append(record)
        delete_statuses.append([r["status"] for r in evidence.requests if r["method"] == "DELETE"])
        context.close()

    assert records[0].ref == ref
    assert records[0].locator == {"role": "button", "name": "Delete Write report", "nth": 0}
    assert records[1].ref is None and records[1].error is None
    assert delete_statuses == [[500], [500]]  # the same observation recurs


# ---- evidence.py + run_script.py ---------------------------------------------

def test_scripted_delete_on_the_buggy_build_records_the_500(server, page, tmp_path):
    run(page, server + BUGGY, load_steps("steps_delete.json"), tmp_path)

    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    step = evidence["steps"][0]
    assert step["action"] == "click" and step["error"] is None and step["blocked"] is None
    assert step["locator"] == {"role": "button", "name": "Delete Buy milk", "nth": 0}

    deletes = [r for r in step["requests"] if r["method"] == "DELETE"]
    assert len(deletes) == 1
    assert re.fullmatch(r"/api/tasks/\d+", urlsplit(deletes[0]["url"]).path)
    assert deletes[0]["status"] == 500 and deletes[0]["failed"] is True

    # Chromium's "Failed to load resource" console line is marked, with the URL it is about
    marked = [c for c in step["console"] if c["resource_load_error"]]
    assert len(marked) == 1 and marked[0]["type"] == "error" and marked[0]["url"] == deletes[0]["url"]

    everything = step["requests"] + evidence["load"]["requests"]
    assert not any(p.startswith("/__") for p in paths(everything))  # demo plumbing is ignored

    assert step["url_before"] == step["url_after"]
    assert step["fingerprint_before"] == step["fingerprint_after"]  # the UI showed nothing new
    assert "Delete Buy milk" in [r["name"] for r in evidence["states"][-1]["refs"]]  # task still there
    for shot in (step["screenshot_before"], step["screenshot_after"]):
        assert (tmp_path / shot).stat().st_size > 0
    assert set(step["timings"]) == {"total_ms", "action_ms", "settle_ms"}
    assert step["dialogs"] == [] and step["page_errors"] == []
    assert "S1" in trigger_ids(server)  # ground truth agrees: the seeded path ran


def test_scripted_delete_on_the_clean_build_succeeds_without_failed_requests(server, page, tmp_path):
    run(page, server + CLEAN, load_steps("steps_delete.json"), tmp_path)

    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    step = evidence["steps"][0]
    assert step["error"] is None
    deletes = [r for r in step["requests"] if r["method"] == "DELETE"]
    assert [r["status"] for r in deletes] == [200]
    everything = step["requests"] + evidence["load"]["requests"]
    assert not any(r["failed"] for r in everything)
    assert not any(c["resource_load_error"] for c in step["console"])
    assert not any(p.startswith("/__") for p in paths(everything))
    assert step["fingerprint_before"] != step["fingerprint_after"]
    assert "Delete Buy milk" not in [r["name"] for r in evidence["states"][-1]["refs"]]
    assert trigger_ids(server) == []


def test_plumbing_requests_are_ignored_unless_ignore_paths_says_otherwise(server, browser, tmp_path):
    steps = [Step("check", {"role": "checkbox", "name": "Buy milk"})]  # buggy build posts /__trigger (S6)
    seen = {}
    for label, ignore in (("default", DEFAULT_IGNORE_PATHS), ("nothing ignored", ())):
        http("POST", server + "/__reset")
        context = new_context(browser)
        result = run(context.new_page(), server + BUGGY, steps, tmp_path / label.replace(" ", "_"),
                     ignore_paths=ignore)
        context.close()
        seen[label] = paths(result["steps"][0]["requests"])

    assert "/api/tasks" in seen["default"]
    assert not any(p.startswith("/__") for p in seen["default"])
    assert "/__trigger" in seen["nothing ignored"]


def test_run_stops_executing_steps_after_the_budget(server, page, tmp_path):
    steps = [Step("check", {"role": "checkbox", "name": "Buy milk"}),
             Step("check", {"role": "checkbox", "name": "Write report"})]
    result = run(page, server + CLEAN, steps, tmp_path, max_steps=1)

    first, second = result["steps"]
    assert first["blocked"] is None and any(r["method"] == "PATCH" for r in first["requests"])
    assert "budget" in second["blocked"]
    assert second["requests"] == []  # nothing was sent for the blocked step


def test_dialogs_are_recorded_and_dismissed(server, page, tmp_path):
    page.goto(server + CLEAN)
    page.set_content("<button onclick=\"alert('Saved')\">Alert</button>")
    recorder = EvidenceRecorder(page, tmp_path)
    state = capture_state(page)
    recorder.begin_step(1, state)
    record = execute_step(page, state, Step("click", {"role": "button", "name": "Alert"}), recorder)
    evidence = recorder.end_step(record, capture_state(page))

    assert record.error is None
    assert evidence.dialogs == [{"type": "alert", "message": "Saved"}]


# ---- safety.py --------------------------------------------------------------

APP = "http://app.test:8765"


def fake_state(url=APP + "/") -> PageState:
    return PageState(url=url, title="t", snapshot_ai="", snapshot_plain="", fingerprint="",
                     refs=[], scroll_width=0, client_width=0)


def fake_entry(name, role="button", href=None) -> RefEntry:
    return RefEntry(ref="e1", role=role, name=name, nth=0, checked=None, disabled=False, href=href)


def check(policy, entry, url=APP + "/", target=None, action="click"):
    step = Step(action, target or {"ref": "e1"}, text="x" if action in ("type", "press") else None)
    return policy.check(step, entry, fake_state(url))


def test_safety_blocks_links_that_leave_the_origin():
    policy = SafetyPolicy(APP)
    allowed, reason = check(policy, fake_entry("Docs", "link", href="https://evil.example/x"))
    assert not allowed and "evil.example" in reason
    assert check(policy, fake_entry("About", "link", href=APP + "/about"))[0]
    assert not check(policy, fake_entry("Docs", "link", href="http://app.test:9999/x"))[0]  # other port


BLOCKED_NAMES = [
    "Delete account", "Delete my account", "Deleting your account permanently", "Close my account",
    "DEACTIVATE profile", "Deactivating", "Pay now", "Payments", "Purchase", "Purchases", "Buy now", "Buy it now",
    "Place order", "Place your order", "Checkout", "Proceed to check out", "Send email", "Send an e-mail",
    "Unsubscribe all", "Unsubscribe from all", "Publish post", "Published", "Upload file", "Uploads",
    "Download report", "Downloading",
]
ALLOWED_NAMES = [
    "Delete Buy milk", "Delete Write report", "Delete", "Add", "Buy milk", "Display settings",
    "Remove item", "Send report", "Close", "Check the list", "Account settings",
]


def test_safety_blocks_dangerous_names_with_gaps_and_inflections():
    policy = SafetyPolicy(APP)
    for name in BLOCKED_NAMES:
        assert not check(policy, fake_entry(name))[0], name


def test_safety_allows_in_app_crud_and_ordinary_names():
    policy = SafetyPolicy(APP)
    for name in ALLOWED_NAMES:  # "pay" inside "Display" and "buy" without "now" are not dangerous
        assert check(policy, fake_entry(name))[0], name
    allowed, reason = check(policy, fake_entry("Delete my account"))
    assert not allowed and 'matches blocked pattern' in reason


def test_safety_blocks_typing_and_pressing_in_a_field_named_password():
    policy = SafetyPolicy(APP)
    password = fake_entry("Password", role="textbox")
    for action in ("type", "press"):
        allowed, reason = check(policy, password, action=action)
        assert not allowed and "password" in reason
    assert check(policy, password, action="click")[0]  # clicking into it is fine
    assert check(policy, fake_entry("Search", role="textbox"), action="type")[0]


def test_password_field_is_blocked_by_its_real_type_attribute(server, page, tmp_path):
    page.goto(server + CLEAN)
    page.set_content('<input type="password" aria-label="Secret pin"><input aria-label="Search">')
    recorder = EvidenceRecorder(page, tmp_path)
    policy = SafetyPolicy(server)
    state = capture_state(page)
    assert state.refs[0].role == "textbox"  # the snapshot shows no sign of type=password
    pin, search = ref_of(state, "textbox", "Secret pin"), ref_of(state, "textbox", "Search")

    for action, text in (("type", "hunter2"), ("press", "a")):
        record = execute_step(page, state, Step(action, {"ref": pin}, text=text), recorder, policy)
        assert record.blocked and "password" in record.blocked and record.error is None
        assert page.locator("input[type=password]").input_value() == ""  # nothing was typed

    ok = execute_step(page, state, Step("type", {"ref": search}, text="cats"), recorder, policy)
    assert ok.blocked is None and ok.error is None
    assert page.get_by_role("textbox", name="Search").input_value() == "cats"


def test_safety_uses_the_scripted_name_when_the_element_is_not_on_the_page():
    policy = SafetyPolicy(APP)
    blocked = check(policy, None, target={"role": "button", "name": "Delete account"})
    assert blocked[0] is False


def test_safety_blocks_a_page_that_is_already_off_origin():
    allowed, reason = check(SafetyPolicy(APP), fake_entry("Fine"), url="https://elsewhere.example/")
    assert not allowed and "outside the allowed origin" in reason


def test_safety_step_budget():
    policy = SafetyPolicy(APP, max_steps=3)
    results = [check(policy, fake_entry("Fine"))[0] for _ in range(5)]
    assert results == [True, True, True, False, False]
    assert "budget" in check(policy, fake_entry("Fine"))[1]


def test_safety_custom_patterns():
    policy = SafetyPolicy(APP, blocked_patterns=["delete"])
    assert not check(policy, fake_entry("Delete Buy milk"))[0]


def test_blocked_step_is_recorded_with_its_reason_and_not_executed(server, page, tmp_path):
    page.goto(server + CLEAN)
    recorder = EvidenceRecorder(page, tmp_path)
    policy = SafetyPolicy(server, blocked_patterns=["delete"])
    step = Step("click", {"role": "button", "name": "Delete Buy milk"})

    record = execute_step(page, capture_state(page), step, recorder, policy)

    assert record.error is None and "delete" in record.blocked
    assert record.locator == {"role": "button", "name": "Delete Buy milk", "nth": 0}
    _, tasks = http("GET", server + "/api/tasks?bugs=off")
    assert "Buy milk" in tasks  # the click never happened
