"""Pure state and change detection for the Telegram gift upgrade radar."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import tempfile
from typing import Any, Iterable
from uuid import uuid4
import hashlib
import secrets
import time


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Gift:
    gift_id: str
    title: str
    upgrade_stars: int | None
    base_stars: int | None = None

    @classmethod
    def from_telegram(cls, item: Any) -> "Gift":
        gift_id = str(getattr(item, "id"))
        raw_title = getattr(item, "title", None)
        title = str(raw_title) if raw_title else f"Подарок #{gift_id}"
        price = getattr(item, "upgrade_stars", None)
        base = getattr(item, "stars", None)
        return cls(
            gift_id=gift_id,
            title=title,
            upgrade_stars=int(price) if price is not None else None,
            base_stars=int(base) if base is not None else None,
        )


class RadarState:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {
            "version": 1,
            "initialized": False,
            "owner_id": None,
            "subscribers": [],
            "invites": [],
            "activation_consumed": False,
            "paused": False,
            "last_check": None,
            "gifts": {},
            "collections": {},
            "pending": [],
            "history": [],
        }
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or loaded.get("version") != 1:
                raise ValueError("Неподдерживаемый формат state.json")
            self.data.update(loaded)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=self.path.parent, prefix=".state_", suffix=".tmp",
                encoding="utf-8", delete=False,
            ) as f:
                tmp_path = Path(f.name)
                os.chmod(tmp_path, 0o600)
                json.dump(self.data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        finally:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink()

    def update_gifts(self, gifts: Iterable[Gift], notify_price_changes: bool = False, notify: bool = True) -> list[dict[str, Any]]:
        first_scan = not self.data["initialized"]
        current = self.data["gifts"]
        new_events: list[dict[str, Any]] = []
        # The MTProto list may omit old sold-out gifts. Keep their last known state.
        for gift in gifts:
            was_known = gift.gift_id in current
            old_price = current.get(gift.gift_id, {}).get("upgrade_stars")
            if notify and not first_scan and not self.data["paused"]:
                kind = None
                if gift.upgrade_stars is not None and old_price is None:
                    kind = "upgrade_unlocked" if was_known else "new_upgradable_gift"
                elif (notify_price_changes and old_price is not None
                      and gift.upgrade_stars is not None
                      and old_price != gift.upgrade_stars):
                    kind = "upgrade_price_changed"
                if kind is not None:
                    event = {
                        "event_id": uuid4().hex,
                        "kind": kind,
                        "gift_id": gift.gift_id,
                        "title": gift.title,
                        "upgrade_stars": gift.upgrade_stars,
                        "previous_price": old_price,
                        "base_stars": gift.base_stars,
                        "created_at": now_utc(),
                    }
                    new_events.append(event)
            current[gift.gift_id] = {
                "title": gift.title,
                "upgrade_stars": gift.upgrade_stars,
                "base_stars": gift.base_stars,
            }
        self.data["initialized"] = True
        self.data["last_check"] = now_utc()
        if new_events:
            self.data["history"] = (self.data["history"] + new_events)[-50:]
            for event in new_events:
                if self.owner_id is not None:
                    event["targets"] = self.recipient_ids
            self.data["pending"] = self.data["pending"] + new_events
        self.save()
        return new_events

    def record_rare_nft(self, event: dict[str, Any]) -> None:
        event = dict(event, event_id=uuid4().hex)
        if self.owner_id is not None:
            event["targets"] = self.recipient_ids
        self.data["history"] = (self.data["history"] + [event])[-50:]
        # Pause only stops sending; keep every discovered NFT on disk
        # so /resume or /pushall can still deliver it later.
        self.data["pending"] = self.data["pending"] + [event]
        self.save()

    def record_nft_once(self, event: dict[str, Any]) -> bool:
        """Deduplicate incoming see.tg and MTProto NFT mints persistently.

        Both the free WebSocket and the existing MTProto poller may see the
        same collectible. Keep the first alert, not two messages per NFT.
        """
        slug = str(event.get("slug") or "")
        if not slug:
            return False
        seen = self.data.setdefault("seen_nft_slugs", [])
        # Include existing pre-v2 notifications when upgrading the bot.
        prior = set(seen)
        for old in self.data.get("pending", []) + self.data.get("history", []):
            if old.get("kind") in {"new_nft", "rare_nft"} and old.get("slug"):
                prior.add(old["slug"])
        if slug in prior:
            return False
        seen.append(slug)
        del seen[:-6000]
        self.record_rare_nft(event)
        return True

    def set_collection_issued(self, slug: str, issued: int) -> None:
        self.data["collections"][slug] = issued
        self.save()

    def pop_delivered(self, event_id: str) -> None:
        self.data["pending"] = [x for x in self.data["pending"] if x["event_id"] != event_id]
        self.save()

    @property
    def owner_id(self) -> int | None:
        return self.data["owner_id"]


    @property
    def recipient_ids(self) -> list[int]:
        """The primary owner plus explicitly invited subscribers."""
        if self.owner_id is None:
            return []
        recipients = [self.owner_id]
        for value in self.data.get("subscribers", []):
            try:
                user_id = int(value)
            except (ValueError, TypeError):
                continue
            if user_id > 0 and user_id not in recipients:
                recipients.append(user_id)
        return recipients

    def is_subscriber(self, user_id: int) -> bool:
        return user_id != self.owner_id and user_id in self.recipient_ids

    def create_invite(self, ttl_seconds: int = 86400) -> str:
        """Generate a one-time invitation; persist only its SHA-256 digest."""
        if self.owner_id is None:
            raise RuntimeError("Pair the bot owner before inviting anyone.")
        now = int(time.time())
        invites = [v for v in self.data.get("invites", [])
                   if isinstance(v, dict) and int(v.get("expires", 0)) > now]
        if len(invites) >= 5:
            # Limit outstanding secrets to prevent unused invite buildup.
            invites = invites[-4:]
        token = secrets.token_urlsafe(16)
        invites.append({
            "hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "expires": now + max(60, int(ttl_seconds)),
        })
        self.data["invites"] = invites
        self.save()
        return token

    def accept_invite(self, token: str, user_id: int) -> bool:
        """Accept invite once; never grant ownership or management rights."""
        if self.owner_id is None or user_id <= 0 or user_id == self.owner_id:
            return False
        if self.is_subscriber(user_id):
            return True
        if len(self.recipient_ids) >= 11 or not (10 <= len(token) <= 100):
            return False
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = int(time.time())
        invites = self.data.get("invites", [])
        for i, invite in enumerate(invites):
            if (isinstance(invite, dict) and
                    int(invite.get("expires", 0)) > now and
                    secrets.compare_digest(str(invite.get("hash") or ""), digest)):
                del invites[i]
                self.data.setdefault("subscribers", []).append(user_id)
                self.save()
                return True
        return False

    def remove_subscriber(self, user_id: int) -> bool:
        people = self.data.get("subscribers", [])
        if user_id not in people:
            return False
        people.remove(user_id)
        # An unsubscribed user must not get deferred messages.
        for item in self.data["pending"]:
            if "targets" in item:
                item["targets"] = [x for x in item["targets"] if x != user_id]
        self.save()
        return True

    def mark_delivered_to(self, event_id: str, user_id: int) -> None:
        """ACK one recipient, not the whole NFT, so others can retry."""
        for item in self.data["pending"]:
            if item.get("event_id") != event_id:
                continue
            delivered = item.setdefault("delivered_to", [])
            if user_id not in delivered:
                delivered.append(user_id)
            targets = item.get("targets", [self.owner_id])
            if not any(x in self.recipient_ids and x not in delivered
                       for x in targets):
                self.pop_delivered(event_id)
            else:
                self.save()
            return

    def set_owner(self, user_id: int) -> None:
        if self.owner_id is not None:
            raise RuntimeError("Owner is already configured")
        self.data["owner_id"] = user_id
        self.save()

    def consume_activation_key(self, candidate: str, user_id: int) -> bool:
        """Accept the fixed one-use pairing key exactly once, persisted in data/.

        Existing authenticated owners can burn the key without changing owner.
        An outsider can never replace an owner even with the key.
        """
        from activation import valid_activation_key

        if self.data.get("activation_consumed", False):
            return False
        if not valid_activation_key(candidate):
            return False
        if self.owner_id is not None and user_id != self.owner_id:
            return False
        if self.owner_id is None:
            self.data["owner_id"] = user_id
        self.data["activation_consumed"] = True
        self.save()
        return True

    def activation_status(self) -> str:
        if self.data.get("activation_consumed", False):
            return "использован"
        return "не использован"

    def set_paused(self, paused: bool) -> None:
        self.data["paused"] = paused
        self.save()
