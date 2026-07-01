"""Automates Binance P2P ad price edits through the live, already-logged-in
Opera CDP session - the same trick market_watcher.py uses for reads.

Raw API replay does NOT work here: a bare fetch() to the private adv/update
endpoint from page context returns "Please log in first" even on an
authenticated page (confirmed 2026-06-30) - Binance's own JS attaches extra
auth headers beyond cookies that a hand-built fetch() doesn't have. So this
drives the real /en/advEdit form instead, the same way a human would: fill
the price field, click Post, then click "Confirm to post" on the modal that
follows (missed on the first version of this module - clicking Post alone
opens the modal but submits nothing, which silently no-ops the whole thing).
Done in a dedicated tab (not the user's active one) so it never disturbs
whatever they're looking at, and never collides with their own clicks.

Success is verified against the My Ads list page, not the public search
index - search lags behind the real price by an unknown amount, so trusting
"no error was thrown" was the bug that made v1 of this report false
positives.
"""
import math
import time

from playwright.async_api import async_playwright

from focus_guard import FocusGuard

CDP_URL = "http://localhost:9222"
EDIT_URL = "https://p2p.binance.com/en/advEdit?code={adv_no}"
MYADS_URL = "https://p2p.binance.com/en/myads?type=normal&code=default"
# Binance's own verification-challenge copy varies, so this is intentionally
# broad - false positives just trip the kill switch early, which is safe;
# a false negative (missing a real 2FA prompt) is the failure mode that
# isn't, so err toward catching too much.
TWOFA_PATTERN = "text=/verification code|authenticator|SMS code|security verification|enter the code/i"


class AdRepricer:
    def __init__(self, max_delta_idr=50, cooldown_sec=120):
        self.enabled = False  # kill switch - off by default, explicit opt-in only
        self.max_delta_idr = max_delta_idr
        self.cooldown_sec = cooldown_sec
        self._last_reprice_at = {}  # adv_no -> unix ts
        self.last_result = None  # {"ok", "message", "at"} - surfaced on the dashboard

    def _record(self, ok, message):
        self.last_result = {"ok": ok, "message": message, "at": time.time()}
        return ok, message

    def _cooldown_ok(self, adv_no):
        return time.time() - self._last_reprice_at.get(adv_no, 0) >= self.cooldown_sec

    @staticmethod
    async def _read_live_price(page, adv_no):
        """Ground truth: the price My Ads actually shows for this advNo, not
        what the search index returns (that lags) or what we typed (that's
        only real once Binance has accepted it)."""
        rows = await page.evaluate(
            """() => {
              const out = [];
              document.querySelectorAll('tr[aria-rowindex]').forEach(tr => {
                const cells = tr.querySelectorAll('td');
                if (cells.length < 6) return;
                const m = cells[0].textContent.trim().match(/\\d{10,}/);
                if (!m) return;
                out.push({advNo: m[0], priceText: cells[2].textContent.trim()});
              });
              return out;
            }"""
        )
        for r in rows:
            if r["advNo"] == adv_no:
                digits = "".join(ch for ch in r["priceText"].split("--")[0] if ch.isdigit())
                return float(digits) if digits else None
        return None

    async def reprice(self, adv_no: str, current_price: float, new_price: float, is_pending_fn=None):
        """Returns (ok, message). Never raises - callers run this from a
        background loop that must keep going even if one attempt fails.

        is_pending_fn: optional zero-arg callable, re-checked right before
        the Post click and right before the Confirm click - the caller's
        own pending-order check (e.g. main.py's order_watcher.has_pending)
        is only read once before this coroutine starts, and the whole flow
        below can run for several real seconds; re-checking right at the
        two points of no return narrows that gap instead of trusting a
        check that's already stale by the time it matters."""
        if not self.enabled:
            return self._record(False, "disabled (kill switch off)")
        if not adv_no:
            return self._record(False, "no adv_no - can't edit an ad I can't identify")
        delta = new_price - current_price
        if abs(delta) > self.max_delta_idr:
            return self._record(False, f"refused: delta {delta:+.0f} IDR exceeds safety bound {self.max_delta_idr}")
        if not self._cooldown_ok(adv_no):
            return self._record(False, "cooldown active, skipping this cycle")
        if is_pending_fn and is_pending_fn():
            return self._record(False, "order appeared pending just before submit - aborted, nothing touched")

        try:
            async with FocusGuard(), async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(CDP_URL)
                ctx = browser.contexts[0]
                page = await ctx.new_page()
                try:
                    await page.goto(EDIT_URL.format(adv_no=adv_no), wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_selector('input[name="rate"]', timeout=10000)
                    rate = page.locator('input[name="rate"]')
                    await rate.click()
                    await rate.fill(str(math.ceil(new_price)))

                    post_btn = page.get_by_role("button", name="Post", exact=True)
                    for _ in range(15):  # poll instead of a fixed sleep - avoids racing React's state update
                        if await post_btn.get_attribute("disabled") is None:
                            break
                        await page.wait_for_timeout(200)
                    else:
                        return self._record(False, "Post stayed disabled after fill - aborted, nothing submitted")

                    if is_pending_fn and is_pending_fn():
                        return self._record(False, "order appeared pending right before Post - aborted, nothing submitted")
                    await post_btn.click()

                    confirm_btn = page.get_by_role("button", name="Confirm to post", exact=True)
                    try:
                        await confirm_btn.wait_for(state="visible", timeout=5000)
                    except Exception:
                        if await page.locator(TWOFA_PATTERN).count() > 0:
                            self.enabled = False
                            return self._record(
                                False, "verification challenge appeared instead of confirm modal - kill switch tripped"
                            )
                        return self._record(False, "confirm modal never appeared after Post click - aborted")

                    if is_pending_fn and is_pending_fn():
                        return self._record(False, "order appeared pending right before Confirm - aborted, not submitted")
                    await confirm_btn.click()
                    await page.wait_for_timeout(1500)

                    if await page.locator(TWOFA_PATTERN).count() > 0:
                        self.enabled = False
                        return self._record(
                            False, "verification challenge appeared after confirming - kill switch tripped, needs manual check"
                        )

                    # The submission itself is done at this point - Confirm was clicked
                    # and no 2FA prompt appeared, so Binance almost certainly has the
                    # new price now. Set cooldown here, not after verification: a busy
                    # browser (many CDP tabs at once) can make the verification nav
                    # below time out on its own, and if cooldown only got set on a
                    # clean verify, that flaky-but-harmless timeout was retriggering a
                    # full repost every loop tick (2026-07-01 - looked like the bot
                    # frantically hammering the price, when the price was actually fine).
                    self._last_reprice_at[adv_no] = time.time()

                    # Ground-truth check, not "no error was thrown" - the only thing
                    # that matters is what Binance actually has on file now. Generous
                    # timeout since this runs after price/order tabs may already be open.
                    await page.goto(MYADS_URL, wait_until="domcontentloaded", timeout=25000)
                    await page.wait_for_timeout(1500)
                    live_price = await self._read_live_price(page, adv_no)
                    if live_price is None:
                        return self._record(False, f"submitted but couldn't verify - {adv_no} not found in My Ads list")
                    if abs(live_price - new_price) > 0.5:
                        # Unlike a flaky verification timeout (genuinely unknown outcome,
                        # cooldown stays put above), a mismatch means we now know for a
                        # fact the ad is wrong AND what the right price should be - no
                        # reason to make the operator wait out the full cooldown to fix
                        # a known-bad state instead of correcting it next loop tick.
                        self._last_reprice_at.pop(adv_no, None)
                        return self._record(
                            False, f"submitted but verification mismatch: live={live_price:.0f} target={new_price:.0f}"
                        )

                    return self._record(True, f"{adv_no}: {current_price:.0f} -> {live_price:.0f} (verified)")
                finally:
                    for closer in (page.close, browser.close):
                        try:
                            await closer()
                        except Exception:
                            pass
        except Exception as e:
            return self._record(False, f"error: {e}")
