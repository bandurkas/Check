# SESSION HANDOFF — 2026-07-01 (v2)

Read this at the start of the next session before touching anything.

---

## Project: ~/Projects/Check

Binance P2P USDT/IDR trading automation.
- FastAPI backend on **Mac**, port **:8088** (NOT VPS — runs locally via launchd)
- CDP through Opera browser (`:9222`) — all P2P reads/writes via Playwright
- VPS (`root@187.127.114.34`) only runs `pnl_remote.py` + `balance_remote.py` via SSH
- launchd service: `com.check.backend`
- **Restart**: `launchctl stop com.check.backend && sleep 2 && launchctl start com.check.backend`
- **Logs**: `~/Projects/Check/scripts/backend.log` / `backend.err.log`

---

## Current State (end of session 2026-07-01 v2)

### In-memory state (resets on backend restart)
| Setting | Value at session end |
|---|---|
| `active_side_state["override"]` | unknown — resets on restart |
| `bank_balance_state["available_idr"]` | unknown — resets on restart |
| `repricer.enabled` | False (auto-reprice OFF by default) |
| `order_watch_state["enabled"]` | True |

### Action needed at next session start
1. `POST /api/active-side {"override": "BUY"}` — restore BUY override after restart
2. Enter real bank balance in dashboard → Save (actual value ~40M IDR)
3. Confirm BUY ad is online in Binance P2P

---

## What was done this session

### 1. LLM Advisor — `backend/advisor_llm.py` (COMPLETED ✓)

Created `advisor_llm.py` — Claude Haiku-4.5 powered advisor with rule-based fallback.

**Function**: `compute_advice_llm(**kwargs, lang)` — identical signature to `compute_advice()` in `advisor.py`.

**Key implementation details**:
- Model: `claude-haiku-4-5-20251001`
- `load_dotenv()` at module level (backend/ is CWD)
- System prompt encodes all trading constants + priority-ordered decision rules
- User message = JSON payload of all 15 market state fields
- Strips markdown code fences from LLM response (model wraps JSON in ```json ... ``` despite instructions)
- Validates required keys: `{state, side, recommended_price, advice, reasons}`
- Applies cost-basis floor protection AFTER LLM response (same logic as `advisor.py` wrapper)
- Falls back to rule-based `compute_advice()` on any error — silently logs the error

**main.py import change** (line 12–13):
```python
from advisor import TREND_AGAINST_IDR_PER_MIN, WIDE_SPREAD_PCT, ROUND_TRIP_FEE_PCT
from advisor_llm import compute_advice_llm as compute_advice
```
The 3 call sites in main.py are unchanged — they call `compute_advice(lang=lang, **inputs)`.

**Known**: LLM calls add ~0.5-2s latency per advice request. `/api/advice` is polled every 10s, `/api/agent` every 12s — latency is acceptable but noticeable.

### 2. Faster order detection — `main.py` (COMPLETED ✓)
- `ORDER_WATCH_INTERVAL_SECONDS`: 30 → **5** seconds
- Detection cycle: ~10-13s total (5s sleep + 5-8s CDP poll)
- Worst case: 13s from order arrival to UI notification (was 30-42s)

### 3. Frontend refresh fix — `static/index.html` (COMPLETED ✓)
- `refreshPnl` / `refreshEarnings` / `refreshWorkSession`: 90s → **30s** (matches backend poll)
- **Buttons moved ABOVE steps** in agent card — now always visible without scrolling

### 4. "Robot is silent" root causes (PREVIOUS SESSION — already done)
- `PNL_REFRESH_SECONDS`: 90 → 30 (backend PnL SSH poll)
- `TRADE_EVENT_TTL`: 180 → 600 (10 min visibility for trade notification)
- Web Audio beep on `trade_done` / `accept_payment` transition

---

## Known Issues / Deferred

1. **In-memory override resets on restart** — `active_side_state["override"]` not persisted. Fix: write to JSON state file, load at startup. (DEFERRED)

2. **Bank balance resets on restart** — same. Workaround: enter in dashboard after restart. (DEFERRED)

3. **Bank balance BUY lock** — user wants: when bank IDR balance is below a target, the system should lock to BUY mode and not switch to SELL. Not implemented yet. (PENDING)
   - Idea: add `bank_target_idr` field; if `bank_balance < target`, force `active_side = BUY` regardless of debt state
   - This would replace the manual `POST /api/active-side {"override": "BUY"}`

4. **LLM latency** — each `/api/advice` or `/api/agent` call makes a Haiku API call (~0.5-2s). Under heavy load this could slow the UI. Mitigation: add a short cache (e.g. reuse result for 8s before calling again). (DEFERRED)

5. **Order Watch CDP conflicts** — with `ORDER_WATCH_INTERVAL_SECONDS = 5`, the order watcher polls much more frequently. If auto-reprice is also active, CDP tab contention may increase. Monitor for issues.

---

## Architecture: Files

| File | Purpose |
|---|---|
| `backend/main.py` | FastAPI app, all endpoints, agent logic |
| `backend/advisor.py` | Rule-based advice engine (fallback, unchanged) |
| `backend/advisor_llm.py` | **LLM advisor (NEW)** — replaces rule-based in hot path |
| `backend/pnl_tracker.py` | FIFO P&L accounting |
| `backend/market_watcher.py` | Opera CDP orderbook scraper |
| `backend/order_watcher.py` | Pending order detection via CDP |
| `backend/ad_repricer.py` | Playwright-based ad price editor |
| `backend/static/index.html` | Dashboard frontend |
| `root@187.127.114.34:/root/pnl_remote.py` | VPS-side Binance API poller |
| `root@187.127.114.34:/root/balance_remote.py` | VPS-side Binance balance fetcher |

---

## Constants (all in `advisor.py` / `advisor_llm.py`)

| Constant | Value | Meaning |
|---|---|---|
| `FEE_PCT_PER_LEG` | 0.1% | Binance commission per completed order |
| `ROUND_TRIP_FEE_PCT` | 0.20% | Total fee (buy + sell legs) |
| `TIGHT_SPREAD_PCT` | 0.25% | Below this → barely past breakeven |
| `WIDE_SPREAD_PCT` | 0.40% | Above this → genuine margin, can wait |
| `TREND_AGAINST_IDR_PER_MIN` | 2.0 | Adverse trend threshold → act now |
| `MARKET_FAST_IDR_PER_MIN` | 3.0 | Fast market → halve patience |
| `DEBT_STALE_MIN` | 30 | Open debt stale after this |
| `NO_FILL_MIN` | 20 | No fill this long → market not biting |
| `RANK_OK_MAX` | 5 | Must be in top 5 to be visible |

---

## Floor Protection (CRITICAL)

`cost_for_floor = own_max_cost_idr` — NOT avg. Floor = `cost_for_floor * 1.002`.
Reason: FIFO means the most expensive lot can be matched first. Using avg allows selling
an expensive lot below its individual breakeven. This burned real money on 2026-06-30.
Both `advisor.py` and `advisor_llm.py` enforce this. `advisor_llm.py` applies it AFTER
the LLM response (so the LLM can be wrong, but the floor always protects).
