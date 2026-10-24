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
                joined_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_users_pending
                ON users(active, cursor, retry_after);
            CREATE TABLE IF NOT EXISTS invites (
                code_hash TEXT PRIMARY KEY,
                issued_at INTEGER NOT NULL,
                redeemed_by INTEGER,
                redeemed_at INTEGER
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
                "UPDATE users SET active=1, retry_after=0 WHERE chat_id=?",
                (ADMIN_ID,),
            )

    def create_links(self, count: int) -> list[str]:
        """No overall count/expiry cap; each returned secret activates one user."""
        if not (1 <= count <= 50):
            raise ValueError("Use 1-50 links per command; repeat with no total limit.")
        now = int(time.time())
        created = []
        with self.conn:
            for _ in range(count):
                code = secrets.token_urlsafe(24)  # legal /start payload
                digest = hashlib.sha256(code.encode("ascii")).hexdigest()
                self.conn.execute(
                    "INSERT INTO invites(code_hash, issued_at) VALUES(?,?)",
                    (digest, now),
                )
                created.append(code)
        return created

    def is_subscribed(self, chat_id: int) -> bool:
        row = self.conn.execute(
            "SELECT active FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return bool(row and row[0])

    def has_license(self, chat_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone() is not None

    def redeem(self, code: str, chat_id: int) -> str:
        """Atomic single-use redemption, safe if requests race.

        A customer who was already licensed does not consume another code.
        Returns 'activated', 'existing', or 'invalid'.
        """
        if chat_id <= 0 or chat_id == ADMIN_ID:
            return "invalid"
        if self.has_license(chat_id):
            self.reactivate(chat_id)
            return "existing"
        if not isinstance(code, str) or not (10 <= len(code) <= 64):
            return "invalid"
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        now = int(time.time())
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT redeemed_at FROM invites WHERE code_hash=?", (digest,)
            ).fetchone()
            if row is None or row[0] is not None:
                self.conn.rollback()
                return "invalid"
            changed = self.conn.execute(
                "UPDATE invites SET redeemed_by=?, redeemed_at=? "
                "WHERE code_hash=? AND redeemed_at IS NULL",
                (chat_id, now, digest),
            ).rowcount
            if changed != 1:
                self.conn.rollback()
                return "invalid"
            self.conn.execute(
                "INSERT INTO users(chat_id, active, cursor, joined_at) "
                "VALUES(?,1,?,?)",
                (chat_id, self._max_seq(), now),
            )
            self.conn.commit()
            return "activated"
        except BaseException:
            self.conn.rollback()
            raise

    def reactivate(self, chat_id: int) -> bool:
        if not self.has_license(chat_id):
            return False
        with self.conn:
            self.conn.execute(
                "UPDATE users SET active=1, retry_after=0, failures=0 "
                "WHERE chat_id=?",
                (chat_id,),
            )
        return True

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
            "ORDER BY cursor ASC, chat_id ASC LIMIT 1",
            (latest, int(time.time())),
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
            "WHERE active=1 AND cursor<?", (latest,)
        ).fetchone()[0])

    def stats(self) -> dict[str, int]:
        active = self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE active=1"
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
        return {
            "active_users": int(active),
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
