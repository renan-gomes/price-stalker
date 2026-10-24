"""Candidate discovery through the Google Programmable Search (Custom Search JSON) API."""
from __future__ import annotations

import os
from dataclasses import dataclass

import requests

from price_stalker.retailers.registry import Retailer, all_retailers, retailer_for

API_URL = "https://www.googleapis.com/customsearch/v1"


@dataclass(frozen=True)
class Candidate:
    title: str
    url: str
    retailer: Retailer


def _site_clause() -> str:
    return " OR ".join(f"site:{r.domain}" for r in all_retailers())


def search(query: str, *, num: int = 10, session: requests.Session | None = None) -> list[Candidate]:
    """Search Google restricted to trusted retailers; results are re-filtered client-side."""
    key, cx = os.environ.get("GOOGLE_API_KEY"), os.environ.get("GOOGLE_CSE_ID")
    if not key or not cx:
        raise RuntimeError("Set GOOGLE_API_KEY and GOOGLE_CSE_ID to use discovery")
    http = session or requests
    resp = http.get(
        API_URL,
        params={"key": key, "cx": cx, "q": f"{query} ({_site_clause()})", "num": min(num, 10)},
        timeout=15,
    )
    resp.raise_for_status()
    return parse_results(resp.json())


def parse_results(payload: dict) -> list[Candidate]:
    out, seen = [], set()
    for item in payload.get("items", []):
        url = item.get("link", "")
        retailer = retailer_for(url)
        if retailer is None or url in seen:
            continue
        seen.add(url)
        out.append(Candidate(item.get("title", ""), url, retailer))
    return out
