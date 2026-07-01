"""Run on the VPS (which has working network access to api.binance.com).
Prints a JSON summary to stdout so the Mac-side backend can pull it over SSH.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv

from pnl_tracker import PnLTracker

load_dotenv()
tracker = PnLTracker(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])
tracker.poll()

# Manual USDT withdrawals/transfers-out (not visible in Binance trade history).
# File format: [{"usdt": 1218.48, "note": "moved to funding 2026-07-01"}, ...]
# Each removes inventory oldest-lot-first via FIFO with zero P&L so the tracked
# open position matches the real on-exchange balance. Timestamped at the latest
# trade so it never opens a phantom trading-session gap.
_wfile = os.path.join(os.path.dirname(__file__), "withdrawals.json")
if os.path.exists(_wfile):
    latest_trade_ms = max((c["time"] for c in tracker.cycles), default=None)
    with open(_wfile) as f:
        for w in json.load(f):
            tracker.add_withdrawal(
                w["usdt"], time_ms=w.get("time_ms", latest_trade_ms), note=w.get("note", "")
            )

print(json.dumps({"summary": tracker.summary(), "cycles": tracker.annotated_cycles()}))
