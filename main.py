"""Telegram gift upgrade radar: MTProto watcher + private notification bot."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events, functions
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from radar import Gift, RadarState, now_utc
from mint_radar import collection_slug, extract_attributes, is_rare_model, make_event
from candidate_selection import get_candidates

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
GIFT_COLLECTIONS = [x.strip() for x in os.getenv("GIFT_COLLECTIONS", "").split(",") if x.strip()]
# Bothost preserves /app/data during redeploys.
DATA_DIR = Path(os.getenv("DATA_DIR") or ("/app/data" if os.getenv("BOT_ID") else "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
state = RadarState(DATA_DIR / "state.json")
PAIR_CODE = os.getenv("PAIR_CODE", "").strip() or secrets.token_hex(4)
DELIVERY_LOCK = asyncio.Lock()


def message_for_gift(item: dict) -> str:
    title = html.escape(item["title"])
    if item["kind"] in {"rare_nft", "new_nft"}:
        link = f"https://t.me/nft/{item['slug']}"  # Actual slug returned by Telegram.
        return (
            ("💎 <b>ЗАСЕЧЁН РЕДКИЙ NFT!</b>\n\n" if item["kind"] == "rare_nft"
             else "🎁 <b>НОВЫЙ NFT ПОСЛЕ УЛУЧШЕНИЯ</b>\n\n")
            f"🎁 <b>{title} #{item['number']}</b>\n"
            f"🔥 Модель: <b>{html.escape(item['model'])}</b> ({item['model_rarity']})\n"
            f"🎨 Фон: {html.escape(item['backdrop'])} ({item['backdrop_rarity']})\n"
            f"✳️ Символ: {html.escape(item['pattern'])} ({item['pattern_rarity']})\n"
            f"🔗 <a href=\"{html.escape(link, quote=True)}\">Открыть NFT</a>\n"
            f"🕒 Обнаружен: {html.escape(item['created_at'])} UTC\n\n"
            "Редкость — частота выпадения атрибута, не рыночная оценка."
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


async def send_alert(bot: TelegramClient, chat_id: int, text: str) -> None:
    await bot.send_message(chat_id, text, parse_mode="html", link_preview=False)


async def deliver_pending(bot: TelegramClient) -> None:
    owner_id = state.owner_id
    if owner_id is None or state.data["paused"]:
        return
    async with DELIVERY_LOCK:
        for item in list(state.data["pending"]):
            try:
                await send_alert(bot, owner_id, message_for_gift(item))
            except FloodWaitError as exc:
                log.warning("Bot flood wait: %s sec", exc.seconds)
                return
            except Exception as exc:
                log.warning("Не удалось отправить уведомление: %s", exc)
                return
            state.pop_delivered(item["event_id"])


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
                            collectible = (
                                await user(functions.payments.GetUniqueStarGiftRequest(
                                    slug=f"{resolved}-{number}"))
                            ).gift
                            # Crafting has its own minting process; the radar tracks upgrades.
                            if not getattr(collectible, "crafted", False):
                                attrs = extract_attributes(collectible)
                                rare = is_rare_model(attrs, MAX_MODEL_RARITY_PCT)
                                event = make_event(collectible, attrs, now_utc(), rare=rare)
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


def command_name(text: str) -> tuple[str, str]:
    head, _, tail = text.strip().partition(" ")
    return head.split("@", 1)[0].lower(), tail.strip()


async def run() -> None:
    # Hosted mode is noninteractive: authenticate using a pre-generated String Session.
    user_session = StringSession(SESSION_STRING) if SESSION_STRING else str(DATA_DIR / "user")
    user = TelegramClient(user_session, API_ID, API_HASH)
    bot = TelegramClient(str(DATA_DIR / "bot"), API_ID, API_HASH)

    # Owner authentication: only the first private sender with PAIR_CODE can bind the bot.
    @bot.on(events.NewMessage(incoming=True))
    async def on_command(event):
        if not event.is_private:
            return
        cmd, argument = command_name(event.raw_text)
        if not cmd.startswith("/"):
            return
        if state.owner_id is None:
            if cmd == "/start" and secrets.compare_digest(argument, PAIR_CODE):
                state.set_owner(event.sender_id)
                await event.respond("✅ Радар привязан к тебе! Напиши /status или /help")
                log.info("Бот привязан к пользователю %d", event.sender_id)
                await deliver_pending(bot)
            else:
                await event.respond("🔐 Для подключения отправь /start КОД. Код указан в консоли запуска.")
            return
        if event.sender_id != state.owner_id:
            await event.respond("⛔ Нет доступа.")
            return
        if cmd in ("/start", "/help"):
            await event.respond(
                "📡 <b>Gift Upgrade Radar</b>\n\n"
                "/status — состояние\n"
                "/diagnose — диагностика NFT\n"
                "/all on — оповещать обо всех улучшенных NFT\n"
                "/all off — только редкие модели\n"
                "/check PlushPepe — проверить коллекцию напрямую\n"
                "/last — последние события\n"
                "/test — пробное уведомление\n"
                "/pause — отключить уведомления\n"
                "/resume — включить уведомления\n"
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
            print("\n" + "=" * 60, flush=True)
            print(f"ПРИВЯЗКА: открой @{me.username} и отправь /start {PAIR_CODE}", flush=True)
            print("=" * 60 + "\n", flush=True)
        tasks = [asyncio.create_task(monitor(user, bot))]
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
