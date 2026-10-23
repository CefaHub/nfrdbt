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


## October 2026 fix for "unknown_517014..." and 0 NFT scans

A normal gift ID is not an NFT collection slug. The scanner now ignores gifts
without `upgrade_stars`, uses a documented April 2026 fallback gift ID/name
catalogue for older Telethon responses, validates the actual `gift_id` returned
by Telegram before following a collection, and reports *unresolved* upgraded
gifts without creating fake `unknown_<id>` requests.

After redeploying from `main`, send `/diagnose`. A healthy catalogue query
alone does not mean NFT tracking works; check `Успешно` in `/diagnose`.

To verify real NFT API access independently of scanning, send
`/check PlushPepe`. It must respond with the collection's minted counter.
The direct check does not alter your last-observed counter, and it does not
mean the latest gift is rare.

On the default `COLLECTIONS_PER_CYCLE=5` and `NFT_SCAN_INTERVAL=20`,
checking 120 collections takes roughly 8 minutes, *plus request time*. Set
`GIFT_COLLECTIONS` to a handful of real collection slugs if you want to
prioritize a smaller watchlist. Raising poll frequency may trigger Telegram
rate limits. This cannot guarantee immediate or exhaustive upgrade detection.

## Mint notifications (all vs. rare only)

By default `NOTIFY_ALL_NFTS=true` causes the scanner to notify the paired user about **every new, non-crafted NFT discovered after the collection baseline**. A rarity of 1% is not required.

The paired owner may switch the behavior without redeploying:
- `/all on` — notify for each new NFT, including common models.
- `/all off` — notify only when the model chance is <= 1% or rarity tier Epic/Legendary.
- `/status`, `/diagnose` and `/last` — check active mode and events.

Both modes show the model, background and pattern with the rarity reported by Telegram when available. The command `/test` is a delivery smoke test. Telegram may still delay or rate-limit the underlying scan, and old NFT numbers are **not** backfilled by turning `/all on`.

An `NFT NEW: ...` entry means the issuing counter increased; it does not imply the model matches the rare threshold. New code logs `NFT UPGRADE` for each non-crafted NFT it inspects and reports whether it was queued. A `queued=true` event is sent only when notifications are unpaused and the bot has a paired owner.


## Contact NFT owners and propose a Stars offer

Each newly detected NFT notification now includes:

- The current owner as returned by \`payments.getUniqueStarGift\`.
- A **"Написать владельцу"** button only if Telegram supplied the
  owner's **public user @username**.
- A **"Открыть NFT / оффер"** button to inspect the gift and use Telegram's
  built-in official buy-offer flow when available.
- \`offer_min_stars\` if Telegram published the minimum official offer.
- The command \`/offer COLLECTION-NUMBER AMOUNT\` to prepare a message.

Example:

\`\`\`
/offer MirageLamp-94843 1500
\`\`\`

The command fetches the **current owner** again (the original NFT owner may
have transferred or sold it since the alert) and prepares a buyer message that
can be copied into a direct chat. It never sends unsolicited private messages.

**Privacy:** Some NFTs have no visible owner username; others are owned by
channels or TON wallets. In those cases the bot does not guess a username,
scrape hidden details or create fake \`tg://user\` links. You may still open the
gift page and check whether the Telegram in-app "Make offer" action is available.
A publicly visible username may also cease to work if the owner changes it.

**Important:** An offer's visibility/eligibility, Stars price, owner identity,
gift transfer restrictions and subsequent resale rules should always be
rechecked inside Telegram before committing money. Neither a received link nor
an official offer guarantees instant resale eligibility.
