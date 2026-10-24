"""Private, unmetered-license (not unlimited delivery capacity) NFT alert service.

One SQLite database holds hashed one-time sales links, Telegram chat IDs needed
for delivery, and one shared event log with per-subscriber cursors. A single
alert is stored once, independently of the number of customers.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Any

ADMIN_ID = 8714018880


class CommercialStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=20)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=20000")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1,
                cursor INTEGER NOT NULL DEFAULT 0,
                retry_after INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                joined_at INTEGER NOT NULL,
                expires_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_users_pending
                ON users(active, cursor, retry_after);
            CREATE TABLE IF NOT EXISTS invites (
                code_hash TEXT PRIMARY KEY,
                issued_at INTEGER NOT NULL,
                redeemed_by INTEGER,
                redeemed_at INTEGER,
                duration_seconds INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_invites_available
                ON invites(redeemed_at);
            CREATE TABLE IF NOT EXISTS alerts (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        # Existing Bothost databases were created before time-limited plans.
        # ALTER instead of replacing tables, so lifetime buyers keep access.
        for table, column in (("users", "expires_at"),
                              ("invites", "duration_seconds")):
            columns = {
                row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in columns:
                with self.conn:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} INTEGER"
                    )
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS star_orders (
                payload TEXT PRIMARY KEY,
                buyer_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                amount INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS star_payments (
                charge_id TEXT PRIMARY KEY,
                invoice_payload TEXT NOT NULL,
                buyer_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                amount INTEGER NOT NULL,
                paid_at INTEGER NOT NULL
            );
        """)
        self._add_admin()

    def _max_seq(self) -> int:
        return int(self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM alerts"
        ).fetchone()[0])

    def _add_admin(self) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO users(chat_id, cursor, joined_at) VALUES(?,?,?)",
                (ADMIN_ID, 0, int(time.time())),
            )
            self.conn.execute(
                "UPDATE users SET active=1, retry_after=0, expires_at=NULL WHERE chat_id=?",
                (ADMIN_ID,),
            )

    def create_links(self, count: int, duration_days: int | None = None) -> list[str]:
        """Create one-use lifetime or 24-hour access links.

        Code inventory has no expiration, while a 1-day license starts only
        when the buyer redeems a code. Existing lifetime links stay lifetime.
        """
        if duration_days not in (None, 1):
            raise ValueError("Only lifetime and 1-day access are supported.")
        if not (1 <= count <= 50):
            raise ValueError("Use 1-50 links per command; repeat with no total limit.")
        now = int(time.time())
        created = []
        with self.conn:
            for _ in range(count):
                code = secrets.token_urlsafe(24)  # legal /start payload
                digest = hashlib.sha256(code.encode("ascii")).hexdigest()
                self.conn.execute(
                    "INSERT INTO invites(code_hash, issued_at, duration_seconds) "
                    "VALUES(?,?,?)",
                    (digest, now, 86400 if duration_days == 1 else None),
                )
                created.append(code)
        return created

    def license_info(self, chat_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT active, expires_at FROM users WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        if row is None:
            return None
        active, expires = row
        now = int(time.time())
        return {
            "active": bool(active) and (expires is None or expires > now),
            "expired": expires is not None and expires <= now,
            "expires_at": int(expires) if expires is not None else None,
            "plan": "1d" if expires is not None else "lifetime",
        }

    def is_subscribed(self, chat_id: int) -> bool:
        info = self.license_info(chat_id)
        return bool(info and info["active"])

    def has_license(self, chat_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone() is not None

    def redeem(self, code: str, chat_id: int) -> str:
        """Atomically redeem a sale code, preserving all lifetime licenses.

        Outcomes: activated, upgraded, extended, existing, invalid.
        Codes for already-lifetime users are NOT burned. A 1-day renewal
        extends from current expiry, or from now if the plan has expired.
        """
        if chat_id <= 0 or chat_id == ADMIN_ID:
            return "invalid"
        if not isinstance(code, str) or not (10 <= len(code) <= 64):
            return "invalid"
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        now = int(time.time())
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.conn.execute(
                "SELECT expires_at FROM users WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if existing is not None and existing[0] is None:
                # Permanent license already owned, never consume a sale link.
                self.conn.execute(
                    "UPDATE users SET active=1,retry_after=0,failures=0 "
                    "WHERE chat_id=?", (chat_id,),
                )
                self.conn.commit()
                return "existing"
            row = self.conn.execute(
                "SELECT duration_seconds FROM invites "
                "WHERE code_hash=? AND redeemed_at IS NULL", (digest,),
            ).fetchone()
            if row is None:
                self.conn.rollback()
                return "invalid"
            duration = row[0]
            changed = self.conn.execute(
                "UPDATE invites SET redeemed_by=?, redeemed_at=? "
                "WHERE code_hash=? AND redeemed_at IS NULL",
                (chat_id, now, digest),
            ).rowcount
            if changed != 1:
                self.conn.rollback()
                return "invalid"

            if existing is None:
                expiry = now + int(duration) if duration is not None else None
                self.conn.execute(
                    "INSERT INTO users(chat_id,active,cursor,joined_at,expires_at) "
                    "VALUES(?,1,?,?,?)",
                    (chat_id, self._max_seq(), now, expiry),
                )
                result = "activated"
            else:
                previous_expiry = int(existing[0])
                expiry = (max(previous_expiry, now) + int(duration)
                          if duration is not None else None)
                # Expired users must NOT receive old NFT backlog after renewal.
                if previous_expiry <= now:
                    self.conn.execute(
                        "UPDATE users SET cursor=? WHERE chat_id=?",
                        (self._max_seq(), chat_id),
                    )
                self.conn.execute(
                    "UPDATE users SET active=1,expires_at=?,retry_after=0,"
                    "failures=0 WHERE chat_id=?", (expiry, chat_id),
                )
                result = "extended" if duration is not None else "upgraded"
            self.conn.commit()
            return result
        except BaseException:
            self.conn.rollback()
            raise

    def reactivate(self, chat_id: int) -> bool:
        info = self.license_info(chat_id)
        if info is None or info["expired"]:
            return False
        with self.conn:
            self.conn.execute(
                "UPDATE users SET active=1, retry_after=0, failures=0 "
                "WHERE chat_id=?", (chat_id,),
            )
        return True

    def create_star_order(self, buyer_id: int, plan: str,
                          amount: int) -> str:
        """Issue an order token for exactly one buyer and a fixed XTR amount."""
        if buyer_id <= 0 or plan not in {"1d", "lifetime"} or amount <= 0:
            raise ValueError("Invalid Telegram Stars order")
        token = "star_" + secrets.token_urlsafe(24)
        with self.conn:
            self.conn.execute(
                "INSERT INTO star_orders(payload,buyer_id,plan,amount,created_at) "
                "VALUES(?,?,?,?,?)",
                (token, buyer_id, plan, amount, int(time.time())),
            )
        return token

    def verify_star_order(self, payload: str, buyer_id: int,
                          amount: int) -> bool:
        """Only accept exact XTR price/user on a recent, unpaid invoice."""
        if buyer_id <= 0 or amount <= 0 or not isinstance(payload, str):
            return False
        row = self.conn.execute(
            "SELECT buyer_id,amount,created_at,plan FROM star_orders "
            "WHERE payload=?", (payload,),
        ).fetchone()
        if row is None:
            return False
        if (int(row[0]) != buyer_id or int(row[1]) != amount
                or not (0 <= int(time.time()) - int(row[2]) <= 3600)):
            return False
        already_paid = self.conn.execute(
            "SELECT 1 FROM star_payments WHERE invoice_payload=?",
            (payload,),
        ).fetchone()
        return already_paid is None

    def fulfill_star_payment(self, charge_id: str, payload: str,
                             buyer_id: int, amount: int) -> str:
        """Idempotently apply only verified successful_payment updates.

        Currency XTR must be checked by the caller. A unique charge and
        invoice payload prevent duplicate grants on repeated Bot API updates.
        """
        if not charge_id or buyer_id <= 0:
            return "invalid"
        now = int(time.time())
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            already = self.conn.execute(
                "SELECT 1 FROM star_payments WHERE charge_id=? "
                "OR invoice_payload=? LIMIT 1",
                (charge_id, payload),
            ).fetchone()
            if already:
                self.conn.commit()
                return "already"
            row = self.conn.execute(
                "SELECT buyer_id,plan,amount,created_at FROM star_orders "
                "WHERE payload=?", (payload,),
            ).fetchone()
            # A completed invoice can arrive late from Telegram; unlike
            # pre-checkout, don't reject it just because the hour passed.
            if row is None or int(row[0]) != buyer_id or int(row[2]) != amount:
                self.conn.rollback()
                return "invalid"
            plan = row[1]
            current = self.conn.execute(
                "SELECT expires_at FROM users WHERE chat_id=?", (buyer_id,),
            ).fetchone()
            if current is None:
                expiry = now + 86400 if plan == "1d" else None
                self.conn.execute(
                    "INSERT INTO users(chat_id,active,cursor,joined_at,expires_at) "
                    "VALUES(?,1,?,?,?)",
                    (buyer_id, self._max_seq(), now, expiry),
                )
            elif current[0] is not None:
                before = int(current[0])
                new_expiry = (max(before, now) + 86400
                              if plan == "1d" else None)
                cursor = self._max_seq() if before <= now else None
                if cursor is not None:
                    self.conn.execute(
                        "UPDATE users SET cursor=? WHERE chat_id=?",
                        (cursor, buyer_id),
                    )
                self.conn.execute(
                    "UPDATE users SET active=1,expires_at=?,retry_after=0,"
                    "failures=0 WHERE chat_id=?",
                    (new_expiry, buyer_id),
                )
            else:
                # Never turn existing permanent access into a timed pass.
                self.conn.execute(
                    "UPDATE users SET active=1,retry_after=0,failures=0 "
                    "WHERE chat_id=?", (buyer_id,),
                )
            self.conn.execute(
                "INSERT INTO star_payments(charge_id,invoice_payload,buyer_id,"
                "plan,amount,paid_at) VALUES(?,?,?,?,?,?)",
                (charge_id, payload, buyer_id, plan, amount, now),
            )
            self.conn.commit()
            return "activated"
        except BaseException:
            self.conn.rollback()
            raise

    def deactivate(self, chat_id: int) -> None:
        if chat_id == ADMIN_ID:
            return
        with self.conn:
            self.conn.execute(
                "UPDATE users SET active=0 WHERE chat_id=?", (chat_id,)
            )

    def publish(self, item: dict[str, Any]) -> bool:
        """Append an alert once, avoiding per-customer JSON copies."""
        event_id = str(item.get("event_id") or "")
        if not event_id:
            raise ValueError("An alert must have an event_id")
        # Legacy target/delivery state is not part of new broadcast protocol.
        payload = {k: v for k, v in item.items()
                   if k not in ("targets", "delivered_to", "_target_id")}
        with self.conn:
            result = self.conn.execute(
                "INSERT OR IGNORE INTO alerts(event_id, payload, created_at) "
                "VALUES(?,?,?)",
                (event_id, json.dumps(payload, ensure_ascii=False), int(time.time())),
            )
        return result.rowcount > 0

    def next_delivery(self) -> tuple[int, int, dict[str, Any]] | None:
        """Fair-ish order with a per-customer rate-limit/backoff cursor."""
        latest = self._max_seq()
        if latest == 0:
            return None
        row = self.conn.execute(
            "SELECT chat_id, cursor FROM users "
            "WHERE active=1 AND cursor<? AND retry_after<=? "
            "AND (expires_at IS NULL OR expires_at>?) "
            "ORDER BY cursor ASC, chat_id ASC LIMIT 1",
            (latest, int(time.time()), int(time.time())),
        ).fetchone()
        if row is None:
            return None
        chat_id, cursor = row
        event = self.conn.execute(
            "SELECT seq, payload FROM alerts WHERE seq>? ORDER BY seq LIMIT 1",
            (cursor,),
        ).fetchone()
        if event is None:
            return None
        return int(chat_id), int(event[0]), json.loads(event[1])

    def ack(self, chat_id: int, seq: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE users SET cursor=MAX(cursor, ?), "
                "retry_after=0, failures=0 WHERE chat_id=?",
                (seq, chat_id),
            )

    def retry_later(self, chat_id: int) -> int:
        """Move a transiently failing subscriber aside, not the whole queue."""
        row = self.conn.execute(
            "SELECT failures FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
        fails = min(20, int(row[0]) + 1) if row else 1
        delay = min(300, 2 ** min(fails, 8))
        with self.conn:
            self.conn.execute(
                "UPDATE users SET failures=?, retry_after=? WHERE chat_id=?",
                (fails, int(time.time()) + delay, chat_id),
            )
        return delay

    def delivery_backlog(self) -> int:
        latest = self._max_seq()
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM users "
            "WHERE active=1 AND cursor<? "
            "AND (expires_at IS NULL OR expires_at>?)",
            (latest, int(time.time()))
        ).fetchone()[0])

    def stats(self) -> dict[str, int]:
        active = self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE active=1 "
            "AND (expires_at IS NULL OR expires_at>?)",
            (int(time.time()),),
        ).fetchone()[0]
        total = self.conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]
        free_links = self.conn.execute(
            "SELECT COUNT(*) FROM invites WHERE redeemed_at IS NULL"
        ).fetchone()[0]
        used_links = self.conn.execute(
            "SELECT COUNT(*) FROM invites WHERE redeemed_at IS NOT NULL"
        ).fetchone()[0]
        trial_active = self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE active=1 "
            "AND expires_at>?", (int(time.time()),),
        ).fetchone()[0]
        trial_expired = self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE expires_at IS NOT NULL "
            "AND expires_at<=?", (int(time.time()),),
        ).fetchone()[0]
        trial_codes = self.conn.execute(
            "SELECT COUNT(*) FROM invites WHERE redeemed_at IS NULL "
            "AND duration_seconds=86400"
        ).fetchone()[0]
        lifetime_codes = self.conn.execute(
            "SELECT COUNT(*) FROM invites WHERE redeemed_at IS NULL "
            "AND duration_seconds IS NULL"
        ).fetchone()[0]
        return {
            "active_users": int(active),
            "trial_active": int(trial_active),
            "trial_expired": int(trial_expired),
            "trial_links": int(trial_codes),
            "lifetime_links": int(lifetime_codes),
            "licensed_users": int(total),
            "unused_links": int(free_links),
            "redeemed_links": int(used_links),
            "pending_subscribers": self.delivery_backlog(),
            "alerts": self._max_seq(),
        }

    def migrate_legacy(self, state: Any) -> None:
        """One-time upgrade. Keep old customers; demote past owner to buyer.

        Pending notifications are moved durably before deleting legacy queue.
        No legacy owner/activation code is permitted to create more links.
        """
        old_owner = state.data.get("owner_id")
        customers = list(state.data.get("subscribers") or [])
        if old_owner and old_owner != ADMIN_ID:
            customers.insert(0, old_owner)
        pending = list(state.data.get("pending") or [])

        already_done = self.conn.execute(
            "SELECT value FROM metadata WHERE key='legacy_imported'"
        ).fetchone() is not None
        if not already_done:
            # Existing pending alerts are unsent; import before newly licensed
            # customers, so they can receive the same unsent messages.
            for item in pending:
                if item.get("kind") in {"new_nft", "rare_nft"}:
                    self.publish(item)
            with self.conn:
                for value in customers:
                    try:
                        chat_id = int(value)
                    except (TypeError, ValueError):
                        continue
                    if chat_id <= 0 or chat_id == ADMIN_ID:
                        continue
                    self.conn.execute(
                        "INSERT OR IGNORE INTO users(chat_id,cursor,joined_at) "
                        "VALUES(?,?,?)", (chat_id, 0, int(time.time())),
                    )
                self.conn.execute(
                    "INSERT OR REPLACE INTO metadata(key,value) VALUES('legacy_imported','1')"
                )
        else:
            # After migration, only clear legacy queue after SQLite confirms
            # that each event is present.
            for item in pending:
                if item.get("kind") in {"new_nft", "rare_nft"}:
                    self.publish(item)

        if pending:
            state.data["pending"] = []
        state.data["owner_id"] = ADMIN_ID
        # Retire all old activation/invitation routes. Never grant owner rights
        # from a legacy key that could be known to previous subscribers.
        state.data["activation_consumed"] = True
        state.data["invites"] = []
        state.data["subscribers"] = []
        state.save()
