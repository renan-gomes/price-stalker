"""Quick compatibility check: can a plain request read a price from this shop?"""
from __future__ import annotations

from dataclasses import dataclass

import requests

from price_stalker.extraction.fetcher import BLOCK_STATUSES, HEADERS
from price_stalker.extraction.parser import VariantError, extract, find_variants, looks_blocked
from price_stalker.retailers.registry import host_of

SUPPORTED = ("OK", "NEEDS_VARIANT")
BLOCK = set(BLOCK_STATUSES) | {401, 412}


@dataclass(frozen=True)
class ProbeResult:
    url: str
    domain: str
    status: str  # OK | NEEDS_VARIANT | NO_PRICE | BLOCKED | ERROR
    detail: str

    @property
    def supported(self) -> bool:
        return self.status in SUPPORTED


def probe(url: str, session: requests.Session | None = None, timeout: int = 20) -> ProbeResult:
    host = host_of(url)
    domain = host[4:] if host.startswith("www.") else host

    def result(status: str, detail: str) -> ProbeResult:
        return ProbeResult(url, domain, status, detail)

    try:
        resp = (session or requests).get(url, headers=HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        return result("ERROR", f"network error: {type(exc).__name__}")
    if resp.status_code in BLOCK:
        return result("BLOCKED", f"HTTP {resp.status_code}")
    if not resp.ok:
        return result("ERROR", f"HTTP {resp.status_code}")
    html = resp.text
    if looks_blocked(html):
        return result("BLOCKED", "robot-check page")
    try:
        found = extract(html, url)
    except VariantError:
        return result("NEEDS_VARIANT", f"{len(find_variants(html))} variants; track one with --variant")
    if found.price_cents is None:
        return result("NO_PRICE", "page loads but the price is not in the HTML (loaded by JavaScript?)")
    return result("OK", f"{found.currency} {found.price_cents / 100:,.2f}" + (f" ({found.variant})" if found.variant else ""))
