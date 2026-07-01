import asyncio
import json
import subprocess
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from ad_repricer import AdRepricer
from advisor import TREND_AGAINST_IDR_PER_MIN, WIDE_SPREAD_PCT, ROUND_TRIP_FEE_PCT, compute_advice
from market_watcher import MarketWatcher
from order_watcher import OrderWatcher
from pnl_tracker import PnLTracker

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

watcher = MarketWatcher(poll_interval=4.0)

VPS_HOST = "root@187.127.114.34"
PNL_REFRESH_SECONDS = 90
BALANCE_REFRESH_SECONDS = 60
DEBT_THRESHOLD_USDT = 1.0  # below this, "open debt" is just rounding noise
pnl_cache = {"summary": None, "cycles": [], "updated_at": 0, "error": None}
balance_cache = {"spot_usdt": None, "funding_usdt": None, "total_usdt": None, "updated_at": 0, "error": None}
# Tracks when the current open rebuy/resell debt first appeared, so the advisor
# can tell a 5-minute gap from a 3-hour one (ADVISOR_DESIGN.md item 3).
debt_state = {"side": None, "amount": 0.0, "started_at": None}
# Manual override for which side the advisor focuses on. The auto-detected
# side (below) is a lifetime FIFO net-flow heuristic ("bought more than sold
# => you have spare to sell") - it has no way to know the user is mid-way
# through a deliberate accumulation target (e.g. "still need to buy $765
# more") and isn't done buying yet, so it can flip to SELL prematurely.
# None = auto-detect (default); "BUY"/"SELL" = pinned by the user.
active_side_state = {"override": None}
# Manual input - how much IDR is actually sitting in the bank account for paying
# out BUY-side trades. Not visible via any Binance API, has to be told to us.
# Caps the BUY ad limit recommendation; SELL limit is capped by USDT balance instead.
bank_balance_state = {"available_idr": 7_000_000.0, "updated_at": time.time()}
# Every time the user tells us a *new* bank balance (a top-up), that's them
# opening a fresh working session - distinct from pnl_tracker's trade-gap
# session detection. Just a marker log, nothing derived from it automatically.
work_sessions = [{"started_at": bank_balance_state["updated_at"],
                   "opening_balance_idr": bank_balance_state["available_idr"]}]

AUTO_REPRICE_INTERVAL_SECONDS = 60  # slower cadence on request 2026-07-01 - less tab churn, still responsive enough
AUTO_REPRICE_STATES = ("push", "trend", "debt", "tight", "neutral", "floor")  # states with an actionable recommended_price
repricer = AdRepricer()  # kill switch off by default - /api/auto-reprice POST to enable

ORDER_WATCH_INTERVAL_SECONDS = 30  # orders have a 15-min payment window, this still gives ~14min worst-case lead
order_watcher = OrderWatcher()
# Separate kill switch from order_watcher itself - lets main.py stop opening
# CDP tabs for this entirely (2026-07-01: the combination of this loop +
# auto-reprice opening tabs back to back looked like the browser flailing,
# even though each individual action was fine - paused on request).
order_watch_state = {"enabled": True}


@app.on_event("startup")
async def startup():
    asyncio.create_task(watcher.run_forever())
    asyncio.create_task(watcher.run_my_ad_forever())
    asyncio.create_task(pnl_refresh_loop())
    asyncio.create_task(balance_refresh_loop())
    asyncio.create_task(auto_reprice_loop())
    asyncio.create_task(order_watch_loop())


async def order_watch_loop():
    while True:
        if order_watch_state["enabled"]:
            await order_watcher.poll()
        await asyncio.sleep(ORDER_WATCH_INTERVAL_SECONDS)


@app.get("/api/order-watch")
async def get_order_watch():
    return order_watch_state


@app.post("/api/order-watch")
async def set_order_watch(request: Request):
    body = await request.json()
    order_watch_state["enabled"] = bool(body.get("enabled", False))
    return order_watch_state


@app.get("/api/pending-orders")
async def pending_orders():
    """Frontend polls this fast (faster than the main dashboard refresh) and
    plays an alert sound on anything new - a taken order gives the paying
    side 15 minutes, so this needs to surface well before that, not on the
    next slow refresh cycle."""
    return {
        "new_orders": order_watcher.drain_new_orders(),
        "pending_count": len(order_watcher.seen_order_numbers),
        "last_poll_at": order_watcher.last_poll_at,
        "error": order_watcher.last_error,
    }


def _update_debt_state(summary):
    gap = summary["open_sell_remainder_usdt"]
    surplus = summary["open_buy_surplus_usdt"]
    if gap > DEBT_THRESHOLD_USDT:
        side, amount = "BUY", gap  # sold more than bought back -> need to rebuy
    elif surplus > DEBT_THRESHOLD_USDT:
        side, amount = "SELL", surplus  # bought more than sold -> have spare to sell
    else:
        side, amount = None, 0.0

    if side != debt_state["side"]:
        debt_state["side"] = side
        debt_state["started_at"] = time.time() if side else None
    debt_state["amount"] = amount


def _compute_session_stats(session_started_at):
    """Realized P&L (IDR) + open position for trades since a work session
    started - same time-respecting FIFO as pnl_tracker.summary(), just scoped
    to this session's cycles instead of full history."""
    cycles = pnl_cache.get("cycles") or []
    cutoff_ms = session_started_at * 1000
    events = sorted((c for c in cycles if c["time"] >= cutoff_ms), key=lambda c: c["time"])
    realized_pnl, matched, _unmatched, open_lots = PnLTracker._fifo_match(events)
    open_usdt, open_avg_cost = PnLTracker._open_position(open_lots)
    return {
        "matched_cycles": matched,
        "realized_pnl_idr": round(realized_pnl, 2),
        "open_usdt": open_usdt,
        "open_avg_cost_idr": open_avg_cost,
    }


async def balance_refresh_loop():
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", VPS_HOST, "cd /root && python3 balance_remote.py",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                data = json.loads(stdout.decode())
                balance_cache.update(data)
                balance_cache["updated_at"] = time.time()
                balance_cache["error"] = None
            else:
                balance_cache["error"] = stderr.decode()[-500:]
        except Exception as e:
            balance_cache["error"] = str(e)
        await asyncio.sleep(BALANCE_REFRESH_SECONDS)


async def pnl_refresh_loop():
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", VPS_HOST, "cd /root && python3 pnl_remote.py",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                data = json.loads(stdout.decode())
                pnl_cache["summary"] = data["summary"]
                pnl_cache["cycles"] = data["cycles"]
                pnl_cache["updated_at"] = time.time()
                pnl_cache["error"] = None
                _update_debt_state(data["summary"])
            else:
                pnl_cache["error"] = stderr.decode()[-500:]
        except Exception as e:
            pnl_cache["error"] = str(e)
        await asyncio.sleep(PNL_REFRESH_SECONDS)


async def auto_reprice_loop():
    """Off by default (repricer.enabled). When on, mirrors exactly what the
    advisor card already tells the user to do by hand - only fires on the
    same actionable states/recommended_price the dashboard shows, never on
    its own separate judgment. AdRepricer enforces the actual safety bounds
    (max delta, cooldown); this loop just decides *whether* to call it.

    Two extra gates beyond repricer.enabled, both added 2026-07-01 after
    review found has_pending() alone wasn't enough:
    - order_watch_state["enabled"]: has_pending() only reflects reality while
      something is actively polling for it. If order-watch is off, its last
      reading is frozen and could be silently stale in either direction -
      treat "not currently checking" as "can't confirm it's safe", not as
      "assume nothing pending".
    - pnl_cache["summary"] is not None: the cost-basis floor (advisor.py)
      needs real P&L data to protect a SELL price; right after a backend
      restart there's a gap before the first VPS poll succeeds where this
      data is None and the floor is a silent no-op. Don't reprice blind."""
    while True:
        try:
            pnl_ready = pnl_cache["summary"] is not None
            if repricer.enabled and order_watch_state["enabled"] and pnl_ready and not order_watcher.has_pending():
                inputs = _advice_inputs()
                result = compute_advice(lang="ru", **inputs)
                my_ad = inputs["my_ad"]
                rec_price = result.get("recommended_price")
                if (
                    result["state"] in AUTO_REPRICE_STATES
                    and my_ad
                    and my_ad.get("adv_no")
                    and rec_price is not None
                    and abs(rec_price - my_ad["price"]) >= 1
                ):
                    ok, msg = await repricer.reprice(
                        my_ad["adv_no"], my_ad["price"], rec_price, is_pending_fn=order_watcher.has_pending,
                    )
                    print(f"[auto_reprice] state={result['state']} ok={ok}: {msg}")
        except Exception as e:
            print(f"[auto_reprice] loop error: {e}")
        await asyncio.sleep(AUTO_REPRICE_INTERVAL_SECONDS)


@app.get("/api/price-history")
async def price_history():
    rows = list(watcher._price_history)
    pnl_summary = pnl_cache.get("summary") or {}
    own_avg_cost = pnl_summary.get("open_tracked_avg_cost_idr")
    own_open_usdt = pnl_summary.get("open_tracked_usdt")
    has_position = own_open_usdt is not None and own_open_usdt > 1
    breakeven = (own_avg_cost * (1 + ROUND_TRIP_FEE_PCT / 100)
                 if has_position and own_avg_cost else None)
    return {
        "points": [{"t": r[0], "buy": r[1], "sell": r[2]} for r in rows],
        "breakeven": breakeven,
    }


@app.get("/api/orderbook")
async def orderbook():
    snap = watcher.latest
    return {
        "timestamp": snap.timestamp,
        "best_buy_price": snap.best_buy_price,
        "best_sell_price": snap.best_sell_price,
        "spread_idr": snap.spread_idr,
        "spread_pct": snap.spread_pct,
        "recommended": snap.recommended_prices(),
        "buy_ads": snap.buy_ads[:10],
        "sell_ads": snap.sell_ads[:10],
        "my_sell_ad": watcher.my_sell_ad,
        "my_buy_ad": watcher.my_buy_ad,
        "my_ads_updated_at": watcher.my_ads_updated_at,
    }


@app.get("/api/pnl")
async def pnl():
    """pnl_cache["cycles"] comes from the VPS (annotated_cycles()), which only
    knows trade history - it can't price open BUY lots since the live market
    feed lives here, on the Mac, via the Opera CDP watcher. So mark-to-market
    each still-open lot (open_usdt > 0) at serve time instead of caching it,
    since the price moves every poll."""
    cycles = pnl_cache.get("cycles") or []
    current_price = watcher.latest.robust_sell_price
    enriched = []
    for c in cycles:
        row = dict(c)
        open_usdt = row.get("open_usdt") or 0.0
        if open_usdt > 1e-9 and current_price is not None:
            row["unrealized_profit_idr"] = round(open_usdt * (current_price - row["unit_price"]), 2)
        else:
            row["unrealized_profit_idr"] = None
        enriched.append(row)
    return {**pnl_cache, "cycles": enriched}


@app.get("/api/active-side")
async def get_active_side():
    return {"override": active_side_state["override"], "auto_detected": debt_state["side"]}


@app.post("/api/active-side")
async def set_active_side(request: Request):
    body = await request.json()
    side = body.get("override")
    if side not in (None, "BUY", "SELL"):
        return {"error": "override must be null, 'BUY', or 'SELL'"}
    active_side_state["override"] = side
    return {"override": active_side_state["override"]}


@app.get("/api/auto-reprice")
async def get_auto_reprice():
    return {
        "enabled": repricer.enabled,
        "max_delta_idr": repricer.max_delta_idr,
        "cooldown_sec": repricer.cooldown_sec,
        "last_result": repricer.last_result,
        # Mirrors the gates auto_reprice_loop actually checks, so the dashboard
        # can say *why* "enabled" isn't currently doing anything instead of
        # looking like it's silently broken (code review finding, 2026-07-01).
        "order_watch_enabled": order_watch_state["enabled"],
        "pnl_ready": pnl_cache["summary"] is not None,
        "has_pending_order": order_watcher.has_pending(),
    }


@app.post("/api/auto-reprice")
async def set_auto_reprice(request: Request):
    body = await request.json()
    repricer.enabled = bool(body.get("enabled", False))
    return {"enabled": repricer.enabled}


@app.get("/api/balance")
async def balance():
    return balance_cache


LIMIT_BUFFER_IDR = 1_000_000.0  # headroom kept below the full balance/inventory value


def _suggested_limits():
    """Min/max ad limits to set by hand on Binance (editing an ad's limits
    isn't automated - only price is, via ad_repricer). Surfaced on the
    dashboard instead of being recalculated by hand in chat every session
    (2026-06-30 handoff item: 'BUY limits should always be suggested when
    we open a session and Save the bank balance'). BUY is capped by IDR
    cash on hand; SELL is capped by USDT actually held, valued at the
    current realistic sell price - same convention /api/earnings uses."""
    bank_idr = bank_balance_state["available_idr"]
    buy_max = max(0.0, bank_idr - LIMIT_BUFFER_IDR)
    buy_min = min(1_000_000.0, buy_max)

    usdt = balance_cache.get("total_usdt")
    price = watcher.latest.robust_sell_price
    sell_value_idr = usdt * price if usdt is not None and price is not None else None
    sell_max = max(0.0, sell_value_idr - LIMIT_BUFFER_IDR) if sell_value_idr is not None else None
    sell_min = min(1_000_000.0, sell_max) if sell_max is not None else None

    return {
        "buy_min_idr": buy_min, "buy_max_idr": buy_max,
        "sell_min_idr": sell_min, "sell_max_idr": sell_max,
    }


@app.get("/api/bank-balance")
async def get_bank_balance():
    return {**bank_balance_state, "session_started_at": work_sessions[-1]["started_at"],
            "suggested_limits": _suggested_limits()}


@app.post("/api/bank-balance")
async def set_bank_balance(request: Request):
    body = await request.json()
    amount = float(body["available_idr"])
    is_new_session = amount != bank_balance_state["available_idr"]
    bank_balance_state["available_idr"] = amount
    bank_balance_state["updated_at"] = time.time()
    if is_new_session:
        # Fix the just-ended session's numbers in place before opening the new one.
        prev = work_sessions[-1]
        prev["closed_at"] = bank_balance_state["updated_at"]
        prev.update(_compute_session_stats(prev["started_at"]))
        work_sessions.append({"started_at": bank_balance_state["updated_at"], "opening_balance_idr": amount})
    return {**bank_balance_state, "session_started_at": work_sessions[-1]["started_at"],
            "suggested_limits": _suggested_limits()}


@app.get("/api/work-sessions")
async def get_work_sessions():
    sessions = []
    for i, s in enumerate(work_sessions):
        if i == len(work_sessions) - 1:
            stats = _compute_session_stats(s["started_at"])
            stats["closed_at"] = None
        else:
            stats = {k: s.get(k) for k in ("matched_cycles", "realized_pnl_idr", "open_usdt", "open_avg_cost_idr", "closed_at")}
        sessions.append({"started_at": s["started_at"], "opening_balance_idr": s["opening_balance_idr"], **stats})
    return {"sessions": sessions, "current": sessions[-1]}


@app.get("/api/earnings")
async def earnings():
    """$/hour framing per ADVISOR_DESIGN.md point 5 - realized profit from the
    current session plus a mark-to-market estimate of whatever's still open,
    divided by hours actually spent on it. Not lifetime P&L (that's /api/pnl) -
    this is "was the last stretch of attention worth it"."""
    summary = pnl_cache.get("summary")
    if not summary:
        return {"error": "no pnl data yet"}

    snap = watcher.latest
    current_price = snap.robust_sell_price  # realistic price if you sold right now

    open_usdt = summary.get("session_open_usdt") or 0.0
    open_avg_cost = summary.get("session_open_avg_cost_idr")
    unrealized_idr = None
    if open_usdt > 0 and open_avg_cost is not None and current_price is not None:
        unrealized_idr = open_usdt * (current_price - open_avg_cost)

    session_start_ms = summary.get("session_start_ms")
    hours_elapsed = (time.time() * 1000 - session_start_ms) / 3_600_000 if session_start_ms else None

    session_realized = summary.get("session_realized_pnl_idr") or 0.0
    total_potential_idr = session_realized + (unrealized_idr or 0.0)

    idr_per_hour = total_potential_idr / hours_elapsed if hours_elapsed and hours_elapsed > 0.05 else None
    usd_per_hour = idr_per_hour / current_price if idr_per_hour is not None and current_price else None

    return {
        "lifetime_realized_pnl_idr": summary.get("realized_pnl_idr"),
        "lifetime_matched_cycles": summary.get("matched_cycles"),
        "session_start_ms": session_start_ms,
        "session_hours_elapsed": hours_elapsed,
        "session_matched_cycles": summary.get("session_matched_cycles"),
        "session_realized_pnl_idr": session_realized,
        "session_open_usdt": open_usdt,
        "session_open_avg_cost_idr": open_avg_cost,
        "current_sell_price_idr": current_price,
        "unrealized_pnl_idr": unrealized_idr,
        "total_potential_idr": total_potential_idr,
        "idr_per_hour": idr_per_hour,
        "usd_per_hour": usd_per_hour,
    }


def _advice_inputs():
    """Shared by /api/advice and the auto-reprice loop, so both branch on
    exactly the same active-side/price-target logic - the loop must never
    decide based on stale or differently-derived numbers than what the
    dashboard is showing."""
    now = time.time()

    if active_side_state["override"] in ("BUY", "SELL"):
        active_side = active_side_state["override"]
        my_ad = watcher.my_buy_ad if active_side == "BUY" else watcher.my_sell_ad
        my_ad_changed_at = watcher.my_buy_ad_changed_at if active_side == "BUY" else watcher.my_sell_ad_changed_at
    elif debt_state["side"] == "BUY":
        active_side, my_ad, my_ad_changed_at = "BUY", watcher.my_buy_ad, watcher.my_buy_ad_changed_at
    elif debt_state["side"] == "SELL":
        active_side, my_ad, my_ad_changed_at = "SELL", watcher.my_sell_ad, watcher.my_sell_ad_changed_at
    else:
        # No open debt: fall back to raw balance, same rule the dashboard highlight uses.
        total_usdt = balance_cache.get("total_usdt")
        if total_usdt is not None and total_usdt >= 1:
            active_side, my_ad, my_ad_changed_at = "SELL", watcher.my_sell_ad, watcher.my_sell_ad_changed_at
        else:
            active_side, my_ad, my_ad_changed_at = "BUY", watcher.my_buy_ad, watcher.my_buy_ad_changed_at

    my_ad_age_min = (now - my_ad_changed_at) / 60 if my_ad_changed_at else None
    debt_age_min = (now - debt_state["started_at"]) / 60 if debt_state["started_at"] else None

    cycles = pnl_cache.get("cycles") or []
    minutes_since_fill = (now * 1000 - max(c["time"] for c in cycles)) / 60000 if cycles else None

    snap = watcher.latest
    rec = snap.recommended_prices()
    AGGRESSIVE_EXTRA_IDR = 5  # beyond rank-1, when speed beats the last few IDR of spread
    velocity_side = "buy" if active_side == "SELL" else "sell"

    pnl_summary = pnl_cache.get("summary") or {}
    own_avg_cost = pnl_summary.get("open_tracked_avg_cost_idr")
    own_open_usdt = pnl_summary.get("open_tracked_usdt")
    has_position = own_open_usdt is not None and own_open_usdt > 1
    # Minimum SELL price to recover cost + both legs of Binance's ~0.1% commission.
    # Used to decide whether rank-5 pricing is still profitable before committing to it.
    sell_breakeven = (own_avg_cost * (1 + ROUND_TRIP_FEE_PCT / 100)
                      if has_position and own_avg_cost else None)

    if active_side == "SELL":
        rank1_price = rec.get("sell_usdt_at")       # buy_ads[0] - 1 tick (cheapest ask, rank 1)
        rank5_price = snap.rank5_sell_price()        # buy_ads[4] (most expensive still in top-5)
        # Wide spread + rank-5 still above breakeven: capture extra margin by sitting at
        # rank 5 instead of racing to rank 1. Urgent states (trend, debt) use aggressive_price
        # instead of baseline anyway, so this only affects push/neutral/wide/tight branches.
        wide_spread = snap.spread_pct is not None and snap.spread_pct > WIDE_SPREAD_PCT
        rank5_profitable = rank5_price is not None and (sell_breakeven is None or rank5_price > sell_breakeven)
        if wide_spread and rank5_profitable:
            baseline_price = rank5_price
        else:
            baseline_price = rank1_price
        aggressive_price = (snap.robust_buy_price - AGGRESSIVE_EXTRA_IDR
                            if snap.robust_buy_price is not None else None)
    else:
        rank1_price = rec.get("buy_usdt_at")        # sell_ads[0] + 1 tick (highest bid, rank 1)
        rank5_price = snap.rank5_buy_price()         # sell_ads[4] (lowest bid still in top-5)
        # Wide spread: lower acquisition cost by posting at rank-5 price instead of
        # outbidding to rank 1. Lower buy price → lower future sell breakeven → more margin.
        wide_spread = snap.spread_pct is not None and snap.spread_pct > WIDE_SPREAD_PCT
        if wide_spread and rank5_price is not None:
            baseline_price = rank5_price
        else:
            baseline_price = rank1_price
        aggressive_price = (snap.robust_sell_price + AGGRESSIVE_EXTRA_IDR
                            if snap.robust_sell_price is not None else None)

    market_trend = watcher.price_trend_idr_per_min(velocity_side)
    market_speed = abs(market_trend) if market_trend is not None else None

    return {
        "active_side": active_side,
        "baseline_price": baseline_price,
        "aggressive_price": aggressive_price,
        "my_ad": my_ad,
        "my_ad_age_min": my_ad_age_min,
        "spread_pct": snap.spread_pct,
        "debt_amount": debt_state["amount"],
        "debt_age_min": debt_age_min,
        "minutes_since_fill": minutes_since_fill,
        "market_speed_idr_per_min": market_speed,
        "market_trend_idr_per_min": market_trend,
        # Cost-basis floor (advisor.py) - never recommend selling currently-held
        # inventory below what we actually paid for it.
        "own_avg_cost_idr": pnl_summary.get("open_tracked_avg_cost_idr"),
        "own_open_usdt": pnl_summary.get("open_tracked_usdt"),
    }


@app.get("/api/advice")
async def advice(lang: str = "ru"):
    inputs = _advice_inputs()
    active_side = inputs["active_side"]
    market_trend = inputs["market_trend_idr_per_min"]

    result = compute_advice(lang=lang, **inputs)
    result["debt_age_min"] = inputs["debt_age_min"]
    result["my_ad_age_min"] = inputs["my_ad_age_min"]
    result["minutes_since_fill"] = inputs["minutes_since_fill"]
    result["market_speed_idr_per_min"] = inputs["market_speed_idr_per_min"]
    result["market_trend_idr_per_min"] = market_trend
    # Language-neutral codes ("up"/"down"/"flat"/"unknown") - the frontend
    # translates these for display. Indicator is independent of which advice
    # branch fired above - "against you" should be visible even when the
    # advice itself is still "neutral" for other reasons.
    if market_trend is None:
        result["market_direction"] = "unknown"
        result["market_against"] = False
    elif active_side == "SELL":
        result["market_against"] = market_trend < -TREND_AGAINST_IDR_PER_MIN
        result["market_direction"] = "down" if market_trend < 0 else ("up" if market_trend > 0 else "flat")
    else:
        result["market_against"] = market_trend > TREND_AGAINST_IDR_PER_MIN
        result["market_direction"] = "up" if market_trend > 0 else ("down" if market_trend < 0 else "flat")
    return result


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/static", StaticFiles(directory="static"), name="static")
