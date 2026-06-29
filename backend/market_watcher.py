"""Polls Binance P2P order book (USDT/IDR, Bank Transfer) via an already
authenticated Opera tab over the Chrome DevTools Protocol, so requests never
hit Binance's Cloudflare bot-challenge directly.

Requires Opera running with --remote-debugging-port=9222 and at least one
open tab on binance.com (logged in to P2P).
"""
import asyncio
import time
from dataclasses import dataclass, field

from playwright.async_api import async_playwright

CDP_URL = "http://localhost:9222"
SEARCH_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
ASSET = "USDT"
FIAT = "IDR"
PAY_TYPES = ["BANK"]  # Bank Transfer identifier for IDR
TICK = 1  # IDR, smallest price increment to undercut/outbid by


@dataclass
class OrderBookSnapshot:
    timestamp: float = field(default_factory=time.time)
    buy_ads: list = field(default_factory=list)   # ads where YOU would BUY (merchants selling)
    sell_ads: list = field(default_factory=list)  # ads where YOU would SELL (merchants buying)

    @property
    def best_buy_price(self):
        return self.buy_ads[0]["price"] if self.buy_ads else None

    @property
    def best_sell_price(self):
        return self.sell_ads[0]["price"] if self.sell_ads else None

    @property
    def spread_idr(self):
        if self.best_buy_price is None or self.best_sell_price is None:
            return None
        return self.best_buy_price - self.best_sell_price

    @property
    def spread_pct(self):
        if not self.spread_idr or not self.best_sell_price:
            return None
        mid = (self.best_buy_price + self.best_sell_price) / 2
        return (self.spread_idr / mid) * 100

    def recommended_prices(self):
        """Price to post your own ads at, one tick inside the current best."""
        rec = {}
        if self.best_buy_price is not None:
            # You are SELLING USDT -> want to be the cheapest BUY-side ad (lowest ask)
            rec["sell_usdt_at"] = self.best_buy_price - TICK
        if self.best_sell_price is not None:
            # You are BUYING USDT -> want to be the highest SELL-side bid
            rec["buy_usdt_at"] = self.best_sell_price + TICK
        return rec


class MarketWatcher:
    def __init__(self, poll_interval: float = 4.0):
        self.poll_interval = poll_interval
        self.latest = OrderBookSnapshot()
        self._playwright = None
        self._browser = None
        self._page = None
        self._running = False

    async def _connect(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(CDP_URL)
        for ctx in self._browser.contexts:
            for pg in ctx.pages:
                if "binance.com" in pg.url:
                    self._page = pg
                    break
            if self._page:
                break
        if self._page is None:
            raise RuntimeError(
                "No open binance.com tab found in Opera. Open p2p.binance.com "
                "in Opera (with VPN on) and log in, then retry."
            )

    async def _fetch_side(self, trade_type: str):
        payload = {
            "asset": ASSET,
            "fiat": FIAT,
            "tradeType": trade_type,
            "page": 1,
            "rows": 20,
            "payTypes": PAY_TYPES,
            "publisherType": None,
        }
        result = await self._page.evaluate(
            """async (args) => {
                const [url, payload] = args;
                try {
                    const res = await fetch(url, {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify(payload),
                    });
                    const body = await res.json();
                    return {status: res.status, body};
                } catch (e) {
                    return {status: -1, body: {error: String(e)}};
                }
            }""",
            [SEARCH_URL, payload],
        )
        if result["status"] != 200 or not result["body"].get("success"):
            return []
        rows = []
        for item in result["body"].get("data", []):
            adv = item["adv"]
            rows.append(
                {
                    "price": float(adv["price"]),
                    "available": float(adv.get("surplusAmount") or adv.get("tradableQuantity") or 0),
                    "min_limit": float(adv.get("minSingleTransAmount") or 0),
                    "max_limit": float(adv.get("maxSingleTransAmount") or 0),
                    "merchant": item.get("advertiser", {}).get("nickName", "?"),
                }
            )
        return rows

    async def poll_once(self):
        # Binance UI's "BUY" tab = ads where merchants SELL to you = your buy price.
        # Its "SELL" tab = ads where merchants BUY from you = your sell price.
        buy_side = await self._fetch_side("BUY")
        sell_side = await self._fetch_side("SELL")
        buy_side.sort(key=lambda r: r["price"])          # cheapest ask first
        sell_side.sort(key=lambda r: r["price"], reverse=True)  # highest bid first
        self.latest = OrderBookSnapshot(buy_ads=buy_side, sell_ads=sell_side)
        return self.latest

    async def run_forever(self):
        await self._connect()
        self._running = True
        while self._running:
            try:
                await self.poll_once()
            except Exception as e:
                print(f"[market_watcher] poll error: {e}")
            await asyncio.sleep(self.poll_interval)

    def stop(self):
        self._running = False


if __name__ == "__main__":
    async def _demo():
        watcher = MarketWatcher(poll_interval=5)
        await watcher._connect()
        snap = await watcher.poll_once()
        print("Best buy (you pay):", snap.best_buy_price)
        print("Best sell (you receive):", snap.best_sell_price)
        print("Spread:", snap.spread_idr, f"({snap.spread_pct:.3f}%)" if snap.spread_pct else "")
        print("Recommended:", snap.recommended_prices())

    asyncio.run(_demo())
