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
   - A new single-use activation key is provided **privately** to the owner. Do not look for it in hosting logs. Its SHA-256 digest lives in `activation.py`.
6. For a fresh bot, send `/start ONE_TIME_KEY` from your own Telegram account. If it was already paired, use `/activate ONE_TIME_KEY` to consume the key without changing the owner. Check `/key` for status. Then `/status` and `/test`; use `/diagnose` for collection discovery.

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


## Direct owner chat/profile links (2026-10-24)

When `payments.getUniqueStarGift` exposes `owner_id: PeerUser`, the radar
includes the owner's Telegram numeric ID and a clickable profile link
even when that user has no public username.

- Public username: `https://t.me/username` — direct chat link.
- No username but an ID: `tg://user?id=123456789` — may open the profile
  only if the Telegram client and privacy/user-access checks permit.
- Owner is a channel / TON wallet / hidden: no invented private-chat link.

Run `/owner MirageLamp-94843` to refresh the current owner and get a
dedicated profile button. New NFT alerts include a profile link as well,
and `/offer MirageLamp-94843 1500` uses the same link.

**Warning:** Telegram officially says numeric ID mentions are not
guaranteed to resolve for unrelated users who never interacted with the
bot. Numeric IDs do not bypass privacy restrictions. A TON wallet address
is not a Telegram chat. Use the official NFT purchase offer when possible.
The bot never sends DMs to strangers automatically.

## Single-use activation key (2026-10-24)

The owner receives one **high-entropy** activation key separately, never
through GitHub or Bothost logs. Only its SHA-256 digest is committed in
`activation.py`. Do not share the actual key.

- First setup with no registered user: `/start ONE_TIME_KEY`.
- Already registered owner: `/activate ONE_TIME_KEY` (marks it consumed).
- `/key` shows whether it was used; only the authorized owner can run it.
- No replacement key is created during deploy, restart or update.
- `/app/data/state.json` stores `owner_id` and `activation_consumed`.
  Bothost's persistence docs confirm `/app/data` is kept across Git deploys.
- A different user cannot steal an already registered owner slot even if they
  possess the key. An already-consumed key never authenticates again.

Do not delete or overwrite `/app/data/state.json`: the one-use consumption
flag is recorded there. If hosting storage is deleted altogether, this local
flag disappears; redeploys that preserve the data volume do not reset it.

## Owner visibility and Telegram API limitations (2026-10-24)

`payments.getUniqueStarGift` may expose an exact owner ID, or only
`owner_name` (a **display name**, not necessarily a public username).
The bot now checks public usernames included in `users` and also tries to
resolve **only that disclosed ID** through Telethon's accessible entity cache.

If the gift gives **only a display name**, a public username may exist for
that person but **cannot be reliably linked** from the gifted NFT metadata.
The program does not infer identity from similar display names or attempt
to defeat privacy protections. On October 22, 2026, Telegram began removing
owner profile links from gift pages for some users, so this limitation can
affect previously visible owners. A missing link is not necessarily a bug.

The `/owner COLLECTION-NUMBER` command always refreshes the gift's
owner metadata. If a public owner username can be verified, it creates a
direct chat link. Otherwise it shows the owner's display name and/or a
conditional `tg://user?id=...` deep link when the numeric owner ID is
present. A TON wallet is not a Telegram account.


## Deal/watch commands for hidden gift owners

Since October 22, 2026, Telegram can return only owner_name for a collectible gift.
A display name alone never uniquely identifies a Telegram profile.

- `/deal SpicedWine-108832`: check visible owner link, offer eligibility,
  and current official resale prices; show a button opening the NFT.
- `/watch SpicedWine-108832`: save a persistent 3-minute monitor for this gift.
  Notify the registered user if the official Telegram API later exposes a
  public username, numeric owner ID, official purchase offer or resale price.
- `/watches`: see the monitored gifts (max 20).
- `/unwatch SpicedWine-108832`: stop monitoring the selected gift.

State lives in /app/data/state.json and survives Bothost code updates
as long as its persistent data volume isn't deleted. Configure
`DEAL_WATCH_INTERVAL=180` in Bothost env variables to adjust cadence
(minimum 60 seconds).

Polling cannot identify a hidden Telegram account from the owner's
display name. It doesn't send private messages or make a purchase.
An official offer without owner_id is not available through MTProto.
All monetary decisions remain with the user in the Telegram client.

## Notification delivery fix — October 24, 2026

NFT discovery and Telegram message sending are now independent tasks.
The scanner only appends new NFT events to the persistent delivery outbox;
a separate worker sends them at a controlled rate (about 1 every 1.5 s).

- No 100-event truncation: old notifications are retained until acknowledged.
- New and rare NFTs use the same reliable queue (`/all on` for all models).
- Telegram FloodWait and temporary network problems trigger automatic retries.
- If Telegram rejects HTML/buttons for an NFT, the worker sends a plain
  text fallback containing the NFT title and clickable NFT URL.
- An alert is removed from the queue only after a successful send.
- Delivery failures do not block collection scanning.
- On restart, previously unsent alerts are retried automatically.
- The one-time activation key, owner binding and previous baselines
  in `/app/data/state.json` are preserved.

Use `/delivery` to inspect the queue, last successful send, retry count,
and last error without having to restart the bot. `/status` also includes
the queue size and sender error. If a user blocked the bot, or the bot
loses permission to message them, Telegram may continue rejecting sends;
in that case `/delivery` shows the reason while events stay queued.

Telegram deliveries are at-least-once rather than mathematically exactly
once: if the network loses an acknowledgement after Telegram has already
sent a message, a retry can rarely duplicate it. The bot does not
silently discard that event.

Collection detection is still polling, not a Telegram push stream:
the time to scan all collections depends on `NFT_SCAN_INTERVAL` and
`COLLECTIONS_PER_CYCLE`. Default 5 collections per 20 seconds means
121 collections can take around 8 minutes per sweep, not including
extra NFT detail requests or Telegram rate limits. More aggressive
settings can incur FloodWait, so tune with live logs.


### Verify end-to-end delivery without waiting for a new NFT

After redeploying the bot on Bothost, send `/all on`, then `/testdelivery`.
The command inserts one artificial event at the front of the SAME persisted
outbox that handles real NFT alerts. A second, separate message saying
"ПРОВЕРКА ОЧЕРЕДИ УСПЕШНА" confirms the worker can send.

If that separate message does not arrive, use `/delivery`. It shows the
pending count, first queued event, Telegram delivery error, last send
and retry delay. Unlike the older `/test` command, this checks the actual
notification queue rather than merely replying to a Telegram command.

## Telegram NFT image card previews

Each newly discovered NFT alert now contains the original
`https://t.me/nft/Collection-Number` URL **as the first link in its message**.
The sender enables `link_preview=True` for `new_nft` and `rare_nft`
while keeping the existing buttons for gift / owner profile.
Even if a bad inline button forces a plain-text fallback, the NFT
link remains in the message and link previews are enabled.

To test a specific gift without waiting for a mint, send
`/preview PartySparkler-189558` in the authorized bot chat.
The bot sends a test message with that gift link, preview enabled,
and a button to open the gift. `/testdelivery` still validates
queue delivery, but does **not** test an NFT image preview.

Telegram generates and caches the visual card itself. The program
cannot guarantee that the recipient's Telegram client renders
the exact preview appearance every time, and bot-side settings
or Telegram previews being unavailable can affect rendering.

## October 24: one-minute NFT sweep pacing and clearer scan timestamps

These independent timestamps and settings must not be confused:

- `/delivery` **last send** means the last message actually sent to the owner.
  It can remain unchanged for hours if no new upgraded NFT was observed.
- `/status` and `/diagnose` **last NFT batch** mean the last collection-scan
  pass. `last successful NFT request` confirms metadata access.
- `CHECK_INTERVAL=60` polls the base **gift catalog** every minute.
- `NFT_SCAN_INTERVAL=60` (or optional `NFT_SWEEP_SECONDS=60`) now targets
  checking **all** known NFT collections within ~60 seconds, not waiting
  a full minute between batches of five.
- `COLLECTIONS_PER_CYCLE=5` is only a round-robin batch size. The delay
  between those batches is scaled to cover the whole collection set
  during the target sweep; `NFT_REQUEST_SPACING_SECONDS=0.40` paces calls.

With 121 collections, a five-item batch starts roughly every 2.5 seconds
when the Telegram API responds fast enough. The actual duration can exceed
the target if extra newly minted NFTs must be fetched, host/network latency
rises, or Telegram enforces rate limits (FloodWait). We cannot guarantee
one-minute detection or that every NFT becomes visible immediately.

Use `/status` to see sweep progress (x/121), last NFT batch and last full
sweep. The same progress is visible in `/delivery`, separate from last send.
`/diagnose` shows API successes and current collection coverage.
Telegram throttling pauses the scanner and is logged; it also no longer
skips the collection at which FloodWait occurred.

Persistent pairing, stored baseline NFT counters and unacknowledged
notification events are preserved under `/app/data/state.json`.

## Original gift participants (historical contact links)

The Telegram collectible API can expose a
`StarGiftAttributeOriginalDetails` attribute containing `sender_id` and
`recipient_id` for the gift before it was upgraded. The first recipient
is not necessarily the current NFT owner. The gift may have changed hands
multiple times; this historic evidence must never be labelled as current ownership.

- Run `/origin SpicedWine-108832` or `/origin PartySparkler-189558`.
- The bot creates separate buttons **Кто подарил (история)** and
  **Первый получатель** when Telegram includes the original participants.
- New NFT alerts show the same history links when the metadata is available.
- If a public username can be confirmed from Telegram, open
  `https://t.me/username`. If only a user ID is exposed, a
  `tg://user?id=...` link is offered but it may fail under Telegram privacy
  or entity-access rules.
- An anonymous original sender is not reconstructed or guessed.
- If the original details were removed or never retained, the bot
  explicitly reports that no original contacts are available.

This is a contact/history navigation aid, **not** a method to deanonymize
a currently hidden NFT holder. For verified market sales, open the
Telegram collectible's official sale/offer page. For additional
public transfer records, independent services such as see.tg may help;
their historical claims should be treated as unverified until cross-checked.

## Automatic public contact discovery (2026-10-24)

Every newly discovered NFT triggers automatic, bounded checks of three
separate *Telegram-provided* sources in parallel:

1. CURRENT OWNER: exact owner peer ID from the collectible metadata,
   with a public username if Telegram returns or resolves it.
2. ORIGINAL GIFT CONTACTS: the publicly exposed original sender and
   first recipient in StarGiftAttributeOriginalDetails, when retained.
3. HOSTED PROFILE: a public Telegram profile listed as host_id.
   This is a DISPLAY profile, not necessarily the NFT holder (especially
   when a TON wallet actually owns the gift).

Results appear with separate labels and buttons in new NFT alerts.
Run `/owner SpicedWine-108832` to execute this combined lookup for an
existing NFT: it now includes all available related public profiles
automatically, with a clear distinction between CURRENT and HISTORICAL.

The bot does NOT identify a private/hidden current owner by guessing from
an arbitrary display name. A historic sender or a public host cannot
be safely promoted to 'current owner' without present-day owner evidence.
Public discovery does not authorize unsolicited bulk DM messaging.

External source: see.tg documents a public gift/owner API, but using it
requires a separate see.tg app token and may incur TON costs. No external
API authorization or paid lookups are activated by this code update.
