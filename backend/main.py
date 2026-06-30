import asyncio
import json
import subprocess
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from advisor import TREND_AGAINST_IDR_PER_MIN, compute_advice
from market_watcher import MarketWatcher
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
# Manual input - how much IDR is actually sitting in the bank account for paying
# out BUY-side trades. Not visible via any Binance API, has to be told to us.
# Caps the BUY ad limit recommendation; SELL limit is capped by USDT balance instead.
bank_balance_state = {"available_idr": 7_000_000.0, "updated_at": time.time()}
# Every time the user tells us a *new* bank balance (a top-up), that's them
# opening a fresh working session - distinct from pnl_tracker's trade-gap
# session detection. Just a marker log, nothing derived from it automatically.
work_sessions = [{"started_at": bank_balance_state["updated_at"],
                   "opening_balance_idr": bank_balance_state["available_idr"]}]


@app.on_event("startup")
async def startup():
    asyncio.create_task(watcher.run_forever())
    asyncio.create_task(watcher.run_my_ad_forever())
    asyncio.create_task(pnl_refresh_loop())
    asyncio.create_task(balance_refresh_loop())


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


@app.get("/api/balance")
async def balance():
    return balance_cache


@app.get("/api/bank-balance")
async def get_bank_balance():
    return {**bank_balance_state, "session_started_at": work_sessions[-1]["started_at"]}


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
    return {**bank_balance_state, "session_started_at": work_sessions[-1]["started_at"]}


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


@app.get("/api/advice")
async def advice(lang: str = "ru"):
    now = time.time()

    if debt_state["side"] == "BUY":
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
    AGGRESSIVE_EXTRA_IDR = 5  # beyond bare top, when speed beats the last few IDR of spread
    velocity_side = "buy" if active_side == "SELL" else "sell"
    if active_side == "SELL":
        baseline_price = rec.get("sell_usdt_at")
        aggressive_price = snap.robust_buy_price - AGGRESSIVE_EXTRA_IDR if snap.robust_buy_price is not None else None
    else:
        baseline_price = rec.get("buy_usdt_at")
        aggressive_price = snap.robust_sell_price + AGGRESSIVE_EXTRA_IDR if snap.robust_sell_price is not None else None
    market_trend = watcher.price_trend_idr_per_min(velocity_side)
    market_speed = abs(market_trend) if market_trend is not None else None

    result = compute_advice(
        active_side=active_side,
        baseline_price=baseline_price,
        aggressive_price=aggressive_price,
        my_ad=my_ad,
        my_ad_age_min=my_ad_age_min,
        spread_pct=snap.spread_pct,
        debt_amount=debt_state["amount"],
        debt_age_min=debt_age_min,
        minutes_since_fill=minutes_since_fill,
        market_speed_idr_per_min=market_speed,
        market_trend_idr_per_min=market_trend,
        lang=lang,
    )
    result["debt_age_min"] = debt_age_min
    result["my_ad_age_min"] = my_ad_age_min
    result["minutes_since_fill"] = minutes_since_fill
    result["market_speed_idr_per_min"] = market_speed
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
