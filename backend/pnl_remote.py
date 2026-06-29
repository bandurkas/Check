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
print(json.dumps({"summary": tracker.summary(), "cycles": tracker.cycles}))
