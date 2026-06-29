"""Run on the VPS - prints Spot + Funding USDT balance as JSON."""
import hashlib
import hmac
import json
import os
import time
import urllib.parse

import httpx
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.environ["BINANCE_API_KEY"]
API_SECRET = os.environ["BINANCE_API_SECRET"]
BASE = "https://api.binance.com"


def signed_get(path, params=None):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    q = urllib.parse.urlencode(params)
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    q += f"&signature={sig}"
    r = httpx.get(f"{BASE}{path}?{q}", headers={"X-MBX-APIKEY": API_KEY}, timeout=15)
    r.raise_for_status()
    return r.json()


def signed_post(path, params=None):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    q = urllib.parse.urlencode(params)
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    body = q + f"&signature={sig}"
    r = httpx.post(
        f"{BASE}{path}",
        headers={"X-MBX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"},
        content=body,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


spot_usdt = 0.0
account = signed_get("/api/v3/account", {})
for b in account.get("balances", []):
    if b["asset"] == "USDT":
        spot_usdt = float(b["free"])

funding_usdt = 0.0
funding = signed_post("/sapi/v1/asset/get-funding-asset", {"asset": "USDT"})
for b in funding:
    if b["asset"] == "USDT":
        funding_usdt = float(b["free"])

print(json.dumps({"spot_usdt": spot_usdt, "funding_usdt": funding_usdt, "total_usdt": spot_usdt + funding_usdt}))
