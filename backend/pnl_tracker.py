"""Pulls completed P2P trades via the official signed endpoint
GET /sapi/v1/c2c/orderMatch/listUserOrderHistory and keeps a running ledger.

Note: this calls api.binance.com directly. On this Mac, direct TLS
connections to exchange APIs are blocked at the network level (same issue
as Bybit) - run this from a VPS, or route through a working proxy.
"""
import hashlib
import hmac
import os
import time
import urllib.parse

import httpx

BASE_URL = "https://api.binance.com"


SESSION_GAP_HOURS = 3  # a quiet gap this long marks the start of a new trading session


class PnLTracker:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.seen_order_numbers = set()
        self.cycles = []  # list of {time, side, asset, fiat, amount, unit_price, total}

    def _signed_get(self, path: str, params: dict):
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params)
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        query += f"&signature={signature}"
        headers = {"X-MBX-APIKEY": self.api_key}
        resp = httpx.get(f"{BASE_URL}{path}?{query}", headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def poll(self):
        """Fetch recent BUY+SELL history, return list of newly seen completed trades."""
        new_trades = []
        for trade_type in ("BUY", "SELL"):
            data = self._signed_get(
                "/sapi/v1/c2c/orderMatch/listUserOrderHistory",
                {"tradeType": trade_type, "rows": 50},
            )
            for order in data.get("data", []):
                order_no = order["orderNumber"]
                if order["orderStatus"] != "COMPLETED" or order_no in self.seen_order_numbers:
                    continue
                self.seen_order_numbers.add(order_no)
                trade = {
                    "order_number": order_no,
                    "time": order["createTime"],
                    "side": order["tradeType"],
                    "asset": order["asset"],
                    "fiat": order["fiat"],
                    "amount": float(order["amount"]),
                    "unit_price": float(order["unitPrice"]),
                    "total": float(order["totalPrice"]),
                    # Binance P2P commission, charged in the crypto asset on every
                    # completed order regardless of side - invisible anywhere else
                    # in the API/UI (~0.1%/leg, confirmed 2026-06-30 from this exact
                    # field). Without it, FIFO profit looks real when it's often a
                    # net loss once both legs' fees are counted.
                    "commission": float(order.get("commission", 0) or 0),
                }
                self.cycles.append(trade)
                new_trades.append(trade)
        self.cycles.sort(key=lambda t: t["time"])
        return new_trades

    @staticmethod
    def _fee_per_unit_idr(c):
        """Commission for one trade, spread evenly across its units and
        converted to IDR at that trade's own price - the only sane way to
        blend a USDT-denominated fee into an IDR FIFO ledger."""
        return (c.get("commission", 0.0) / c["amount"]) * c["unit_price"] if c["amount"] > 1e-9 else 0.0

    @staticmethod
    def _fifo_match(events):
        """events: trades sorted by time. True time-respecting FIFO - a SELL can
        only consume BUY lots that happened at or before it, never a later one
        (you can't fund a sale with USDT you haven't bought yet). Any sell amount
        left over once the queue runs dry was funded by untracked/pre-existing
        balance - no known cost basis, excluded from realized P&L rather than
        wrongly matched against a future buy's price.

        realized_pnl is net of both legs' Binance commission (see poll()) -
        a price-only FIFO match looks profitable far more often than it
        actually is once that's counted in."""
        buy_queue = []
        realized_pnl = 0.0
        matched_cycles = 0
        unmatched_sell_usdt = 0.0
        for c in events:
            if c["side"] == "BUY":
                buy_queue.append({
                    "amount": c["amount"], "unit_price": c["unit_price"],
                    "fee_per_unit_idr": PnLTracker._fee_per_unit_idr(c),
                })
            else:
                remaining = c["amount"]
                sell_unit = c["unit_price"]
                sell_fee_per_unit = PnLTracker._fee_per_unit_idr(c)
                while remaining > 1e-9 and buy_queue:
                    lot = buy_queue[0]
                    take = min(remaining, lot["amount"])
                    gross = take * (sell_unit - lot["unit_price"])
                    fees = take * (lot["fee_per_unit_idr"] + sell_fee_per_unit)
                    realized_pnl += gross - fees
                    lot["amount"] -= take
                    remaining -= take
                    if lot["amount"] <= 1e-9:
                        buy_queue.pop(0)
                        matched_cycles += 1
                if remaining > 1e-9:
                    unmatched_sell_usdt += remaining
        return realized_pnl, matched_cycles, unmatched_sell_usdt, buy_queue

    @staticmethod
    def _fifo_annotate(events):
        """Same time-respecting FIFO as _fifo_match, but tags each trade with
        the profit_idr it actually realized instead of only a running total -
        lets the ledger table show real per-row profit. BUY rows get
        profit_idr=None (nothing realized until a later sell closes them) and
        open_usdt = however much of that lot is still unsold once every event
        has been processed (0 once it's fully matched away). open_usdt carries
        no price - the caller knows the live market price, this module doesn't.
        A SELL that outruns the buy queue gets unmatched_usdt>0 on top of the
        profit from whatever portion did match."""
        buy_queue = []  # [{"row": dict, "amount": float, "unit_price": float, "fee_per_unit_idr": float}, ...]
        annotated = []
        for c in events:
            row = dict(c)
            if c["side"] == "BUY":
                buy_queue.append({
                    "row": row, "amount": c["amount"], "unit_price": c["unit_price"],
                    "fee_per_unit_idr": PnLTracker._fee_per_unit_idr(c),
                })
                row["profit_idr"] = None
                row["unmatched_usdt"] = 0.0
                row["open_usdt"] = 0.0
            else:
                remaining = c["amount"]
                sell_unit = c["unit_price"]
                sell_fee_per_unit = PnLTracker._fee_per_unit_idr(c)
                profit = 0.0
                while remaining > 1e-9 and buy_queue:
                    lot = buy_queue[0]
                    take = min(remaining, lot["amount"])
                    gross = take * (sell_unit - lot["unit_price"])
                    fees = take * (lot["fee_per_unit_idr"] + sell_fee_per_unit)
                    profit += gross - fees
                    lot["amount"] -= take
                    remaining -= take
                    if lot["amount"] <= 1e-9:
                        buy_queue.pop(0)
                # profit_idr is net of both legs' commission (see poll()/_fee_per_unit_idr) -
                # the dashboard ledger shows real take-home, not a price-only mirage.
                row["profit_idr"] = round(profit, 2)
                row["unmatched_usdt"] = round(remaining, 4) if remaining > 1e-9 else 0.0
            annotated.append(row)
        for lot in buy_queue:
            lot["row"]["open_usdt"] = round(lot["amount"], 4)
        return annotated

    def annotated_cycles(self):
        """self.cycles (raw trades) with per-row profit_idr filled in - what
        the dashboard ledger table renders instead of bare totals."""
        events = sorted(self.cycles, key=lambda c: c["time"])
        return self._fifo_annotate(events)

    @staticmethod
    def _open_position(lots):
        usdt = sum(l["amount"] for l in lots)
        cost_idr = sum(l["amount"] * l["unit_price"] for l in lots)
        avg_cost = round(cost_idr / usdt, 2) if usdt > 1e-9 else None
        return round(usdt, 4), avg_cost

    def summary(self):
        events = sorted(self.cycles, key=lambda c: c["time"])
        realized_pnl, matched_cycles, unmatched_sell_usdt, open_lots = self._fifo_match(events)
        open_tracked_usdt, open_tracked_avg_cost = self._open_position(open_lots)

        buys = [c for c in self.cycles if c["side"] == "BUY"]
        sells = [c for c in self.cycles if c["side"] == "SELL"]
        total_buy_amount = sum(c["amount"] for c in buys)
        total_sell_amount = sum(c["amount"] for c in sells)
        # Simple net flow (not FIFO-causal) - the right number for "current state"
        # questions like "do I owe a rebuy right now", since it's just conservation
        # of USDT and doesn't care which specific lot funded which sale.
        # Positive => sold more than bought back yet (open "need to rebuy" gap).
        # Negative => bought more than sold (holding spare USDT, free to sell).
        net = round(total_sell_amount - total_buy_amount, 4)

        # Current trading session = trades after the most recent gap > SESSION_GAP_HOURS.
        session_events = events
        for i in range(len(events) - 1, 0, -1):
            gap_h = (events[i]["time"] - events[i - 1]["time"]) / 3_600_000
            if gap_h > SESSION_GAP_HOURS:
                session_events = events[i:]
                break
        session_pnl, session_matched, _, session_open_lots = self._fifo_match(session_events)
        session_open_usdt, session_open_avg_cost = self._open_position(session_open_lots)

        return {
            "total_trades": len(self.cycles),
            "buy_trades": len(buys),
            "sell_trades": len(sells),
            "matched_cycles": matched_cycles,
            "realized_pnl_idr": round(realized_pnl, 2),
            "unmatched_sell_usdt": round(unmatched_sell_usdt, 4),
            "open_tracked_usdt": open_tracked_usdt,
            "open_tracked_avg_cost_idr": open_tracked_avg_cost,
            "open_sell_remainder_usdt": max(0.0, net),
            "open_buy_surplus_usdt": max(0.0, -net),
            "session_start_ms": session_events[0]["time"] if session_events else None,
            "session_matched_cycles": session_matched,
            "session_realized_pnl_idr": round(session_pnl, 2),
            "session_open_usdt": session_open_usdt,
            "session_open_avg_cost_idr": session_open_avg_cost,
        }


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    tracker = PnLTracker(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])
    new = tracker.poll()
    print(f"New completed trades this poll: {len(new)}")
    print("Summary:", tracker.summary())
