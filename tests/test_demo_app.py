"""Checks for the TaskBoard demo app (test code only, it is not the product).

A real uvicorn server is started on a free port, and hand-written Playwright steps
drive the page. Buggy build ("/"): every seeded issue S1..S6 must show up in the
server's trigger log. Clean build ("/?bugs=off"): the same actions must work
correctly and leave the trigger log empty.
"""
import json
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent

LONG_SENTENCE = " ".join(["long"] * 40)  # 159 characters with spaces
LONG_WORD = "x" * 200                    # 200 characters, no spaces to wrap at


# ---- fixtures and helpers -------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode()


def trigger_ids(base: str) -> list[str]:
    _, text = http("GET", base + "/__trigger_log")
    return [entry["id"] for entry in json.loads(text)]


def wait_for_trigger(base: str, trigger_id: str, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if trigger_id in trigger_ids(base):
            return True
        time.sleep(0.05)
    return False


def assert_log_empty(base: str):
    time.sleep(0.3)  # the client posts S4/S6 asynchronously; give it time to arrive
    assert trigger_ids(base) == []


@pytest.fixture(scope="session")
def server():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "demo_app.server:app",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 15
        while True:
            assert proc.poll() is None, "uvicorn exited during start-up"
            try:
                http("GET", base + "/__trigger_log")
                break
            except OSError:
                assert time.time() < deadline, "server did not start in 15 s"
                time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(server, browser):
    """A fresh page on a freshly reset app."""
    http("POST", server + "/__reset")
    context = browser.new_context()  # default 1280x720 viewport
    yield context.new_page()
    context.close()


def is_method(method: str):
    return lambda resp: resp.request.method == method and "/api/tasks" in resp.url


def row(page, text):
    return page.get_by_role("listitem").filter(has_text=text)


def add_task(page, title):
    """Type a title and press Add; returns the POST response."""
    page.get_by_role("textbox", name="New task").fill(title)
    with page.expect_response(is_method("POST")) as info:
        page.get_by_role("button", name="Add", exact=True).click()
    return info.value


def delete_task(page, text):
    with page.expect_response(is_method("DELETE")) as info:
        row(page, text).get_by_role("button", name="Delete").click()
    return info.value


def toggle(page, text):
    """Click the task checkbox (tick or untick); returns the PATCH response."""
    box = page.get_by_role("checkbox", name=text)
    with page.expect_response(is_method("PATCH")) as info:
        box.click()
    return info.value


def footer(page):
    return page.get_by_text(re.compile(r"^\d+ items? left$"))


def overflows(page) -> bool:
    return page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth")


# ---- buggy build: each seeded issue must fire -----------------------------

def test_buggy_s1_delete_returns_500_and_task_stays(server, page):
    page.goto(server + "/")
    resp = delete_task(page, "Buy milk")
    assert resp.status == 500
    expect(row(page, "Buy milk")).to_be_visible()
    expect(page.get_by_role("alert")).to_be_hidden()  # no message shown
    assert wait_for_trigger(server, "S1")


def test_buggy_s2_completed_task_cannot_be_reopened(server, page):
    page.goto(server + "/")
    toggle(page, "Buy milk")
    expect(page.get_by_role("checkbox", name="Buy milk")).to_be_checked()
    resp = toggle(page, "Buy milk")
    assert resp.status == 200
    assert wait_for_trigger(server, "S2")
    expect(page.get_by_role("checkbox", name="Buy milk")).to_be_checked()  # snapped back


def test_buggy_s3_blank_title_is_accepted(server, page):
    page.goto(server + "/")
    resp = add_task(page, "   ")
    assert resp.status == 201
    expect(page.get_by_role("listitem")).to_have_count(3)
    expect(page.get_by_role("alert")).to_be_hidden()
    assert wait_for_trigger(server, "S3")


def test_buggy_s4_long_title_overflows_page(server, page):
    page.goto(server + "/")
    add_task(page, LONG_SENTENCE)
    assert wait_for_trigger(server, "S4")
    assert overflows(page)


def test_buggy_s5_duplicate_gives_409_and_no_message(server, page):
    page.goto(server + "/")
    resp = add_task(page, "BUY MILK")
    assert resp.status == 409
    expect(page.get_by_role("listitem")).to_have_count(2)
    expect(page.get_by_role("alert")).to_be_hidden()
    assert wait_for_trigger(server, "S5")


def test_buggy_s6_footer_shows_total_instead_of_active(server, page):
    page.goto(server + "/")
    expect(footer(page)).to_have_text("2 items left")
    toggle(page, "Buy milk")
    expect(footer(page)).to_have_text("2 items left")  # correct would be "1 item left"
    assert wait_for_trigger(server, "S6")


# ---- clean build: same actions, correct behaviour, empty trigger log ------

CLEAN = "/?bugs=off"


def test_clean_page_loads_and_sends_bugs_off_to_the_api(server, page):
    status, html = http("GET", server + CLEAN)
    assert status == 200 and "TaskBoard" in html
    with page.expect_response(is_method("GET")) as info:
        page.goto(server + CLEAN)
    assert "bugs=off" in info.value.url
    expect(page).to_have_title("TaskBoard")
    expect(page.get_by_role("listitem")).to_have_count(2)
    expect(row(page, "Buy milk")).to_be_visible()
    expect(row(page, "Write report")).to_be_visible()
    expect(footer(page)).to_have_text("2 items left")
    assert_log_empty(server)


def test_clean_delete_works(server, page):
    page.goto(server + CLEAN)
    resp = delete_task(page, "Buy milk")
    assert resp.status == 200
    expect(page.get_by_role("listitem")).to_have_count(1)
    expect(row(page, "Buy milk")).to_have_count(0)
    expect(footer(page)).to_have_text("1 item left")
    assert_log_empty(server)


def test_clean_reopen_works(server, page):
    page.goto(server + CLEAN)
    toggle(page, "Buy milk")
    expect(page.get_by_role("checkbox", name="Buy milk")).to_be_checked()
    toggle(page, "Buy milk")
    expect(page.get_by_role("checkbox", name="Buy milk")).not_to_be_checked()
    assert_log_empty(server)


def test_clean_blank_title_is_rejected_with_message(server, page):
    page.goto(server + CLEAN)
    resp = add_task(page, "   ")
    assert resp.status == 422
    expect(page.get_by_role("alert")).to_be_visible()
    expect(page.get_by_role("alert")).to_contain_text("cannot be blank")
    expect(page.get_by_role("listitem")).to_have_count(2)
    assert_log_empty(server)


def test_clean_long_titles_wrap_without_overflow(server, page):
    page.goto(server + CLEAN)
    add_task(page, LONG_SENTENCE)
    expect(page.get_by_role("listitem")).to_have_count(3)
    assert not overflows(page)
    add_task(page, LONG_WORD)
    expect(page.get_by_role("listitem")).to_have_count(4)
    assert not overflows(page)
    assert_log_empty(server)


def test_clean_duplicate_title_shows_message(server, page):
    page.goto(server + CLEAN)
    resp = add_task(page, "BUY MILK")
    assert resp.status == 409
    expect(page.get_by_role("alert")).to_be_visible()
    expect(page.get_by_role("alert")).to_contain_text("already exists")
    expect(page.get_by_role("listitem")).to_have_count(2)
    assert_log_empty(server)


def test_clean_footer_counts_active_tasks(server, page):
    page.goto(server + CLEAN)
    expect(footer(page)).to_have_text("2 items left")
    toggle(page, "Buy milk")
    expect(footer(page)).to_have_text("1 item left")
    toggle(page, "Write report")
    expect(footer(page)).to_have_text("0 items left")
    toggle(page, "Buy milk")
    expect(footer(page)).to_have_text("1 item left")
    assert_log_empty(server)


def test_clean_add_and_filters_work(server, page):
    page.goto(server + CLEAN)
    resp = add_task(page, "Call Bob")
    assert resp.status == 201
    expect(page.get_by_role("listitem")).to_have_count(3)
    expect(footer(page)).to_have_text("3 items left")
    toggle(page, "Buy milk")
    expect(footer(page)).to_have_text("2 items left")

    page.get_by_role("button", name="Active").click()
    expect(page.get_by_role("listitem")).to_have_count(2)
    expect(row(page, "Buy milk")).to_have_count(0)
    page.get_by_role("button", name="Completed").click()
    expect(page.get_by_role("listitem")).to_have_count(1)
    expect(row(page, "Buy milk")).to_be_visible()
    page.get_by_role("button", name="All", exact=True).click()
    expect(page.get_by_role("listitem")).to_have_count(3)
    assert_log_empty(server)


# ---- ground truth plumbing ------------------------------------------------

def test_reset_restores_seeds_and_clears_trigger_log(server, page):
    page.goto(server + "/")
    add_task(page, "   ")
    assert wait_for_trigger(server, "S3")
    http("POST", server + "/__reset")
    assert trigger_ids(server) == []
    _, text = http("GET", server + "/api/tasks")
    assert [t["title"] for t in json.loads(text)] == ["Buy milk", "Write report"]
