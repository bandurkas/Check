"""Watches the Binance P2P "Processing" orders tab for a counterparty taking
one of our ads. Binance gives the paying side 15 minutes to transfer once an
order opens - this exists purely to surface that moment fast (it's a private,
authenticated page, so same as ad_repricer.py this drives the real UI in a
dedicated tab rather than a bare fetch(), which "please log in"s on private
endpoints even when logged in).

This never acts on an order - no clicking "paid", no chat, nothing. It only
detects "a new order appeared since last poll" and hands that to main.py to
surface on the dashboard. The actual transfer is a real bank action only the
human can do.
"""
import time

from playwright.async_api import async_playwright

from focus_guard import FocusGuard

CDP_URL = "http://localhost:9222"
PROCESSING_URL = "https://p2p.binance.com/en/fiatOrder?tab=0&page=1"


class OrderWatcher:
    def __init__(self):
        self.seen_order_numbers = set()
        self.new_orders = []  # cleared once main.py's poller reads it
        self.last_poll_at = None
        self.last_error = None

    async def poll(self):
        """Never raises - this runs from a background loop that must keep
        going even if one poll fails (page hiccup, Cloudflare blip, etc)."""
        try:
            async with FocusGuard(), async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(CDP_URL)
                ctx = browser.contexts[0]
                page = await ctx.new_page()
                try:
                    await page.goto(PROCESSING_URL, wait_until="domcontentloaded", timeout=15000)
                    # Wait for the table to actually settle (rows OR the "No records"
                    # empty-state text) instead of a blind fixed sleep - under load from
                    # auto-reprice/order_watch sharing the same browser, a flat 2s wasn't
                    # always enough and produced false "empty read" results.
                    try:
                        await page.wait_for_function(
                            """() => document.body.innerText.includes('No records')
                               || document.querySelectorAll('tr[aria-rowindex] td').length > 0""",
                            timeout=8000,
                        )
                    except Exception:
                        pass  # fall through - the rows/confirmed_empty check below handles it either way
                    rows = await page.evaluate(
                        """() => {
                          const out = [];
                          document.querySelectorAll('tr[aria-rowindex]').forEach(tr => {
                            const cells = tr.querySelectorAll('td');
                            if (cells.length < 5) return;
                            const m = cells[1] ? cells[1].textContent.trim().match(/\\d{10,}/) : null;
                            if (!m) return;
                            out.push({
                              order_number: m[0],
                              type_date: cells[0].textContent.trim(),
                              price: cells[2].textContent.trim(),
                              amount: cells[3].textContent.trim(),
                              counterparty: cells[4].textContent.trim(),
                              status: cells[5] ? cells[5].textContent.trim() : null,
                            });
                          });
                          return out;
                        }"""
                    )
                    if not rows:
                        # Could be a genuinely empty Processing tab, or could be a slow
                        # render that hasn't painted rows yet - only trust "empty" when
                        # the page itself says so, otherwise keep the last known set
                        # rather than wiping it (losing track of a real pending order
                        # mid payment-window would un-gate auto-reprice on it).
                        confirmed_empty = await page.evaluate("() => document.body.innerText.includes('No records')")
                        if not confirmed_empty:
                            self.last_error = "empty read, not confirmed - kept previous pending set"
                            return
                    current = {r["order_number"]: r for r in rows}
                    new_numbers = set(current) - self.seen_order_numbers
                    if new_numbers:
                        self.new_orders.extend(current[n] for n in new_numbers)
                    self.seen_order_numbers = set(current)
                    self.last_poll_at = time.time()
                    self.last_error = None
                finally:
                    for closer in (page.close, browser.close):
                        try:
                            await closer()
                        except Exception:
                            pass
        except Exception as e:
            self.last_error = str(e)

    def drain_new_orders(self):
        """Returns and clears the new-orders-since-last-check list - call
        this from the API handler so each new order is surfaced exactly
        once, not re-alerted on every poll until the page is reloaded."""
        orders = self.new_orders
        self.new_orders = []
        return orders

    def has_pending(self):
        """True while any order is sitting in Processing/Pending payment -
        auto_reprice_loop checks this before touching anything: an ad mid
        live transaction with a real counterparty isn't a free target for
        a price edit, regardless of what the dashboard's "active" reading
        of it shows (2026-07-01: reprice fired on an ad that had just had
        an order opened against it - this is the guard that should have
        stopped it)."""
        return len(self.seen_order_numbers) > 0
