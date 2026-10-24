import pytest

from price_stalker.database.store import Store
from price_stalker.discovery.google import parse_results
from price_stalker.extraction.parser import extract, parse_price
from price_stalker.retailers.registry import is_trusted, retailer_for
from price_stalker.scheduler.runner import check_all


@pytest.mark.parametrize("url,ok", [
    ("https://www.bestbuy.com/site/x/123.p", True),
    ("https://bestbuy.com/x", True),
    ("https://evilbestbuy.com/x", False),
    ("https://bestbuy.com.evil.io/x", False),
    ("ftp://bestbuy.com/x", False),
    ("https://example.com", False),
])
def test_trusted(url, ok):
    assert is_trusted(url) is ok


@pytest.mark.parametrize("raw,cents", [
    ("$1,299.99", 129999), (1299.99, 129999), ("19", 1900), ("1.299,99", 129999),
    ("12,50", 1250), ("", None), (None, None), ("0", None), ("free", None),
])
def test_parse_price(raw, cents):
    assert parse_price(raw) == cents


JSON_LD = """<html><script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":["Product"],"name":"Widget",
"offers":{"@type":"Offer","price":"49.99","priceCurrency":"USD",
"availability":"https://schema.org/InStock"}}]}</script></html>"""


def test_extract_json_ld():
    r = extract(JSON_LD)
    assert (r.name, r.price_cents, r.currency, r.in_stock) == ("Widget", 4999, "USD", True)


def test_extract_out_of_stock_and_aggregate():
    html = """<script type="application/ld+json">{"@type":"Product","name":"W","offers":
    {"@type":"AggregateOffer","lowPrice":10,"priceCurrency":"USD"}}</script>"""
    assert extract(html).price_cents == 1000
    html2 = JSON_LD.replace("InStock", "OutOfStock")
    assert extract(html2).in_stock is False


def test_extract_meta_fallback_and_none():
    html = '<meta property="product:price:amount" content="5.50"><title>T</title>'
    assert extract(html).price_cents == 550
    assert extract("<html></html>").price_cents is None


def test_discovery_filters_untrusted():
    payload = {"items": [
        {"title": "ok", "link": "https://www.target.com/p/a"},
        {"title": "dup", "link": "https://www.target.com/p/a"},
        {"title": "bad", "link": "https://sketchy.shop/a"},
    ]}
    got = parse_results(payload)
    assert [c.url for c in got] == ["https://www.target.com/p/a"]
    assert got[0].retailer.name == "Target"


def test_store_and_check_all(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    good = "https://www.walmart.com/ip/1"
    bad = "https://www.target.com/p/2"
    s.add_listing("Widget", retailer_for(good).name, good)
    s.add_listing("Widget", retailer_for(good).name, good)  # idempotent
    s.add_listing("Widget", retailer_for(bad).name, bad)
    assert len(s.active_listings()) == 2

    def fake_fetch(url):
        if url == bad:
            raise RuntimeError("blocked")
        return JSON_LD

    assert check_all(s, fetch=fake_fetch) == {"ok": 1, "failed": 1}
    hist = s.history("Widget")
    assert len(hist) == 1 and hist[0]["price_cents"] == 4999


def test_extract_next_data_and_blocked():
    from price_stalker.extraction.parser import looks_blocked
    html = ('<script id="__NEXT_DATA__" type="application/json">{"props":{"product":{"name":"P",'
            '"availabilityStatus":"IN_STOCK","priceInfo":{"currentPrice":{"price":62.99,'
            '"currencyUnit":"USD"}}}}}</script>')
    r = extract(html)
    assert (r.name, r.price_cents, r.in_stock) == ("P", 6299, True)
    assert looks_blocked("<title>Robot or human?</title>")


def _next(product_nodes):
    import json
    return '<script id="__NEXT_DATA__">' + json.dumps({"props": {"items": product_nodes}}) + "</script>"


def test_next_data_takes_lowest_offer_of_the_url_product_only():
    main = {"usItemId": "349278478", "name": "Main", "availabilityStatus": "IN_STOCK",
            "priceInfo": {"currentPrice": {"price": 63.94},
                          "secondaryOfferPrice": {"currentPrice": {"price": 62.99}}},
            "topBoostedOffer": {"displayPrice": "$62.99"}}
    sponsored = {"usItemId": "999", "name": "Other", "priceInfo": {"currentPrice": {"price": 9.99}}}
    r = extract(_next([sponsored, main]), "https://www.walmart.com/ip/slug/349278478?x=1")
    assert (r.name, r.price_cents, r.offers) == ("Main", 6299, (6299, 6394))


def test_next_data_single_price_is_stored_as_is():
    main = {"usItemId": "1", "name": "M", "priceInfo": {"currentPrice": {"price": 20}}}
    r = extract(_next([main]), "https://www.walmart.com/ip/slug/1")
    assert (r.price_cents, r.offers) == (2000, (2000,))


def test_json_ld_multiple_offers_takes_lowest():
    html = """<script type="application/ld+json">{"@type":"Product","name":"W","offers":[
    {"@type":"Offer","price":"30.00","priceCurrency":"USD"},
    {"@type":"Offer","price":"25.50","priceCurrency":"USD"}]}</script>"""
    r = extract(html)
    assert (r.price_cents, r.offers) == (2550, (2550, 3000))


def test_json_ld_and_next_data_offers_are_merged():
    import json
    ld = ('<script type="application/ld+json">{"@type":"Product","name":"M","offers":'
          '{"@type":"Offer","price":"63.94","priceCurrency":"USD"}}</script>')
    nd = _next([{"usItemId": "7", "name": "M", "topBoostedOffer": {"displayPrice": "$62.99"},
                 "priceInfo": {"currentPrice": {"price": 63.94}}}])
    r = extract(ld + nd, "https://www.walmart.com/ip/m/7")
    assert (r.price_cents, r.offers) == (6299, (6299, 6394))


def test_overview_latest_price_and_unchecked(tmp_path):
    from datetime import datetime, timedelta, timezone
    s = Store(str(tmp_path / "o.db"))
    a = s.add_listing("Widget", "Walmart", "https://www.walmart.com/ip/1")
    s.add_listing("Widget", "Target", "https://www.target.com/p/2")
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    s.record_price(a, 5000, when=t)
    s.record_price(a, 4500, when=t + timedelta(hours=1))
    rows = {r["retailer"]: r for r in s.overview()}
    assert rows["Walmart"]["price_cents"] == 4500
    assert rows["Target"]["price_cents"] is None


# ---- variants -------------------------------------------------------------------------------

import html as _html
import json as _json

from price_stalker.extraction.parser import VariantError, find_variants


def _woo(variations):
    return ('<form class="variations_form" data-product_variations="'
            + _html.escape(_json.dumps(variations)) + '"></form><title>Explorer</title>')


WOO = _woo([
    {"attributes": {"attribute_pa_mont-blanc_size": "mont-blanc_33oz-tester"}, "display_price": 39.5, "is_in_stock": True},
    {"attributes": {"attribute_pa_mont-blanc_size": "mont-blanc_17oz"}, "display_price": 29.0, "is_in_stock": True},
    {"attributes": {"attribute_pa_mont-blanc_size": "mont-blanc_34oz"}, "display_price": 55.0, "is_in_stock": False},
])
ECO_URL = "https://www.ecosmetics.com/product/explorer-eau-de-parfum-2-2/"


def test_woo_variant_from_url_query_needs_no_flag():
    r = extract(WOO, ECO_URL + "?attribute_pa_mont-blanc_size=mont-blanc_33oz-tester")
    assert (r.price_cents, r.in_stock) == (3950, True) and "33oz" in r.variant


def test_variant_flag_selects_by_tokens():
    assert extract(WOO, ECO_URL, "3.4 oz").price_cents == 5500
    assert extract(WOO, ECO_URL, "3.4oz").in_stock is False
    assert extract(WOO, ECO_URL, "1.7 oz").price_cents == 2900


def test_ambiguous_page_without_variant_raises_with_options():
    with pytest.raises(VariantError, match="choose one with --variant") as e:
        extract(WOO, ECO_URL)
    assert "$29.00" in str(e.value) and "$55.00" in str(e.value)


def test_unknown_or_multiple_variant_and_flag_on_single_page():
    with pytest.raises(VariantError, match="not found"):
        extract(WOO, ECO_URL, "8 oz")
    with pytest.raises(VariantError, match="matches several"):
        extract(WOO, ECO_URL, "oz")
    with pytest.raises(VariantError, match="no variants"):
        extract(JSON_LD, "https://www.target.com/p/1", "3.4 oz")


def test_json_ld_has_variant_does_not_pick_first_variant():
    html = """<script type="application/ld+json">{"@type":"ProductGroup","name":"PS5","hasVariant":[
    {"@type":"Product","name":"PS5 Slim","offers":{"@type":"Offer","price":"499.99","priceCurrency":"USD"}},
    {"@type":"Product","name":"PS5 Pro","offers":{"@type":"Offer","price":"699.99","priceCurrency":"USD"}}]}</script>"""
    assert [v.label for v in find_variants(html)] == ["PS5 Slim", "PS5 Pro"]
    assert extract(html, None, "Pro").price_cents == 69999
    assert extract(html, None, "slim").price_cents == 49999
    with pytest.raises(VariantError):
        extract(html)


def test_same_url_different_variants_are_separate_listings(tmp_path):
    s = Store(str(tmp_path / "v.db"))
    a = s.add_listing("Fragrance 100ml", "eCosmetics", ECO_URL, "3.4 oz")
    b = s.add_listing("Fragrance 50ml", "eCosmetics", ECO_URL, "1.7 oz")
    assert a != b and s.add_listing("Fragrance 100ml", "eCosmetics", ECO_URL, "3.4 oz") == a


def test_check_all_passes_variant_and_reports_error(tmp_path):
    s = Store(str(tmp_path / "c.db"))
    s.add_listing("Big", "eCosmetics", ECO_URL, "3.4 oz")
    s.add_listing("Unspecified", "eCosmetics", ECO_URL + "?x=1")
    assert check_all(s, fetch=lambda url: WOO) == {"ok": 1, "failed": 1}
    assert s.history("Big")[0]["price_cents"] == 5500


def test_migration_keeps_history_from_pre_variant_db(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
    CREATE TABLE listings (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        retailer TEXT NOT NULL, url TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE prices (id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
        price_cents INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'USD', in_stock INTEGER, checked_at TEXT NOT NULL);
    INSERT INTO products VALUES (1,'Old');
    INSERT INTO listings VALUES (1,1,'Walmart','https://www.walmart.com/ip/1',1);
    INSERT INTO prices VALUES (1,1,6299,'USD',1,'2026-10-24T20:00:00+00:00');
    """)
    c.commit(); c.close()
    s = Store(path)
    assert [(r["price_cents"], r["variant"]) for r in s.history("Old")] == [(6299, "")]
    assert s.add_listing("Old", "Walmart", "https://www.walmart.com/ip/1") == 1
    Store(path)  # second open is a no-op
    assert len(s.history("Old")) == 1


ECO_REAL = _woo([{"attributes": {"attribute_pa_mont-blanc_size": f"mont-blanc_{slug}"}, "display_price": price,
                  "is_in_stock": True} for slug, price in [
    ("67oz-tester", 89.99), ("1oz", 80), ("2oz", 100), ("33oz", 136), ("33oz-tester", 81.36), ("67oz", 167)]])

FN_REAL = ('<script type="application/ld+json">' + _json.dumps({"@type": "ProductGroup", "hasVariant": [
    {"@type": "Product", "name": n, "offers": {"@type": "Offer", "price": p, "priceCurrency": "USD"}} for n, p in [
        ("Mont Blanc Explorer Eau De Parfum Spray Vial", 3.99),
        ("Mont Blanc Explorer Eau De Parfum Mini 0.15 oz", 14.99),
        ("Mont Blanc Explorer Eau De Parfum Spray 0.27 oz Travel Spray", 20.99),
        ("Mont Blanc Explorer Eau De Parfum Spray 1 oz", 51.99),
        ("Mont Blanc Explorer Eau De Parfum Spray 2 oz", 66.99),
        ("Mont Blanc Explorer Eau De Parfum Spray 3.3 oz", 97.99),
        ("Mont Blanc Explorer Eau De Parfum Spray 3.3 oz Tester", 81.99),
        ("Mont Blanc Explorer Eau De Parfum Spray 6.7 oz", 138.99)]]}) + "</script>")


@pytest.mark.parametrize("html,wanted,cents", [
    (ECO_REAL, "3.3 oz", 13600), (ECO_REAL, "3.3 oz tester", 8136), (ECO_REAL, "tester 6.7oz", 8999),
    (ECO_REAL, "6.7 oz", 16700), (ECO_REAL, "1 oz", 8000), (ECO_REAL, "2oz", 10000),
    (FN_REAL, "3.3 oz", 9799), (FN_REAL, "3.3 oz tester", 8199), (FN_REAL, "1 oz", 5199),
    (FN_REAL, "0.15 oz", 1499), (FN_REAL, "0.27 oz", 2099), (FN_REAL, "vial", 399),
    (FN_REAL, "6.7 oz", 13899), (FN_REAL, "2 oz", 6699),
])
def test_real_site_variant_labels(html, wanted, cents):
    assert extract(html, None, wanted).price_cents == cents


# ---- pause / resume / remove ----------------------------------------------------------------

from price_stalker.cli import main, parse_ids


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PRICE_STALKER_DB", str(tmp_path / "cli.db"))
    s = Store()
    ids = [s.add_listing("A", "Walmart", "https://www.walmart.com/ip/1"),
           s.add_listing("A", "Target", "https://www.target.com/p/2"),
           s.add_listing("B", "Newegg", "https://www.newegg.com/p/3")]
    for i in ids:
        s.record_price(i, 1000)
    return s, ids


def test_parse_ids():
    assert parse_ids(["1,3", "5"]) == [1, 3, 5] and parse_ids(["2-4"]) == [2, 3, 4]
    with pytest.raises(SystemExit):
        parse_ids(["abc"])
    with pytest.raises(SystemExit):
        parse_ids(["5-2"])


def test_pause_and_resume_by_number(db, capsys):
    s, ids = db
    assert main(["pause", "1,2"]) == 0
    assert [r["id"] for r in s.active_listings()] == [3]
    assert len(s.history()) == 3  # history kept
    main(["list"])
    assert "(paused)" in capsys.readouterr().out
    main(["resume", "2"])
    assert [r["id"] for r in s.active_listings()] == [2, 3]


def test_remove_asks_for_confirmation_and_cleans_up(db, monkeypatch):
    s, ids = db
    monkeypatch.setattr("builtins.input", lambda _="": "n")
    main(["remove", "3"])
    assert len(s.overview()) == 3
    monkeypatch.setattr("builtins.input", lambda _="": "y")
    main(["remove", "3"])
    assert sorted(r["id"] for r in s.overview()) == [1, 2]
    assert s.products() == ["A"]  # product B had no listing left
    assert len(s.history()) == 2  # prices of the removed item went with it


def test_remove_yes_flag_and_interactive_menu(db, monkeypatch, capsys):
    s, ids = db
    main(["remove", "-y", "1"])
    assert [r["id"] for r in s.overview()] == [2, 3]
    answers = iter(["2", "y"])
    monkeypatch.setattr("builtins.input", lambda _="": next(answers))
    main(["remove"])
    assert [r["id"] for r in s.overview()] == [3]
    assert "#2" in capsys.readouterr().out


def test_unknown_number_and_empty_pool(db, capsys):
    with pytest.raises(SystemExit, match="99"):
        main(["remove", "99"])
    main(["resume"])  # nothing is paused
    assert "nothing to resume" in capsys.readouterr().out


# ---- alerts, variation in list, repeated failures --------------------------------------------

from price_stalker import alerts


def _row(**kw):
    base = {"product": "P", "retailer": "Walmart", "variant": "", "url": "https://www.walmart.com/ip/1",
            "target_cents": None}
    return {**base, **kw}


def test_price_alert_rules():
    assert alerts.price_alert(_row(), None, 5000) is None          # first price: nothing to compare
    assert alerts.price_alert(_row(), 5000, 5000) is None          # unchanged
    assert alerts.price_alert(_row(), 5000, 5500) is None          # went up
    assert "dropped from $50.00" in alerts.price_alert(_row(), 5000, 4500)
    t = alerts.price_alert(_row(target_cents=4600), 5000, 4500)    # crossed the target
    assert "reached your target of $46.00" in t and "dropped" in t
    assert alerts.price_alert(_row(target_cents=4600), 4500, 4500) is None   # already below: no repeat
    assert "target" in alerts.price_alert(_row(target_cents=4600), None, 4000)  # first price already <= target


def test_check_all_sends_alerts_and_failure_streak_once(tmp_path):
    s = Store(str(tmp_path / "a.db"))
    url = "https://www.walmart.com/ip/1"
    lid = s.add_listing("W", "Walmart", url)
    s.set_target([lid], 4500)
    sent, page = [], {"price": 50}

    def fetch(_):
        if page["price"] is None:
            raise RuntimeError("blocked")
        return f'<script type="application/ld+json">{{"@type":"Product","name":"W","offers":{{"price":{page["price"]},"priceCurrency":"USD"}}}}</script>'

    check_all(s, fetch=fetch, notify=sent.append)
    assert sent == []                                  # first price
    page["price"] = 44
    check_all(s, fetch=fetch, notify=sent.append)
    assert len(sent) == 1 and "reached your target" in sent[0]
    page["price"] = None
    for _ in range(5):
        check_all(s, fetch=fetch, notify=sent.append)
    fails = [m for m in sent if m.startswith("⚠")]
    assert len(fails) == 1 and "3 failed checks" in fails[0]     # once per streak
    page["price"] = 44
    check_all(s, fetch=fetch, notify=sent.append)
    assert s.overview()[0]["fail_count"] == 0                    # success resets the streak


def test_list_shows_variation_min_target_and_failures(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PRICE_STALKER_DB", str(tmp_path / "l.db"))
    s = Store()
    lid = s.add_listing("W", "Walmart", "https://www.walmart.com/ip/1")
    s.record_price(lid, 5000); s.record_price(lid, 4500)
    s.set_target([lid], 4600)
    for _ in range(3):
        s.record_failure(lid, "blocked by anti-bot page")
    main(["list"])
    out = capsys.readouterr().out
    assert "▼ 5.00" in out and "min 45.00" in out and "target 46.00 ✔" in out
    assert "failing since" in out and "3 checks in a row" in out and "anti-bot" in out


def test_target_command_and_add_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PRICE_STALKER_DB", str(tmp_path / "t.db"))
    main(["add", "W", "https://www.walmart.com/ip/1", "--target", "119.99"])
    s = Store()
    assert s.active_listings()[0]["target_cents"] == 11999
    main(["target", "1", "$100"])
    assert s.active_listings()[0]["target_cents"] == 10000
    main(["target", "1", "none"])
    assert s.active_listings()[0]["target_cents"] is None
    with pytest.raises(SystemExit):
        main(["target", "9", "10"])
    with pytest.raises(SystemExit):
        main(["add", "W", "https://www.walmart.com/ip/2", "--target", "abc"])


def test_telegram_only_when_configured(monkeypatch):
    calls = []

    class FakeSession:
        def post(self, url, data, timeout):
            calls.append((url, data))
            class R:
                def raise_for_status(self): pass
            return R()

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    alerts.telegram_send("hi", FakeSession())
    assert calls == []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    alerts.telegram_send("hi", FakeSession())
    assert calls[0][0].endswith("/bot123:abc/sendMessage") and calls[0][1]["chat_id"] == "42"

    class Boom:
        def post(self, *a, **k):
            raise RuntimeError("network down")
    alerts.telegram_send("hi", Boom())  # must not raise


def test_migration_adds_alert_columns_to_variant_era_db(tmp_path):
    import sqlite3
    path = str(tmp_path / "v1.db")
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
    CREATE TABLE listings (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        retailer TEXT NOT NULL, url TEXT NOT NULL, variant TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
        UNIQUE (url, variant));
    CREATE TABLE prices (id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
        price_cents INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'USD', in_stock INTEGER, checked_at TEXT NOT NULL);
    INSERT INTO products VALUES (1,'Old');
    INSERT INTO listings (id, product_id, retailer, url) VALUES (1,1,'Walmart','https://www.walmart.com/ip/1');
    INSERT INTO prices VALUES (1,1,6299,'USD',1,'2026-10-24T20:00:00+00:00');
    """)
    c.commit(); c.close()
    s = Store(path)
    row = s.overview()[0]
    assert (row["price_cents"], row["target_cents"], row["fail_count"]) == (6299, None, 0)
    s.set_target([1], 5000)
    assert Store(path).active_listings()[0]["target_cents"] == 5000


# ---- user-registered retailers ---------------------------------------------------------------

from price_stalker.retailers import registry
from price_stalker.extraction.fetcher import fetch_html


@pytest.fixture(autouse=True)
def _reset_registry():
    registry.set_custom([])
    yield
    registry.set_custom([])


@pytest.mark.parametrize("text,domain", [
    ("sephora.com", "sephora.com"), ("www.Sephora.com", "sephora.com"),
    ("https://www.sephora.com/product/x-P1?skuId=1#a", "sephora.com"), ("shop.example.com:8080/x", "shop.example.com"),
])
def test_normalize_domain(text, domain):
    assert registry.normalize_domain(text) == domain


@pytest.mark.parametrize("bad", ["", "localhost", "1.2.3.4", "com", "co.uk", "not a domain", "http://", "a..b.com"])
def test_normalize_domain_rejects(bad):
    with pytest.raises(ValueError):
        registry.normalize_domain(bad)


def test_retailer_add_list_remove_via_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PRICE_STALKER_DB", str(tmp_path / "r.db"))
    sephora = "https://www.sephora.com/product/explorer-P450810?skuId=2227908"
    main(["add", "Perfume", sephora])
    assert "skipped: sephora.com is not a registered retailer" in capsys.readouterr().out
    main(["retailer", "add", "Sephora", "sephora.com"])
    assert "registered Sephora (sephora.com)" in capsys.readouterr().out
    assert registry.is_trusted(sephora) and registry.is_trusted("https://shop.sephora.com/x")
    assert not registry.is_trusted("https://evilsephora.com/x")
    assert not registry.is_trusted("https://sephora.com.evil.io/x")
    main(["add", "Perfume", sephora])
    assert Store().active_listings()[0]["retailer"] == "Sephora"
    main(["retailers"])
    out = capsys.readouterr().out
    assert "Sephora" in out and "(yours)" in out and "Walmart" in out and out.count("(yours)") == 1
    main(["retailer", "remove", "https://www.sephora.com"])
    out = capsys.readouterr().out
    assert "removed sephora.com" in out and "1 tracked item(s)" in out
    assert not registry.is_trusted(sephora)


def test_retailer_add_argument_forms_duplicates_and_builtin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PRICE_STALKER_DB", str(tmp_path / "r2.db"))
    main(["retailer", "add", "ulta.com"])                       # name derived from the domain
    main(["retailer", "add", "ulta.com", "Ulta Beauty"])        # swapped order, already registered
    assert "already registered" in capsys.readouterr().out.splitlines()[-1]
    main(["retailer", "add", "Best Buy", "bestbuy.com"])        # built in
    assert "already registered as Best Buy" in capsys.readouterr().out
    assert [(r.name, r.domain) for r in Store().custom_retailers()] == [("Ulta", "ulta.com")]
    with pytest.raises(SystemExit, match="not a valid shop domain"):
        main(["retailer", "add", "Nope", "co.uk"])
    with pytest.raises(SystemExit, match="built in"):
        main(["retailer", "remove", "walmart.com"])
    with pytest.raises(SystemExit, match="not one of your"):
        main(["retailer", "remove", "never-added.com"])


def test_untrusted_fetch_gives_actionable_message_and_debug_exits_cleanly(tmp_path, monkeypatch):
    with pytest.raises(registry.UntrustedDomain, match=r"retailer add <Name> sephora.com"):
        fetch_html("https://www.sephora.com/product/x")
    monkeypatch.setenv("PRICE_STALKER_DB", str(tmp_path / "d.db"))
    with pytest.raises(SystemExit, match="sephora.com is not a registered retailer"):
        main(["debug", "https://www.sephora.com/product/x"])


def test_registered_retailers_persist_and_reach_discovery(tmp_path):
    from price_stalker.discovery.google import _site_clause
    path = str(tmp_path / "p.db")
    Store(path).add_retailer("Sephora", "sephora.com")
    registry.set_custom([])
    Store(path)  # reopening loads them again
    assert registry.is_trusted("https://www.sephora.com/x") and "site:sephora.com" in _site_clause()


def test_fetch_errors_are_short_and_debug_exits_cleanly(tmp_path, monkeypatch):
    import requests
    from price_stalker.extraction.fetcher import FetchError

    class Resp:
        def __init__(self, code):
            self.status_code, self.ok, self.text = code, code < 400, ""

    class S:
        def __init__(self, code=None): self.code = code
        def get(self, *a, **k):
            if self.code is None:
                raise requests.ConnectionError("dns")
            return Resp(self.code)

    def no_browser(url):
        raise FetchError("browser mode is not installed")

    url = "https://www.walmart.com/ip/1"
    with pytest.raises(FetchError, match=r"^HTTP 404$"):
        fetch_html(url, session=S(404))
    with pytest.raises(FetchError, match="network error: ConnectionError"):
        fetch_html(url, session=S())
    with pytest.raises(FetchError, match=r"^blocked by the site \(HTTP 403\)"):
        fetch_html(url, session=S(403), browser=no_browser)
    monkeypatch.setenv("PRICE_STALKER_DB", str(tmp_path / "e.db"))
    monkeypatch.setattr("price_stalker.extraction.fetcher.requests.get", lambda *a, **k: Resp(404))
    with pytest.raises(SystemExit, match="HTTP 404"):
        main(["debug", url])


def test_amazon_robot_check_is_recognised_and_amazon_not_builtin(tmp_path, monkeypatch, capsys):
    from price_stalker.extraction.parser import looks_blocked
    page = ('<html><title>Amazon.com</title>To discuss automated access to Amazon data please contact '
            'api-services-support@amazon.com. <a href="/errors/validateCaptcha">Continue shopping</a></html>')
    assert looks_blocked(page) and extract(page).price_cents is None
    assert not looks_blocked(JSON_LD)
    url = "https://www.amazon.com/dp/B07K1BW1XY"
    assert not registry.is_trusted(url)
    monkeypatch.setenv("PRICE_STALKER_DB", str(tmp_path / "a.db"))
    main(["retailer", "add", "Amazon", "amazon.com"])    # opt-in still possible
    assert registry.is_trusted(url)
