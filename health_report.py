"""Pure admin-only health report. Empty NFT hours are not by themselves a failure."""
from __future__ import annotations
from datetime import datetime, timezone
from html import escape


def age(value):
    if not value:
        return "нет данных"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        sec = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
        return f"{sec // 60} мин назад" if sec < 3600 else f"{sec // 3600} ч {sec % 3600 // 60} мин назад"
    except (ValueError, TypeError):
        return "нет данных"


def diagnostic_text(nft, live, counters, delivery, state, subscribers):
    safe = lambda value: escape(str(value if value is not None else "нет"))
    rows = [
        "<b>🩺 NFT Gift Radar — диагностика</b>",
        f"Пауза: {'да' if state.get('paused') else 'нет'}",
        "",
        "<b>Сканер Telegram</b>",
        f"Каталог обновлялся: {age(nft.get('last_catalog'))}",
        f"Коллекций отслеживается: {nft.get('candidates', 0)}",
        f"Успешная NFT-проверка: {age(nft.get('last_success'))}",
        f"Полных обходов: {nft.get('sweeps_completed', 0)}",
        f"Последняя ошибка: {safe(nft.get('last_error') or 'нет')}",
        f"Без данных о получателе: {nft.get('filtered_no_recipient', 0)}",
        f"Ожидают перепроверки: {len(state.get('recipient_reviews') or {})}",
        "",
        "<b>see.tg (поток NFT)</b>",
        f"Подключение: {safe(live.get('status'))}",
        f"Подписки: {safe(', '.join(live.get('subscribed_kinds') or []))}",
        f"Отключённые типы: {safe(', '.join(live.get('disabled_kinds') or []))}",
        f"Heartbeat see.tg: {age(live.get('last_stats'))} (всего: {live.get('stats_seen', 0)})",
        f"Последнее NFT-событие: {age(live.get('last_event'))}",
        f"Всего событий: {live.get('events_seen', 0)}",
        f"Подтверждённых новых NFT: {counters.get('new_notified', 0)}",
        f"Пропуск по лимиту: {counters.get('new_skipped_limit', 0)}",
        f"Отсеяно без получателя: {counters.get('filtered_no_recipient', 0)}",
        f"Ошибка live: {safe(live.get('last_error') or live.get('last_notice') or 'нет')}",
        "",
        "<b>Отправка</b>",
        f"Активных подписок: {subscribers}",
        f"Последняя отправка: {age(delivery.get('last_sent'))}",
        f"Ошибок отправки: {delivery.get('errors', 0)}",
        f"Последний отправленный NFT: {safe(delivery.get('last_slug') or 'нет')}",
        "",
        "Для теста доставки: /testdelivery",
        "Час без новых NFT не доказывает сбой.",
    ]
    return "\n".join(rows)
