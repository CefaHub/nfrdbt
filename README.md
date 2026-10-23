# Telegram Gift Radar — Bothost (Python / Telethon)

This repository contains a Telegram gift upgrade monitoring prototype designed for Bothost hosting. It uses a spare user's MTProto session for reading gift metadata and a separate bot for alerts.

## Setup

1. Create your bot using @BotFather.
2. On your Windows computer run `generate_session_windows.bat` (or `pip install -r requirements.txt` and `python gen_session.py`) and sign in **only to your spare account**.
3. Never commit or share the generated `SESSION_STRING`: it gives access to the authorized account. Add it in the secret environment settings of Bothost.
4. Import this repository in Bothost using Python and entrypoint `main.py`. Use `requirements.txt` for dependencies.
5. Configure the environment variables:
   - `SESSION_STRING` — the generated private session
   - `BOT_TOKEN` — the bot token (Bothost may provide it automatically)
   - `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — **prefer your own** credentials; the values in `.env.example` are public *test* keys and may be rejected
   - `CHECK_INTERVAL=60`, `NFT_SCAN_INTERVAL=60`, `DATA_DIR=/app/data`
   - `PAIR_CODE` — optional binding code; otherwise inspect hosting logs for it.
6. Start the bot. Send `/start CODE` from your primary account, then `/status` and `/test`; use `/diagnose` to inspect NFT collection discovery and errors.

## Limitations

The tool is an experimental polling prototype, not a complete realtime stream of all newly minted NFTs. Real-world Telegram API behavior, exact methods supported by the installed Telethon version, marketplace transfer restrictions and scan latency have **not** been validated. It cannot guarantee every upgrade or instant resale.

**Security:** Keep `.env`, `SESSION_STRING`, `BOT_TOKEN`, session files and `data/` off GitHub. These files are excluded by `.gitignore`.

## Windows connectivity troubleshooting

Run `check_network_windows.bat`. If it fails, try another network. If TCP connectivity works but the default connection times out, try `generate_session_obfuscated_windows.bat`. Public test credentials may still fail authorization.

## Further instructions

- https://bothost.ru/docs/telegram-userbot-setup
- https://bothost.ru/docs/environment-variables
- https://bothost.ru/docs/main-file-entrypoint

## Why am I not receiving upgrade notifications?

There are two independent checks:

- \`payments.getStarGifts\` checks whether the **upgrade option** has opened for a base gift (not whether someone upgraded it).
- The collectible watcher checks \`payments.getUniqueStarGift\`, reads its \`availability_issued\` count and fetches newly numbered gifts after establishing a baseline.

Use \`/diagnose\` in the bot. The output includes: candidate collections, successful NFT requests, error count, last error and backlog.

If there are 0 successful NFT requests, the watcher has **not** confirmed it can see newly minted NFTs. The new scanner tries public official resale listings to discover the true collection slug when the catalog title cannot be converted to a valid slug. This fallback works only when Telegram returns a suitable resale gift. You can optionally set \`GIFT_COLLECTIONS=PlushPepe,AnotherCollection\` with **real** collection slugs (not human titles). Known slugs only; don't invent names.

If the counter has an initial value but no new numbers, the bot won't send historical NFT notifications. Subsequent numbers are scanned if the count increases.

**Limits:** Telegram does not provide a public push feed of every upgrade. Request throttling, missing collections, batching, server errors and short bursts can delay or prevent detection. Market resale availability/transfer restrictions must be assessed independently. This bot neither automatically buys NFTs nor guarantees profit or instant delivery.
