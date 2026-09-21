"""Start a headless Chromium and give every run its own clean browser context."""
from playwright.sync_api import Browser, BrowserContext, Playwright


def launch_chromium(playwright: Playwright) -> Browser:
    return playwright.chromium.launch(headless=True)


def new_context(browser: Browser, viewport: tuple[int, int] = (1280, 800)) -> BrowserContext:
    """A fresh context: its own cookies and storage, nothing shared with earlier runs."""
    width, height = viewport
    return browser.new_context(viewport={"width": width, "height": height})
