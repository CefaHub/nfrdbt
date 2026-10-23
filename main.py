"""Telegram gift upgrade radar: MTProto watcher + private notification bot."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import secrets
from pathlib import Path

from dotenv import load_dotenv
from telethon import Button, TelegramClient, events, functions
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from radar import Gift, RadarState, now_utc
from mint_radar import collection_slug, extract_attributes, is_rare_model, make_event
from candidate_selection import get_candidates
from owner_contact import owner_contact, resolve_owner_contact, offer_text
from deal_watch import valid_gift_slug, deal_snapshot, deal_opportunities, deal_details
from delivery import dispatch_next, fallback_text

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
NOTIFY_PRICE_CHANGES = os.getenv("NOTIFY_PRICE_CHANGES", "false").lower() in {"true", "yes", "1"}
TRACK_RARE_NFTS = os.getenv("TRACK_RARE_NFTS", "true").lower() in {"true", "yes", "1"}
NOTIFY_ALL_NFTS = os.getenv("NOTIFY_ALL_NFTS", "true").lower() in {"true", "yes", "1"}
NFT_SCAN_INTERVAL = max(5, int(os.getenv("NFT_SCAN_INTERVAL", "20")))
COLLECTIONS_PER_CYCLE = max(1, int(os.getenv("COLLECTIONS_PER_CYCLE", "5")))
MAX_NEW_NFTS_PER_CYCLE = max(1, int(os.getenv("MAX_NEW_NFTS_PER_CYCLE", "25")))
MAX_MODEL_RARITY_PCT = float(os.getenv("MAX_MODEL_RARITY_PCT", "1.0"))
DEAL_WATCH_INTERVAL = max(60, int(os.getenv("DEAL_WATCH_INTERVAL", "180")))
GIFT_COLLECTIONS = [x.strip() for x in os.getenv("GIFT_COLLECTIONS", "").split(",") if x.strip()]
# Bothost preserves /app/data during redeploys.
DATA_DIR = Path(os.getenv("DATA_DIR") or ("/app/data" if os.getenv("BOT_ID") else "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
state = RadarState(DATA_DIR / "state.json")
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
    title = html.escape(item["title"])
    if item["kind"] in {"rare_nft", "new_nft"}:
        link = f"https://t.me/nft/{item['slug']}"
        headline = (
            "💎 <b>ЗАСЕЧЁН РЕДКИЙ NFT!</b>"
            if item["kind"] == "rare_nft"
            else "🎁 <b>НОВЫЙ NFT ПОСЛЕ УЛУЧШЕНИЯ</b>"
        )
        owner = html.escape(item.get("owner_label") or "Не раскрыт")
        owner_url = item.get("owner_profile_url") or item.get("owner_url")
        if owner_url:
            owner_display = (
                f'<a href="{html.escape(owner_url, quote=True)}">{owner}</a>'
            )
        else:
            owner_display = owner
        offer_min = item.get("offer_min_stars")
        offer_line = (
            f"💰 Официальный оффер: от {offer_min} ⭐"
            if isinstance(offer_min, int) else
            "💰 Оффер: проверь доступность в Telegram"
        )
        has_public_username = bool(item.get("owner_url") and item.get("owner_type") == "user")
        has_id_mention = bool(item.get("owner_user_id") and not has_public_username)
        contact_note = (
            "✉️ Нажми «Открыть чат владельца» ниже."
            if has_public_username else
            "✉️ Доступна попытка открыть профиль через Telegram ID; ссылка может не работать из-за приватности."
            if has_id_mention else
            "🔐 Прямой контакт не раскрыт; проверь NFT и официальный оффер."
        )
        id_line = (
            f"🆔 Telegram ID: <code>{int(item['owner_user_id'])}</code>\n"
            if item.get("owner_user_id") else ""
        )
        return (
            f"{headline}\n\n"
            f"🎁 <b>{title} #{item['number']}</b>\n"
            f"🔥 Модель: <b>{html.escape(item['model'])}</b> "
            f"({html.escape(item['model_rarity'])})\n"
            f"🎨 Фон: {html.escape(item['backdrop'])} "
            f"({html.escape(item['backdrop_rarity'])})\n"
            f"✳️ Символ: {html.escape(item['pattern'])} "
            f"({html.escape(item['pattern_rarity'])})\n"
            f"👤 Владелец: {owner_display}\n"
            f"{id_line}"
            f"{offer_line}\n"
            f"{contact_note}\n"
            f"📝 Черновик: <code>/offer {html.escape(item['slug'])}</code>\n"
            f"🔎 Способы покупки: <code>/deal {html.escape(item['slug'])}</code>\n"
            f"🔔 Следить за продажей: <code>/watch {html.escape(item['slug'])}</code>\n"
            f"🕒 Обнаружен: {html.escape(item['created_at'])} UTC\n\n"
            "Проверь актуального владельца перед покупкой: подарок могли передать."
        )
    stars = item["upgrade_stars"]
    kind = item["kind"]
    if kind == "upgrade_unlocked":
        headline = "🚨 <b>ОТКРЫЛСЯ АПГРЕЙД!</b>"
    elif kind == "new_upgradable_gift":
        headline = "🎁 <b>Новый подарок с апгрейдом!</b>"
    else:
        headline = "📈 <b>Изменилась цена улучшения!</b>"
    msg = (
        f"{headline}\n\n"
        f"🎁 <b>{title}</b>\n"
        f"✨ Улучшение: <b>{stars} ⭐</b>\n"
        f"🆔 Gift ID: <code>{item['gift_id']}</code>"
    )
    if kind == "upgrade_price_changed":
        msg += f"\n⏮ Было: {item['previous_price']} ⭐"
    msg += "\n\nПроверь подарок в Telegram → Настройки → Подарки."
    return msg


def nft_buttons(item: dict):
    if item.get("kind") not in {"rare_nft", "new_nft"}:
        return None
    buttons = [
        [Button.url("🎁 Открыть NFT / оффер", f"https://t.me/nft/{item['slug']}")]
    ]
    # MTProto exposes a PeerUser ID even when the owner has no username.
    # tg://user?id is a *conditional* Bot API mention; privacy and client
    # user-cache restrictions may prevent it from opening a profile.
    if item.get("owner_type") == "user":
        if item.get("owner_url"):
            buttons.append([Button.url("✉️ Открыть чат владельца", item["owner_url"])])
        elif item.get("owner_profile_url"):
            buttons.append([Button.url("👤 Открыть профиль по ID", item["owner_profile_url"])])
    return buttons


async def send_alert(bot: TelegramClient, chat_id: int, text: str, item: dict | None = None) -> None:
    await bot.send_message(
        chat_id, text, parse_mode="html", link_preview=False,
        buttons=nft_buttons(item or {}),
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
                    link_preview=False,
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
}
# Avoid retrying an invalid slug every minute. This cache is process-local.
UNRESOLVED_UNTIL: dict[str, float] = {}


def all_nft_notifications() -> bool:
    """Persistent on/off mode controlled by the authorized bot owner."""
    return bool(state.data.get("notify_all_nfts", NOTIFY_ALL_NFTS))


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
            state.data.setdefault("resolved_slugs", {})[str(base_id)] = actual
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
    state.data.setdefault("resolved_slugs", {})[str(base_id)] = actual
    state.save()
    log.info("Resolved NFT collection from resale: gift_id=%s -> %s", base_id, actual)
    return actual, item


async def monitor_rare_nfts(user: TelegramClient, bot: TelegramClient) -> None:
    round_robin = 0
    # One loop is one batch of collections, not every available collectible.
    while True:
        delay = NFT_SCAN_INTERVAL
        try:
            candidates = nft_candidates()
            NFT_HEALTH["candidates"] = len(candidates)
            NFT_HEALTH["backlog"] = 0
            NFT_HEALTH["last_scan_cycle"] = now_utc()
            if not candidates:
                NFT_HEALTH["last_error"] = "Нет коллекций из каталога. Проверяй логи payments.getStarGifts."
                log.warning("NFT: не обнаружено ни одной коллекции-кандидата")
            else:
                batch = [
                    candidates[(round_robin + n) % len(candidates)]
                    for n in range(min(COLLECTIONS_PER_CYCLE, len(candidates)))
                ]
                round_robin = (round_robin + len(batch)) % len(candidates)
                checked_this_round = 0
                for slug, base_id in batch:
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
                                attrs = extract_attributes(collectible)
                                rare = is_rare_model(attrs, MAX_MODEL_RARITY_PCT)
                                event = make_event(collectible, attrs, now_utc(), rare=rare)
                                event.update(await resolve_owner_contact(collectible, details, user))
                                NFT_HEALTH["total_new_nfts"] += 1
                                NFT_HEALTH["last_nft"] = str(collectible.slug)
                                if rare:
                                    NFT_HEALTH["total_rare_nfts"] += 1
                                if rare or all_nft_notifications():
                                    state.record_rare_nft(event)
                                    NFT_HEALTH["notified_nfts"] += 1
                                    NFT_HEALTH["discovered"] += 1
                                    NFT_HEALTH["last_found"] = now_utc()
                                log.info(
                                    "NFT UPGRADE: %s | model=%s | rarity=%s | rare=%s | queued=%s",
                                    collectible.slug, event["model"], event["model_rarity"],
                                    rare, rare or all_nft_notifications(),
                                )
                            state.set_collection_issued(resolved, number)
                            await deliver_pending(bot)
                    except FloodWaitError:
                        raise
                    except Exception as exc:
                        NFT_HEALTH["errors"] += 1
                        NFT_HEALTH["last_error"] = f"{slug}: {type(exc).__name__}: {str(exc)[:160]}"
                        # Unresolvable candidates are retried every 10 minutes.
                        UNRESOLVED_UNTIL[slug] = asyncio.get_running_loop().time() + 600
                        log.warning("NFT scan failed for %s: %s", slug, exc)
                log.info(
                    "NFT diagnostics: candidates=%d checked=%d total_checked=%d "
                    "baselines=%d backlog=%d errors=%d last_error=%s",
                    len(candidates), checked_this_round, NFT_HEALTH["checked"],
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
            new_events = state.update_gifts(gifts, NOTIFY_PRICE_CHANGES)
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



async def fetch_deal_snapshot(user: TelegramClient, slug: str) -> dict:
    if not valid_gift_slug(slug):
        raise ValueError("Неправильный номер NFT")
    details = await user(functions.payments.GetUniqueStarGiftRequest(slug=slug))
    contact = await resolve_owner_contact(details.gift, details, user)
    return deal_snapshot(details.gift, contact)


def deal_watch_buttons(slug: str, snapshot: dict):
    buttons = [[Button.url("🎁 Открыть NFT / покупка", f"https://t.me/nft/{slug}")]]
    link = snapshot.get("owner_url") or snapshot.get("owner_profile_url")
    if link and snapshot.get("owner_type") == "user":
        buttons.append([Button.url("👤 Профиль владельца", link)])
    return buttons


async def monitor_deals(user: TelegramClient, bot: TelegramClient) -> None:
    """Watch manually selected gifts for a newly available legitimate trade path."""
    while True:
        watchlist = dict(state.data.get("deal_watchlist") or {})
        for slug, previous in watchlist.items():
            try:
                current = await fetch_deal_snapshot(user, slug)
                if slug not in state.data.get("deal_watchlist", {}):
                    continue
                changes = deal_opportunities(previous, current)
                if changes and not state.data.get("paused") and state.owner_id is not None:
                    description = "\n".join("• " + html.escape(x) for x in changes)
                    await bot.send_message(
                        state.owner_id,
                        f"🚨 <b>Появился способ купить NFT!</b>\n"
                        f"🎁 <a href=\"https://t.me/nft/{html.escape(slug, quote=True)}\">"
                        f"{html.escape(slug)}</a>\n"
                        f"{description}\n\n"
                        f"{html.escape(deal_details(current))}",
                        parse_mode="html",
                        link_preview=False,
                        buttons=deal_watch_buttons(slug, current),
                    )
                state.data["deal_watchlist"][slug] = current
                state.save()
            except FloodWaitError as exc:
                log.warning("Deal watch: Telegram FloodWait %d seconds", exc.seconds)
                await asyncio.sleep(exc.seconds + 3)
            except Exception as exc:
                log.warning("Deal watch failed for %s: %s", slug, exc)
            await asyncio.sleep(0.25)
        await asyncio.sleep(DEAL_WATCH_INTERVAL)


def command_name(text: str) -> tuple[str, str]:
    head, _, tail = text.strip().partition(" ")
    return head.split("@", 1)[0].lower(), tail.strip()


async def run() -> None:
    # Hosted mode is noninteractive: authenticate using a pre-generated String Session.
    user_session = StringSession(SESSION_STRING) if SESSION_STRING else str(DATA_DIR / "user")
    user = TelegramClient(user_session, API_ID, API_HASH)
    bot = TelegramClient(str(DATA_DIR / "bot"), API_ID, API_HASH)

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
            if cmd == "/start" and state.consume_activation_key(argument, event.sender_id):
                await event.respond("✅ Одноразовый ключ погашен! Бот привязан к твоему аккаунту. /help")
                log.info("Активация: бот привязан к Telegram ID %d", event.sender_id)
                await deliver_pending(bot)
            else:
                await event.respond(
                    "🔐 Для первой привязки отправь /start КЛЮЧ. "
                    "Ключ выдан отдельно владельцу; в логах он не публикуется."
                )
            return
        if event.sender_id != state.owner_id:
            await event.respond("⛔ Нет доступа.")
            return
        if cmd == "/activate":
            # Existing owner can consume the same single-use key once.
            if state.consume_activation_key(argument, event.sender_id):
                await event.respond("✅ Одноразовый ключ активирован и навсегда погашен в состоянии этого бота.")
            elif state.data.get("activation_consumed", False):
                await event.respond("🔒 Этот ключ уже использован. Повторная активация невозможна.")
            else:
                await event.respond("❌ Неверный одноразовый ключ.")
        elif cmd == "/key":
            await event.respond(
                "🔐 Статус одноразового ключа: " + state.activation_status()
                + ". При Git-обновлениях статус хранится в /app/data/state.json."
            )
        elif cmd in ("/start", "/help"):
            await event.respond(
                "📡 <b>Gift Upgrade Radar</b>\n\n"
                "/status — состояние\n"
                "/delivery — очередь и ошибки доставки\n"
                "/diagnose — диагностика NFT\n"
                "/all on — оповещать обо всех улучшенных NFT\n"
                "/owner PlushPepe-123 — ссылка на профиль владельца\n"
                "/deal PlushPepe-123 — способы связаться или купить\n"
                "/watch PlushPepe-123 — следить за продажей\n"
                "/watches — отслеживаемые NFT; /unwatch СЛАГ — отменить\n"
                "/offer PlushPepe-123 1500 — черновик сделки\n"
                "/all off — только редкие модели\n"
                "/check PlushPepe — проверить коллекцию напрямую\n"
                "/last — последние события\n"
                "/test — пробное уведомление\n"
                "/pause — отключить уведомления\n"
                "/resume — включить уведомления\n"
                "/key — статус одноразового ключа\n"
                "/activate КЛЮЧ — использовать выданный ключ\n"
                "/help — команды\n\n"
                "Отслеживаю выпуск улучшенных NFT; включить все: /all on.", parse_mode="html"
            )
        elif cmd == "/status":
            gifts = state.data["gifts"]
            upgradeable = sum(1 for g in gifts.values() if g["upgrade_stars"] is not None)
            await event.respond(
                f"📡 <b>Радар {'⏸ на паузе' if state.data['paused'] else '🟢 активен'}</b>\n"
                f"⏱ Проверка каждые {INTERVAL} сек\n"
                f"🎁 Известных подарков: {len(gifts)}\n"
                f"✨ С доступным апгрейдом: {upgradeable}\n"
                f"💎 Радар NFT: {'включён' if TRACK_RARE_NFTS else 'выключен'}; "
                f"коллекций с baseline: {len(state.data['collections'])}\n"
                f"🔥 Редкая модель: ≤ {MAX_MODEL_RARITY_PCT:g}% или tier epic/legendary\n"
                f"📬 В очереди: {len(state.data['pending'])}\n"
                f"📤 Отправлено с запуска: {DELIVERY_STATS['sent']}\n"
                f"⚠️ Ошибка доставки: {html.escape(DELIVERY_STATS['last_error'] or 'нет')}\n"
                f"🔔 Все новые NFT: {'включено' if all_nft_notifications() else 'только редкие'}\n"
                f"🆕 Увидено новых NFT: {NFT_HEALTH['total_new_nfts']} (редких: {NFT_HEALTH['total_rare_nfts']})\n"
                f"🕒 Последняя проверка каталога: {state.data['last_check'] or 'ещё не было'} (UTC)\n"
                f"📡 NFT кандидатов: {NFT_HEALTH['candidates']}\n"
                f"❓ Коллекций без имени: {NFT_HEALTH['unresolved']}\n"
                f"✅ Проверок NFT: {NFT_HEALTH['checked']}\n"
                f"📬 Уведомлений создано: {NFT_HEALTH['notified_nfts']}\n"
                f"⚠️ Ошибок: {NFT_HEALTH['errors']}\n"
                f"🕒 Последний успешный NFT-запрос: {NFT_HEALTH['last_success'] or 'ни одного'}\n"
                f"❗ Ошибка NFT: {html.escape(NFT_HEALTH['last_error'] or 'нет')}",
                parse_mode="html",
            )
        elif cmd == "/delivery":
            pending = state.data.get("pending") or []
            first = pending[0] if pending else None
            await event.respond(
                "📤 <b>Доставка уведомлений</b>\n"
                f"📬 В очереди: {len(pending)}\n"
                f"✅ Отправлено с запуска: {DELIVERY_STATS['sent']}\n"
                f"📝 Упрощённым текстом: {DELIVERY_STATS['plain']}\n"
                f"🔁 Повторов: {DELIVERY_STATS['retries']}\n"
                f"🕒 Последняя отправка: {DELIVERY_STATS['last_sent'] or 'нет'}\n"
                f"📌 Первое событие: {html.escape((first or {}).get('slug') or (first or {}).get('kind') or 'нет')}\n"
                f"⚠️ Ошибка: {html.escape(DELIVERY_STATS['last_error'] or 'нет')}\n"
                f"⏱ Задержка повторной попытки: {DELIVERY_STATS['next_delay_sec']} сек\n"
                f"🔔 Уведомления: {'на паузе' if state.data['paused'] else 'включены'}",
                parse_mode="html",
            )
        elif cmd == "/diagnose":
            candidates = nft_candidates()
            head = ", ".join(x[0] for x in candidates[:6]) or "нет"
            await event.respond(
                "🔎 <b>Диагностика NFT</b>\n"
                f"Кандидатов: {len(candidates)}; без названия: {NFT_HEALTH['unresolved']}\n"
                f"Успешно: {NFT_HEALTH['checked']}; последний успешный: {NFT_HEALTH['last_success'] or 'нет'}\n"
                f"Известных коллекций: {len(state.data['collections'])}\n"
                f"Новых NFT просмотрено: {NFT_HEALTH['total_new_nfts']}; редких: {NFT_HEALTH['total_rare_nfts']}\n"
                f"Оповещений поставлено: {NFT_HEALTH['notified_nfts']}; backlog: {NFT_HEALTH['backlog']}\n"
                f"Уведомления: {'все NFT' if all_nft_notifications() else 'только редкие'}\n"
                f"Последняя ошибка: {html.escape(NFT_HEALTH['last_error'] or 'нет')}\n"
                f"Примеры: {html.escape(head)}", parse_mode="html"
            )
        elif cmd == "/check":
            # Single direct API query to test access to a known collection.
            slug = collection_slug(argument)
            if not slug or len(slug) > 64:
                await event.respond("Укажи название коллекции: /check PlushPepe")
                return
            try:
                resolved, sample = await get_head_for_collection(user, slug, None)
                minted = int(sample.availability_issued)
                await event.respond(
                    "✅ <b>NFT API отвечает!</b>\n"
                    f"Коллекция: <b>{html.escape(str(sample.title))}</b>\n"
                    f"Slug: <code>{html.escape(resolved)}</code>\n"
                    f"Выпущено NFT: <b>{minted}</b>\n"
                    f"Проверочный NFT: <a href=\"https://t.me/nft/{html.escape(str(sample.slug), quote=True)}\">открыть</a>\n\n"
                    "Это тест доступности коллекции, а не уведомление о новом выпуске.",
                    parse_mode="html", link_preview=False,
                )
            except FloodWaitError as exc:
                await event.respond(f"⏳ Telegram временно ограничил запросы на {exc.seconds} секунд.")
            except Exception as exc:
                await event.respond(
                    f"❌ Не удалось проверить коллекцию: "
                    f"{html.escape(type(exc).__name__ + ': ' + str(exc)[:120])}",
                    parse_mode="html",
                )
        elif cmd in ("/deal", "/watch", "/unwatch"):
            slug = argument.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            if not valid_gift_slug(slug):
                await event.respond("Укажи NFT: /deal SpicedWine-108832\n"
                                    "Или: /watch SpicedWine-108832")
                return
            if cmd == "/unwatch":
                if slug in state.data.get("deal_watchlist", {}):
                    del state.data["deal_watchlist"][slug]
                    state.save()
                    await event.respond(f"🔕 Убрал {slug} из отслеживания.")
                else:
                    await event.respond("Этого подарка нет в списке отслеживания.")
                return
            try:
                snapshot = await fetch_deal_snapshot(user, slug)
                if cmd == "/watch":
                    watchlist = state.data.setdefault("deal_watchlist", {})
                    if slug not in watchlist and len(watchlist) >= 20:
                        await event.respond("Лимит — 20 одновременно отслеживаемых NFT.")
                        return
                    watchlist[slug] = snapshot
                    state.save()
                    prefix = "🔔 NFT добавлен в отслеживание.\n"
                else:
                    prefix = "🔎 Текущие возможности сделки:\n"
                await event.respond(
                    f"{prefix}"
                    f"🎁 <a href=\"https://t.me/nft/{html.escape(slug, quote=True)}\">"
                    f"{html.escape(slug)}</a>\n\n"
                    f"{html.escape(deal_details(snapshot))}\n\n"
                    + (
                        f"Проверяю изменения каждые {DEAL_WATCH_INTERVAL} сек. "
                        f"Чтобы отключить: /unwatch {slug}.\n"
                        if cmd == "/watch" else
                        f"Чтобы получить уведомление при изменении: /watch {slug}.\n"
                    )
                    + "При скрытом владельце невозможно надёжно определить его "
                    "Telegram-аккаунт по одному отображаемому имени.",
                    parse_mode="html", link_preview=False,
                    buttons=deal_watch_buttons(slug, snapshot),
                )
            except FloodWaitError as exc:
                await event.respond(f"⏳ Telegram ограничил запросы на {exc.seconds} секунд.")
            except Exception as exc:
                log.warning("Deal lookup failed for %s: %s", slug, exc)
                await event.respond("❌ Не удалось проверить NFT через Telegram API.")
        elif cmd == "/watches":
            watched = state.data.get("deal_watchlist") or {}
            if not watched:
                await event.respond("🔕 Пока ничего не отслеживаю.\n"
                                    "Добавь подарок: /watch SpicedWine-108832")
            else:
                lines = ["🔔 <b>Отслеживаемые NFT</b>"]
                for slug in list(watched)[:20]:
                    lines.append(
                        f"• <a href=\"https://t.me/nft/{html.escape(slug, quote=True)}\">"
                        f"{html.escape(slug)}</a>"
                    )
                lines.append(f"\nПроверка раз в {DEAL_WATCH_INTERVAL} сек; "
                             "отмена — /unwatch НАЗВАНИЕ-НОМЕР.")
                await event.respond("\n".join(lines), parse_mode="html",
                                    link_preview=False)
        elif cmd == "/owner":
            slug = argument.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            if not re.fullmatch(r"[A-Za-z0-9]+-[0-9]+", slug):
                await event.respond("Пример: /owner PlushPepe-123")
                return
            try:
                details = await user(functions.payments.GetUniqueStarGiftRequest(slug=slug))
                contact = await resolve_owner_contact(details.gift, details, user)
                url = contact.get("owner_profile_url")
                real_username = contact.get("owner_url")
                user_id = contact.get("owner_user_id")
                if contact.get("owner_type") == "user" and url:
                    buttons = [[Button.url(
                        "✉️ Открыть чат владельца" if real_username else
                        "👤 Открыть профиль по Telegram ID", url
                    )]]
                    note = (
                        "✅ Публичная ссылка на чат."
                        if real_username else
                        "⚠️ Ссылка по ID может не открыться, если Telegram "
                        "не разрешает доступ к профилю по настройкам приватности."
                    )
                    await event.respond(
                        f"🎁 <a href=\"https://t.me/nft/{html.escape(slug, quote=True)}\">"
                        f"{html.escape(slug)}</a>\n"
                        f"👤 Владелец: {html.escape(contact['owner_label'])}\n"
                        f"🆔 Telegram ID: <code>{user_id}</code>\n"
                        f"🔗 <a href=\"{html.escape(url, quote=True)}\">Открыть профиль</a>\n"
                        f"{note}\n\n"
                        "Узнать, можно ли написать этому человеку, можно только при открытии профиля.",
                        parse_mode="html", link_preview=False, buttons=buttons,
                    )
                else:
                    await event.respond(
                        f"🎁 NFT: {html.escape(slug)}\n"
                        f"👤 Владелец: {html.escape(contact['owner_label'])}\n"
                        f"📋 Тип данных: {html.escape(str(contact.get('owner_type', 'unknown')))}\n"
                        "🔐 Telegram передал отображаемое имя или кошелёк, "
                        "но не подтверждённый профиль владельца. "
                        "Даже если у человека есть @username, по одному имени "
                        "нельзя безопасно определить его аккаунт. "
                        f"Включи проверку продажи: /watch {slug}",
                        parse_mode="html",
                        buttons=[[Button.url("🎁 Открыть NFT", f"https://t.me/nft/{slug}")]],
                    )
            except FloodWaitError as exc:
                await event.respond(f"⏳ Telegram ограничил запросы: {exc.seconds} сек.")
            except Exception as exc:
                log.warning("Failed to find owner for %s: %s", slug, exc)
                await event.respond("❌ Не удалось получить актуального владельца NFT.")
        elif cmd == "/offer":
            args = argument.split()
            # Allow either a direct slug or a copied Telegram NFT link.
            slug = args[0].split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] if args else ""
            if not re.fullmatch(r"[A-Za-z0-9]+-[0-9]+", slug):
                await event.respond("Пример: /offer PlushPepe-123 1500\nИли: /offer PlushPepe-123")
                return
            amount = None
            if len(args) > 1:
                if not args[1].isdigit() or not 1 <= int(args[1]) <= 1_000_000_000:
                    await event.respond("Укажи сумму звёзд положительным целым числом.")
                    return
                amount = int(args[1])
            try:
                details = await user(functions.payments.GetUniqueStarGiftRequest(slug=slug))
                unique = details.gift
                contact = await resolve_owner_contact(unique, details, user)
                minimum = contact.get("offer_min_stars")
                if amount is not None and minimum is not None and amount < minimum:
                    await event.respond(
                        f"⚠️ Минимальный официальный оффер: {minimum} ⭐. "
                        "Попробуй другую сумму."
                    )
                    return
                draft = html.escape(offer_text(unique, amount))
                url = contact.get("owner_profile_url") or contact.get("owner_url")
                peer_kind = contact.get("owner_type")
                buttons = [[Button.url("🎁 Открыть NFT / оффер", f"https://t.me/nft/{slug}")]]
                if url and peer_kind == "user":
                    label = "✉️ Открыть чат владельца" if contact.get("owner_url") else "👤 Открыть профиль по ID"
                    buttons.append([Button.url(label, url)])
                if peer_kind == "user" and contact.get("owner_url"):
                    note = "Ссылка на публичный чат пользователя — можно написать лично."
                elif peer_kind == "user" and contact.get("owner_user_id"):
                    note = (
                        f"Telegram ID владельца: {contact['owner_user_id']}. "
                        "Ссылка на профиль по ID может не работать из-за ограничений Telegram."
                    )
                else:
                    note = (
                        "Прямой контакт Telegram не раскрыт. "
                        "Проверь наличие официального оффера на странице NFT."
                    )
                await event.respond(
                    "📝 <b>Сообщение для продавца</b>\n"
                    f"<code>{draft}</code>\n\n"
                    f"👤 Владелец сейчас: {html.escape(contact['owner_label'])}\n"
                    f"💰 Мин. оффер: {minimum if minimum is not None else 'не указан'} ⭐\n"
                    f"{note}\n\n"
                    "Сообщение не отправлено: скопируй его и реши сам, писать ли человеку.",
                    parse_mode="html", link_preview=False, buttons=buttons,
                )
            except FloodWaitError as exc:
                await event.respond(f"⏳ Telegram ограничил запросы: {exc.seconds} сек.")
            except Exception as exc:
                log.warning("Cannot prepare offer for %s: %s", slug, exc)
                await event.respond(
                    "❌ Не удалось получить актуальные сведения о владельце "
                    f"этого NFT: {html.escape(type(exc).__name__)}",
                    parse_mode="html",
                )
        elif cmd == "/all":
            value = argument.lower()
            if value in {"on", "off"}:
                state.data["notify_all_nfts"] = value == "on"
                state.save()
                await event.respond(
                    "🔔 Отныне оповещаю о КАЖДОМ обнаруженном улучшенном NFT."
                    if value == "on" else
                    "💎 Отныне оповещаю только о редких моделях (до порога или Epic/Legendary)."
                )
            else:
                await event.respond(
                    f"🔔 Режим: {'все NFT' if all_nft_notifications() else 'только редкие'}."
                    " Команды: /all on или /all off"
                )
        elif cmd == "/last":
            recent = state.data["history"][-10:]
            if not recent:
                await event.respond("Пока новых апгрейдов не обнаружено.")
            else:
                lines = ["🗂 <b>Последние события</b>"]
                for x in reversed(recent):
                    if x["kind"] in {"rare_nft", "new_nft"}:
                        icon = "💎" if x["kind"] == "rare_nft" else "🎁"
                        lines.append(f"{icon} {html.escape(x['title'])} #{x['number']} — {x['model_rarity']}")
                    else:
                        lines.append(f"• {html.escape(x['title'])}: {x['upgrade_stars']}⭐ — {x['created_at']}")
                await event.respond("\n".join(lines), parse_mode="html")
        elif cmd == "/test":
            await event.respond("✅ Тестовое уведомление: бот умеет отправлять сообщения.")
        elif cmd == "/pause":
            state.set_paused(True)
            # Pause = skip alerts seen while paused; discard unsent queue too.
            state.data["pending"] = []
            state.save()
            await event.respond("⏸ Уведомления остановлены. Мониторинг продолжает запоминать изменения.")
        elif cmd == "/resume":
            state.set_paused(False)
            await event.respond("▶️ Уведомления снова включены.")
            await deliver_pending(bot)

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
                 asyncio.create_task(monitor(user, bot)),
                 asyncio.create_task(monitor_deals(user, bot))]
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
