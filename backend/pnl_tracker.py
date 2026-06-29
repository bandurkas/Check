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
                }
                self.cycles.append(trade)
                new_trades.append(trade)
        self.cycles.sort(key=lambda t: t["time"])
        return new_trades

    def summary(self):
        """FIFO-match BUY lots against SELL lots to estimate realized P&L in fiat."""
        buys = [c for c in self.cycles if c["side"] == "BUY"]
        sells = [c for c in self.cycles if c["side"] == "SELL"]
        buy_queue = [dict(c) for c in sorted(buys, key=lambda c: c["time"])]
        realized_pnl = 0.0
        matched_cycles = 0
        for sell in sorted(sells, key=lambda c: c["time"]):
            remaining = sell["amount"]
            sell_unit = sell["unit_price"]
            while remaining > 1e-9 and buy_queue:
                lot = buy_queue[0]
                take = min(remaining, lot["amount"])
                realized_pnl += take * (sell_unit - lot["unit_price"])
                lot["amount"] -= take
                remaining -= take
                if lot["amount"] <= 1e-9:
                    buy_queue.pop(0)
                    matched_cycles += 1
            if remaining > 1e-9:
                # sold more than bought via this bot - ignore the unmatched remainder
                pass
        return {
            "total_trades": len(self.cycles),
            "buy_trades": len(buys),
            "sell_trades": len(sells),
            "matched_cycles": matched_cycles,
            "realized_pnl_idr": round(realized_pnl, 2),
        }


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    tracker = PnLTracker(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])
    new = tracker.poll()
    print(f"New completed trades this poll: {len(new)}")
    print("Summary:", tracker.summary())
