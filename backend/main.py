import asyncio
import json
import math
import os
import subprocess
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from ad_repricer import AdRepricer
from advisor import TREND_AGAINST_IDR_PER_MIN, WIDE_SPREAD_PCT, ROUND_TRIP_FEE_PCT
from advisor import compute_advice as _rule_compute_advice
from advisor_llm import compute_advice_llm_async as _llm_compute_advice_async
from market_watcher import MarketWatcher
from order_watcher import OrderWatcher
from pnl_tracker import PnLTracker

# USE_LLM_ADVISOR turns on the async-safe LLM advisor. It uses AsyncAnthropic with
# a timeout (no event-loop blocking — the "18005 frozen" incident cause) and falls
# back to the deterministic rule-based advisor on any failure. When on it feeds:
#   - the DISPLAY endpoints (/api/advice, /api/agent) via compute_advice_display();
#   - the auto-reprice hot path, but ONLY when the market is above breakeven (our
#     zone of interest, where there's a real "sell higher" decision) — see
#     auto_reprice_loop. Below breakeven the deterministic advisor holds the floor
#     and no LLM request is spent. The independent breakeven guard in the loop is
#     the final safety net regardless of which advisor produced the price.
USE_LLM_ADVISOR = os.environ.get("USE_LLM_ADVISOR", "").lower() in ("1", "true", "yes")


async def compute_advice_display(*, lang="ru", **inputs):
    """Advice for the dashboard. Uses the async LLM advisor only inside our zone of
    interest (market above breakeven) — same gate as the auto-reprice hot path, so
    no LLM request is spent below breakeven where the answer is always the
    deterministic floor. Falls back to rule-based internally on any error."""
    if USE_LLM_ADVISOR and _market_above_breakeven():
        return await _llm_compute_advice_async(lang=lang, orderbook=_orderbook_for_llm(), **inputs)
    return _rule_compute_advice(lang=lang, **inputs)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

watcher = MarketWatcher(poll_interval=4.0)

VPS_HOST = "root@187.127.114.34"
PNL_REFRESH_SECONDS = 30
BALANCE_REFRESH_SECONDS = 60
DEBT_THRESHOLD_USDT = 1.0  # below this, "open debt" is just rounding noise
pnl_cache = {"summary": None, "cycles": [], "updated_at": 0, "error": None}
# Fires when pnl_refresh detects total_trades increased — lets the agent surface
# "you just made a trade" immediately, before the 90s loop catches up.
# acknowledged=True until a new trade is detected so the card stays quiet at rest.
trade_event = {"detected_at": None, "side": None, "amount": None, "price": None,
               "acknowledged": True, "last_seen_count": 0}
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
# "floor" intentionally excluded: when the market is below our breakeven the
# advisor holds the price at cost-basis and there is nothing for auto-reprice
# to do. Firing anyway just opens a CDP tab for no reason and keeps the
# browser busy while we're waiting for the market to recover.
AUTO_REPRICE_STATES = ("push", "trend", "debt", "tight", "neutral")
repricer = AdRepricer()  # kill switch off by default - /api/auto-reprice POST to enable

ORDER_WATCH_INTERVAL_SECONDS = 5  # fast detection: poll every 5s (poll itself takes 5-8s, so ~10-13s real cycle)
order_watcher = OrderWatcher()
order_watch_state = {"enabled": True}


def _market_above_breakeven() -> bool:
    """True when the market's best buy price is above our sell breakeven.
    Both auto_reprice_loop and order_watch_loop gate on this: while the market
    is below breakeven the ad sits at the floor price and is not competitive,
    so there are no orders to watch and no price to chase — opening CDP tabs
    is wasted browser activity. Returns True when there is no open position
    (nothing to protect) so the gate never blocks normal operation."""
    pnl_summary = pnl_cache.get("summary") or {}
    own_avg_cost = pnl_summary.get("open_tracked_avg_cost_idr")
    own_open_usdt = pnl_summary.get("open_tracked_usdt")
    if not (own_open_usdt and own_open_usdt > 1 and own_avg_cost):
        return True
    breakeven = own_avg_cost * (1 + ROUND_TRIP_FEE_PCT / 100)
    market_buy = watcher.latest.robust_buy_price
    return market_buy is not None and market_buy > breakeven


_ORDERBOOK_DEPTH = 20  # levels per side fed to the LLM (watcher fetches 20)


def _orderbook_for_llm() -> dict:
    """The live order book for the LLM to read when deciding a price. ask_ladder =
    sellers of USDT (ascending price; our competition on a SELL), bid_ladder =
    buyers of USDT (descending; the demand that fills a SELL). Each level carries
    the full watcher fields (price/available/limits/merchant) so the model can
    weigh depth, not just top-of-book."""
    snap = watcher.latest

    def _levels(ads):
        return [
            {"price": a["price"], "available": a.get("available"),
             "min_limit": a.get("min_limit"), "max_limit": a.get("max_limit"),
             "merchant": a.get("merchant")}
            for a in ads[:_ORDERBOOK_DEPTH]
        ]

    return {
        "timestamp": snap.timestamp,
        "ask_ladder": _levels(snap.buy_ads),
        "bid_ladder": _levels(snap.sell_ads),
        "best_ask": snap.best_buy_price,
        "best_bid": snap.best_sell_price,
        "robust_ask": snap.robust_buy_price,
        "robust_bid": snap.robust_sell_price,
        "spread_idr": snap.spread_idr,
        "spread_pct": snap.spread_pct,
    }


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
        if order_watch_state["enabled"] and _market_above_breakeven():
            await order_watcher.poll()
        await asyncio.sleep(ORDER_WATCH_INTERVAL_SECONDS)


@app.get("/api/order-watch")
async def get_order_watch():
    return {**order_watch_state, "market_above_breakeven": _market_above_breakeven()}


@app.post("/api/order-watch")
async def set_order_watch(request: Request):
    body = await request.json()
    order_watch_state["enabled"] = bool(body.get("enabled", False))
    return {**order_watch_state, "market_above_breakeven": _market_above_breakeven()}


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
                new_count = data["summary"].get("total_trades", 0)
                old_count = trade_event["last_seen_count"]
                if old_count > 0 and new_count > old_count:
                    # New trade(s) arrived — find the most recent one to surface
                    cycles = sorted(data.get("cycles", []), key=lambda c: c["time"], reverse=True)
                    latest = cycles[0] if cycles else {}
                    trade_event["detected_at"] = time.time()
                    trade_event["side"] = latest.get("side")
                    trade_event["amount"] = latest.get("amount")
                    trade_event["price"] = latest.get("unit_price")
                    trade_event["acknowledged"] = False
                trade_event["last_seen_count"] = new_count
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


PNL_STALE_SECONDS = 300    # 5 min = 3 refresh cycles; stale avg_cost silently bypasses the floor
MARKET_STALE_SECONDS = 30  # 7-8 poll cycles (4s each); frozen snapshot feeds bad prices to advisor


def _data_staleness():
    """Single source of truth for 'is our data too old to trade on'. Used by
    auto-reprice (which BLOCKS on stale) AND the displayed advice/agent (which
    must WARN, not render a confident price computed from frozen inputs). Before
    this, only auto-reprice was gated: the dashboard advice kept showing a price
    off a stale avg_cost, letting a below-breakeven sell through for -13237 IDR
    on 2026-07-01. Same thresholds everywhere => no divergence between what the
    bot will act on and what the human is told."""
    now = time.time()
    pnl_age = now - pnl_cache.get("updated_at", 0)
    market_age = now - watcher.latest.timestamp
    pnl_stale = pnl_cache["summary"] is None or pnl_age >= PNL_STALE_SECONDS
    market_stale = market_age >= MARKET_STALE_SECONDS
    return {
        "stale": pnl_stale or market_stale,
        "pnl_stale": pnl_stale,
        "market_stale": market_stale,
        "pnl_age_sec": round(pnl_age),
        "market_age_sec": round(market_age),
    }


async def auto_reprice_loop():
    """Off by default (repricer.enabled). When on, mirrors exactly what the
    advisor card already tells the user to do by hand - only fires on the
    same actionable states/recommended_price the dashboard shows, never on
    its own separate judgment. AdRepricer enforces the actual safety bounds
    (max delta, cooldown); this loop just decides *whether* to call it.

    Gates beyond repricer.enabled (all must pass):
    - order_watch_state["enabled"]: has_pending() only reflects reality while
      something is actively polling for it. If order-watch is off, its last
      reading is frozen — treat as "can't confirm safe".
    - pnl_cache["summary"] is not None AND fresh: the cost-basis floor needs
      real P&L data. A None summary (post-restart) or stale summary (VPS SSH
      failing for >5min) both make the floor a silent no-op — confirmed
      2026-07-01: stale avg_cost 17931 let aggressive_price 17969 slip past
      a floor that should have blocked it (real avg was 17969, breakeven 18005),
      causing a -13237 IDR loss from pure commission at cost price.
    - watcher.latest fresh: frozen market snapshot (CDP offline) feeds the
      advisor wrong prices; when data restores the jump can trigger aggressive
      repricing that slips past a temporarily-correct floor."""
    while True:
        try:
            st = _data_staleness()
            pnl_ready = not st["pnl_stale"]
            market_ready = not st["market_stale"]
            if not pnl_ready and repricer.enabled:
                print(f"[auto_reprice] skipping: pnl_cache is {st['pnl_age_sec']}s stale (limit {PNL_STALE_SECONDS}s)")
            if not market_ready and repricer.enabled:
                print(f"[auto_reprice] skipping: market data is {st['market_age_sec']}s stale (limit {MARKET_STALE_SECONDS}s)")
            if repricer.enabled and order_watch_state["enabled"] and pnl_ready and market_ready and not order_watcher.has_pending():
                inputs = _advice_inputs()
                # The LLM decides the price ONLY inside our zone of interest — the
                # market above breakeven, where there is a real "sell higher"
                # choice (read the book, weigh factors). Below breakeven the
                # deterministic advisor returns state=floor (not in
                # AUTO_REPRICE_STATES), so nothing reprices and no LLM request is
                # spent chasing a price we would never set. The independent
                # breakeven guard below still applies regardless of which advisor
                # produced the price.
                if USE_LLM_ADVISOR and _market_above_breakeven():
                    result = await _llm_compute_advice_async(lang="ru", orderbook=_orderbook_for_llm(), **inputs)
                else:
                    result = _rule_compute_advice(lang="ru", **inputs)
                my_ad = inputs["my_ad"]
                rec_price = result.get("recommended_price")
                if (
                    result["state"] in AUTO_REPRICE_STATES
                    and my_ad
                    and my_ad.get("adv_no")
                    and rec_price is not None
                    and abs(rec_price - my_ad["price"]) >= 1
                ):
                    # Independent breakeven guard (last line before a live order).
                    # The advisor's floor should already clamp any below-cost SELL
                    # to state=floor (excluded from AUTO_REPRICE_STATES), but if
                    # that floor was bypassed — stale cost, or the position isn't
                    # FIFO-tracked so max_cost is None — never let auto-reprice
                    # push a SELL below its own fee-aware breakeven. On the SELL
                    # side, an unknown cost (None) is treated as unsafe: refuse.
                    block = False
                    if inputs["active_side"] == "SELL":
                        max_cost = inputs.get("own_max_cost_idr")
                        if max_cost is None:
                            block = True
                            print("[auto_reprice] BLOCKED: SELL with no tracked cost basis — refusing")
                        else:
                            breakeven = max_cost * (1 + ROUND_TRIP_FEE_PCT / 100)
                            if rec_price < breakeven:
                                block = True
                                print(f"[auto_reprice] BLOCKED: SELL rec {rec_price:.0f} < breakeven {breakeven:.0f} — floor bypassed")
                    if not block:
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
            # Fee-aware MTM: what this lot would net if sold NOW. The buy leg's
            # fee is already sunk in unit_price's cost; the sell leg's fee plus
            # the buy leg's are folded into the *1.002 breakeven so this number
            # is consistent with the advisor floor (cost * (1+ROUND_TRIP_FEE_PCT)).
            # Fee-blind (current - unit_price) showed ~-12/unit when the real
            # gap-to-exit was ~-48/unit and lied about profitability.
            breakeven = row["unit_price"] * (1 + ROUND_TRIP_FEE_PCT / 100)
            row["unrealized_profit_idr"] = round(open_usdt * (current_price - breakeven), 2)
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
    st = _data_staleness()
    return {
        "enabled": repricer.enabled,
        "max_delta_idr": repricer.max_delta_idr,
        "cooldown_sec": repricer.cooldown_sec,
        "last_result": repricer.last_result,
        "order_watch_enabled": order_watch_state["enabled"],
        "pnl_ready": not st["pnl_stale"],
        "pnl_age_sec": st["pnl_age_sec"],
        "pnl_stale": st["pnl_stale"],
        "market_ready": not st["market_stale"],
        "market_age_sec": st["market_age_sec"],
        "market_stale": st["market_stale"],
        "has_pending_order": order_watcher.has_pending(),
        "market_above_breakeven": _market_above_breakeven(),
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

    # Phase-1 scorecard on the live session: "было X IDR → станет Y IDR".
    # Projected closing bank balance = opening + realized (already fee-net via
    # _fifo_match) + fee-aware MTM of whatever USDT is still open (net of the
    # round-trip fee, same convention as /api/pnl and /api/earnings). This is
    # the single number that proves/disproves profitability of a full round trip.
    current = sessions[-1]
    current_price = watcher.latest.robust_sell_price
    open_usdt = current.get("open_usdt") or 0.0
    open_avg = current.get("open_avg_cost_idr")
    unrealized = None
    if open_usdt > 1e-9 and open_avg is not None and current_price is not None:
        breakeven = open_avg * (1 + ROUND_TRIP_FEE_PCT / 100)
        unrealized = open_usdt * (current_price - breakeven)
    realized = current.get("realized_pnl_idr") or 0.0
    session_pnl = realized + (unrealized or 0.0)
    current["unrealized_net_idr"] = round(unrealized, 2) if unrealized is not None else None
    current["session_pnl_idr"] = round(session_pnl, 2)
    current["projected_balance_idr"] = round(current["opening_balance_idr"] + session_pnl, 2)
    current["current_sell_price_idr"] = current_price
    return {"sessions": sessions, "current": current}


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
        # Fee-aware MTM (see /api/pnl): net-of-round-trip-fee, so total_potential
        # and the session scorecard don't overstate what a close would realize.
        breakeven = open_avg_cost * (1 + ROUND_TRIP_FEE_PCT / 100)
        unrealized_idr = open_usdt * (current_price - breakeven)

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
        # inventory below what we actually paid for it (per most expensive lot).
        "own_avg_cost_idr": pnl_summary.get("open_tracked_avg_cost_idr"),
        "own_max_cost_idr": pnl_summary.get("open_tracked_max_cost_idr"),
        "own_open_usdt": pnl_summary.get("open_tracked_usdt"),
    }


@app.get("/api/advice")
async def advice(lang: str = "ru"):
    inputs = _advice_inputs()
    active_side = inputs["active_side"]
    market_trend = inputs["market_trend_idr_per_min"]

    result = await compute_advice_display(lang=lang, **inputs)
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

    # Staleness gate: a price computed from a frozen orderbook or a stale
    # cost-basis (floor silently bypassed) must never be shown as actionable.
    # Same threshold auto-reprice blocks on, so bot and human agree.
    st = _data_staleness()
    result["data_stale"] = st["stale"]
    result["pnl_stale"] = st["pnl_stale"]
    result["market_stale"] = st["market_stale"]
    if st["stale"]:
        parts = []
        if st["pnl_stale"]:
            parts.append((f"P&L {st['pnl_age_sec']}с", f"P&L {st['pnl_age_sec']}s"))
        if st["market_stale"]:
            parts.append((f"рынок {st['market_age_sec']}с", f"market {st['market_age_sec']}s"))
        detail_ru = ", ".join(p[0] for p in parts)
        detail_en = ", ".join(p[1] for p in parts)
        stale_msgs = {
            "ru": (f"⚠️ Данные устарели ({detail_ru}) — не торгуй по этой цене, floor не гарантирован",
                   "Пока данные не обновятся, рекомендация ненадёжна — проверь Opera/связь с VPS"),
            "en": (f"⚠️ Data is stale ({detail_en}) — do not trade on this price, floor not guaranteed",
                   "Recommendation unreliable until data refreshes — check Opera / VPS link"),
        }
        adv, reason = stale_msgs.get(lang, stale_msgs["ru"])
        result["state"] = "stale"
        result["recommended_price"] = None
        result["advice"] = adv
        result["reasons"] = [reason]
    return result


# ---------------------------------------------------------------------------
# Rule-based agent — maps current advisor state to one concrete human action.
# LLM-ready: swap _build_instruction() body for a Claude API call later;
# the endpoint contract (/api/agent, /api/agent/done) stays identical.
# ---------------------------------------------------------------------------

agent_history: list[dict] = []   # newest first, capped at 30
_AGENT_HISTORY_MAX = 30


async def _force_pnl_refresh():
    """Immediate VPS poll outside the 90-second loop — called after the user
    confirms an action so the next instruction sees up-to-date P&L."""
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
    except Exception:
        pass  # non-fatal; regular loop retries in ≤90s


def _build_instruction(advice: dict, inputs: dict, pending_orders: list) -> dict:
    """Single most-important instruction for the operator right now.

    Returns a dict:
      action   – machine key (change_price | accept_payment | wait | hold | auto_working)
      urgency  – "high" | "medium" | "low"
      title    – one-liner shown in the card header
      context  – why (reasons / numbers)
      steps    – list[str] of concrete things to do; empty when action==wait
      price    – recommended price or None
    """
    state = advice["state"]
    rec = advice.get("recommended_price")
    active_side = inputs["active_side"]
    my_ad = inputs["my_ad"]
    current_price = my_ad["price"] if my_ad else None
    ad_type = active_side          # "BUY" or "SELL"
    context_text = " · ".join(advice.get("reasons") or [advice.get("advice", "")])

    # 1. Pending order → absolute priority regardless of everything else
    if pending_orders:
        n = len(pending_orders)
        extra = f" (+{n - 1} ещё)" if n > 1 else ""
        return {
            "action": "accept_payment",
            "urgency": "high",
            "title": f"Прими платёж{extra}",
            "context": "Входящий ордер — у тебя 15 минут на подтверждение получения",
            "steps": [
                "Открой Binance P2P → Orders",
                "Убедись что деньги пришли на банковский счёт",
                "Нажми «Confirm Release» в Binance",
                "Вернись и нажми «Выполнено»",
            ],
            "price": None,
        }

    # 2. Recent unacknowledged trade — surface immediately so user knows to act
    TRADE_EVENT_TTL = 600  # show for 10 min after detection; stale after that
    if (not trade_event["acknowledged"]
            and trade_event["detected_at"]
            and (time.time() - trade_event["detected_at"]) < TRADE_EVENT_TTL):
        side = trade_event["side"] or "?"
        amt = f"{trade_event['amount']:.0f}" if trade_event["amount"] else "?"
        px = f"{trade_event['price']:.0f}" if trade_event["price"] else "?"
        next_side = "SELL" if side == "BUY" else "BUY"
        return {
            "action": "trade_done",
            "urgency": "high",
            "title": f"Сделка {side}: {amt} USDT по {px} — подтверди",
            "context": f"Новая {side}-сделка зафиксирована в P&L. Нажми «Выполнено» чтобы обновить данные и получить следующий шаг ({next_side}).",
            "steps": [
                f"Убедись что Binance P2P показывает сделку завершённой",
                f"Нажми «Выполнено» — система переключится на {next_side}",
            ],
            "price": None,
        }

    # 2.5 Stale data — never emit a price action computed from frozen inputs.
    # Comes after pending-order/trade-done (those don't depend on market price)
    # but before any price logic. Mirrors the /api/advice staleness gate.
    st = _data_staleness()
    if st["stale"]:
        parts = []
        if st["pnl_stale"]:
            parts.append(f"P&L {st['pnl_age_sec']}с")
        if st["market_stale"]:
            parts.append(f"рынок {st['market_age_sec']}с")
        return {
            "action": "wait",
            "urgency": "medium",
            "title": "⚠️ Данные устарели — не выставляй цену",
            "context": f"Устарели: {', '.join(parts)}. Совет по цене ненадёжен, floor не гарантирован. Проверь Opera и связь с VPS.",
            "steps": [],
            "price": None,
        }

    # 3. Floor: market at/below cost — hold, do not lower
    if state == "floor":
        floor_price = math.ceil(rec) if rec else None
        return {
            "action": "hold",
            "urgency": "medium",
            "title": f"Держи цену {floor_price} — у безубытка, не снижай",
            "context": context_text,
            "steps": [
                "Ничего не менять — ждать отскока рынка вверх",
                f"Минимальная цена продажи: {floor_price} IDR",
            ],
            "price": floor_price,
        }

    # 3. Wide spread: best to wait
    if state == "wide":
        return {
            "action": "wait",
            "urgency": "low",
            "title": "Жди — спред широкий, позиция стоит хорошо",
            "context": context_text,
            "steps": [],
            "price": rec,
        }

    # 4. Price change needed
    if state in ("push", "trend", "debt", "tight", "neutral") and rec is not None:
        target = math.ceil(rec)
        delta = (target - current_price) if current_price else None
        delta_str = f" ({delta:+.0f})" if delta is not None else ""
        current_str = f"{current_price:.0f} → " if current_price else ""
        urgency = "high" if state in ("push", "trend") else "medium"

        # If auto-reprice is on and healthy, nothing for the user to do
        now = time.time()
        pnl_age = now - pnl_cache.get("updated_at", 0)
        market_age = now - watcher.latest.timestamp
        auto_healthy = (repricer.enabled
                        and order_watch_state["enabled"]
                        and pnl_age < PNL_STALE_SECONDS
                        and market_age < MARKET_STALE_SECONDS)
        if auto_healthy and delta and abs(delta) >= 1:
            return {
                "action": "auto_working",
                "urgency": "low",
                "title": f"Авто-репрайс: {current_str}{target}{delta_str}",
                "context": f"Бот сделает это сам в течение ~{AUTO_REPRICE_INTERVAL_SECONDS}с · {context_text}",
                "steps": [],
                "price": target,
            }

        if current_price and abs(target - current_price) < 1:
            return {
                "action": "wait",
                "urgency": "low",
                "title": f"Цена актуальна ({current_price:.0f}) — ждём исполнения",
                "context": context_text,
                "steps": [],
                "price": target,
            }

        return {
            "action": "change_price",
            "urgency": urgency,
            "title": f"Измени цену: {current_str}{target}{delta_str} IDR",
            "context": context_text,
            "steps": [
                "Binance P2P → My Ads",
                f"Найди свою {ad_type} заявку",
                f"Нажми Edit → поставь цену {target} IDR",
                "Нажми Post → Confirm to post",
                "Нажми «Выполнено» когда готово",
            ],
            "price": target,
        }

    # 5. Fallback: neutral / no clear signal
    return {
        "action": "wait",
        "urgency": "low",
        "title": "Нейтрально — наблюдай",
        "context": context_text or "Нет доминирующего сигнала",
        "steps": [],
        "price": rec,
    }


async def _agent_payload(lang: str = "ru") -> dict:
    inputs = _advice_inputs()
    advice = await compute_advice_display(lang=lang, **inputs)
    pending = list(order_watcher.seen_order_numbers)  # proxy: count of open orders
    pending_orders = [{"id": oid} for oid in pending] if order_watcher.has_pending() else []
    instruction = _build_instruction(advice, inputs, pending_orders)
    return {
        "instruction": instruction,
        "advice_state": advice["state"],
        "history": agent_history[:10],
    }


@app.get("/api/agent")
async def get_agent(lang: str = "ru"):
    return await _agent_payload(lang)


@app.post("/api/agent/done")
async def agent_done(request: Request):
    body = await request.json()
    action = body.get("action", "unknown")
    title = body.get("title", "")
    skipped = bool(body.get("skipped", False))
    lang = body.get("lang", "ru")
    agent_history.insert(0, {
        "action": action,
        "title": title,
        "skipped": skipped,
        "at": time.time(),
    })
    if len(agent_history) > _AGENT_HISTORY_MAX:
        agent_history.pop()
    # Acknowledging a trade_done event: just clear the notification.
    # Do NOT touch the side override — user manages that manually.
    if action == "trade_done" and not skipped:
        trade_event["acknowledged"] = True
    await _force_pnl_refresh()
    return await _agent_payload(lang)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/static", StaticFiles(directory="static"), name="static")
