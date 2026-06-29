# P2P USDT/IDR помощник

Минимальная v1: дашборд с живым стаканом Binance P2P (USDT/IDR, Bank Transfer),
рекомендуемой ценой для твоих ad'ов и P&L по истории сделок. Без авто-выставления
заявок и без мерчант-API — это всё ручное, бот только подсказывает.

Подробный контекст и архитектура: `~/.claude/plans/tidy-dreaming-backus.md`.

## Запуск

1. Открыть Opera, перезапустить с remote debugging:
   ```
   /Applications/Opera.app/Contents/MacOS/Opera --remote-debugging-port=9222 \
     --user-data-dir="$HOME/Library/Application Support/com.operasoftware.Opera"
   ```
2. В Opera (с включённым VPN) открыть и быть залогиненным на `p2p.binance.com`.
3. Backend:
   ```
   cd backend
   source venv/bin/activate
   cp .env.example .env   # заполнить BINANCE_API_KEY/SECRET (обычный read-only ключ)
   uvicorn main:app --host 0.0.0.0 --port 8088
   ```
4. Открыть `http://<IP-Mac-в-локальной-сети>:8088/` с телефона.

## Как это работает

- `market_watcher.py` — подключается к уже открытой вкладке Binance в Opera через
  Chrome DevTools Protocol и читает публичный стакан из её авторизованного контекста
  (Cloudflare не блокирует, т.к. сессию уже прошёл человек).
- `pnl_tracker.py` / `pnl_remote.py` — официальный эндпоинт
  `GET /sapi/v1/c2c/orderMatch/listUserOrderHistory`. Выполняется на VPS3
  (на Mac прямые TLS-запросы к api.binance.com блокируются на уровне сети),
  backend дёргает его по SSH раз в минуту.
- `main.py` — FastAPI, отдаёт `/api/orderbook`, `/api/pnl` и сам дашборд (`static/index.html`).

## Известные ограничения v1

- Поиск объявлений по рынку через подписанный API недоступен без Merchant-статуса
  (проверено: `/sapi/v1/c2c/ads/search` → 401/500 с обычным ключом).
- Авто-выставление/перевыставление цены не реализовано — посты в приложении делаются руками.
- Требует, чтобы Opera с открытой вкладкой Binance P2P и VPN были запущены весь день.
