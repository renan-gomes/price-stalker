"""Allowlist of trusted US retailers: the built-in list plus the ones the user registered in the database."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse


@dataclass(frozen=True)
class Retailer:
    name: str
    domain: str  # registrable domain, e.g. "bestbuy.com"


TRUSTED_RETAILERS: tuple[Retailer, ...] = (
    Retailer("Walmart", "walmart.com"),
    Retailer("Target", "target.com"),
    Retailer("Best Buy", "bestbuy.com"),
    Retailer("Costco", "costco.com"),
    Retailer("Home Depot", "homedepot.com"),
    Retailer("Lowe's", "lowes.com"),
    Retailer("Newegg", "newegg.com"),
    Retailer("B&H Photo", "bhphotovideo.com"),
    Retailer("Macy's", "macys.com"),
    Retailer("Kohl's", "kohls.com"),
    Retailer("Wayfair", "wayfair.com"),
    Retailer("FragranceNet", "fragrancenet.com"),
    Retailer("eCosmetics", "ecosmetics.com"),
)

BUILTIN_DOMAINS = frozenset(r.domain for r in TRUSTED_RETAILERS)

# Retailers registered by the user (loaded from the database by Store).
_custom: dict[str, Retailer] = {}


class UntrustedDomain(ValueError):
    """The URL does not belong to any registered retailer."""


def set_custom(retailers: Iterable[Retailer]) -> None:
    _custom.clear()
    _custom.update({r.domain: r for r in retailers})


def all_retailers() -> tuple[Retailer, ...]:
    return TRUSTED_RETAILERS + tuple(_custom.values())


_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")
# Public suffixes: trusting one of these would trust every shop under it.
_TOO_BROAD = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.br", "com.au", "com.mx", "com.cn", "co.jp", "co.in", "co.nz"}


def normalize_domain(text: str) -> str:
    """'https://www.Sephora.com/x?y' / 'www.sephora.com' / 'sephora.com' -> 'sephora.com'."""
    t = text.strip().lower()
    host = urlparse(t if "://" in t else "//" + t).hostname or ""
    host = host[4:] if host.startswith("www.") else host
    if not _DOMAIN_RE.match(host) or host in _TOO_BROAD:
        raise ValueError(f"not a valid shop domain: {text!r}")
    return host


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def retailer_for(url: str) -> Retailer | None:
    """Return the trusted retailer owning `url` (exact domain or subdomain), else None.

    Matches on a dot boundary so `evilbestbuy.com` or `bestbuy.com.evil.io` don't pass.
    """
    host = host_of(url)
    if urlparse(url).scheme not in ("http", "https"):
        return None
    for retailer in all_retailers():
        if host == retailer.domain or host.endswith("." + retailer.domain):
            return retailer
    return None


def is_trusted(url: str) -> bool:
    return retailer_for(url) is not None


def untrusted_message(url: str) -> str:
    host = host_of(url)
    dom = host[4:] if host.startswith("www.") else host
    return (f"{dom or url} is not a registered retailer; "
            f"register it with: price-stalker retailer add <Name> {dom or '<domain>'}")
