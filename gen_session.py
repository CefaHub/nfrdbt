"""Locally generate Telethon StringSession for the spare account.
DO NOT share this session or upload it to public hosting/GitHub.
"""
from __future__ import annotations
import os
import sys
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.network.connection import ConnectionTcpFull, ConnectionTcpObfuscated

load_dotenv()
api_id = int(os.getenv("TELEGRAM_API_ID") or os.getenv("API_ID") or "17349")
api_hash = os.getenv("TELEGRAM_API_HASH") or os.getenv("API_HASH") or "344583e45741c457fe1862106095a5eb"
mode = os.getenv("TELETHON_CONNECTION_MODE", "full").strip().lower()
connections = {"full": ConnectionTcpFull, "obfuscated": ConnectionTcpObfuscated}
if mode not in connections:
    print("Неизвестный режим. Выбери full или obfuscated.")
    sys.exit(2)
print("Авторизуй ТОЛЬКО запасной Telegram-аккаунт.")
print("Публичные тестовые API-ключи могут НЕ работать для обычного входа.")
print("SESSION_STRING — пароль доступа к сессии. НЕ публикуй и НЕ пересылай.")
print(f"Режим соединения: {mode}")
try:
    with TelegramClient(
        StringSession(), api_id, api_hash,
        connection=connections[mode],
        timeout=12, connection_retries=2, retry_delay=1,
    ) as client:
        print("\nSESSION_STRING (вставить только в секреты Bothost):")
        print(client.session.save())
except (TimeoutError, OSError) as exc:
    print("\nСоединение с Telegram не установлено.")
    print("Запусти check_network_windows.bat, проверь сеть, а затем попробуй снова.")
    print(f"Тип ошибки: {type(exc).__name__}")
    sys.exit(1)
