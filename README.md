# grambot — GRAM/TON news monitor bot

A Telegram bot that watches RSS/Atom feeds (official blogs, and Telegram/X
RSS bridges), filters items relevant to GRAM/TON, classifies their likely
market impact, deduplicates/clusters similar reports across sources, checks
recent TON price/volume movement, and sends a Telegram notification for
signals that meet a minimum verification bar.

The bot produces **informational signals only** — never price predictions
or promises. Impact classification is a best-effort heuristic and can be
wrong; always verify via the linked source before acting.

## How it works

1. **Collect** — poll RSS/Atom feeds (`RSS_FEEDS`). This can include
   official blogs and Telegram-channel/X-account RSS bridges (e.g. RSSHub,
   Nitter), so no platform is scraped directly.
2. **Filter** — keep only items mentioning configured keywords (`KEYWORDS`,
   e.g. `TON, Toncoin, GRAM, Telegram`).
3. **Classify** — sentiment (positive/negative/neutral/unknown) and signal
   strength (low/medium/high) estimation. Uses a deterministic rule-based
   classifier by default (`grambot/processing/classifier.py`, no API key
   needed); if `OPENAI_API_KEY` is set, an LLM-backed classifier
   (`grambot/processing/llm_classifier.py`, any OpenAI-compatible endpoint)
   is used instead, with automatic fallback to the rule-based classifier on
   any API error.
4. **Cluster & verify** — near-duplicate headlines within a time window are
   grouped (`grambot/processing/clustering.py`) using both character-level
   similarity and significant-word overlap, so differently phrased reports
   of the same event from different outlets still match. A signal is only
   sent once it's corroborated by `MIN_SOURCES_FOR_VERIFIED` independent
   sources, otherwise it's treated as an unverified rumor and suppressed.
5. **Price context** — `grambot/price.py` polls CoinGecko for TON
   price/volume and computes the recent % move using locally stored
   history.
6. **Notify** — a formatted message (sentiment, strength, source count,
   price move, link to the original) is sent to a Telegram chat via the Bot
   API (`grambot/notifier.py`). Without a configured bot token, it logs the
   message instead of sending (dry-run mode).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, RSS_FEEDS, KEYWORDS, ...
```

Create a bot via [@BotFather](https://t.me/BotFather) to get
`TELEGRAM_BOT_TOKEN`, and set `TELEGRAM_CHAT_ID` to your user/group/channel
id where notifications should be posted.

## Run

```bash
python main.py
```

This starts an infinite poll loop: fetches news on `POLL_INTERVAL_SECONDS`,
polls TON price on `PRICE_POLL_INTERVAL_SECONDS`, and sends Telegram
notifications for verified, sufficiently strong signals.

### Run persistently in the background (macOS)

To keep the bot running after closing the terminal, and to have it restart
automatically on crash or login, install it as a launchd user agent:

```bash
./scripts/install_launchd.sh
```

Logs go to `grambot.log` in the project directory. Manage it with:

```bash
launchctl unload ~/Library/LaunchAgents/com.grambot.monitor.plist   # stop
launchctl load -w ~/Library/LaunchAgents/com.grambot.monitor.plist  # start
./scripts/uninstall_launchd.sh                                      # remove
```

## LLM-backed classification (optional)

By default the bot uses a free, deterministic keyword classifier. To use an
LLM for better judgement, set in `.env`:

```
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1   # or any OpenAI-compatible endpoint
OPENAI_MODEL=gpt-4o-mini
```

If the API key is missing, or any request fails, the bot automatically
falls back to the rule-based classifier — it never blocks notifications
because of an LLM outage.

## Telegram channel / X (Twitter) sources

RSS_FEEDS supports Telegram channels and X/Twitter accounts via RSS bridges
(RSSHub for Telegram, Nitter for X), which avoids scraping either platform
directly. The default feeds include a few TON-related Telegram channels via
a public RSSHub instance. Public bridge instances can be rate-limited or go
offline; for reliability, self-host your own:

```bash
docker run -d --name rsshub -p 1200:1200 diygod/rsshub
```

Then point feed URLs at `http://localhost:1200/telegram/channel/<name>` (or
`/twitter/user/<name>` if a Twitter route is enabled on your instance).

## Tests

```bash
pytest
```

## Extending

- Add on-chain metrics and historical signal backtesting as described in the
  original design notes.
- Add a message queue (e.g. Redis) if you scale to many more feeds/sources.

