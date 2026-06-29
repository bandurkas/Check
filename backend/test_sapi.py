import hashlib
import hmac
import os
import time
import urllib.parse

import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ["BINANCE_API_KEY"]
API_SECRET = os.environ["BINANCE_API_SECRET"]
BASE_URL = "https://api.binance.com"


def signed_get(path: str, params: dict):
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    query += f"&signature={signature}"
    headers = {"X-MBX-APIKEY": API_KEY}
    return httpx.get(f"{BASE_URL}{path}?{query}", headers=headers, timeout=15)


def headers_only_post(path: str, payload: dict):
    headers = {"X-MBX-APIKEY": API_KEY, "Content-Type": "application/json"}
    return httpx.post(f"{BASE_URL}{path}", headers=headers, json=payload, timeout=15)


def signed_post(path: str, payload: dict):
    payload = dict(payload)
    payload["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(payload)
    signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    body = query + f"&signature={signature}"
    return httpx.post(f"{BASE_URL}{path}", headers=headers, content=body, timeout=15)


print("=== 1. Sanity check: account endpoint (confirms key/secret + connectivity) ===")
r = signed_get("/sapi/v1/account/apiRestrictions", {})
print(r.status_code, r.text[:500])

print("\n=== 2. ads/search as headers-only POST (JSON body) ===")
payload = {
    "asset": "USDT",
    "fiat": "IDR",
    "tradeType": "BUY",
    "page": 1,
    "rows": 5,
    "payTypes": ["BankTransfer"],
}
r2 = headers_only_post("/sapi/v1/c2c/ads/search", payload)
print(r2.status_code, r2.text[:1000])

print("\n=== 3. ads/search as signed POST (form-encoded) ===")
r3 = signed_post("/sapi/v1/c2c/ads/search", {
    "asset": "USDT",
    "fiat": "IDR",
    "tradeType": "BUY",
    "page": 1,
    "rows": 5,
})
print(r3.status_code, r3.text[:1000])

print("\n=== 4. listUserOrderHistory (confirmed official, sanity check) ===")
r4 = signed_get("/sapi/v1/c2c/orderMatch/listUserOrderHistory", {"tradeType": "BUY"})
print(r4.status_code, r4.text[:500])
