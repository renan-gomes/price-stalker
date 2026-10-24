"""SQLite persistence: products, listings (one URL per retailer), price history."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from price_stalker.retailers import registry

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    retailer TEXT NOT NULL,
    url TEXT NOT NULL,
    variant TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    target_cents INTEGER,
    fail_count INTEGER NOT NULL DEFAULT 0,
    fail_since TEXT,
    last_error TEXT,
    UNIQUE (url, variant)
);
CREATE TABLE IF NOT EXISTS retailers (
    domain TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    price_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    in_stock INTEGER,
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prices_listing_time ON prices(listing_id, checked_at);
"""


# Databases created before variants existed had UNIQUE(url): rebuild the table, keeping ids so
# price history stays attached. Foreign keys must be off or DROP would cascade into prices.
MIGRATE_ADD_VARIANT = """
PRAGMA foreign_keys = OFF;
BEGIN;
CREATE TABLE listings_new (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    retailer TEXT NOT NULL,
    url TEXT NOT NULL,
    variant TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (url, variant)
);
INSERT INTO listings_new (id, product_id, retailer, url, active)
    SELECT id, product_id, retailer, url, active FROM listings;
DROP TABLE listings;
ALTER TABLE listings_new RENAME TO listings;
COMMIT;
PRAGMA foreign_keys = ON;
"""


# Columns added after the first release; older databases get them through ALTER TABLE.
ADDED_COLUMNS = {
    "target_cents": "INTEGER",
    "fail_count": "INTEGER NOT NULL DEFAULT 0",
    "fail_since": "TEXT",
    "last_error": "TEXT",
}


def default_path() -> str:
    return os.environ.get("PRICE_STALKER_DB", "price_stalker.db")


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        self._migrate()
        with self._conn() as c:
            c.executescript(SCHEMA)
        self._sync_retailers()

    def _migrate(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
            if cols and "variant" not in cols:
                conn.executescript(MIGRATE_ADD_VARIANT)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
            for name, ddl in ADDED_COLUMNS.items():
                if cols and name not in cols:
                    conn.execute(f"ALTER TABLE listings ADD COLUMN {name} {ddl}")
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def custom_retailers(self) -> list[registry.Retailer]:
        with self._conn() as c:
            return [registry.Retailer(r["name"], r["domain"])
                    for r in c.execute("SELECT name, domain FROM retailers ORDER BY name")]

    def _sync_retailers(self) -> None:
        registry.set_custom(self.custom_retailers())

    def add_retailer(self, name: str, domain: str) -> bool:
        """False if the domain is already registered (built-in or custom)."""
        if domain in registry.BUILTIN_DOMAINS:
            return False
        with self._conn() as c:
            added = c.execute("INSERT OR IGNORE INTO retailers(domain, name) VALUES (?,?)", (domain, name)).rowcount
        self._sync_retailers()
        return bool(added)

    def remove_retailer(self, domain: str) -> bool:
        with self._conn() as c:
            removed = c.execute("DELETE FROM retailers WHERE domain=?", (domain,)).rowcount
        self._sync_retailers()
        return bool(removed)

    def add_listing(self, product_name: str, retailer: str, url: str, variant: str = "") -> int:
        """Idempotent per (url, variant): returns the listing id, creating product/listing as needed."""
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO products(name) VALUES (?)", (product_name,))
            pid = c.execute("SELECT id FROM products WHERE name=?", (product_name,)).fetchone()["id"]
            c.execute(
                "INSERT OR IGNORE INTO listings(product_id, retailer, url, variant) VALUES (?,?,?,?)",
                (pid, retailer, url, variant),
            )
            return c.execute("SELECT id FROM listings WHERE url=? AND variant=?", (url, variant)).fetchone()["id"]

    def active_listings(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT l.id, l.url, l.retailer, l.variant, l.target_cents, p.name AS product FROM listings l "
                "JOIN products p ON p.id=l.product_id WHERE l.active=1 ORDER BY l.id"
            ).fetchall()

    def record_price(self, listing_id: int, price_cents: int, currency: str = "USD",
                     in_stock: bool | None = None, when: datetime | None = None) -> None:
        ts = (when or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute(
                "INSERT INTO prices(listing_id, price_cents, currency, in_stock, checked_at) VALUES (?,?,?,?,?)",
                (listing_id, price_cents, currency, None if in_stock is None else int(in_stock), ts),
            )
            c.execute("UPDATE listings SET fail_count=0, fail_since=NULL, last_error=NULL WHERE id=?", (listing_id,))

    def last_price(self, listing_id: int) -> int | None:
        with self._conn() as c:
            row = c.execute("SELECT price_cents FROM prices WHERE listing_id=? ORDER BY checked_at DESC, id DESC LIMIT 1",
                            (listing_id,)).fetchone()
            return row["price_cents"] if row else None

    def record_failure(self, listing_id: int, error: str) -> int:
        """Count one more failed check in a row; returns the new streak length."""
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute("UPDATE listings SET fail_count=fail_count+1, fail_since=COALESCE(fail_since, ?), last_error=? "
                      "WHERE id=?", (ts, error, listing_id))
            return c.execute("SELECT fail_count FROM listings WHERE id=?", (listing_id,)).fetchone()["fail_count"]

    def set_target(self, ids: list[int], cents: int | None) -> int:
        if not ids:
            return 0
        with self._conn() as c:
            return c.execute(f"UPDATE listings SET target_cents=? WHERE id IN ({','.join('?' * len(ids))})",
                             (cents, *ids)).rowcount

    def history(self, product: str | None = None) -> list[sqlite3.Row]:
        q = (
            "SELECT p.name AS product, l.retailer, l.variant, l.url, pr.price_cents, pr.currency, "
            "pr.in_stock, pr.checked_at FROM prices pr "
            "JOIN listings l ON l.id=pr.listing_id JOIN products p ON p.id=l.product_id"
        )
        args: tuple = ()
        if product:
            q, args = q + " WHERE p.name=?", (product,)
        with self._conn() as c:
            return c.execute(q + " ORDER BY pr.checked_at", args).fetchall()

    def overview(self) -> list[sqlite3.Row]:
        """One row per listing with its latest recorded price (NULL if never checked)."""
        with self._conn() as c:
            return c.execute(
                "SELECT l.id AS id, p.name AS product, l.retailer, l.variant, l.url, l.active, l.target_cents, "
                "l.fail_count, l.fail_since, l.last_error, pr.price_cents, pr.currency, pr.checked_at, "
                "(SELECT price_cents FROM prices WHERE listing_id=l.id ORDER BY checked_at DESC, id DESC LIMIT 1 OFFSET 1) AS prev_cents, "
                "(SELECT MIN(price_cents) FROM prices WHERE listing_id=l.id) AS min_cents "
                "FROM listings l JOIN products p ON p.id=l.product_id "
                "LEFT JOIN prices pr ON pr.id = (SELECT id FROM prices WHERE listing_id=l.id "
                "ORDER BY checked_at DESC, id DESC LIMIT 1) "
                "ORDER BY p.name, l.retailer, l.id"
            ).fetchall()

    def set_active(self, ids: list[int], active: bool) -> int:
        """Pause/resume listings; history is kept. Returns how many rows changed."""
        if not ids:
            return 0
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE listings SET active=? WHERE id IN ({','.join('?' * len(ids))})", (int(active), *ids))
            return cur.rowcount

    def delete_listings(self, ids: list[int]) -> int:
        """Delete listings and (by cascade) their price history; drops products left with no listing."""
        if not ids:
            return 0
        with self._conn() as c:
            cur = c.execute(f"DELETE FROM listings WHERE id IN ({','.join('?' * len(ids))})", ids)
            c.execute("DELETE FROM products WHERE id NOT IN (SELECT DISTINCT product_id FROM listings)")
            return cur.rowcount

    def products(self) -> list[str]:
        with self._conn() as c:
            return [r["name"] for r in c.execute("SELECT name FROM products ORDER BY name")]
