"""Record what the browser really did during each step.

Everything here comes from browser events and from the page itself, never from a model.
"""
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Demo plumbing (trigger log, reset) that must not show up as evidence.
DEFAULT_IGNORE_PATHS = ("/__",)


@dataclass
class StepEvidence:
    step: int
    action: str
    locator: dict | None      # {"role", "name", "nth"}
    text: str | None
    expect: str | None        # what the agent said should change
    error: str | None         # set when the step could not be carried out
    blocked: str | None       # set when the safety policy refused the step
    url_before: str
    url_after: str
    fingerprint_before: str   # url + accessibility snapshot, see probe/state.py
    fingerprint_after: str
    scroll_width: int         # after the step
    client_width: int         # after the step
    requests: list[dict]      # {"method", "url", "status", "failed", "error", "resource_type"}
    console: list[dict]       # {"type", "text", "resource_load_error", "url"}
    page_errors: list[dict]   # {"message"}
    dialogs: list[dict]       # {"type", "message"}; each one was dismissed
    screenshot_before: str    # file name inside out_dir
    screenshot_after: str
    timings: dict             # milliseconds: total_ms, action_ms, settle_ms

    def to_dict(self) -> dict:
        return asdict(self)


class EvidenceRecorder:
    """Listens to one page. `requests`, `console`, `page_errors` and `dialogs` grow for the whole session;
    begin_step / end_step cut out the part that belongs to one step."""

    def __init__(self, page, out_dir, ignore_paths=DEFAULT_IGNORE_PATHS):
        self.page = page
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.ignore_paths = tuple(ignore_paths)

        self.requests: list[dict] = []
        self.console: list[dict] = []
        self.page_errors: list[dict] = []
        self.dialogs: list[dict] = []
        self.in_flight = 0  # requests started but not yet finished or failed

        page.on("request", self._on_request)
        page.on("requestfinished", self._on_request_done)
        page.on("requestfailed", self._on_request_failed)
        page.on("response", self._on_response)
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        page.on("dialog", self._on_dialog)

    # ---- browser events ---------------------------------------------------

    def _ignored(self, url: str) -> bool:
        return urlsplit(url).path.startswith(self.ignore_paths)

    def _on_request(self, request):
        if not self._ignored(request.url):
            self.in_flight += 1

    def _on_request_done(self, request):
        if not self._ignored(request.url):
            self.in_flight = max(0, self.in_flight - 1)

    def _on_request_failed(self, request):
        self._on_request_done(request)
        if self._ignored(request.url):
            return
        self.requests.append({
            "method": request.method, "url": request.url, "status": None,
            "failed": True, "error": request.failure, "resource_type": request.resource_type,
        })

    def _on_response(self, response):
        if self._ignored(response.url):
            return
        self.requests.append({
            "method": response.request.method, "url": response.url, "status": response.status,
            "failed": response.status >= 400, "error": None,
            "resource_type": response.request.resource_type,
        })

    def _on_console(self, message):
        # Chromium logs "Failed to load resource ..." for every failed request. That repeats what the
        # response entry already says, so it is marked here and the oracles can merge the two.
        self.console.append({
            "type": message.type, "text": message.text,
            "resource_load_error": message.text.startswith("Failed to load resource"),
            "url": message.location.get("url"),
        })

    def _on_page_error(self, error):
        self.page_errors.append({"message": str(error)})

    def _on_dialog(self, dialog):
        self.dialogs.append({"type": dialog.type, "message": dialog.message})
        dialog.dismiss()  # what Playwright does anyway when nobody listens; now it is on record

    # ---- waiting ----------------------------------------------------------

    def wait_until_settled(self, quiet_ms: int = 300, cap_ms: int = 3000) -> bool:
        """Wait until no request has been in flight for quiet_ms. Gives up after cap_ms."""
        start = time.monotonic()
        quiet_since = start
        while (time.monotonic() - start) * 1000 < cap_ms:
            now = time.monotonic()
            if self.in_flight > 0:
                quiet_since = now
            elif (now - quiet_since) * 1000 >= quiet_ms:
                return True
            self.page.wait_for_timeout(50)  # also lets Playwright deliver browser events
        return False

    # ---- one step ---------------------------------------------------------

    def _screenshot(self, name: str) -> str:
        self.page.screenshot(path=str(self.out_dir / name))
        return name

    def begin_step(self, n: int, state_before):
        self._n = n
        self._state_before = state_before
        self._mark = (len(self.requests), len(self.console), len(self.page_errors), len(self.dialogs))
        self._t0 = time.monotonic()
        self._shot_before = self._screenshot(f"step_{n:02d}_before.png")

    def end_step(self, record, state_after) -> StepEvidence:
        """`record` is the StepRecord from execute_step, `state_after` a fresh capture_state()."""
        shot_after = self._screenshot(f"step_{self._n:02d}_after.png")
        r0, c0, e0, d0 = self._mark
        before = self._state_before
        return StepEvidence(
            step=self._n,
            action=record.action,
            locator=record.locator,
            text=record.text,
            expect=record.expect,
            error=record.error,
            blocked=record.blocked,
            url_before=before.url,
            url_after=state_after.url,
            fingerprint_before=before.fingerprint,
            fingerprint_after=state_after.fingerprint,
            scroll_width=state_after.scroll_width,
            client_width=state_after.client_width,
            requests=self.requests[r0:],
            console=self.console[c0:],
            page_errors=self.page_errors[e0:],
            dialogs=self.dialogs[d0:],
            screenshot_before=self._shot_before,
            screenshot_after=shot_after,
            timings={
                "total_ms": round((time.monotonic() - self._t0) * 1000),
                "action_ms": record.action_ms,
                "settle_ms": record.settle_ms,
            },
        )
