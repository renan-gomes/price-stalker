"""Parse product name and price out of HTML (JSON-LD first, then meta tags)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlparse

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class Extracted:
    name: str | None
    price_cents: int | None
    currency: str
    in_stock: bool | None
    offers: tuple[int, ...] = ()  # every price compared; price_cents is their minimum
    variant: str = ""  # label of the page variant that was picked (empty if the page has one item)


@dataclass(frozen=True)
class Variant:
    """One selectable option of a product page (size, edition, model...)."""
    label: str
    price_cents: int
    currency: str = "USD"
    in_stock: bool | None = None
    attrs: tuple[tuple[str, str], ...] = ()  # raw attributes, used to match URL query params


class VariantError(ValueError):
    """The page lists several variants and we can't tell which one to track."""


def parse_price(raw) -> int | None:
    """'$1,299.99' / 1299.99 / '1299' -> cents."""
    if raw is None:
        return None
    s = re.sub(r"[^\d.,]", "", str(raw))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else s.replace(",", "")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return int((value * 100).to_integral_value())


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _is_type(node: dict, name: str) -> bool:
    t = node.get("@type")
    return name in t if isinstance(t, list) else t == name


def _offer_price(offer: dict) -> tuple[int | None, str | None]:
    spec = offer.get("priceSpecification")
    if isinstance(spec, list):
        spec = spec[0] if spec else None
    for src in (offer, spec if isinstance(spec, dict) else {}):
        cents = parse_price(src.get("price", src.get("lowPrice")))
        if cents is not None:
            return cents, src.get("priceCurrency") or offer.get("priceCurrency")
    return None, None


def _ld_blobs(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            yield json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue


def _node_offers(node: dict) -> list[tuple[int, str, bool | None]]:
    offers = node.get("offers") or []
    offers = [offers] if isinstance(offers, dict) else offers
    found = []  # (cents, currency, in_stock)
    for offer in _walk(offers):
        if not any(k in offer for k in ("price", "lowPrice", "priceSpecification")):
            continue
        cents, cur = _offer_price(offer)
        if cents is None:
            continue
        avail = str(offer.get("availability", ""))
        stock = None if not avail else not re.search(r"OutOfStock|SoldOut|Discontinued", avail)
        found.append((cents, cur or "USD", stock))
    return found


def _from_json_ld(soup: BeautifulSoup) -> Extracted | None:
    for data in _ld_blobs(soup):
        for node in _walk(data):
            if not _is_type(node, "Product"):
                continue
            found = _node_offers(node)
            if found:
                best = min(found, key=lambda f: f[0])
                return Extracted(node.get("name"), best[0], best[1], best[2],
                                 tuple(sorted({f[0] for f in found})))
    return None


def _pretty(slug: str) -> str:
    return re.sub(r"[_\-]+", " ", slug).strip()


def _woo_variants(soup: BeautifulSoup) -> list[Variant]:
    """WooCommerce embeds every variation as JSON in the add-to-cart form."""
    form = soup.find(attrs={"data-product_variations": True})
    if form is None:
        return []
    try:
        items = json.loads(form["data-product_variations"])
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        cents = parse_price(it.get("display_price"))
        if cents is None:
            continue
        attrs = tuple(sorted((k, str(v)) for k, v in (it.get("attributes") or {}).items() if v))
        label = " / ".join(_pretty(v) for _, v in attrs) or str(it.get("sku") or it.get("variation_id"))
        out.append(Variant(label, cents, "USD", it.get("is_in_stock"), attrs))
    return out


def _ld_variants(soup: BeautifulSoup) -> list[Variant]:
    """schema.org ProductGroup / Product with hasVariant."""
    out = []
    for data in _ld_blobs(soup):
        for node in _walk(data):
            for v in node.get("hasVariant") if isinstance(node.get("hasVariant"), list) else []:
                found = _node_offers(v) if isinstance(v, dict) else []
                label = str(v.get("name") or v.get("sku") or "").strip() if found else ""
                if label:
                    best = min(found, key=lambda f: f[0])
                    out.append(Variant(label, best[0], best[1], best[2]))
    return out


def _variants(soup: BeautifulSoup) -> list[Variant]:
    return _woo_variants(soup) or _ld_variants(soup)


def find_variants(html: str) -> list[Variant]:
    """Every variant the page lists (empty when the page describes a single item)."""
    return _variants(BeautifulSoup(html, "html.parser"))


def _tokens(text: str) -> set[str]:
    """Words and numbers; numbers drop their dot so '3.3' matches a slug's '33' and '33oz' splits in two."""
    return {t.replace(".", "") for t in re.findall(r"\d+(?:\.\d+)?|[a-z]+", text.lower())}


def _match(variants: list[Variant], wanted: str) -> list[Variant]:
    """Variants containing every wanted token; when several do, keep those with the fewest extra tokens
    (so '3.3 oz' picks the plain bottle rather than the 'tester')."""
    w = _tokens(wanted)
    hits = [(len(_tokens(v.label) - w), v) for v in variants if w and w <= _tokens(v.label)]
    if not hits:
        return []
    fewest = min(extra for extra, _ in hits)
    return [v for extra, v in hits if extra == fewest]


def _listing(variants: list[Variant]) -> str:
    return " | ".join(f"{v.label} (${v.price_cents / 100:,.2f})" for v in variants)


def _choose(variants: list[Variant], wanted: str | None, url: str | None) -> Variant:
    if wanted:
        hits = _match(variants, wanted)
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise VariantError(f"variant {wanted!r} not found; available: {_listing(variants)}")
        raise VariantError(f"variant {wanted!r} matches several: {_listing(hits)}")
    query = [(k, v) for k, v in parse_qsl(urlparse(url).query) if k.startswith("attribute_")] if url else []
    if query:
        hits = [v for v in variants if all(dict(v.attrs).get(k) == val for k, val in query)]
        if len(hits) == 1:
            return hits[0]
    raise VariantError(f"page lists {len(variants)} variants; choose one with --variant: {_listing(variants)}")


def _page_name(soup: BeautifulSoup) -> str | None:
    for data in _ld_blobs(soup):
        for node in _walk(data):
            if (_is_type(node, "Product") or _is_type(node, "ProductGroup")) and node.get("name"):
                return node["name"]
    return _meta(soup, "og:title") or (soup.title.string.strip() if soup.title and soup.title.string else None)


def _item_id(url: str | None) -> str | None:
    m = re.search(r"/(\d+)(?:[/?#]|$)", urlparse(url).path + "/") if url else None
    return m.group(1) if m else None


def _next_data_prices(node: dict) -> list[int]:
    """Offer prices of one product node: buy-box, current and secondary offers (bounded, no deep walk)."""
    info = node.get("priceInfo") if isinstance(node.get("priceInfo"), dict) else {}
    top = node.get("topBoostedOffer")
    secondary = info.get("secondaryOfferPrice")
    raw = [
        (info.get("currentPrice") or {}).get("price"),
        ((secondary or {}).get("currentPrice") or {}).get("price") if isinstance(secondary, dict) else None,
        top.get("displayPrice") if isinstance(top, dict) else None,
    ]
    return [c for c in (parse_price(r) for r in raw) if c is not None]


def _from_next_data(soup: BeautifulSoup, url: str | None = None) -> Extracted | None:
    """Next.js sites (e.g. Walmart) embed state in __NEXT_DATA__. Only the product matching the
    URL's item id is read, so sponsored/related products with other prices are ignored."""
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None
    try:
        data = json.loads(tag.string)
    except json.JSONDecodeError:
        return None
    wanted = _item_id(url)
    candidates = [n for n in _walk(data) if isinstance(n.get("priceInfo"), dict)]
    exact = [n for n in candidates if wanted and str(n.get("usItemId")) == wanted]
    for node in exact or candidates:
        prices = _next_data_prices(node)
        if prices:
            stock = node.get("availabilityStatus")
            cur = (node["priceInfo"].get("currentPrice") or {}).get("currencyUnit") or "USD"
            return Extracted(node.get("name"), min(prices), cur,
                             None if not stock else stock == "IN_STOCK", tuple(sorted(set(prices))))
    return None


def looks_blocked(html: str) -> bool:
    low = html.lower()
    return any(m in low for m in (
        "robot or human", "px-captcha", "access denied", "are you a human",
        "api-services-support@amazon.com", "/errors/validatecaptcha",  # Amazon's robot-check page
    ))


def _meta(soup: BeautifulSoup, *keys: str) -> str | None:
    for k in keys:
        tag = soup.find("meta", attrs={"property": k}) or soup.find("meta", attrs={"name": k})
        if tag and tag.get("content"):
            return tag["content"]
    return None


def extract(html: str, url: str | None = None, variant: str | None = None) -> Extracted:
    soup = BeautifulSoup(html, "html.parser")
    variants = _variants(soup)
    if len(variants) >= 2:
        v = _choose(variants, variant, url)
        return Extracted(_page_name(soup), v.price_cents, v.currency, v.in_stock, (v.price_cents,), v.label)
    if variant:
        raise VariantError(f"variant {variant!r} requested but the page lists no variants")
    sources = [f for f in (_from_json_ld(soup), _from_next_data(soup, url)) if f]
    if sources:
        # Same product described by several embedded sources: compare all offers, keep the lowest.
        first = sources[0]
        offers = tuple(sorted({c for f in sources for c in f.offers}))
        best = min(sources, key=lambda f: f.price_cents)
        return Extracted(first.name or best.name, offers[0], best.currency, best.in_stock, offers)
    cents = parse_price(_meta(soup, "product:price:amount", "og:price:amount"))
    cur = _meta(soup, "product:price:currency", "og:price:currency") or "USD"
    name = _meta(soup, "og:title") or (soup.title.string.strip() if soup.title and soup.title.string else None)
    return Extracted(name, cents, cur, None, () if cents is None else (cents,))
