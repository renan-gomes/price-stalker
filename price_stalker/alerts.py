"""Price-drop / target / repeated-failure notifications (terminal always, Telegram if configured)."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

FAIL_WARN_AFTER = 3  # consecutive failed checks before an item is flagged and an alert is sent


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _name(row) -> str:
    tag = row["retailer"] + (f" · {row['variant']}" if row["variant"] else "")
    return f"{row['product']} [{tag}]"


def price_alert(row, prev: int | None, price: int) -> str | None:
    """Message when the price crossed the target or dropped since the previous check, else None."""
    target = row["target_cents"]
    hit = target is not None and price <= target and (prev is None or prev > target)
    drop = prev is not None and price < prev
    if not (hit or drop):
        return None
    parts = []
    if hit:
        parts.append(f"reached your target of {_money(target)}")
    if drop:
        parts.append(f"dropped from {_money(prev)}")
    return f"💰 {_name(row)}: now {_money(price)} — {', '.join(parts)}\n{row['url']}"


def failure_alert(row, count: int, error: str) -> str:
    return f"⚠ {_name(row)}: {count} failed checks in a row — {error}\n{row['url']}"


def telegram_send(text: str, session: requests.Session | None = None) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        resp = (session or requests).post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"}, timeout=10)
        resp.raise_for_status()
    except Exception as exc:  # an alert channel must never break the price check
        log.warning("telegram alert failed: %s", exc)


def notify(text: str) -> None:
    print(text, flush=True)
    telegram_send(text)
