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
3. **Classify** — a rule-based classifier (`grambot/processing/classifier.py`)
   estimates sentiment (positive/negative/neutral/unknown) and signal
   strength (low/medium/high) from keyword patterns. It's a small, swappable
   interface so an LLM-backed classifier can replace it later.
4. **Cluster & verify** — near-duplicate headlines within a time window are
   grouped (`grambot/processing/clustering.py`); a signal is only sent once
   it's corroborated by `MIN_SOURCES_FOR_VERIFIED` independent sources,
   otherwise it's treated as an unverified rumor and suppressed.
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

## Tests

```bash
pytest
```

## Extending

- Add more feeds (Telegram/X RSS bridges, exchange blogs, GitHub release
  feeds, regulatory news) to `RSS_FEEDS`.
- Swap `RuleBasedClassifier` for an LLM-backed classifier implementing the
  same `Classifier` protocol in `grambot/processing/classifier.py`.
- Add on-chain metrics and historical signal backtesting as described in the
  original design notes.
