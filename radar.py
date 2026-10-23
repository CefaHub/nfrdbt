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

    def update_gifts(self, gifts: Iterable[Gift], notify_price_changes: bool = False) -> list[dict[str, Any]]:
        first_scan = not self.data["initialized"]
        current = self.data["gifts"]
        new_events: list[dict[str, Any]] = []
        # The MTProto list may omit old sold-out gifts. Keep their last known state.
        for gift in gifts:
            was_known = gift.gift_id in current
            old_price = current.get(gift.gift_id, {}).get("upgrade_stars")
            if not first_scan and not self.data["paused"]:
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
            self.data["pending"] = (self.data["pending"] + new_events)[-100:]
        self.save()
        return new_events

    def record_rare_nft(self, event: dict[str, Any]) -> None:
        event = dict(event, event_id=uuid4().hex)
        self.data["history"] = (self.data["history"] + [event])[-50:]
        if not self.data["paused"]:
            self.data["pending"] = (self.data["pending"] + [event])[-100:]
        self.save()

    def set_collection_issued(self, slug: str, issued: int) -> None:
        self.data["collections"][slug] = issued
        self.save()

    def pop_delivered(self, event_id: str) -> None:
        self.data["pending"] = [x for x in self.data["pending"] if x["event_id"] != event_id]
        self.save()

    @property
    def owner_id(self) -> int | None:
        return self.data["owner_id"]

    def set_owner(self, user_id: int) -> None:
        if self.owner_id is not None:
            raise RuntimeError("Owner is already configured")
        self.data["owner_id"] = user_id
        self.save()

    def set_paused(self, paused: bool) -> None:
        self.data["paused"] = paused
        self.save()
