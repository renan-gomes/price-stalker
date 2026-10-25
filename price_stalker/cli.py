from __future__ import annotations

import argparse
import logging
import re

from price_stalker.alerts import FAIL_WARN_AFTER
from price_stalker.database.store import Store
from price_stalker.extraction.parser import parse_price
from price_stalker.retailers import registry
from price_stalker.retailers.registry import retailer_for


def parse_ids(tokens: list[str]) -> list[int]:
    """'1 3', '1,3' and '2-4' (any mix) -> sorted unique item numbers."""
    ids: set[int] = set()
    for tok in " ".join(tokens).replace(",", " ").split():
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", tok)
        if not m or int(m[2] or m[1]) < int(m[1]) or int(m[2] or m[1]) > 1_000_000:
            raise SystemExit(f"not an item number or range: {tok!r} (examples: 3  1,4  2-5)")
        ids.update(range(int(m[1]), int(m[2] or m[1]) + 1))
    return sorted(ids)


def _price_line(r) -> str:
    if r["price_cents"] is None:
        return "not checked yet"
    now, prev = r["price_cents"], r["prev_cents"]
    text = f"{r['currency']} {now / 100:,.2f}"
    if prev is not None:
        text += " =" if now == prev else f" {'▼' if now < prev else '▲'} {abs(now - prev) / 100:,.2f}"
    extras = []
    if r["min_cents"] is not None and prev is not None:
        extras.append(f"min {r['min_cents'] / 100:,.2f}")
    if r["target_cents"] is not None:
        extras.append(f"target {r['target_cents'] / 100:,.2f}" + (" ✔" if now <= r["target_cents"] else ""))
    return text + (f" ({', '.join(extras)})" if extras else "") + f"  [{r['checked_at'][:16].replace('T', ' ')} UTC]"


def print_overview(rows) -> None:
    last = None
    for r in rows:
        if r["product"] != last:
            print(f"\n{r['product']}")
            last = r["product"]
        tag = r["retailer"] + (f" · {r['variant']}" if r["variant"] else "")
        print(f"  #{r['id']} [{tag}] {_price_line(r)}{'' if r['active'] else '  (paused)'}\n      {r['url']}")
        if r["fail_count"] >= FAIL_WARN_AFTER:
            print(f"      ⚠ failing since {r['fail_since'][:16].replace('T', ' ')} UTC "
                  f"({r['fail_count']} checks in a row): {r['last_error']}")


def split_name_domain(items: list[str]) -> tuple[str, str]:
    """`retailer add` accepts 'Name domain', 'domain Name' or just 'domain'."""
    if len(items) == 1:
        domain = registry.normalize_domain(items[0])
        return (domain.split(".")[0].capitalize() if domain.count(".") == 1 else domain), domain
    if len(items) != 2:
        raise SystemExit('usage: price-stalker retailer add "Name" domain.com   (quote names with spaces)')
    name, dom = items
    try:
        return name, registry.normalize_domain(dom)
    except ValueError:
        try:
            return dom, registry.normalize_domain(name)
        except ValueError as exc:
            raise SystemExit(str(exc))


def pick(store: Store, tokens: list[str], verb: str, active: bool | None = None):
    """Resolve item numbers from the command line, or ask interactively. Returns (ids, rows)."""
    pool = [r for r in store.overview() if active is None or bool(r["active"]) == active]
    if not pool:
        print(f"nothing to {verb}")
        return [], []
    if not tokens:
        print_overview(pool)
        try:
            text = input(f"\nNumber(s) to {verb} (e.g. 1,3 or 2-4; Enter to cancel): ")
        except EOFError:
            text = ""
        tokens = [text]
    ids = parse_ids(tokens)
    unknown = sorted(set(ids) - {r["id"] for r in pool})
    if unknown:
        raise SystemExit(f"no item to {verb} with number(s) {unknown}; see `price-stalker list`")
    return ids, [r for r in pool if r["id"] in ids]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="price-stalker")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="find listings on trusted retailers via Google")
    s.add_argument("query")
    s.add_argument("--track", metavar="NAME", help="also track every result under this product name")

    a = sub.add_parser("add", help="track specific URLs")
    a.add_argument("name")
    a.add_argument("urls", nargs="+")
    a.add_argument("--target", metavar="PRICE", help="alert when the price reaches this value (e.g. 120 or 119.99)")
    a.add_argument("--variant", help='option to track when the page lists several (e.g. "3.3 oz tester", "Pro")')

    d = sub.add_parser("debug", help="fetch one URL and report what the extractor sees")
    d.add_argument("url")
    d.add_argument("--variant")
    d.add_argument("--browser", action="store_true", help="skip the plain request and use the headless browser")

    sub.add_parser("list", help="show tracked products, URLs and latest prices")
    for name, text in (("pause", "stop checking items (history is kept)"),
                       ("resume", "start checking paused items again"),
                       ("remove", "delete items and their price history")):
        sp = sub.add_parser(name, help=text)
        sp.add_argument("ids", nargs="*", help="item numbers from `list` (e.g. 3  1,4  2-5); omit to pick from a menu")
        if name == "remove":
            sp.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    t = sub.add_parser("target", help="set or clear the alert price of an item")
    t.add_argument("id", type=int, help="item number from `list`")
    t.add_argument("price", help="e.g. 120 or 119.99; use 'none' to clear")

    sub.add_parser("retailers", help="list the retailers that can be tracked")
    rp = sub.add_parser("retailer", help="register or remove a retailer")
    rsub = rp.add_subparsers(dest="rcmd", required=True)
    ra = rsub.add_parser("add", help='e.g. retailer add Sephora sephora.com')
    ra.add_argument("items", nargs="+", metavar="NAME DOMAIN")
    rr = rsub.add_parser("remove", help="remove a retailer you registered")
    rr.add_argument("domain")

    pr = sub.add_parser("probe", help="test whether shops can be read (give one product URL per shop)")
    pr.add_argument("urls", nargs="*")
    pr.add_argument("-f", "--file", help="text file with one URL per line (# comments allowed)")
    pr.add_argument("--register", action="store_true", help="register the shops that work")

    al = sub.add_parser("alerts", help="set up and test Telegram alerts")
    asub = al.add_subparsers(dest="acmd", required=True)
    asub.add_parser("test", help="send a test message to your Telegram")
    asub.add_parser("chat-id", help="find your chat id (send any message to your bot first)")

    sub.add_parser("check", help="fetch current prices once")
    sc = sub.add_parser("schedule", help="check periodically")
    sc.add_argument("--every", type=float, default=6.0, help="hours between checks")

    args = p.parse_args(argv)
    store = Store()

    if args.cmd == "search":
        from price_stalker.discovery.google import search
        for c in search(args.query):
            print(f"[{c.retailer.name}] {c.title}\n    {c.url}")
            if args.track:
                store.add_listing(args.track, c.retailer.name, c.url)
    elif args.cmd == "add":
        target = None
        if args.target is not None:
            target = parse_price(args.target)
            if target is None:
                raise SystemExit(f"invalid --target: {args.target!r}")
        for url in args.urls:
            r = retailer_for(url)
            if r is None:
                print(f"skipped: {registry.untrusted_message(url)}")
                continue
            lid = store.add_listing(args.name, r.name, url, args.variant or "")
            if target is not None:
                store.set_target([lid], target)
            print(f"tracking [{r.name}]{' (' + args.variant + ')' if args.variant else ''} {url}")
    elif args.cmd == "debug":
        from price_stalker.extraction.fetcher import FetchError, fetch_html
        from price_stalker.extraction.parser import VariantError, extract, find_variants, looks_blocked
        try:
            html = fetch_html(args.url, force_browser=args.browser)
        except (registry.UntrustedDomain, FetchError) as exc:
            raise SystemExit(str(exc))
        print(f"bytes={len(html)} ld+json={'ld+json' in html} next_data={'__NEXT_DATA__' in html} "
              f"blocked={looks_blocked(html)}")
        variants = find_variants(html)
        print(f"variants found: {len(variants)}")
        for v in variants:
            print(f"  - {v.label}: {v.currency} {v.price_cents / 100:,.2f}  in_stock={v.in_stock}")
        try:
            result = extract(html, args.url, args.variant)
            print(result)
            print('offers compared (USD):', [f'{c / 100:.2f}' for c in result.offers], '-> stored:', result.price_cents)
        except VariantError as exc:
            print(f"variant error: {exc}")
        out = "debug_page.html"
        open(out, "w", encoding="utf-8").write(html)
        print(f"saved raw page to {out}")
    elif args.cmd == "retailers":
        custom = {r.domain for r in store.custom_retailers()}
        for r in registry.all_retailers():
            print(f"  {r.name:<16} {r.domain}{'   (yours)' if r.domain in custom else ''}")
    elif args.cmd == "retailer" and args.rcmd == "add":
        name, domain = split_name_domain(args.items)
        if store.add_retailer(name, domain):
            print(f"registered {name} ({domain}); try: price-stalker debug '<product url>'")
        else:
            owner = registry.retailer_for(f"https://{domain}")
            print(f"{domain} is already registered as {owner.name if owner else domain}; nothing changed")
    elif args.cmd == "retailer" and args.rcmd == "remove":
        try:
            domain = registry.normalize_domain(args.domain)
        except ValueError as exc:
            raise SystemExit(str(exc))
        if domain in registry.BUILTIN_DOMAINS:
            raise SystemExit(f"{domain} is built in and can't be removed")
        affected = [r for r in store.overview() if registry.host_of(r["url"]) in (domain, "www." + domain)
                    or registry.host_of(r["url"]).endswith("." + domain)]
        if not store.remove_retailer(domain):
            raise SystemExit(f"{domain} is not one of your registered retailers (see `price-stalker retailers`)")
        print(f"removed {domain}")
        if affected:
            print(f"note: {len(affected)} tracked item(s) from {domain} will fail checks until you register it again "
                  f"(see `price-stalker list`; `pause` stops the checks)")
    elif args.cmd == "probe":
        from price_stalker.extraction.probe import probe
        urls = list(args.urls)
        if args.file:
            with open(args.file, encoding="utf-8") as fh:
                urls += [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
        if not urls:
            raise SystemExit("give product URLs, or a file with -f urls.txt (one product page per shop)")
        results = []
        for url in dict.fromkeys(urls):
            res = probe(url)
            results.append(res)
            print(f"{res.status:<13} {res.domain:<26} {res.detail}")
        counts = {s: sum(r.status == s for r in results) for s in ("OK", "NEEDS_VARIANT", "NO_PRICE", "BLOCKED", "ERROR")}
        print("\n" + ", ".join(f"{n} {s}" for s, n in counts.items() if n))
        todo = {}
        for r in results:
            if r.supported and registry.retailer_for(r.url) is None:
                todo.setdefault(r.domain, r)
        for domain in todo:
            name, dom = split_name_domain([domain])
            if args.register:
                store.add_retailer(name, dom)
                print(f"registered {name} ({dom})")
            else:
                print(f"works but not registered: price-stalker retailer add {name} {dom}")
        if todo and not args.register:
            print("(or run the same probe again with --register)")
    elif args.cmd == "alerts":
        from price_stalker import alerts
        token, chat = alerts._telegram_creds()
        if args.acmd == "chat-id":
            if not token:
                raise SystemExit("set TELEGRAM_BOT_TOKEN first (get it from @BotFather); see README > Alerts")
            try:
                chats = alerts.telegram_chat_ids()
            except Exception as exc:
                raise SystemExit(f"telegram failed: {alerts.telegram_error(exc)}")
            if not chats:
                raise SystemExit("no messages found: open your bot in Telegram, press Start / send any text, run this again")
            for cid, label in chats:
                print(f"TELEGRAM_CHAT_ID={cid}   ({label})")
        else:
            if not token or not chat:
                raise SystemExit("not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
                                 "(`price-stalker alerts chat-id` finds the second one); see README > Alerts")
            try:
                alerts.telegram_send("✅ price-stalker: Telegram alerts are working", raise_errors=True)
            except Exception as exc:
                raise SystemExit(f"telegram failed: {alerts.telegram_error(exc)}")
            print("test message sent; check your Telegram")
    elif args.cmd == "target":
        if args.id not in {r["id"] for r in store.overview()}:
            raise SystemExit(f"no item #{args.id}; see `price-stalker list`")
        if args.price.lower() in ("none", "clear", "off"):
            store.set_target([args.id], None)
            print(f"#{args.id}: target cleared")
        else:
            cents = parse_price(args.price)
            if cents is None:
                raise SystemExit(f"invalid price: {args.price!r}")
            store.set_target([args.id], cents)
            print(f"#{args.id}: you'll be alerted at ${cents / 100:,.2f} or less")
    elif args.cmd == "list":
        rows = store.overview()
        print_overview(rows) if rows else print("nothing tracked yet")
    elif args.cmd in ("pause", "resume"):
        ids, rows = pick(store, args.ids, args.cmd, active=args.cmd == "pause")
        if ids:
            store.set_active(ids, args.cmd == "resume")
            print(f"{'paused' if args.cmd == 'pause' else 'resumed'} {len(ids)} item(s): "
                  + ", ".join(f"#{r['id']}" for r in rows))
    elif args.cmd == "remove":
        ids, rows = pick(store, args.ids, "remove")
        if ids:
            if not args.yes:
                print_overview(rows)
                try:
                    ok = input(f"\nDelete {len(ids)} item(s) and their price history? "
                               "(`pause` keeps the history) [y/N] ").strip().lower() in ("y", "yes", "s", "sim")
                except EOFError:
                    ok = False
                if not ok:
                    print("cancelled, nothing deleted")
                    return 0
            print(f"deleted {store.delete_listings(ids)} item(s)")
    elif args.cmd == "check":
        from price_stalker.scheduler.runner import check_all
        print(check_all(store))
    elif args.cmd == "schedule":
        from price_stalker.scheduler.runner import run_forever
        run_forever(store, args.every)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
