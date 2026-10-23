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
EVENTS = ["new", "listing", "price", "sale", "transfer", "opened"]
HEALTH: dict[str, Any] = {
    "status": "не запускался", "connected_at": None,
    "last_event": None, "last_event_type": None, "events_seen": 0,
    "events_valid": 0, "reconnects": 0, "last_error": None,
    "authenticated": False, "last_deal": None,
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
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "error":
            code = str(msg.get("code") or "unknown")
            HEALTH["last_error"] = "see.tg WebSocket: " + code[:80]
            LOG.warning("see.tg control error: %s", code[:80])
            continue
        if msg.get("type") == "kind_enabled":
            await ws.send(json.dumps(subscribe_packet(collections)))
            continue
        if msg.get("type") != "event":
            continue
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
