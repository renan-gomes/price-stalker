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


def _telegram_creds() -> tuple[str | None, str | None]:
    return os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def telegram_error(exc: Exception) -> str:
    """One readable line for a failed Telegram call (Telegram explains itself in the JSON body)."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            return f"HTTP {resp.status_code}: {resp.json().get('description', '')}".strip()
        except Exception:
            return f"HTTP {resp.status_code}"
    return f"{type(exc).__name__}: {exc}"


def telegram_send(text: str, session: requests.Session | None = None, raise_errors: bool = False) -> bool | None:
    """True = sent, None = not configured. Failures are logged, or raised with raise_errors=True."""
    token, chat = _telegram_creds()
    if not token or not chat:
        return None
    try:
        resp = (session or requests).post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as exc:  # an alert channel must never break the price check
        if raise_errors:
            raise
        log.warning("telegram alert failed: %s", telegram_error(exc))
        return False


def telegram_chat_ids(session: requests.Session | None = None) -> list[tuple[int, str]]:
    """Chats that recently wrote to the bot: [(chat_id, label)]. Needs TELEGRAM_BOT_TOKEN only."""
    token, _ = _telegram_creds()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    resp = (session or requests).get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
    resp.raise_for_status()
    seen: dict[int, str] = {}
    for update in resp.json().get("result", []):
        msg = next((update[k] for k in ("message", "channel_post", "edited_message") if k in update), {})
        chat = msg.get("chat") or {}
        if "id" in chat:
            seen[chat["id"]] = chat.get("title") or " ".join(
                x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username", "")
    return list(seen.items())


def notify(text: str) -> None:
    print(text, flush=True)
    telegram_send(text)
