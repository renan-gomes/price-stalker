"""HTTP fetching: a plain request first, a real (headless) browser when the shop blocks it."""
from __future__ import annotations

import os

import requests

from price_stalker.extraction.parser import looks_blocked
from price_stalker.retailers.registry import UntrustedDomain, host_of, is_trusted, untrusted_message

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
BLOCK_STATUSES = {403, 429, 503}  # what anti-bot systems answer; anything else is a plain error

BROWSER_HELP = ("browser mode is not installed: pip install -e '.[browser]' && playwright install chromium "
                "(or point PRICE_STALKER_CHROMIUM to an installed Chrome/Chromium)")


class FetchError(RuntimeError):
    """A fetch failed in a way worth one clear line (not a traceback)."""


def fetch_with_browser(url: str, timeout_ms: int = 45_000) -> str:
    """Load the page in headless Chromium and return the rendered HTML.

    PRICE_STALKER_CHROMIUM = path to a Chrome/Chromium binary (default: Playwright's own).
    PRICE_STALKER_HEADED=1 = show the window (some shops only accept a visible browser).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise FetchError(BROWSER_HELP) from None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=os.environ.get("PRICE_STALKER_HEADED") != "1",
                executable_path=os.environ.get("PRICE_STALKER_CHROMIUM") or None,
                args=["--disable-blink-features=AutomationControlled"])
            try:
                page = browser.new_context(user_agent=USER_AGENT, locale="en-US",
                                           viewport={"width": 1366, "height": 768}).new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(2500)  # let client-side scripts add prices / structured data
                return page.content()
            finally:
                browser.close()
    except FetchError:
        raise
    except Exception as exc:
        msg = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        hint = f" — {BROWSER_HELP}" if "Executable doesn't exist" in str(exc) else ""
        raise FetchError(f"browser fetch failed: {msg}{hint}") from None


def fetch_html(url: str, *, session: requests.Session | None = None, timeout: int = 20,
               browser=fetch_with_browser, force_browser: bool = False) -> str:
    if not is_trusted(url):
        raise UntrustedDomain(untrusted_message(url))
    if force_browser:
        return browser(url)
    try:
        resp = (session or requests).get(url, headers=HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        raise FetchError(f"network error: {type(exc).__name__}") from None
    if resp.status_code in BLOCK_STATUSES or (resp.ok and looks_blocked(resp.text)):
        try:
            return browser(url)
        except FetchError as exc:
            raise FetchError(f"blocked by the site (HTTP {resp.status_code}); {exc}") from None
    if not resp.ok:
        raise FetchError(f"HTTP {resp.status_code}")
    return resp.text
