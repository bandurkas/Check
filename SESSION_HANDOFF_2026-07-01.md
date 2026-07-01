# SESSION HANDOFF — 2026-07-01

Read this at the start of the next session before touching anything.

---

## Project: ~/Projects/Check

Binance P2P USDT/IDR trading automation.
- FastAPI backend on Mac, port **:8088**
- CDP through Opera browser (`:9222`) — all P2P reads/writes go via Playwright here
- VPS (`root@187.127.114.34`) runs `pnl_remote.py` and `balance_remote.py` via SSH
- launchd service: `com.check.backend` (NOT `local.check-backend`)

---

## Current State (end of session 2026-07-01)

### In-memory state (resets on backend restart)
| Setting | Value at session end |
|---|---|
| `active_side_state["override"]` | **BUY** (manually set via API) |
| `bank_balance_state["available_idr"]` | **7,000,000** (WRONG — real is ~40M) |
| `repricer.enabled` | False (auto-reprice OFF) |
| `order_watch_state["enabled"]` | True |

### Action needed at next session start
1. `POST /api/active-side {"override": "BUY"}` — restore BUY override after any restart
2. Enter real bank balance in dashboard → Save (currently defaults to 7M, real is ~40M IDR)
3. Confirm BUY ad is online in Binance P2P at price suggested by `/api/advice`

---

## What was done this session

### 1. Dashboard full rewrite (COMPLETED)
- Removed all `reveal`/`rise` CSS animations that caused visual gap between sections
- Replaced `.panel + .reveal` with `.card` pattern
- Agent card gets explicit amber border: `border-color: rgba(255,179,0,.25)`
- File: `backend/static/index.html`

### 2. `trade_event` — trade detection system (COMPLETED)
Added to `main.py`:
- **State variable** (`trade_event` dict, lines 30–31): fires when `total_trades` count increases
- **Detection** in `pnl_refresh_loop` (lines 191–202): compares old/new trade count; captures side, amount, price
- **Priority #2** in `_build_instruction` (lines 672–690): surfaces as `trade_done` action with Done button
- **Acknowledgement** in `agent_done` (lines 815–816): clears `trade_event["acknowledged"]` WITHOUT touching override

Key behavior:
- TTL = 180s — card disappears if not acknowledged within 3 min
- Pressing Done triggers `_force_pnl_refresh()` for immediate P&L update
- Does NOT auto-clear side override (user manages that manually)

### 3. Bug fix: override auto-clear on trade_done (COMPLETED)
First version erroneously set `active_side_state["override"] = None` on trade_done acknowledgement.
This caused adviser to flip to SELL while user was still in BUY accumulation mode.
Fix: removed that line from `agent_done`. Override is now purely manual.

---

## PRIMARY PENDING TASK: LLM Integration

**Status: NOT STARTED — interrupted by live trade event mid-session**

### What the user wants
Replace rule engine in `advisor.py` with **Claude API** call.
User selected option: **"Замена advisor.py"** — Claude analyzes full market context and returns structured JSON advice (not just a complement/overlay).

### Design (agreed before interruption)
1. Create `backend/advisor_llm.py`
2. Add `ANTHROPIC_API_KEY` to `.env`
3. Add `anthropic` to `requirements.txt`
4. Modify `main.py` to call LLM advisor, with fallback to rule-based `advisor.py`
5. The `/api/advice` and `/api/agent` endpoint contracts stay identical — only the internals change

### What LLM advisor receives (from `_advice_inputs()`)
```python
{
    "active_side": "BUY" | "SELL",
    "baseline_price": float,       # rank-1 or rank-5 depending on spread
    "aggressive_price": float,
    "my_ad": {...},                # rank, price, adv_no, or None
    "my_ad_age_min": float,
    "spread_pct": float,
    "debt_amount": float,
    "debt_age_min": float,
    "minutes_since_fill": float,
    "market_speed_idr_per_min": float,
    "market_trend_idr_per_min": float,  # signed; positive = rising
    "own_avg_cost_idr": float,
    "own_max_cost_idr": float,          # MOST EXPENSIVE open lot — floor is based on THIS
    "own_open_usdt": float,
}
```

### What LLM advisor must return (same schema as `compute_advice()`)
```python
{
    "state": "push" | "trend" | "debt" | "tight" | "wide" | "neutral" | "floor",
    "side": "BUY" | "SELL",
    "recommended_price": float | None,
    "advice": str,       # human-readable summary
    "reasons": [str],    # list of reasons
}
```

### Critical constraints the LLM must respect
- **NEVER recommend selling below breakeven**: `breakeven = own_max_cost_idr * 1.002`
  (rule-based `compute_advice()` enforces this as a wrapper; LLM must also respect it)
- `ROUND_TRIP_FEE_PCT = 0.20%` — both buy + sell legs cost 0.1% each
- `TIGHT_SPREAD_PCT = 0.25%`, `WIDE_SPREAD_PCT = 0.40%`
- Trend against: `>2.0 IDR/min` — act urgently regardless of spread/debt age
- Fast market: `>3.0 IDR/min` — halve patience thresholds

### Implementation approach
Use `anthropic` SDK, `claude-haiku-4-5-20251001` for speed/cost.
System prompt encodes the trading rules. User message is JSON market state.
Response parsed as JSON with Pydantic validation. Fallback to rule-based on any error.

---

## Architecture notes

### Floor protection (CRITICAL — learned the hard way)
`cost_for_floor = own_max_cost_idr` (not avg) — see `advisor.py` line 138.
Reason: under FIFO, the most expensive lot can be matched first. Avg cost of 17,993
allowed selling a 17,969 lot at 17,993 which was still a net loss after fees.
The LLM implementation must replicate this max-lot floor.

### Auto-reprice safety gates (`main.py` lines 244–250)
Auto-reprice only fires when ALL of:
- `repricer.enabled` is True
- `order_watch_state["enabled"]` is True
- PnL cache is fresh (< 300s stale)
- Market data is fresh (< 30s stale)
- No pending orders (`order_watcher.has_pending()` is False)

### Session detection
Two separate mechanisms:
1. **pnl_tracker**: gap > 3h between trades = new session
2. **work_sessions**: new entry created every time bank balance is saved with a new value

---

## Files

| File | Purpose |
|---|---|
| `backend/main.py` | FastAPI app, all endpoints, agent logic |
| `backend/advisor.py` | Rule-based advice engine (to be replaced by LLM) |
| `backend/pnl_tracker.py` | FIFO P&L accounting, Binance API polling |
| `backend/market_watcher.py` | Opera CDP orderbook scraper |
| `backend/order_watcher.py` | Pending order detection |
| `backend/ad_repricer.py` | Playwright-based ad price editor |
| `backend/static/index.html` | Dashboard frontend |
| `root@187.127.114.34:/root/pnl_remote.py` | VPS-side Binance API poller |
| `root@187.127.114.34:/root/balance_remote.py` | VPS-side Binance balance fetcher |

---

## Known issues / deferred

1. **In-memory override resets on restart** — `active_side_state["override"]` is not persisted to disk. Every restart requires manual `POST /api/active-side`. Fix: write to a small JSON state file on disk, load at startup.

2. **Bank balance resets on restart** — same issue. Workaround: enter in dashboard after every restart.

3. **VPS3 unreliable on manual restarts** — running `launchctl stop com.check.backend` then `start` is the safe sequence. Never kill the process directly.
