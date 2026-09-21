"""Safety policy: stay on the declared app, respect a step budget, never press dangerous buttons.

In-app create / update / delete is allowed on purpose (the target is declared as a staging app).
"""
import re
from urllib.parse import urlsplit

DEFAULT_BLOCKED_PATTERNS = [
    "delete account", "close account", "deactivate", "pay", "purchase", "buy now",
    "place order", "checkout", "send email", "unsubscribe all",
    "publish", "upload", "download",
]


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


class SafetyPolicy:
    def __init__(self, allowed_origin: str, max_steps: int = 15,
                 blocked_patterns: list[str] = DEFAULT_BLOCKED_PATTERNS):
        self.allowed_origin = origin_of(allowed_origin)
        self.max_steps = max_steps
        self.blocked_patterns = list(blocked_patterns)
        self.steps_used = 0

    def check(self, step, element, state) -> tuple[bool, str]:
        """Return (allowed, reason). `element` is the RefEntry the step points at, or None when
        it could not be found. Every call uses up one step of the budget, blocked or not."""
        self.steps_used += 1
        if self.steps_used > self.max_steps:
            return False, f"step budget exceeded (max {self.max_steps} steps)"

        if origin_of(state.url) != self.allowed_origin:
            return False, f"page {state.url} is outside the allowed origin {self.allowed_origin}"

        href = element.href if element else None
        if href and href.startswith(("http://", "https://")) and origin_of(href) != self.allowed_origin:
            return False, f"link leaves the allowed origin: {href}"

        name = element.name if element else step.target.get("name", "")
        role = element.role if element else step.target.get("role", "")
        if step.action == "type" and role == "textbox" and "password" in name.lower():
            return False, f'typing into a password field is not allowed: "{name}"'

        for pattern in self.blocked_patterns:
            # whole words only, so "pay" blocks "Pay now" but not "Display settings"
            if re.search(rf"\b{re.escape(pattern)}\b", name, re.IGNORECASE):
                return False, f'element "{name}" matches blocked pattern "{pattern}"'

        return True, ""
