"""Resilient, free see.tg gift WebSocket consumer.

Guest WSS stream is free and works without a token.
Optional SEE_TG_API_TOKEN authenticates the SAME free WebSocket; it never
calls chargeable REST endpoints, /who, marketplace purchase or wallet API.
"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Any

import websockets
from live_market import normalize_live_event

URL = "wss://live.see.tg/v1/ws"
EVENTS = ["new", "stats"]
HEALTH: dict[str, Any] = {
    "status": "не запускался", "connected_at": None,
    "last_event": None, "last_event_type": None, "events_seen": 0,
    "events_valid": 0, "reconnects": 0, "last_error": None,
    "authenticated": False, "last_deal": None,
    "disabled_kinds": [], "subscribed_kinds": [],
    "last_notice": None, "event_counts": {},
    "last_kind_enabled": None, "last_kind_disabled": None,
    "last_stats": None, "stats_seen": 0,
}

LOG = logging.getLogger("gift-radar.live")


def subscribe_packet(collections: list[str] | None = None) -> dict:
    packet: dict = {"type": "subscribe", "events": EVENTS[:]}
    if collections:
        cleaned = []
        for name in collections[:500]:
            if (isinstance(name, str) and name.isalnum() and
                    name not in cleaned):
                cleaned.append(name)
        if cleaned:
            packet["collections"] = cleaned
    return packet


def _kinds(value: Any) -> list[str]:
    """Return only known, printable server-supplied event kinds."""
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        item for item in value
        if isinstance(item, str) and len(item) <= 40
        and item.replace("_", "").isalnum()
    ))


def _update_disabled(add: list[str] | None = None,
                     remove: list[str] | None = None) -> None:
    active = set(HEALTH.get("disabled_kinds") or [])
    active.update(add or [])
    active.difference_update(remove or [])
    HEALTH["disabled_kinds"] = sorted(active)


async def consume_socket(ws: Any, on_event: Callable[[dict], Awaitable[None]],
                         token: str = "", collections: list[str] | None = None) -> None:
    """One connection. Reconnect is owned by run_stream."""
    if token:
        # Never put the token in URL, logs, state.json or Telegram messages.
        await ws.send(json.dumps({"type": "auth", "token": token}))
    await ws.send(json.dumps(subscribe_packet(collections)))
    HEALTH["status"] = "подключено"
    HEALTH["connected_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    HEALTH["authenticated"] = bool(token)
    HEALTH["last_error"] = None
    HEALTH["disabled_kinds"] = []
    HEALTH["subscribed_kinds"] = []
    HEALTH["last_notice"] = None
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(msg, dict):
            continue
        kind = msg.get("type")
        if kind == "hello":
            _update_disabled(add=_kinds(msg.get("disabled")))
            available = _kinds(msg.get("events"))
            if available:
                HEALTH["available_kinds"] = available
            continue
        if kind == "subscribed":
            active = _kinds(msg.get("events")) or _kinds(msg.get("subscribed"))
            if active:
                HEALTH["subscribed_kinds"] = active
            rejected = _kinds(msg.get("disabled"))
            if rejected:
                _update_disabled(add=rejected)
                HEALTH["last_notice"] = (
                    "see.tg временно отключил: " + ", ".join(rejected)
                )
            continue
        if kind == "kind_disabled":
            disabled = _kinds(msg.get("events"))
            _update_disabled(add=disabled)
            HEALTH["last_kind_disabled"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            HEALTH["last_notice"] = (
                "see.tg отключил события: " + ", ".join(disabled or ["неизвестный тип"])
            )
            LOG.info("%s", HEALTH["last_notice"])
            continue
        if kind == "error":
            code = str(msg.get("code") or "unknown")[:80]
            if code == "kind_disabled":
                disabled = _kinds(msg.get("events"))
                _update_disabled(add=disabled)
                HEALTH["last_kind_disabled"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                HEALTH["last_notice"] = (
                    "see.tg не подписал на отключённые типы: "
                    + ", ".join(disabled or ["неизвестный тип"])
                )
                # This is not a connection failure; other kinds still work.
                LOG.info("%s", HEALTH["last_notice"])
            else:
                HEALTH["last_error"] = "see.tg WebSocket: " + code
                LOG.warning("see.tg control error: %s", code)
            continue
        if kind == "kind_enabled":
            enabled = _kinds(msg.get("events"))
            _update_disabled(remove=enabled)
            HEALTH["last_kind_enabled"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            HEALTH["last_notice"] = "see.tg включил: " + ", ".join(enabled or ["неизвестный тип"])
            await ws.send(json.dumps(subscribe_packet(collections)))
            continue
        if kind == "stats" or (kind == "event" and msg.get("event") == "stats"):
            # Server heartbeat is independent of any NFT creation.
            HEALTH["last_stats"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            HEALTH["stats_seen"] += 1
            continue
        if kind != "event":
            continue
        event_kind = str(msg.get("event") or "")[:40]
        event_counts = HEALTH.setdefault("event_counts", {})
        event_counts[event_kind] = event_counts.get(event_kind, 0) + 1
        HEALTH["events_seen"] += 1
        HEALTH["last_event"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        HEALTH["last_event_type"] = str(msg.get("event"))[:30]
        normalized = normalize_live_event(msg)
        if normalized is None:
            continue
        HEALTH["events_valid"] += 1
        try:
            await on_event(normalized)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # One malformed event must not stop the entire live feed.
            HEALTH["last_error"] = f"Handler {type(exc).__name__}"
            LOG.warning("Cannot process live gift %s: %s",
                        normalized.get("slug"), type(exc).__name__)


async def run_stream(on_event: Callable[[dict], Awaitable[None]],
                     token: str = "", collections: list[str] | None = None) -> None:
    delay = 2
    while True:
        try:
            HEALTH["status"] = "подключение"
            async with websockets.connect(
                URL, open_timeout=12, close_timeout=5, ping_interval=25,
                ping_timeout=20, max_size=500_000, max_queue=64,
            ) as ws:
                await consume_socket(ws, on_event, token, collections)
                delay = 2
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            HEALTH["status"] = "переподключение"
            HEALTH["reconnects"] += 1
            HEALTH["last_error"] = type(exc).__name__
            LOG.warning("see.tg WebSocket reconnect in %d sec: %s",
                        delay, type(exc).__name__)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 120)
