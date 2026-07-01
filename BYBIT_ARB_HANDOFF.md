# Bybit P2P — исследование арбитража и план перехода
*Создан: 2026-07-01*

---

## Контекст: откуда идея

Binance P2P и Bybit P2P торгуют USDT/IDR с разными структурами ликвидности.
Снимок 2026-07-01 ~15:00 WIB:

| Направление     | Binance P2P (Bank) | Bybit P2P (all pay) |
|-----------------|-------------------|---------------------|
| Купить USDT (лучший ask) | **17,970** IDR | **17,970** IDR |
| Продать USDT (лучший bid) | **17,962** IDR | **17,951** IDR |
| Спред внутри платформы | **8 IDR** (0.045%) | **19 IDR** (0.106%) |
| Комиссия (round-trip) | **0.2%** ≈ 36 IDR | **0%** (P2P бесплатно) |
| Чистый P&L маркет-мейкера | 8 − 36 = **−28 IDR/USDT** | 19 − 0 = **+19 IDR/USDT** |

**Вывод:** прямого кросс-платформенного арбитража (купил на A, продал на B) сейчас нет —
цены слишком близкие. Но структурная разница есть:
- **Bybit P2P выгоднее для маркет-мейкера**: нулевая комиссия + более широкий спред
- Binance P2P невыгоден при спреде < 36 IDR (порог безубытка по комиссиям)

---

## Текущий статус аккаунтов

### Binance P2P
- Никнейм: **bandurkas**
- Сейчас: SELL-заявка на 17,993 IDR (ранг ~65/120), рынок ниже безубытка (~17,960)
- Решение: `~/Projects/Check` — FastAPI :8088, CDP-доступ через Opera :9222
- Авто-ворота: оба цикла (auto-reprice + order-watch) ждут market > 17,993

### Bybit P2P
- Никнейм: **Serra** (userId 333699577, KYC Indonesia)
- API Key: `LspYT75nMe4VNJEyMh`
- API Secret: `FfGLcthN6m1Qsafie1Uckvm2QWocFK0u4CMY`
- Сейчас: BUY-заявка на 17,975 IDR, **ранг #3** из 160 (1 ордер, 100% rate)
- Конкуренты на 17,975: ClickToChange (8782 ord), EZ-CHANGER (1795 ord) → Serra ниже по репутации
- Чтобы стать #1 — поднять цену на 1 IDR до **17,976**

---

## Ключевое архитектурное отличие Bybit от Binance

| | Binance P2P | Bybit P2P |
|---|---|---|
| Публичный стакан | CDP через Opera (Cloudflare блокирует прямые запросы) | **Прямой HTTP — работает без браузера** |
| Управление заявками | CDP (авторизованный браузер) | **Bybit REST API + HMAC-подпись** |
| Ордера / история | CDP (приватная страница) | **Bybit API (нужны правильные permissions)** |
| Ответ с VPS3 | Да (Binance не блокирует VPS3) | **Да** (проверено: `api2.bybit.com` + `api.bybit.com` работают) |

**Bybit значительно проще в реализации**: нет нужды в CDP, Opera, Playwright.
Всё через чистый HTTP с HMAC-подписью.

---

## Как работает Bybit P2P API (проверено)

### Публичный стакан — без авторизации
```
POST https://api2.bybit.com/fiat/otc/item/online
{
  "tokenId": "USDT",
  "currencyId": "IDR",
  "payment": [],
  "side": "0",      // 0 = BUY ads (sorted DESC by price, highest bid first)
                    // 1 = SELL ads (sorted ASC by price, lowest ask first)
  "size": "20",
  "page": "1",
  "amount": ""
}
```

Поля в ответе: `nickName`, `price`, `lastQuantity`, `minAmount`, `maxAmount`,
`orderNum`, `finishNum`, `recentOrderNum`, `recentExecuteRate`, `payments`

### Авторизованные запросы — HMAC-SHA256
```python
import hmac, hashlib, time, urllib.request, urllib.parse, json

API_KEY    = "LspYT75nMe4VNJEyMh"
API_SECRET = "FfGLcthN6m1Qsafie1Uckvm2QWocFK0u4CMY"

ts = str(int(time.time() * 1000))
recv_window = "5000"
qs = urllib.parse.urlencode(params)  # или "" для body-запросов
sign_str = ts + API_KEY + recv_window + qs
sig = hmac.new(API_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()

headers = {
    "X-BAPI-API-KEY": API_KEY,
    "X-BAPI-SIGN": sig,
    "X-BAPI-SIGN-TYPE": "2",
    "X-BAPI-TIMESTAMP": ts,
    "X-BAPI-RECV-WINDOW": recv_window,
}
```

Проверено: `GET /v5/user/query-api` → 200 OK, аккаунт Serra.

### Текущие permissions API-ключа
- ContractTrade, Spot, Options, Derivatives: ✅
- Wallet: ❌ (нет доступа к балансам funding/spot)
- FiatP2P: ❌ (нет — нужно добавить для управления P2P заявками)

**Для Check-Bybit нужно пересоздать ключ с FiatP2P permission.**

---

## Задачи новой сессии

### Шаг 1: Исследование — проверить арб-гипотезу (30 мин)
- [ ] Собрать данные обеих платформ за ~2 часа (каждые 60с): buy/sell prices, spreads
- [ ] Посчитать реальную среднюю разницу и её волатильность
- [ ] Понять: есть ли моменты когда Bybit buy > Binance sell (реальный арб)?
- [ ] Оценить минимальный размер позиции и время перевода между платформами

### Шаг 2: Быстрый Bybit мониторинг (MVP, 1-2 ч)
- [ ] Добавить Bybit-виджет в существующий Check-дашборд (отдельный блок)
- [ ] Показывать: Bybit best bid / best ask / spread / ваша заявка Serra ранг+цена
- [ ] Всё через прямой HTTP с VPS3 или локально (без CDP)

### Шаг 3: Bybit Check (полная версия, отдельный проект)
Архитектура аналогична `~/Projects/Check` но без CDP:
```
MarketWatcher   → прямой HTTP к api2.bybit.com/fiat/otc/item/online
AdRepricer      → HMAC-подписанные POST к Bybit P2P API (нужен FiatP2P permission)
OrderWatcher    → HMAC-подписанные GET к Bybit P2P order history
PnLTracker      → тот же алгоритм FIFO (комиссия = 0%)
Dashboard       → тот же FastAPI + HTML (порт отличный от 8088)
```

---

## Ключевые отличия логики для Bybit

1. **Комиссия = 0%** → ROUND_TRIP_FEE_PCT = 0 → breakeven = avg_buy_cost (без надбавки)
2. **Сортировка BUY ads**: Bybit возвращает DESC (высшая цена = ранг 1) — так же как Binance
3. **Ранжирование при равной цене**: по репутации аккаунта (orderNum, rate) — нужно учитывать
4. **API для заявок**: нужно изучить `/v5/p2p/item/list` и `/v5/p2p/item/update` (или аналоги на api2.bybit.com)
5. **Нет CDP**: всё упрощается — нет Opera, нет Playwright, нет FocusGuard

---

## Текущая ситуация торговли

### Binance (SELL режим)
- Средняя цена покупки: **17,957 IDR/USDT** (2,214.76 USDT)
- Безубыток: **17,993 IDR** (с учётом 0.2% Binance fee)
- Текущая заявка: 17,993 IDR, рынок ~17,960 → ниже безубытка
- Система: авто-ворота активны, ждём market > 17,993

### Bybit (BUY режим, Serra)
- Заявка: 17,975 IDR, ранг #3, 1 ордер, qty ~9,913 USDT
- Рынок: best ask 17,970, best bid 17,951
- Чтобы стать #1: поднять до 17,976 IDR

---

## Файлы проекта

| Файл | Описание |
|------|----------|
| `~/Projects/Check/backend/main.py` | FastAPI бэкенд (~570 строк) |
| `~/Projects/Check/backend/market_watcher.py` | CDP-стакан Binance + price_history |
| `~/Projects/Check/backend/advisor.py` | Логика рекомендаций + STRINGS (ru/en) |
| `~/Projects/Check/backend/order_watcher.py` | CDP-мониторинг ордеров |
| `~/Projects/Check/backend/ad_repricer.py` | CDP-смена цены в Binance P2P |
| `~/Projects/Check/backend/pnl_tracker.py` | FIFO P&L с Binance комиссией |
| `~/Projects/Check/backend/static/index.html` | Дашборд (~1050 строк) |
| `~/Projects/Check/ADVISOR_DESIGN.md` | Дизайн логики advisor'а |
| `~/Projects/Check/SESSION_HANDOFF.md` | Хэндофф предыдущей сессии |
