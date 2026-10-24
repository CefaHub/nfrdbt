"""Telegram gift upgrade radar: MTProto watcher + private notification bot."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import Button, TelegramClient, events, functions
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from radar import Gift, RadarState, now_utc
from mint_radar import extract_attributes, make_event
from candidate_selection import get_candidates
from gift_origin import original_participants, resolve_original_participants
from gift_owner import resolve_current_owner, profile_url
from delivery import fallback_text, is_bad_markup
from commercial import CommercialStore, ADMIN_ID
from stars_payment import StarsMerchant, MERCHANT_USERNAME
from scan_pacing import remaining_pause
from recipient_filter import recipient_from_history, filter_pending
from recipient_recheck import schedule_review, due_reviews, reschedule_review, drop_review
from recovery import (schedule_recovery, current_recovery, complete_recovery_item,
                      fail_recovery_item, pending_count)
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
PAYMENT_BOT_TOKEN = os.getenv("PAYMENT_BOT_TOKEN", "").strip()
STARS_PRICE_1D = max(0, int(os.getenv("STARS_PRICE_1D", "0")))
STARS_PRICE_LIFETIME = max(0, int(os.getenv("STARS_PRICE_LIFETIME", "0")))
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
store = CommercialStore(DATA_DIR / "commercial.sqlite3")
LIVE_COUNTERS = {
    "new_notified": 0,
    "new_skipped_limit": 0,
    "filtered_no_recipient": 0,
    "minute_bucket": None,
    "minute_count": 0,
    "last_new": None,
}
DELIVERY_WAKE = asyncio.Event()
RECOVERY_WAKE = asyncio.Event()
RECOVERY_LOOKBACK = max(1, min(100, int(os.getenv("RECOVERY_LOOKBACK", "25"))))
RECOVERY_MAX_TOTAL = max(1, min(3000, int(os.getenv("RECOVERY_MAX_TOTAL", "1500"))))
RECOVERY_ITEM_DELAY = max(1.0, float(os.getenv("RECOVERY_ITEM_DELAY", "2.0")))
RECOVERY_STATS = {"checked": 0, "found": 0, "errors": 0, "last_error": None,
                  "last_slug": None, "started": None}
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
    owner = item.get("owner") or {}
    name = html.escape(str(owner.get("label") or "Не указан"))
    url = profile_url(owner)
    if url:
        name = f'<a href="{html.escape(url, quote=True)}">{name}</a>'

    fields = [f"<b>{title} #{number}</b>", nft_link, f"Владелец: {name}"]
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
    # Intentionally use the ORIGINAL gift recipient for this optional
    # profile shortcut, not current ownership. Keep displayed owner text as-is.
    recipient = recipient_from_history(item.get("original_participants"))
    if recipient:
        url = str(recipient.get("url") or "")
        if (url.startswith("https://t.me/") or
                url.startswith("tg://user?id=")):
            buttons.append([Button.url("Профиль", url)])
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


def queue_new_nft(item: dict) -> bool:
    """Deduplicate at source, then put the alert into one shared SQL outbox."""
    if not state.record_nft_once(item):
        return False
    # record_nft_once is persistent first: an interrupted SQLite write can
    # always be recovered from state.pending during a restart.
    pending_event = state.data["pending"][-1]
    store.publish(pending_event)
    state.pop_delivered(pending_event["event_id"])
    return True


def transfer_legacy_queue() -> None:
    """Idempotently bridge alerts left by old versions or interrupted writes."""
    legacy = list(state.data.get("pending") or [])
    if not legacy:
        return
    for item in legacy:
        if item.get("kind") in {"new_nft", "rare_nft"}:
            store.publish(item)
        state.pop_delivered(item["event_id"])


async def delivery_worker(bot: TelegramClient) -> None:
    """Fan out each NFT privately, with a durable per-subscriber cursor.

    Telegram can occasionally resend after a crash between send and ACK,
    but one failed customer cannot stall the entire broadcast permanently.
    """
    while True:
        target = None
        try:
            transfer_legacy_queue()
            if state.data.get("paused"):
                DELIVERY_WAKE.clear()
                try:
                    await asyncio.wait_for(DELIVERY_WAKE.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                continue
            work = store.next_delivery()
            if work is None:
                DELIVERY_WAKE.clear()
                try:
                    await asyncio.wait_for(DELIVERY_WAKE.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass
                continue

            target, seq, item = work
            try:
                await asyncio.wait_for(
                    send_alert(bot, target, message_for_gift(item), item),
                    timeout=45,
                )
                result = "sent"
            except Exception as exc:
                if not is_bad_markup(exc):
                    raise
                await asyncio.wait_for(
                    bot.send_message(
                        target, fallback_text(item),
                        link_preview=item.get("kind") in {"new_nft", "rare_nft"},
                    ), timeout=45,
                )
                result = "plain"

            store.ack(target, seq)
            DELIVERY_STATS["sent"] += 1
            DELIVERY_STATS["plain"] += int(result == "plain")
            DELIVERY_STATS["last_sent"] = now_utc()
            DELIVERY_STATS["last_slug"] = item.get("slug")
            DELIVERY_STATS["last_error"] = None
            DELIVERY_STATS["next_delay_sec"] = 0
            # Do not write customer IDs to logs.
            log.info("NFT notification delivered; remaining subscribers=%d",
                     store.delivery_backlog())
            await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            delay = max(3, int(exc.seconds) + 3)
            DELIVERY_STATS["last_error"] = f"FloodWait {exc.seconds}s"
            DELIVERY_STATS["last_error_at"] = now_utc()
            DELIVERY_STATS["retries"] += 1
            DELIVERY_STATS["next_delay_sec"] = delay
            log.warning("Telegram delivery rate limit: %ss", delay)
            await asyncio.sleep(delay)
        except Exception as exc:
            name = type(exc).__name__
            DELIVERY_STATS["errors"] += 1
            DELIVERY_STATS["retries"] += 1
            DELIVERY_STATS["last_error"] = name
            DELIVERY_STATS["last_error_at"] = now_utc()
            if target is not None and name in {
                "UserIsBlockedError", "InputUserDeactivatedError",
                "UserDeactivatedBanError", "ChatWriteForbiddenError",
                "PeerIdInvalidError", "UserIsBotError",
            } and target != ADMIN_ID:
                store.deactivate(target)
                log.warning("A subscriber became unreachable and was disabled (%s)", name)
            elif target is not None:
                delay = store.retry_later(target)
                DELIVERY_STATS["next_delay_sec"] = delay
                log.warning("NFT delivery retry scheduled (%s) in %ss", name, delay)
            else:
                log.error("Delivery worker exception: %s", name)
            await asyncio.sleep(2.0)


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
    "last_filtered_slug": None,
    "last_queued_slug": None,
    "last_duplicate_slug": None,
    "rechecked": 0,
    "recovered": 0,
    "permanently_skipped": 0,
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


async def recovery_worker(user: TelegramClient, bot: TelegramClient) -> None:
    """Rescan recent NFT numbers saved by /pushall, at a conservative rate.

    Existing main scan cursors are left untouched. Individual recovery jobs
    survive process restarts and only advance after the Telegram lookup.
    """
    while True:
        try:
            if (not state.owner_id or state.data.get("paused")
                    or not state.data.get("recovery_jobs")):
                RECOVERY_WAKE.clear()
                try:
                    await asyncio.wait_for(RECOVERY_WAKE.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
                continue
            item = current_recovery(state.data)
            if item is None:
                state.save()
                continue
            slug, number = item
            full_slug = f"{slug}-{number}"
            try:
                details = await user(
                    functions.payments.GetUniqueStarGiftRequest(slug=full_slug)
                )
                gift = details.gift
                if str(getattr(gift, "slug", "")).lower() != full_slug.lower():
                    raise ValueError("Recovered NFT slug mismatch")
                if not getattr(gift, "crafted", False):
                    history = original_participants(gift, details)
                    if recipient_from_history(history):
                        try:
                            enriched = await asyncio.wait_for(
                                resolve_original_participants(gift, details, user),
                                timeout=2.5,
                            )
                            if recipient_from_history(enriched):
                                history = enriched
                        except Exception:
                            pass
                        event = make_event(
                            gift, extract_attributes(gift), now_utc(), rare=False
                        )
                        event["original_participants"] = history
                        event["owner"] = await resolve_current_owner(gift, details, user)
                        if queue_new_nft(event):
                            RECOVERY_STATS["found"] += 1
                            NFT_HEALTH["last_queued_slug"] = full_slug
                            log.info("NFT REPLAY QUEUED: %s, pending=%d",
                                     full_slug, len(state.data["pending"]))
                            await deliver_pending(bot)
                    else:
                        # Metadata may arrive later than the mint itself.
                        schedule_review(state.data, full_slug)
                complete_recovery_item(state.data)
                RECOVERY_STATS["checked"] += 1
                RECOVERY_STATS["last_slug"] = full_slug
                state.save()
            except FloodWaitError as exc:
                RECOVERY_STATS["last_error"] = f"FloodWait {exc.seconds}s"
                log.warning("NFT REPLAY FloodWait %ds; cursor unchanged", exc.seconds)
                await asyncio.sleep(max(3, exc.seconds + 3))
                continue
            except Exception as exc:
                RECOVERY_STATS["errors"] += 1
                RECOVERY_STATS["last_error"] = f"{full_slug}: {type(exc).__name__}"
                skipped = fail_recovery_item(state.data)
                state.save()
                log.warning("NFT REPLAY failed %s (%s); skipped=%s",
                            full_slug, type(exc).__name__, skipped)
                await asyncio.sleep(6)
                continue
            await asyncio.sleep(RECOVERY_ITEM_DELAY)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("NFT REPLAY worker error; retry later")
            await asyncio.sleep(10)


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
                                    event["owner"] = await resolve_current_owner(
                                        collectible, details, user
                                    )
                                    NFT_HEALTH["total_new_nfts"] += 1
                                    NFT_HEALTH["last_nft"] = str(collectible.slug)
                                    if queue_new_nft(event):
                                        NFT_HEALTH["notified_nfts"] += 1
                                        NFT_HEALTH["discovered"] += 1
                                        NFT_HEALTH["last_found"] = now_utc()
                                        NFT_HEALTH["last_queued_slug"] = str(collectible.slug)
                                        log.info("NFT QUEUED: %s recipient=%s pending=%d",
                                                 collectible.slug, history[0]["label"],
                                                 len(state.data["pending"]))
                                    else:
                                        NFT_HEALTH["last_duplicate_slug"] = str(collectible.slug)
                                        log.info("NFT ALREADY SEEN: %s", collectible.slug)
                                else:
                                    NFT_HEALTH["filtered_no_recipient"] += 1
                                    NFT_HEALTH["last_filtered_slug"] = str(collectible.slug)
                                    scheduled = schedule_review(state.data, str(collectible.slug))
                                    if scheduled:
                                        state.save()
                                    log.info("NFT NO RECIPIENT: %s review=%s",
                                             collectible.slug,
                                             "scheduled" if scheduled else "already scheduled/full")
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


async def retry_new_recipient_details(user: TelegramClient, bot: TelegramClient) -> None:
    """Retry fresh gifts which initially lacked original recipient metadata."""
    while True:
        try:
            for slug in due_reviews(state.data, limit=2):
                NFT_HEALTH["rechecked"] += 1
                try:
                    details = await user(
                        functions.payments.GetUniqueStarGiftRequest(slug=slug)
                    )
                    unique = details.gift
                    if str(getattr(unique, "slug", "")).lower() != slug.lower():
                        raise ValueError("Gift slug mismatch")
                    history = original_participants(unique, details)
                    if recipient_from_history(history) and not getattr(unique, "crafted", False):
                        try:
                            enriched = await asyncio.wait_for(
                                resolve_original_participants(unique, details, user),
                                timeout=2.5,
                            )
                            if recipient_from_history(enriched):
                                history = enriched
                        except Exception:
                            pass
                        event = make_event(
                            unique, extract_attributes(unique), now_utc(), rare=False
                        )
                        event["original_participants"] = history
                        event["owner"] = await resolve_current_owner(unique, details, user)
                        drop_review(state.data, slug)
                        state.save()
                        if queue_new_nft(event):
                            NFT_HEALTH["recovered"] += 1
                            NFT_HEALTH["last_queued_slug"] = slug
                            log.info("NFT RECOVERED AND QUEUED: %s pending=%d",
                                     slug, len(state.data["pending"]))
                            await deliver_pending(bot)
                        else:
                            log.info("NFT RECOVERED BUT ALREADY SEEN: %s", slug)
                        continue
                    more = reschedule_review(state.data, slug)
                    state.save()
                    if more:
                        log.info("NFT RECHECK NO RECIPIENT: %s (retry later)", slug)
                    else:
                        NFT_HEALTH["permanently_skipped"] += 1
                        log.info("NFT FINAL SKIP: %s (original recipient unavailable)", slug)
                except FloodWaitError as exc:
                    more = reschedule_review(state.data, slug)
                    state.save()
                    log.warning("NFT recipient review FloodWait=%ds for %s", exc.seconds, slug)
                    await asyncio.sleep(exc.seconds + 3)
                except Exception as exc:
                    more = reschedule_review(state.data, slug)
                    state.save()
                    log.warning("NFT recipient review failed %s: %s",
                                slug, type(exc).__name__)
                await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("NFT recipient review loop failed")
        await asyncio.sleep(5)


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
        if schedule_review(state.data, event["slug"]):
            state.save()
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
            if schedule_review(state.data, event["slug"]):
                state.save()
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
        alert["owner"] = await resolve_current_owner(collectible, details, user)
        if queue_new_nft(alert):
            LIVE_COUNTERS["minute_count"] += 1
            LIVE_COUNTERS["new_notified"] += 1
            LIVE_COUNTERS["last_new"] = now_utc()
            log.info("LIVE GIFT %s (recipient visible)", event["slug"])
            await deliver_pending(None)
    except FloodWaitError as exc:
        if schedule_review(state.data, event["slug"]):
            state.save()
        log.warning("LIVE recipient verification delayed by %ds", exc.seconds)
    except Exception as exc:
        if schedule_review(state.data, event["slug"]):
            state.save()
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
    # Retire the original /activate owner. This hardcoded Telegram ID is
    # the only account that may access the admin panel or mint access links.
    store.migrate_legacy(state)
    bot_identity = {"username": None}

    @bot.on(events.NewMessage(incoming=True))
    async def on_command(event):
        if not event.is_private:
            return
        cmd, argument = command_name(event.raw_text)
        if not cmd.startswith("/"):
            return
        sender = event.sender_id
        if sender is None:
            return

        if sender != ADMIN_ID:
            if cmd in ("/buy", "/pay", "/payment"):
                if not PAYMENT_BOT_TOKEN or not (
                    STARS_PRICE_1D > 0 or STARS_PRICE_LIFETIME > 0
                ):
                    await event.respond(
                        "Оплата Stars пока не настроена. "
                        "Доступ можно активировать по коду, полученному после покупки."
                    )
                    return
                await event.respond(
                    "NFT Gift Radar — оплата через Telegram Stars ⭐\n"
                    f"Перейди в @{MERCHANT_USERNAME} и выбери тариф.\n"
                    "После подтверждения платежа вернись сюда и нажми /start.",
                    buttons=[Button.url(
                        "Оплатить звёздами",
                        f"https://t.me/{MERCHANT_USERNAME}?start=buy"
                    )],
                    link_preview=False,
                )
                return
            if cmd in ("/start", "/join") and argument:
                result = store.redeem(argument, sender)
                info = store.license_info(sender)
                if result in {"activated", "extended", "upgraded", "existing"}:
                    if info and info["expires_at"] is not None:
                        until = datetime.fromtimestamp(
                            info["expires_at"], tz=timezone.utc
                        ).strftime("%d.%m.%Y %H:%M UTC")
                        plan = f"Доступ на 1 день. Активен до {until}."
                    else:
                        plan = "Бессрочный доступ активирован."
                    await event.respond(
                        f"NFT Gift Radar подключён!\n{plan}\n"
                        "Новые NFT будут приходить сюда автоматически.\n"
                        "/status — состояние подписки\n"
                        "/stop — отключить уведомления."
                    )
                    await deliver_pending(bot)
                else:
                    await event.respond(
                        "Эта ссылка уже использована или недействительна. "
                        "Обратись к продавцу за новой ссылкой."
                    )
                return

            info = store.license_info(sender)
            if store.is_subscribed(sender):
                if cmd in ("/start", "/status", "/help", "/ping"):
                    if info["expires_at"] is None:
                        plan = "Бессрочный доступ"
                    else:
                        until = datetime.fromtimestamp(
                            info["expires_at"], tz=timezone.utc
                        ).strftime("%d.%m.%Y %H:%M UTC")
                        plan = f"Доступ на 1 день до {until}"
                    await event.respond(
                        f"NFT-радар активен. {plan}.\n"
                        "Новые подарки приходят автоматически.\n"
                        "/stop — отключить уведомления."
                    )
                elif cmd in ("/stop", "/leave"):
                    store.deactivate(sender)
                    await event.respond(
                        "Уведомления выключены. Для повторного включения "
                        "отправь /start."
                    )
                else:
                    await event.respond("Эта команда недоступна.")
            elif info and info["expired"]:
                if cmd in ("/start", "/status", "/help", "/ping", "/join"):
                    await event.respond(
                        "Доступ на 1 день закончился. Уведомления отключены. "
                        "Для продления или покупки бессрочного доступа "
                        "активируй новую ссылку."
                    )
            elif info is not None:
                if cmd == "/start" and store.reactivate(sender):
                    await event.respond("Уведомления снова включены.")
                    await deliver_pending(bot)
                elif cmd in ("/status", "/help", "/ping"):
                    await event.respond("Уведомления отключены. Отправь /start.")
            elif cmd in ("/start", "/status", "/help", "/ping", "/join"):
                await event.respond(
                    "Это платный NFT-радар. Для активации открой "
                    "персональную ссылку или используй /buy для оплаты Stars."
                )
            # Other buyers never see anyone else's Telegram ID or purchases.
            return

        if cmd in ("/start", "/help", "/admin"):
            await event.respond(
                "<b>Gift Radar — админ-панель</b>\n"
                "/invite — бессрочный доступ, 1 ссылка\n"
                "/links 20 — бессрочный доступ, 20 ссылок\n"
                "/invite1d — доступ на 1 день, 1 ссылка\n"
                "/links1d 20 — доступ на 1 день, 20 ссылок\n"
                "/buy — оплата через Stars в отдельном боте\n"
                "/stats — только общая статистика, без пользователей\n"
                "/status — здоровье сканера и очереди\n"
                "/pushall — перепроверить пропущенные NFT\n"
                "/flush — разбудить рассылку\n"
                "/last — последние NFT\n"
                "/check ССЫЛКА — диагностика одного NFT\n"
                "/pause /resume — пауза и возобновление\n"
                "Ссылки одноразовые; 24 часа отсчитываются с момента "
                "активации дневного доступа. "
                "Количество покупателей и ссылок не ограничено кодом.",
                parse_mode="html",
            )
        elif cmd in ("/invite", "/links", "/invite1d", "/links1d"):
            daily = cmd.endswith("1d")
            amount = 1
            if cmd in ("/links", "/links1d"):
                if not argument.isdecimal() or not 1 <= int(argument) <= 50:
                    await event.respond(
                        f"Формат: {cmd} 20 (1–50 за одну команду; "
                        "общего лимита нет, можно повторять)."
                    )
                    return
                amount = int(argument)
            username = bot_identity["username"]
            if not username:
                me = await bot.get_me()
                username = me.username
                bot_identity["username"] = username
            codes = store.create_links(amount, duration_days=1 if daily else None)
            links = [f"https://t.me/{username}?start={code}" for code in codes]
            heading = (
                "ДОСТУП НА 1 ДЕНЬ — 24 часа с активации:"
                if daily else "БЕССРОЧНЫЙ ДОСТУП:"
            )
            # Send inventories only to the hardcoded admin. Never cross-post
            # a daily inventory to the lifetime-product auto-delivery SKU.
            for i in range(0, len(links), 40):
                await event.respond(
                    heading + "\n" + "\n".join(links[i:i + 40]),
                    link_preview=False,
                )
        elif cmd in ("/stats", "/linkstats", "/friends"):
            st = store.stats()
            await event.respond(
                "<b>Статистика NFT-радара</b>\n"
                f"Активных подписок: {st['active_users']}\n"
                f"Всего активаций: {st['licensed_users'] - 1}\n"
                f"Неиспользованных бессрочных ссылок: {st['lifetime_links']}\n"
                f"Неиспользованных однодневных ссылок: {st['trial_links']}\n"
                f"Активированных ссылок: {st['redeemed_links']}\n"
                f"Действующих дневных доступов: {st['trial_active']}\n"
                f"Истёкших дневных доступов: {st['trial_expired']}\n"
                f"Подписчиков с очередью: {st['pending_subscribers']}\n"
                "Данные пользователей и их Telegram ID не отображаются.",
                parse_mode="html",
            )
        elif cmd == "/status":
            st = store.stats()
            disabled = LIVE_HEALTH.get("disabled_kinds") or []
            live = LIVE_HEALTH["status"] if LIVE_ENABLED else "выключен"
            if "new" in disabled:
                live += " (new отключён на see.tg)"
            await event.respond(
                "<b>Gift Radar</b>\n"
                f"Уведомления: {'пауза' if state.data['paused'] else 'включены'}\n"
                f"Подписчиков: {st['active_users']}\n"
                f"Ожидают уведомления: {st['pending_subscribers']}\n"
                f"Всего NFT-событий в базе: {st['alerts']}\n"
                f"Сканер: {NFT_HEALTH['sweep_progress']}/{NFT_HEALTH['candidates']} коллекций\n"
                f"Последняя проверка: {NFT_HEALTH['last_scan_finished'] or 'нет'} UTC\n"
                f"see.tg: {html.escape(live)}\n"
                f"Из потока: {LIVE_COUNTERS['new_notified']}\n"
                f"На перепроверке: {pending_count(state.data)} NFT\n"
                f"Последняя отправка: {DELIVERY_STATS['last_sent'] or 'нет'} UTC\n"
                f"Ошибок отправки: {DELIVERY_STATS['errors']}\n"
                f"Последняя ошибка: {html.escape(DELIVERY_STATS['last_error'] or 'нет')}",
                parse_mode="html",
            )
        elif cmd in ("/pushall", "/flush"):
            if state.data.get("paused"):
                await event.respond("Сначала включи уведомления командой /resume.")
                return
            if cmd == "/pushall" and not TRACK_RARE_NFTS:
                await event.respond("NFT-сканер выключен.")
                return
            transfer_legacy_queue()
            await deliver_pending(bot)
            if cmd == "/pushall":
                collections, gifts = schedule_recovery(
                    state.data, RECOVERY_LOOKBACK, RECOVERY_MAX_TOTAL
                )
                for job in (state.data.get("recipient_reviews") or {}).values():
                    if isinstance(job, dict):
                        job["next_at"] = 0
                state.save()
                RECOVERY_WAKE.set()
                await event.respond(
                    f"Запущена перепроверка {gifts} NFT "
                    f"({collections} коллекций). Рассылка пробуждена. /status"
                )
            else:
                await event.respond(
                    "Рассылка пробуждена. Подписчиков с очередью: "
                    f"{store.delivery_backlog()}."
                )
        elif cmd == "/check":
            slug = argument.strip().split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            if not re.fullmatch(r"[A-Za-z0-9]+-[1-9][0-9]*", slug):
                await event.respond("Пример: /check SnakeBox-189137")
                return
            try:
                details = await user(
                    functions.payments.GetUniqueStarGiftRequest(slug=slug)
                )
                gift = details.gift
                history = original_participants(gift, details)
                recipient = recipient_from_history(history)
                seen = slug in (state.data.get("seen_nft_slugs") or [])
                await event.respond(
                    f"<b>{html.escape(slug)}</b>\n"
                    f"Получатель: {'есть' if recipient else 'не указан'}\n"
                    f"Ранее учтён: {'да' if seen else 'нет'}",
                    parse_mode="html", link_preview=False,
                )
            except FloodWaitError as exc:
                await event.respond(f"Telegram ограничил проверку на {exc.seconds} сек.")
            except Exception as exc:
                await event.respond(
                    "Не удалось проверить NFT: " + type(exc).__name__
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
                slug = html.escape(str(item["slug"]), quote=True)
                title = html.escape(str(item.get("title") or "NFT"))
                rows.append(
                    f'<a href="https://t.me/nft/{slug}">'
                    f'{title} #{html.escape(str(item.get("number") or "?"))}</a>'
                )
            await event.respond("\n".join(rows), parse_mode="html",
                                link_preview=False)
        elif cmd == "/pause":
            state.set_paused(True)
            await event.respond("Уведомления приостановлены, очередь сохранена.")
        elif cmd == "/resume":
            state.set_paused(False)
            RECOVERY_WAKE.set()
            await deliver_pending(bot)
            await event.respond("Уведомления снова включены.")
        elif cmd == "/testdelivery":
            await send_alert(
                bot, ADMIN_ID, "Тест доставки: бот успешно отвечает.",
                {"kind": "delivery_test"},
            )
        elif cmd in ("/activate", "/key", "/remove"):
            await event.respond(
                "Старая схема активации отключена. Доступны /invite, /links, "
                "/invite1d, /links1d и /stats."
            )

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
        bot_identity["username"] = me.username
        log.info("NFT Radar connected. Commercial admin mode enabled.")
        tasks = [asyncio.create_task(delivery_worker(bot)),
                 asyncio.create_task(monitor(user, bot))]
        if PAYMENT_BOT_TOKEN:
            tasks.append(asyncio.create_task(StarsMerchant(
                PAYMENT_BOT_TOKEN, store, me.username,
                STARS_PRICE_1D, STARS_PRICE_LIFETIME,
            ).run()))
        if LIVE_ENABLED:
            tasks.append(asyncio.create_task(
                run_stream(lambda event: live_market_event(event, user),
                           LIVE_API_TOKEN, LIVE_COLLECTIONS or None)
            ))
        DELIVERY_WAKE.set()  # Flush persisted unsent NFT alerts on startup.
        if TRACK_RARE_NFTS:
            tasks.append(asyncio.create_task(monitor_rare_nfts(user, bot)))
            tasks.append(asyncio.create_task(retry_new_recipient_details(user, bot)))
            tasks.append(asyncio.create_task(recovery_worker(user, bot)))
            RECOVERY_WAKE.set()  # Resume any interrupted /pushall after restart.
        await asyncio.gather(*tasks)
    finally:
        await bot.disconnect()
        await user.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nРадар остановлен")
