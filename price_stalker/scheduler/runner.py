"""Periodic price checks."""
from __future__ import annotations

import logging

from price_stalker import alerts
from price_stalker.database.store import Store
from price_stalker.extraction.fetcher import fetch_html
from price_stalker.extraction.parser import extract, looks_blocked

log = logging.getLogger(__name__)


def check_all(store: Store, fetch=fetch_html, notify=None) -> dict[str, int]:
    """Fetch every active listing once. One failure never aborts the run.

    `notify(text)` receives price-drop / target / repeated-failure alerts (default: terminal + Telegram).
    """
    notify = notify or alerts.notify
    ok = failed = 0
    for row in store.active_listings():
        message = None
        try:
            html = fetch(row["url"])
            result = extract(html, row["url"], row["variant"] or None)
            if result.price_cents is None:
                raise ValueError("blocked by anti-bot page" if looks_blocked(html) else "no price found on page")
            prev = store.last_price(row["id"])
            store.record_price(row["id"], result.price_cents, result.currency, result.in_stock)
            ok += 1
            message = alerts.price_alert(row, prev, result.price_cents)
        except Exception as exc:  # network, HTTP block, parse: log and continue
            failed += 1
            log.warning("check failed for %s: %s", row["url"], exc)
            streak = store.record_failure(row["id"], str(exc)[:300])
            if streak == alerts.FAIL_WARN_AFTER:  # alert once per streak, not on every retry
                message = alerts.failure_alert(row, streak, str(exc)[:300])
        if message:
            notify(message)
    return {"ok": ok, "failed": failed}


def run_forever(store: Store, every_hours: float = 6.0) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    sched = BlockingScheduler()
    sched.add_job(lambda: log.info("check: %s", check_all(store)), "interval",
                  hours=every_hours, next_run_time=__import__("datetime").datetime.now())
    sched.start()
