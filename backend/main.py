import asyncio
import json
import subprocess
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from market_watcher import MarketWatcher

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

watcher = MarketWatcher(poll_interval=4.0)

VPS_HOST = "root@187.127.114.34"
PNL_REFRESH_SECONDS = 90
BALANCE_REFRESH_SECONDS = 60
pnl_cache = {"summary": None, "cycles": [], "updated_at": 0, "error": None}
balance_cache = {"spot_usdt": None, "funding_usdt": None, "total_usdt": None, "updated_at": 0, "error": None}


@app.on_event("startup")
async def startup():
    asyncio.create_task(watcher.run_forever())
    asyncio.create_task(watcher.run_my_ad_forever())
    asyncio.create_task(pnl_refresh_loop())
    asyncio.create_task(balance_refresh_loop())


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
    return pnl_cache


@app.get("/api/balance")
async def balance():
    return balance_cache


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/static", StaticFiles(directory="static"), name="static")
