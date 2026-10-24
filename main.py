"""Telegram gift upgrade radar: MTProto watcher + private notification bot."""
from __future__ import annotations

import asyncio
import html
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import Button, TelegramClient, events, functions
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from radar import Gift, RadarState, now_utc
from mint_radar import extract_attributes, make_event
from candidate_selection import get_candidates
from gift_origin import original_participants, resolve_original_participants
from delivery import dispatch_next, fallback_text
from scan_pacing import remaining_pause
from recipient_filter import recipient_from_history, filter_pending
from see_live import HEALTH as LIVE_HEALTH, run_stream
from live_market import live_nft_notification

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("gift-radar")


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"❌ Заполни {name} в .env")
    return value


# Public credentials are for TESTING ONLY and may fail at Telegram login.
# Supply your own TELEGRAM_API_ID and TELEGRAM_API_HASH for reliable operation.
TEST_API_ID = "17349"
TEST_API_HASH = "344583e45741c457fe1862106095a5eb"
API_ID = int(os.getenv("TELEGRAM_API_ID") or os.getenv("API_ID") or TEST_API_ID)
API_HASH = os.getenv("TELEGRAM_API_HASH") or os.getenv("API_HASH") or TEST_API_HASH
BOT_TOKEN = required("BOT_TOKEN")
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
INTERVAL = max(15, int(os.getenv("CHECK_INTERVAL", "20")))
TRACK_RARE_NFTS = os.getenv("TRACK_RARE_NFTS", "true").lower() in {"true", "yes", "1"}
LIVE_ENABLED = os.getenv("LIVE_ENABLED", "true").lower() in {"true", "yes", "1"}
LIVE_API_TOKEN = os.getenv("SEE_TG_API_TOKEN", "").strip()
LIVE_NOTIFY_NEW = os.getenv("LIVE_NOTIFY_NEW", "true").lower() in {"true", "yes", "1"}
LIVE_MAX_NEW_PER_MINUTE = max(0, min(60, int(os.getenv("LIVE_MAX_NEW_PER_MINUTE", "15"))))
LIVE_COLLECTIONS = [x.strip() for x in os.getenv("LIVE_COLLECTIONS", "").split(",")
                    if x.strip() and x.strip().isalnum()]
# NFT_SCAN_INTERVAL now targets one complete sweep, not one batch.
NFT_SWEEP_SECONDS = max(60, int(
    os.getenv("NFT_SWEEP_SECONDS") or os.getenv("NFT_SCAN_INTERVAL", "60")
))
NFT_REQUEST_SPACING_SECONDS = max(
    0.35, float(os.getenv("NFT_REQUEST_SPACING_SECONDS", "0.40"))
)
COLLECTIONS_PER_CYCLE = max(1, int(os.getenv("COLLECTIONS_PER_CYCLE", "5")))
MAX_NEW_NFTS_PER_CYCLE = max(1, int(os.getenv("MAX_NEW_NFTS_PER_CYCLE", "25")))
GIFT_COLLECTIONS = [x.strip() for x in os.getenv("GIFT_COLLECTIONS", "").split(",") if x.strip()]
# Bothost preserves /app/data during redeploys.
DATA_DIR = Path(os.getenv("DATA_DIR") or ("/app/data" if os.getenv("BOT_ID") else "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
state = RadarState(DATA_DIR / "state.json")
LIVE_COUNTERS = {
    "new_notified": 0,
    "new_skipped_limit": 0,
    "filtered_no_recipient": 0,
    "minute_bucket": None,
    "minute_count": 0,
    "last_new": None,
}
DELIVERY_WAKE = asyncio.Event()
DELIVERY_STATS = {
    "sent": 0,
    "plain": 0,
    "errors": 0,
    "retries": 0,
    "last_sent": None,
    "last_error": None,
    "last_error_at": None,
    "last_slug": None,
    "next_delay_sec": 0,
}


def message_for_gift(item: dict) -> str:
    """Small, consistent Telegram message. The first URL is the NFT preview."""
    if item.get("kind") == "delivery_test":
        return "Проверка доставки\nСообщение отправлено."

    title = html.escape(str(item.get("title") or "NFT"))
    number = html.escape(str(item.get("number") or "?"))
    slug = str(item["slug"])
    nft_link = f"https://t.me/nft/{slug}"
    recipient = recipient_from_history(item.get("original_participants"))
    if recipient:
        name = html.escape(str(recipient.get("label") or "Получатель"))
        url = str(recipient.get("url") or "")
        if url.startswith("https://t.me/"):
            name = f'<a href="{html.escape(url, quote=True)}">{name}</a>'
        elif recipient.get("id"):
            name += f" (ID {recipient['id']})"
        recipient_line = f"Кому подарили: {name}"
    else:
        # Should never enter the automatic queue, but tolerate historical data.
        recipient_line = "Получатель не указан"

    fields = [f"<b>{title} #{number}</b>", nft_link, recipient_line]
    for label, field in (("Модель", "model"), ("Фон", "backdrop"),
                         ("Символ", "pattern")):
        value = str(item.get(field) or "").strip()
        if value and value != "?":
            fields.append(f"{label}: {html.escape(value)}")
    return "\n\n".join((fields[0], fields[1])) + "\n\n" + "\n".join(fields[2:])


def nft_buttons(item: dict):
    if item.get("kind") not in {"new_nft", "rare_nft"}:
        return None
    buttons = [[Button.url("Открыть подарок", f"https://t.me/nft/{item['slug']}")]]
    recipient = recipient_from_history(item.get("original_participants"))
    if recipient:
        url = str(recipient.get("url") or "")
        if url.startswith("https://t.me/"):
            buttons.append([Button.url("Профиль получателя", url)])
    return buttons


async def send_alert(bot: TelegramClient, chat_id: int, text: str, item: dict | None = None) -> None:
    item = item or {}
    # Telegram's webpage preview requires both an actual URL in message
    # text and link_preview=True. A URL button alone produces no preview.
    show_gift_preview = item.get("kind") in {"new_nft", "rare_nft"}
    await bot.send_message(
        chat_id, text, parse_mode="html", link_preview=show_gift_preview,
        buttons=nft_buttons(item),
    )


async def deliver_pending(bot: TelegramClient) -> None:
    """Wake the independent notification worker; never wait for Telegram here."""
    DELIVERY_WAKE.set()


async def delivery_worker(bot: TelegramClient) -> None:
    """Send persisted events independently of NFT discovery.

    Event queue survives restarts. Scan tasks only enqueue and signal,
    while this worker handles Telegram FloodWait/transport errors.
    """
    backoff = 2.0
    while True:
        try:
            removed = filter_pending(state)
            if removed:
                log.info("Recipient-only filter discarded %d older queued alerts", removed)
            if not state.owner_id or state.data.get("paused") or not state.data["pending"]:
                DELIVERY_WAKE.clear()
                try:
                    await asyncio.wait_for(DELIVERY_WAKE.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                continue

            async def send_rich(item: dict) -> None:
                await send_alert(bot, state.owner_id, message_for_gift(item), item)

            async def send_plain(item: dict) -> None:
                await bot.send_message(
                    state.owner_id,
                    fallback_text(item),
                    link_preview=item.get("kind") in {"new_nft", "rare_nft"},
                )

            result, item = await asyncio.wait_for(
                dispatch_next(state, send_rich, send_plain), timeout=45,
            )
            if result == "idle":
                await asyncio.sleep(0.5)
                continue

            DELIVERY_STATS["sent"] += 1
            DELIVERY_STATS["plain"] += int(result == "plain")
            DELIVERY_STATS["last_sent"] = now_utc()
            DELIVERY_STATS["last_slug"] = (item or {}).get("slug")
            DELIVERY_STATS["last_error"] = None
            DELIVERY_STATS["next_delay_sec"] = 0
            log.info(
                "DELIVERY OK (%s) nft=%s pending=%d total_sent=%d",
                result, DELIVERY_STATS["last_slug"] or (item or {}).get("kind"),
                len(state.data["pending"]), DELIVERY_STATS["sent"],
            )
            backoff = 2.0
            # A gentle send rate avoids Telegram bot flood protection.
            await asyncio.sleep(1.5)

        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            delay = max(3, int(exc.seconds) + 3)
            DELIVERY_STATS["last_error"] = f"FloodWait {exc.seconds} sec"
            DELIVERY_STATS["last_error_at"] = now_utc()
            DELIVERY_STATS["retries"] += 1
            DELIVERY_STATS["next_delay_sec"] = delay
            log.warning("DELIVERY FloodWait=%ss; saved %d pending for retry",
                        exc.seconds, len(state.data["pending"]))
            await asyncio.sleep(delay)
        except Exception as exc:
            DELIVERY_STATS["errors"] += 1
            DELIVERY_STATS["retries"] += 1
            DELIVERY_STATS["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            DELIVERY_STATS["last_error_at"] = now_utc()
            DELIVERY_STATS["next_delay_sec"] = int(backoff)
            log.exception(
                "DELIVERY FAILED, will retry in %.1fs; queued=%d",
                backoff, len(state.data["pending"]),
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120.0)


# These counters are intentionally visible via /status and /diagnose.
NFT_HEALTH = {
    "candidates": 0, "checked": 0, "discovered": 0, "errors": 0,
    "last_success": None, "last_error": None, "last_found": None,
    "last_catalog": None, "backlog": 0, "unresolved": 0, "last_scan_cycle": None,
    "total_new_nfts": 0, "total_rare_nfts": 0, "notified_nfts": 0,
    "last_nft": None,
    "last_scan_finished": None,
    "last_sweep_finished": None,
    "sweeps_completed": 0,
    "sweep_progress": 0,
    "last_batch_duration": None,
    "last_batch_successful": 0,
    "filtered_no_recipient": 0,
}
# Avoid retrying an invalid slug every minute. This cache is process-local.
UNRESOLVED_UNTIL: dict[str, float] = {}


def nft_candidates() -> list[tuple[str, int | None]]:
    """Only include *upgradable* collectibles, with resolvable collection names."""
    candidates, unresolved = get_candidates(
        state.data["gifts"], GIFT_COLLECTIONS,
        state.data.setdefault("resolved_slugs", {}),
    )
    NFT_HEALTH["unresolved"] = unresolved
    return candidates


def slug_prefix(value: str) -> str:
    return value.rsplit("-", 1)[0] if value.rsplit("-", 1)[-1].isdigit() else ""


async def get_head_for_collection(
    user: TelegramClient, slug: str, base_id: int | None
):
    """Fetch a valid issued counter; recover bad/missing slugs via resale."""
    try:
        result = await user(functions.payments.GetUniqueStarGiftRequest(slug=f"{slug}-1"))
        first = result.gift
        if int(getattr(first, "availability_issued", -1)) < 0:
            raise ValueError("Telegram omitted availability_issued")
        actual = slug_prefix(first.slug)
        if not actual:
            raise ValueError(f"Invalid Telegram slug: {first.slug}")
        # A gift with the wrong guessed title should NEVER be counted as
        # another collection. Verify the base gift ID on the live response.
        if base_id is not None and int(first.gift_id) != base_id:
            raise ValueError(
                f"Collection mismatch: expected gift_id={base_id}, "
                f"got gift_id={first.gift_id} ({actual})"
            )
        if base_id is not None:
            mapping = state.data.setdefault("resolved_slugs", {})
            if mapping.get(str(base_id)) != actual:
                mapping[str(base_id)] = actual
                state.save()
        return actual, first
    except FloodWaitError:
        raise
    except Exception as exc:
        log.debug("GetUniqueStarGift(%s-1) failed: %s", slug, exc)

    # Official fallback only works if at least one gift is listed for resale.
    if base_id is None:
        raise ValueError(f"Cannot resolve manually configured collection {slug}")
    resale = await user(
        functions.payments.GetResaleStarGiftsRequest(
            gift_id=base_id, offset="", limit=1,
        )
    )
    found = getattr(resale, "gifts", [])
    if not found:
        raise ValueError(
            f"NFT slug cannot be resolved for gift_id={base_id}: "
            "no matching collectible and nothing currently on resale"
        )
    # payments.resaleStarGifts.gifts contains StarGiftUnique values.
    item = found[0]
    actual = slug_prefix(item.slug)
    if not actual or int(getattr(item, "gift_id", -1)) != base_id:
        raise ValueError(f"Resale mapping validation failed for gift_id={base_id}")
    mapping = state.data.setdefault("resolved_slugs", {})
    if mapping.get(str(base_id)) != actual:
        mapping[str(base_id)] = actual
        state.save()
    log.info("Resolved NFT collection from resale: gift_id=%s -> %s", base_id, actual)
    return actual, item


async def monitor_rare_nfts(user: TelegramClient, bot: TelegramClient) -> None:
    round_robin = 0
    # Full collection pass is paced at NFT_SWEEP_SECONDS (usually 60s).
    while True:
        tick_start = asyncio.get_running_loop().time()
        delay = 0.0
        candidate_count = 0
        batch_count = 0
        try:
            candidates = nft_candidates()
            candidate_count = len(candidates)
            NFT_HEALTH["candidates"] = len(candidates)
            NFT_HEALTH["backlog"] = 0
            NFT_HEALTH["last_scan_cycle"] = now_utc()
            if not candidates:
                delay = NFT_SWEEP_SECONDS
                NFT_HEALTH["last_error"] = "Нет коллекций из каталога. Проверяй логи payments.getStarGifts."
                log.warning("NFT: не обнаружено ни одной коллекции-кандидата")
            else:
                batch = [
                    candidates[(round_robin + n) % len(candidates)]
                    for n in range(min(COLLECTIONS_PER_CYCLE, len(candidates)))
                ]
                batch_count = len(batch)
                previous_position = round_robin
                round_robin = (round_robin + batch_count) % len(candidates)
                NFT_HEALTH["sweep_progress"] = round_robin
                sweep_wrapped = round_robin <= previous_position
                checked_this_round = 0
                for batch_index, (slug, base_id) in enumerate(batch):
                    # Temporary cooldown for invalid / not yet upgraded gifts.
                    if UNRESOLVED_UNTIL.get(slug, 0) > asyncio.get_running_loop().time():
                        continue
                    try:
                        resolved, gift = await get_head_for_collection(user, slug, base_id)
                        NFT_HEALTH["checked"] += 1
                        checked_this_round += 1
                        NFT_HEALTH["last_success"] = now_utc()
                        NFT_HEALTH["last_error"] = None
                        count = int(gift.availability_issued)
                        previous = state.data["collections"].get(resolved)
                        if previous is None:
                            # First valid scan establishes baseline: do not spam old NFTs.
                            state.set_collection_issued(resolved, count)
                            log.info("NFT baseline %s: %s already issued", resolved, count)
                            continue
                        if count < previous:
                            state.set_collection_issued(resolved, count)
                            log.warning("NFT counter decreased for %s: %d -> %d", resolved, previous, count)
                            continue
                        if count > previous:
                            log.info("NFT NEW: %s #%d–#%d (checking up to %d)", resolved, previous+1,
                                     count, MAX_NEW_NFTS_PER_CYCLE)
                        upper = min(count, previous + MAX_NEW_NFTS_PER_CYCLE)
                        NFT_HEALTH["backlog"] += max(0, count - upper)
                        for number in range(previous + 1, upper + 1):
                            # A failed request must not move the cursor forward.
                            details = await user(
                                functions.payments.GetUniqueStarGiftRequest(
                                    slug=f"{resolved}-{number}")
                            )
                            collectible = details.gift
                            # Crafting has its own minting process; the radar tracks upgrades.
                            if not getattr(collectible, "crafted", False):
                                history = original_participants(collectible, details)
                                if recipient_from_history(history):
                                    try:
                                        enriched = await asyncio.wait_for(
                                            resolve_original_participants(
                                                collectible, details, user
                                            ), timeout=2.5
                                        )
                                        if recipient_from_history(enriched):
                                            history = enriched
                                    except (asyncio.TimeoutError, Exception):
                                        pass
                                    attrs = extract_attributes(collectible)
                                    event = make_event(
                                        collectible, attrs, now_utc(), rare=False
                                    )
                                    event["original_participants"] = history
                                    NFT_HEALTH["total_new_nfts"] += 1
                                    NFT_HEALTH["last_nft"] = str(collectible.slug)
                                    if state.record_nft_once(event):
                                        NFT_HEALTH["notified_nfts"] += 1
                                        NFT_HEALTH["discovered"] += 1
                                        NFT_HEALTH["last_found"] = now_utc()
                                        log.info("NEW GIFT %s (recipient visible)", collectible.slug)
                                else:
                                    NFT_HEALTH["filtered_no_recipient"] += 1
                            state.set_collection_issued(resolved, number)
                            await deliver_pending(bot)
                    except FloodWaitError:
                        # Do not advance past a gift we couldn't check due to
                        # Telegram's rate limits. Try it after FloodWait ends.
                        round_robin = (previous_position + batch_index) % len(candidates)
                        NFT_HEALTH["sweep_progress"] = round_robin
                        raise
                    except Exception as exc:
                        NFT_HEALTH["errors"] += 1
                        NFT_HEALTH["last_error"] = f"{slug}: {type(exc).__name__}: {str(exc)[:160]}"
                        # Unresolvable candidates are retried every 10 minutes.
                        UNRESOLVED_UNTIL[slug] = asyncio.get_running_loop().time() + 600
                        log.warning("NFT scan failed for %s: %s", slug, exc)
                    # Spread calls across the sweep to reduce rate limits.
                    await asyncio.sleep(NFT_REQUEST_SPACING_SECONDS)
                NFT_HEALTH["last_batch_successful"] = checked_this_round
                if sweep_wrapped:
                    NFT_HEALTH["sweeps_completed"] += 1
                    NFT_HEALTH["last_sweep_finished"] = now_utc()
                log.info(
                    "NFT diagnostics: candidates=%d checked=%d total_checked=%d "
                    "progress=%d/%d sweeps=%d target=%ds "
                    "baselines=%d backlog=%d errors=%d last_error=%s",
                    len(candidates), checked_this_round, NFT_HEALTH["checked"],
                    NFT_HEALTH["sweep_progress"], len(candidates),
                    NFT_HEALTH["sweeps_completed"], NFT_SWEEP_SECONDS,
                    len(state.data["collections"]), NFT_HEALTH["backlog"],
                    NFT_HEALTH["errors"], NFT_HEALTH["last_error"],
                )
        except FloodWaitError as exc:
            delay = max(delay, exc.seconds + 3)
            NFT_HEALTH["last_error"] = f"FloodWait: {exc.seconds} seconds"
            log.warning("NFT rate limit, sleep %d sec", delay)
        except Exception as exc:
            NFT_HEALTH["errors"] += 1
            NFT_HEALTH["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            log.exception("NFT monitor error")
        elapsed = asyncio.get_running_loop().time() - tick_start
        NFT_HEALTH["last_scan_finished"] = now_utc()
        NFT_HEALTH["last_batch_duration"] = round(elapsed, 2)
        delay = remaining_pause(
            elapsed, candidate_count, batch_count, NFT_SWEEP_SECONDS, delay,
        )
        await asyncio.sleep(delay)


async def monitor(user: TelegramClient, bot: TelegramClient) -> None:
    while True:
        delay = INTERVAL
        try:
            result = await user(functions.payments.GetStarGiftsRequest(hash=0))
            if not hasattr(result, "gifts"):
                raise RuntimeError("Telegram не прислал список подарков")
            gifts = [Gift.from_telegram(g) for g in result.gifts]
            NFT_HEALTH["last_catalog"] = now_utc()
            new_events = state.update_gifts(gifts, notify=False)
            if new_events:
                log.info("Обнаружено новых событий: %d", len(new_events))
            else:
                log.info("Каталог: %d подарков; изменений доступности апгрейда нет (НЕ проверка выпуска NFT)", len(gifts))
            await deliver_pending(bot)
        except FloodWaitError as exc:
            delay = max(delay, exc.seconds + 3)
            log.warning("Лимит Telegram API; ждём %d сек", delay)
        except Exception:
            log.exception("Ошибка проверки подарков (повторим позже)")
        await asyncio.sleep(delay)



async def live_market_event(event: dict, user: TelegramClient | None = None) -> None:
    """Verify the historical recipient in Telegram before notifying."""
    if event.get("type") != "new" or user is None or not LIVE_NOTIFY_NEW:
        return
    if LIVE_COLLECTIONS and event["collection"] not in LIVE_COLLECTIONS:
        return
    minute = int(asyncio.get_running_loop().time() // 60)
    if LIVE_COUNTERS["minute_bucket"] != minute:
        LIVE_COUNTERS["minute_bucket"] = minute
        LIVE_COUNTERS["minute_count"] = 0
    if LIVE_COUNTERS["minute_count"] >= LIVE_MAX_NEW_PER_MINUTE:
        LIVE_COUNTERS["new_skipped_limit"] += 1
        return
    try:
        details = await user(
            functions.payments.GetUniqueStarGiftRequest(slug=event["slug"])
        )
        collectible = details.gift
        if (getattr(collectible, "crafted", False)
                or str(getattr(collectible, "slug", "")).lower() != event["slug"].lower()):
            return
        history = original_participants(collectible, details)
        if not recipient_from_history(history):
            LIVE_COUNTERS["filtered_no_recipient"] += 1
            return
        try:
            enriched = await asyncio.wait_for(
                resolve_original_participants(collectible, details, user),
                timeout=2.5,
            )
            if recipient_from_history(enriched):
                history = enriched
        except Exception:
            pass
        alert = live_nft_notification(event)
        alert["original_participants"] = history
        if state.record_nft_once(alert):
            LIVE_COUNTERS["minute_count"] += 1
            LIVE_COUNTERS["new_notified"] += 1
            LIVE_COUNTERS["last_new"] = now_utc()
            log.info("LIVE GIFT %s (recipient visible)", event["slug"])
            await deliver_pending(None)
    except FloodWaitError as exc:
        log.warning("LIVE recipient verification delayed by %ds", exc.seconds)
    except Exception as exc:
        log.debug("LIVE verification failed %s: %s",
                  event["slug"], type(exc).__name__)


def command_name(text: str) -> tuple[str, str]:
    head, _, tail = text.strip().partition(" ")
    return head.split("@", 1)[0].lower(), tail.strip()


async def run() -> None:
    # Hosted mode is noninteractive: authenticate using a pre-generated String Session.
    user_session = StringSession(SESSION_STRING) if SESSION_STRING else str(DATA_DIR / "user")
    user = TelegramClient(
        user_session, API_ID, API_HASH,
        flood_sleep_threshold=0,  # Expose FloodWait instead of silently pausing.
    )
    bot = TelegramClient(
        str(DATA_DIR / "bot"), API_ID, API_HASH,
        flood_sleep_threshold=0,  # Worker handles rate limits independently.
    )

    # A single-use secret's SHA-256 is in activation.py, NEVER the secret.
    # Used/owner state lives at /app/data/state.json and survives Bothost deploys.
    @bot.on(events.NewMessage(incoming=True))
    async def on_command(event):
        if not event.is_private:
            return
        cmd, argument = command_name(event.raw_text)
        if not cmd.startswith("/"):
            return

        if state.owner_id is None:
            if cmd == "/start" and state.consume_activation_key(
                argument, event.sender_id
            ):
                await event.respond("Радар подключён.\n/status — состояние")
                await deliver_pending(bot)
            else:
                await event.respond("Для подключения: /start КЛЮЧ")
            return

        if event.sender_id != state.owner_id:
            return

        if cmd in ("/start", "/help"):
            await event.respond(
                "<b>Gift Radar</b>\n"
                "Новые подарки с указанным получателем.\n\n"
                "/status — состояние\n"
                "/last — последние подарки\n"
                "/pause — отключить уведомления\n"
                "/resume — включить уведомления",
                parse_mode="html",
            )
        elif cmd == "/status":
            disabled = LIVE_HEALTH.get("disabled_kinds") or []
            live = LIVE_HEALTH["status"] if LIVE_ENABLED else "выключен"
            if "new" in disabled:
                live += " (события new отключены на стороне see.tg)"
            await event.respond(
                "<b>Gift Radar</b>\n"
                f"Уведомления: {'пауза' if state.data['paused'] else 'включены'}\n"
                "Фильтр: подарки с указанным получателем\n"
                f"NFT-сканер: {NFT_HEALTH['sweep_progress']}/{NFT_HEALTH['candidates']} коллекций\n"
                f"Последняя проверка: {NFT_HEALTH['last_scan_finished'] or 'нет'} UTC\n"
                f"see.tg: {html.escape(live)}\n"
                f"Из потока: {LIVE_COUNTERS['new_notified']}\n"
                f"Очередь: {len(state.data['pending'])}\n"
                f"Доставлено: {DELIVERY_STATS['sent']}\n"
                f"Ошибка отправки: {html.escape(DELIVERY_STATS['last_error'] or 'нет')}",
                parse_mode="html",
            )
        elif cmd == "/last":
            recent = [
                item for item in reversed(state.data.get("history") or [])
                if item.get("kind") in {"new_nft", "rare_nft"}
                and recipient_from_history(item.get("original_participants"))
            ][:10]
            if not recent:
                await event.respond("Подходящих подарков пока нет.")
                return
            rows = ["<b>Последние подарки</b>"]
            for item in recent:
                slug = html.escape(item["slug"], quote=True)
                title = html.escape(str(item.get("title") or "NFT"))
                recipient = recipient_from_history(item["original_participants"])
                name = html.escape(recipient.get("label") or "Получатель")
                rows.append(
                    f'<a href="https://t.me/nft/{slug}">'
                    f'{title} #{html.escape(str(item.get("number") or "?"))}</a>'
                    f" — {name}"
                )
            await event.respond("\n".join(rows), parse_mode="html",
                                link_preview=False)
        elif cmd == "/pause":
            state.set_paused(True)
            state.data["pending"] = []
            state.save()
            await event.respond("Уведомления выключены.")
        elif cmd == "/resume":
            state.set_paused(False)
            await event.respond("Уведомления включены.")
            await deliver_pending(bot)
        elif cmd == "/testdelivery":
            # Hidden service command: test actual delivery outbox.
            state.data["pending"].insert(0, {
                "kind": "delivery_test",
                "event_id": __import__("secrets").token_hex(16),
                "title": "Test",
                "created_at": now_utc(),
            })
            state.save()
            await deliver_pending(bot)
            await event.respond("Тест поставлен в очередь.")
        elif cmd == "/activate":
            if state.consume_activation_key(argument, event.sender_id):
                await event.respond("Ключ активирован.")
            elif state.data.get("activation_consumed"):
                await event.respond("Ключ уже использован.")
            else:
                await event.respond("Неверный ключ.")
        elif cmd == "/key":
            await event.respond("Ключ: " + state.activation_status())

    try:
        log.info("Подключаем пользовательский MTProto-аккаунт...")
        if SESSION_STRING:
            await user.connect()
            if not await user.is_user_authorized():
                raise RuntimeError("SESSION_STRING больше не действительна — создай новую локально")
        elif os.getenv("BOT_ID") or not __import__("sys").stdin.isatty():
            raise RuntimeError("На Bothost нужна SESSION_STRING. Запусти gen_session.py локально и добавь её в переменные окружения хостинга")
        else:
            await user.start()  # Interactive login is allowed only on a local terminal.
        log.info("Подключаем бота...")
        await bot.start(bot_token=BOT_TOKEN)
        me = await bot.get_me()
        log.info("Готово! Бот: @%s", me.username)
        if state.owner_id is None:
            log.info(
                "Бот не привязан. Используй /start с одноразовым ключом, "
                "выданным владельцу отдельно. Ключ в логи не выводится."
            )
        else:
            log.info(
                "Бот привязан к ID %s; одноразовый ключ: %s",
                state.owner_id, state.activation_status()
            )
        tasks = [asyncio.create_task(delivery_worker(bot)),
                 asyncio.create_task(monitor(user, bot))]
        if LIVE_ENABLED:
            tasks.append(asyncio.create_task(
                run_stream(lambda event: live_market_event(event, user),
                           LIVE_API_TOKEN, LIVE_COLLECTIONS or None)
            ))
        DELIVERY_WAKE.set()  # Flush persisted unsent NFT alerts on startup.
        if TRACK_RARE_NFTS:
            tasks.append(asyncio.create_task(monitor_rare_nfts(user, bot)))
        await asyncio.gather(*tasks)
    finally:
        await bot.disconnect()
        await user.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nРадар остановлен")
