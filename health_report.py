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



def health_issues(nft, live, state, *, scanner_enabled=True, live_enabled=True):
    """Return only actionable source problems, never 'no new NFTs'."""
    if state.get("paused"):
        return []
    def stale(value, maximum):
        if not value:
            return False
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() > maximum
        except (ValueError, TypeError):
            return False

    issues = []
    if scanner_enabled:
        if nft.get("last_catalog") and stale(nft.get("last_catalog"), 1200):
            issues.append("Каталог Telegram не обновлялся больше 20 минут")
        if nft.get("last_catalog") and not nft.get("candidates"):
            issues.append("NFT-сканер не нашёл ни одной коллекции")
        if (nft.get("candidates") and nft.get("last_success")
                and stale(nft.get("last_success"), 1200)):
            issues.append("NFT-сканер не делал успешных проверок больше 20 минут")
    if live_enabled:
        if live.get("status") != "подключено":
            issues.append("see.tg не подключён")
        if "new" in (live.get("disabled_kinds") or []):
            issues.append("see.tg отключил поток новых NFT")
        if ("stats" in (live.get("subscribed_kinds") or [])
                and live.get("last_stats")
                and stale(live.get("last_stats"), 180)):
            issues.append("see.tg перестал присылать heartbeat")
    return issues

def diagnostic_text(nft, live, counters, delivery, state, subscribers, *,
                    scanner_enabled=True, live_enabled=True):
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
    issues = health_issues(nft, live, state, scanner_enabled=scanner_enabled,
                           live_enabled=live_enabled)
    if issues:
        rows.extend(["", "<b>⚠️ Что требует внимания</b>"])
        rows.extend("• " + safe(issue) for issue in issues)
    return "\n".join(rows)
