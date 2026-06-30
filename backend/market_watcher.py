"""Polls Binance P2P order book (USDT/IDR, Bank Transfer) via an already
authenticated Opera tab over the Chrome DevTools Protocol, so requests never
hit Binance's Cloudflare bot-challenge directly.

Requires Opera running with --remote-debugging-port=9222 and at least one
open tab on binance.com (logged in to P2P).
"""
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from playwright.async_api import async_playwright

CDP_URL = "http://localhost:9222"
SEARCH_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
ASSET = "USDT"
FIAT = "IDR"
PAY_TYPES = ["BANK"]  # Bank Transfer identifier for IDR
TICK = 1  # IDR, smallest price increment to undercut/outbid by
MY_NICKNAME = "bandurkas"
MY_AD_CHECK_INTERVAL = 10.0  # seconds - deeper, multi-page search, so kept separate from the 4s top-10 poll
PRICE_HISTORY_MAX_AGE_SEC = 15 * 60  # ADVISOR_DESIGN.md item 4 - market-speed lookback window


@dataclass
class OrderBookSnapshot:
    timestamp: float = field(default_factory=time.time)
    buy_ads: list = field(default_factory=list)   # ads where YOU would BUY (merchants selling)
    sell_ads: list = field(default_factory=list)  # ads where YOU would SELL (merchants buying)

    @property
    def best_buy_price(self):
        """Literal top of book, including any one-off outlier ad - informational only."""
        return self.buy_ads[0]["price"] if self.buy_ads else None

    @property
    def best_sell_price(self):
        """Literal top of book, including any one-off outlier ad - informational only."""
        return self.sell_ads[0]["price"] if self.sell_ads else None

    @staticmethod
    def _robust_price(ads, n=3):
        """Median of the top-n prices. A single outlier ad (e.g. someone posting
        far off the real cluster) can't drag this the way it drags a raw top-1
        read - that's what fed the advisor a bogus negative spread before."""
        if not ads:
            return None
        prices = sorted(a["price"] for a in ads[:n])
        mid = len(prices) // 2
        if len(prices) % 2:
            return prices[mid]
        return (prices[mid - 1] + prices[mid]) / 2

    @property
    def robust_buy_price(self):
        return self._robust_price(self.buy_ads)

    @property
    def robust_sell_price(self):
        return self._robust_price(self.sell_ads)

    @property
    def spread_idr(self):
        if self.robust_buy_price is None or self.robust_sell_price is None:
            return None
        return self.robust_buy_price - self.robust_sell_price

    @property
    def spread_pct(self):
        if not self.spread_idr or not self.robust_sell_price:
            return None
        mid = (self.robust_buy_price + self.robust_sell_price) / 2
        return (self.spread_idr / mid) * 100

    def recommended_prices(self):
        """Price to post your own ads at, one tick inside the robust (outlier-resistant) best."""
        rec = {}
        if self.robust_buy_price is not None:
            # You are SELLING USDT -> want to be the cheapest BUY-side ad (lowest ask)
            rec["sell_usdt_at"] = self.robust_buy_price - TICK
        if self.robust_sell_price is not None:
            # You are BUYING USDT -> want to be the highest SELL-side bid
            rec["buy_usdt_at"] = self.robust_sell_price + TICK
        return rec


class MarketWatcher:
    def __init__(self, poll_interval: float = 4.0):
        self.poll_interval = poll_interval
        self.latest = OrderBookSnapshot()
        self._playwright = None
        self._browser = None
        self._page = None
        self._running = False
        # Populated by run_my_ad_forever(): {"rank": int, "total": int, "price": float, ...} or None
        self.my_sell_ad = None  # my ad to SELL USDT (shows up when others search to BUY)
        self.my_buy_ad = None   # my ad to BUY USDT (shows up when others search to SELL)
        self.my_ads_updated_at = 0.0
        # Timestamp of the last observed price change for each ad (0.0 = never seen yet,
        # which reads as "infinitely stale" until the first successful my-ad poll).
        self.my_sell_ad_changed_at = 0.0
        self.my_buy_ad_changed_at = 0.0
        self._reconnect_lock = asyncio.Lock()
        self._price_history = deque()  # [(ts, robust_buy_price, robust_sell_price), ...], newest last

    async def _connect(self):
        async with self._reconnect_lock:
            # A previous CDP session may still be hanging around (target closed,
            # browser disconnected) - drop it before opening a new one rather
            # than leaking playwright/browser handles on every reconnect.
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
            self._page = None
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
        self._record_price_history(self.latest)
        return self.latest

    def _record_price_history(self, snap):
        now = snap.timestamp
        self._price_history.append((now, snap.robust_buy_price, snap.robust_sell_price))
        while self._price_history and now - self._price_history[0][0] > PRICE_HISTORY_MAX_AGE_SEC:
            self._price_history.popleft()

    def price_trend_idr_per_min(self, side, lookback_min=10):
        """Signed price velocity, IDR/min, over the last `lookback_min` minutes
        (or however much history exists so far). Positive = price rising,
        negative = price falling. side: "buy" or "sell" - which
        OrderBookSnapshot price to track. None if not enough history yet."""
        if len(self._price_history) < 2:
            return None
        idx = 1 if side == "buy" else 2
        now, *_ = self._price_history[-1]
        current_price = self._price_history[-1][idx]
        cutoff = now - lookback_min * 60
        oldest_in_window = next((p for p in self._price_history if p[0] >= cutoff), None)
        if oldest_in_window is None or current_price is None or oldest_in_window[idx] is None:
            return None
        elapsed_min = (now - oldest_in_window[0]) / 60
        if elapsed_min < 0.5:  # too little history yet to be meaningful
            return None
        return (current_price - oldest_in_window[idx]) / elapsed_min

    def price_velocity_idr_per_min(self, side, lookback_min=10):
        """Unsigned speed - how fast the price has been moving, regardless of
        direction. See price_trend_idr_per_min for the signed version."""
        trend = self.price_trend_idr_per_min(side, lookback_min)
        return abs(trend) if trend is not None else None

    async def _search_full(self, trade_type: str, max_pages: int = 6):
        """Multi-page search, no payType filter, used to locate our own ad
        regardless of which payment methods it advertises."""
        all_rows = []
        for page_num in range(1, max_pages + 1):
            payload = {
                "asset": ASSET,
                "fiat": FIAT,
                "tradeType": trade_type,
                "page": page_num,
                "rows": 20,
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
            body = result["body"]
            if result["status"] != 200 or not body.get("success") or not body.get("data"):
                break
            all_rows.extend(body["data"])
        return all_rows

    async def find_my_ad(self, trade_type: str):
        """trade_type='BUY' -> search ads I'd buy from, where MY SELL ad would appear.
        trade_type='SELL' -> search ads I'd sell to, where MY BUY ad would appear."""
        items = await self._search_full(trade_type)
        for i, item in enumerate(items):
            nick = item.get("advertiser", {}).get("nickName", "")
            if nick.lower() == MY_NICKNAME:
                adv = item["adv"]
                return {
                    "rank": i + 1,
                    "total": len(items),
                    "price": float(adv["price"]),
                    "available": float(adv.get("surplusAmount") or adv.get("tradableQuantity") or 0),
                    "min_limit": float(adv.get("minSingleTransAmount") or 0),
                    "max_limit": float(adv.get("maxSingleTransAmount") or 0),
                }
        return None

    async def _reconnect_after_error(self, label, error):
        print(f"[market_watcher] {label} error: {error}")
        try:
            await self._connect()
            print("[market_watcher] reconnected")
        except Exception as reconnect_error:
            print(f"[market_watcher] reconnect failed: {reconnect_error}")

    async def run_forever(self):
        await self._connect()
        self._running = True
        while self._running:
            try:
                await self.poll_once()
            except Exception as e:
                await self._reconnect_after_error("poll", e)
            await asyncio.sleep(self.poll_interval)

    @staticmethod
    def _price_changed(old_ad, new_ad):
        old_price = old_ad["price"] if old_ad else None
        new_price = new_ad["price"] if new_ad else None
        return old_price != new_price

    async def run_my_ad_forever(self):
        while self._page is None:
            await asyncio.sleep(0.5)
        while True:
            try:
                new_sell_ad = await self.find_my_ad("BUY")
                if self._price_changed(self.my_sell_ad, new_sell_ad):
                    self.my_sell_ad_changed_at = time.time()
                self.my_sell_ad = new_sell_ad

                new_buy_ad = await self.find_my_ad("SELL")
                if self._price_changed(self.my_buy_ad, new_buy_ad):
                    self.my_buy_ad_changed_at = time.time()
                self.my_buy_ad = new_buy_ad

                self.my_ads_updated_at = time.time()
            except Exception as e:
                await self._reconnect_after_error("my-ad check", e)
            await asyncio.sleep(MY_AD_CHECK_INTERVAL)

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
