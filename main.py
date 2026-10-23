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
NFT_SCAN_INTERVAL = max(5, int(os.getenv("NFT_SCAN_INTERVAL", "20")))
COLLECTIONS_PER_CYCLE = max(1, int(os.getenv("COLLECTIONS_PER_CYCLE", "3")))
MAX_NEW_NFTS_PER_CYCLE = max(1, int(os.getenv("MAX_NEW_NFTS_PER_CYCLE", "10")))
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
    if item["kind"] == "rare_nft":
        link = f"https://t.me/nft/{item['slug']}"  # Actual slug returned by Telegram.
        return (
            "💎 <b>ЗАСЕЧЁН РЕДКИЙ NFT!</b>\n\n"
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


async def monitor_rare_nfts(user: TelegramClient, bot: TelegramClient) -> None:
    round_robin = 0
    while True:
        delay = NFT_SCAN_INTERVAL
        try:
            if GIFT_COLLECTIONS:
                collections = GIFT_COLLECTIONS
            else:
                collections = list(dict.fromkeys(
                    collection_slug(x["title"])
                    for x in state.data["gifts"].values()
                    if x.get("upgrade_stars") is not None
                    and collection_slug(x["title"])
                ))
            if collections:
                batch = [collections[(round_robin + n) % len(collections)]
                         for n in range(min(COLLECTIONS_PER_CYCLE, len(collections)))]
                round_robin = (round_robin + len(batch)) % len(collections)
                for slug in batch:
                    try:
                        response = await user(functions.payments.GetUniqueStarGiftRequest(slug=f"{slug}-1"))
                        unique = response.gift
                        count = int(unique.availability_issued)
                        previous = state.data["collections"].get(slug)
                        if previous is None:
                            # First observation per collection establishes baseline.
                            state.set_collection_issued(slug, count)
                            log.info("NFT baseline %s: %d issued", slug, count)
                            continue
                        if count < previous:
                            # Telegram can reset counts or change a collection; avoid historical flood.
                            state.set_collection_issued(slug, count)
                            continue
                        upper = min(count, previous + MAX_NEW_NFTS_PER_CYCLE)
                        for number in range(previous + 1, upper + 1):
                            # A failed request must not advance the cursor.
                            details = await user(functions.payments.GetUniqueStarGiftRequest(
                                slug=f"{slug}-{number}"))
                            collectible = details.gift
                            attrs = extract_attributes(collectible)
                            if is_rare_model(attrs, MAX_MODEL_RARITY_PCT):
                                event = make_event(collectible, attrs, now_utc())
                                state.record_rare_nft(event)
                                log.info("Rare NFT: %s", collectible.slug)
                            state.set_collection_issued(slug, number)
                            await deliver_pending(bot)
                    except FloodWaitError:
                        raise
                    except Exception as exc:
                        log.warning("NFT scan %s: %s", slug, exc)
        except FloodWaitError as exc:
            delay = max(delay, exc.seconds + 3)
            log.warning("NFT API FloodWait, sleep %d sec", delay)
        except Exception:
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
            new_events = state.update_gifts(gifts, NOTIFY_PRICE_CHANGES)
            if new_events:
                log.info("Обнаружено новых событий: %d", len(new_events))
            else:
                log.info("Проверено подарков: %d; новых улучшений нет", len(gifts))
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
                "/last — последние события\n"
                "/test — пробное уведомление\n"
                "/pause — отключить уведомления\n"
                "/resume — включить уведомления\n"
                "/help — команды\n\n"
                "Ловлю открытие апгрейдов и редкие NFT после улучшений.", parse_mode="html"
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
                f"🕒 Последняя проверка: {state.data['last_check'] or 'ещё не было'} (UTC)",
                parse_mode="html",
            )
        elif cmd == "/last":
            recent = state.data["history"][-10:]
            if not recent:
                await event.respond("Пока новых апгрейдов не обнаружено.")
            else:
                lines = ["🗂 <b>Последние события</b>"]
                for x in reversed(recent):
                    if x["kind"] == "rare_nft":
                        lines.append(f"💎 {html.escape(x['title'])} #{x['number']} — {x['model_rarity']}")
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
