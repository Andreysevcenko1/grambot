# grambot — монитор новостей GRAM / TON для Telegram

Бот собирает новости о **TON / GRAM / Telegram**, склеивает одинаковые истории
из разных изданий, оценивает вероятное влияние на цену и присылает в Telegram
**информационные сигналы** — не прогнозы и не рекомендации.

```
🔴 Возможное негативное влияние
TON Foundation сообщила о сбое в сети

Сила: высокая
Источники: 3 (Cointelegraph, Decrypt, The Block)
Причина: outage, TON Foundation

TON: $1,405 · −4,2% за 20 мин · −6,1% за 24ч
Объём 24ч: $63,2 млн (×2,1 к вчера)

🔗 Оригинал · 23.09 20:41
ℹ️ Информационный сигнал, не финансовая рекомендация.
```

## Как это работает

```
RSS / Google News / Telegram-каналы (через RSSHub)
        │  параллельная загрузка, таймауты, издатель как источник
        ▼
Фильтр свежести (≤ 24ч) → фильтр ключевых слов (TON, GRAM, Telegram… по границам слов)
        ▼
Кластеризация заголовков (одно событие = один кластер, даже при разных формулировках)
        ▼
Проверка: ≥ 2 независимых источника ИЛИ официальный канал (TRUSTED_SOURCES)
        ▼
Классификация: правила EN/RU (бесплатно) или LLM (любой OpenAI-совместимый API)
   → позитив / негатив / нейтрально / неясно, сила low / medium / high, причина
        ▼
Антиспам: один сигнал на событие, лимит в час, /mute, cooldown ценовых алертов
        ▼
Telegram: сигнал + контекст цены (CoinGecko → Binance → Bybit → OKX)
```

Дополнительно:

- **Ценовые алерты** — резкое движение (по умолчанию ≥ 5 % за 20 мин) без
  новостей тоже приходит отдельным сообщением.
- **Обратная проверка сигналов** — бот запоминает цену в момент каждого
  сигнала и через 1 ч / 24 ч; `/stats` показывает, как часто направление
  совпадало. Это статистика, а не доказательство причинности.
- **Команды в Telegram**: `/status`, `/price`, `/recent [N]`, `/stats`,
  `/feeds`, `/check`, `/test`, `/mute [мин]`, `/unmute`, `/help`.
  Отвечает только чату из `TELEGRAM_CHAT_ID`.
- Устойчивость: любой упавший источник, провайдер цены или ошибка Telegram
  не останавливают цикл; корректное завершение по Ctrl+C / SIGTERM; авто-
  очистка базы (`RETENTION_DAYS`).

## Быстрый старт (macOS / Linux)

```bash
git clone https://github.com/Andreysevcenko1/grambot.git && cd grambot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # вписать TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID
.venv/bin/python main.py --once --no-telegram   # проверка: ленты, цена, ничего не отправляется
.venv/bin/python main.py      # рабочий режим
```

1. Токен: напишите [@BotFather](https://t.me/BotFather) → `/newbot`.
2. `TELEGRAM_CHAT_ID`: отправьте боту любое сообщение, затем откройте
   `https://api.telegram.org/bot<TOKEN>/getUpdates` и возьмите `chat.id`.
3. Отправьте боту `/test` — придёт образец сигнала; `/status` — состояние.

Все настройки — в `.env` (описаны в `.env.example`). Пустое значение =
встроенный дефолт.

## Запуск 24/7

### Вариант A — на Mac (launchd)

```bash
./scripts/install_launchd.sh      # автозапуск при входе, рестарт при падении, лог в grambot.log
./scripts/uninstall_launchd.sh    # удалить
```

Mac должен быть включён и не спать (Настройки → Экран блокировки / Энергия,
или `caffeinate`). Не запускайте одновременно `main.py` вручную — будут двойные
уведомления и конфликт `getUpdates`.

### Вариант B — VPS / сервер (Docker)

```bash
docker compose up -d --build      # бот
docker compose logs -f grambot    # логи
docker compose --profile rsshub up -d   # + собственный RSSHub для Telegram-каналов
```

При своём RSSHub укажите в `.env`
`RSS_FEEDS=...,http://rsshub:1200/telegram/channel/tonblockchain,...`.

## Источники по умолчанию

| Источник | Что даёт |
|---|---|
| Google News RSS (запрос TON/Toncoin/GRAM/The Open Network) | сотни изданий; каждое считается отдельным источником |
| Cointelegraph, Decrypt, CryptoSlate, The Block | профильные крипто-СМИ |
| Telegram-каналы `tonblockchain`, `tonstatus`, `durov`, `telegram` через RSSHub | официальные объявления; считаются подтверждёнными сразу (`TRUSTED_SOURCES`) |

Публичный RSSHub (`rsshub.rssforever.com`) может отваливаться — для надёжности
поднимите свой (`--profile rsshub`). Через RSSHub так же подключаются X/Twitter
и Reddit (`/twitter/user/<name>`, `/reddit/subreddit/<name>`), если инстанс
настроен с нужными ключами.

## LLM-классификация (необязательно)

Задайте `OPENAI_API_KEY` (и при желании `OPENAI_BASE_URL` / `OPENAI_MODEL` —
подходит любой OpenAI-совместимый API). Модель возвращает тональность, силу и
короткую причину; при любой ошибке бот сам откатывается на правила, поэтому
уведомления не теряются. Без ключа работает бесплатный словарь EN/RU с учётом
процентов движения, сумм и крупных имён (Binance, SEC, TON Foundation…).

## Разработка

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Структура: `grambot/sources` (RSS), `grambot/processing` (ключевые слова,
фильтры, кластеризация, классификаторы), `grambot/price.py`, `grambot/storage.py`
(SQLite), `grambot/notifier.py`, `grambot/commands.py`, `grambot/app.py`
(цикл). CI гоняет тесты на Python 3.9 и 3.12 и собирает Docker-образ.

## Ограничения

- Оценка влияния — эвристика (или мнение LLM); она **не гарантирует** реакцию
  цены. Бот выдаёт только информационные сигналы.
- Скорость ограничена RSS: обычно минуты после публикации, не секунды.
- Кластеризация лексическая: сильно перефразированные заголовки одного события
  иногда считаются разными и ждут второго подтверждения каждый.
