"""Safety policy: stay on the declared app, respect a step budget, never press dangerous buttons.

In-app create / update / delete is allowed on purpose (the target is declared as a staging app).
"""
import re
from urllib.parse import urlsplit

# Regular expressions, matched case-insensitively against the element's accessible name.
# Words may sit in between ("delete my account") and words may be inflected ("uploads",
# "downloading"). Over-blocking is accepted: a task called "Pay rent" gets its controls blocked.
GAP = r"\W+(?:\w+\W+){0,2}"  # up to two extra words between the two words of a phrase
DEFAULT_BLOCKED_PATTERNS = [
    rf"\bdelet\w*{GAP}account",
    rf"\bclos\w*{GAP}account",
    r"\bdeactivat",
    r"\bpay",
    r"\bpurchas",
    rf"\bbuy\w*{GAP}now",
    rf"\bplac\w*{GAP}order",
    r"\bcheck\W?out\b",
    rf"\bsend\w*{GAP}e-?mail",
    rf"\bunsubscrib\w*{GAP}all",
    r"\bpublish",
    r"\bupload",
    r"\bdownload",
]


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


class SafetyPolicy:
    def __init__(self, allowed_origin: str, max_steps: int = 15,
                 blocked_patterns: list[str] = DEFAULT_BLOCKED_PATTERNS):
        self.allowed_origin = origin_of(allowed_origin)
        self.max_steps = max_steps
        self.blocked_patterns = [re.compile(p, re.IGNORECASE) for p in blocked_patterns]
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
        if step.action in ("type", "press") and role == "textbox" and "password" in name.lower():
            return False, f'typing into a password field is not allowed: "{name}"'

        for pattern in self.blocked_patterns:
            if pattern.search(name):
                return False, f'element "{name}" matches blocked pattern {pattern.pattern!r}'

        return True, ""

    def check_field(self, step, input_type: str | None) -> tuple[bool, str]:
        """Second look once the element is on hand. The accessibility snapshot hides type=password,
        the element's own `type` attribute does not. Does not use up budget."""
        if step.action in ("type", "press") and (input_type or "").strip().lower() == "password":
            return False, "typing into a password field is not allowed (input type=password)"
        return True, ""
