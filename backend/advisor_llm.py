"""LLM-powered advisor using Claude Haiku. Falls back to rule-based advisor.py on any error."""

import json
import logging
import os
from typing import Optional

import anthropic
from dotenv import load_dotenv

from advisor import ROUND_TRIP_FEE_PCT, compute_advice as _rule_compute_advice

load_dotenv()

logger = logging.getLogger(__name__)

# A short timeout + no client-side retries is deliberate: on the async path a hung
# request must not leave the display endpoint pending, and on any failure we fall
# back to the deterministic advisor immediately rather than stalling on retries.
_LLM_TIMEOUT_SEC = 12.0
_LLM_MODEL = "claude-haiku-4-5-20251001"

_client: Optional[anthropic.Anthropic] = None
_async_client: Optional[anthropic.AsyncAnthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            timeout=_LLM_TIMEOUT_SEC,
            max_retries=0,
        )
    return _client


def _get_async_client() -> anthropic.AsyncAnthropic:
    global _async_client
    if _async_client is None:
        _async_client = anthropic.AsyncAnthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            timeout=_LLM_TIMEOUT_SEC,
            max_retries=0,
        )
    return _async_client


_SYSTEM_PROMPT = """You are a Binance P2P USDT/IDR trading advisor. Read the LIVE order book and decide the single best price to set on the user's ACTIVE ad right now.

## Your objective — BALANCE two goals
(A) transact at the best price (SELL as high / BUY as low as possible) — protect margin;
(B) actually fill in a reasonable time — don't sit unfilled while the market moves away.
Neither extreme is correct: racing to rank 1 gives away margin; sitting far above the market never fills. Choose the best price that still fills in a realistic window given market speed and how long the ad has already waited.

## What you receive (JSON input)
- active_side: "SELL" or "BUY" — which ad you are pricing.
- orderbook.ask_ladder: sellers of USDT, ascending price (cheapest first = what a buyer sees first). When active_side=SELL, this is YOUR COMPETITION.
- orderbook.bid_ladder: buyers of USDT, descending price (highest bid first). When active_side=SELL, this is the DEMAND that fills you.
- Each ladder entry: {price, available (USDT liquidity at that ad), min_limit, max_limit (IDR per order), merchant}.
- orderbook.best_ask/best_bid/robust_ask/robust_bid/spread_idr/spread_pct: summary top-of-book (robust = outlier-resistant median).
- my_ad: your current ad {rank, total, price, available} or null if not posted.
- own_max_cost_idr / own_avg_cost_idr / own_open_usdt: your inventory cost basis and size.
- baseline_price / aggressive_price: reference anchors (≈rank-1 price and a slightly-more-aggressive price). Use as sanity checks, not mandates — you may pick any price justified by the book.
- market_trend_idr_per_min (signed; negative = falling), market_speed_idr_per_min (absolute).
- my_ad_age_min, minutes_since_fill, debt_amount, debt_age_min, spread_pct.

## Constants
- ROUND_TRIP_FEE_PCT = 0.20% (Binance ~0.1% commission on each leg). SELL breakeven = own_max_cost_idr * 1.002. NEVER recommend a SELL below breakeven — the caller also hard-enforces this.
- A rank of 1-5 is visible to buyers; deeper is progressively ignored.

## How to reason (SELL side — mirror the logic for BUY)
1. Locate the real cluster of competing sellers in ask_ladder and where buyers actually bid in bid_ladder. Ignore lone outliers far from the cluster (don't chase or trust a single off-price ad).
2. Fast fill ⇒ price at or just below the cheapest serious competitors (low rank). High price ⇒ sit above part of the competition, accepting a slower fill.
3. Pick the HIGHEST price that still sits inside a realistic fill window:
   - Fresh ad + calm market + comfortable margin above breakeven ⇒ lean HIGH, be patient.
   - Stale ad with no fills, or fast adverse trend (market moving away) ⇒ lean toward the competitive price to actually transact.
   - Weigh depth: thin liquidity above you fills faster; a thick wall of cheaper competitors means you must undercut to be seen.
4. If the whole book is below your SELL breakeven, HOLD — return state="wide", recommended_price=null.

## States (pick exactly one)
- "push": my_ad is null OR rank > 5 (not visible) → set a competitive, visible price. recommended_price set.
- "trend": market moving fast against you → act now at a competitive price. recommended_price set.
- "debt": debt_amount > 1 with stale age and no recent fill → clear it. recommended_price set.
- "neutral": normal reprice to your chosen balanced target. recommended_price set.
- "wide": current price is well-placed for margin and the ad is still fresh → WAIT. recommended_price = null.
Do NOT output "floor" or "tight"; the caller handles the breakeven floor. Only set recommended_price != null when you truly want the ad moved.

## Output — strict JSON, no markdown, no extra text
{
  "state": "push|trend|debt|neutral|wide",
  "side": "<copy active_side exactly>",
  "recommended_price": <float or null>,
  "advice": "<one concrete sentence: the exact price to set, or an explicit wait>",
  "reasons": ["<why, referencing the book>", "<optional second reason>"]
}

Write advice and reasons in the "lang" language ("ru" → Russian, "en" → English, default English). Be concise, never hedge. Do NOT apply the cost-basis floor yourself — the caller does. Respond ONLY with the JSON object."""


def _build_payload(inputs: dict, orderbook=None) -> str:
    """Serialize the advice inputs (plus the live order book, when given) into the
    JSON user-message the model reads."""
    keys = (
        "active_side", "my_ad", "my_ad_age_min", "spread_pct", "debt_amount",
        "debt_age_min", "minutes_since_fill", "baseline_price", "aggressive_price",
        "market_speed_idr_per_min", "market_trend_idr_per_min",
        "own_avg_cost_idr", "own_max_cost_idr", "own_open_usdt", "lang",
    )
    data = {k: inputs.get(k) for k in keys}
    if orderbook is not None:
        data["orderbook"] = orderbook
    return json.dumps(data, ensure_ascii=False)


def _parse_and_floor(raw: str, *, active_side, own_avg_cost_idr, own_max_cost_idr,
                     own_open_usdt, lang) -> dict:
    """Parse the model's raw text into a validated advice dict and clamp any SELL
    recommendation to the fee-aware cost-basis floor (same rule as advisor.py)."""
    raw = raw.strip()
    # Strip markdown code fences the model sometimes adds despite instructions
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()
    result = json.loads(raw)

    required = {"state", "side", "recommended_price", "advice", "reasons"}
    missing = required - result.keys()
    if missing:
        raise ValueError(f"LLM response missing keys: {missing}")
    valid_states = {"push", "trend", "debt", "tight", "wide", "neutral", "floor"}
    if result["state"] not in valid_states:
        raise ValueError(f"Unknown state from LLM: {result['state']!r}")
    if not isinstance(result["reasons"], list):
        raise ValueError("reasons must be a list")

    has_position = own_open_usdt is not None and own_open_usdt > 1
    cost_for_floor = own_max_cost_idr if own_max_cost_idr is not None else own_avg_cost_idr
    if active_side == "SELL" and has_position and cost_for_floor is not None:
        breakeven_price = cost_for_floor * (1 + ROUND_TRIP_FEE_PCT / 100)
        if result["recommended_price"] is not None and result["recommended_price"] < breakeven_price:
            _floor_strings = {
                "ru": (
                    f"Держи на {breakeven_price:.0f} (своя себестоимость) — не отдавай ниже",
                    f"Рынок ниже твоей средней цены покупки ({cost_for_floor:.0f}) — жди отскок, не сливай в убыток",
                ),
                "en": (
                    f"Hold at {breakeven_price:.0f} (your own cost) — don't give it away below that",
                    f"Market is below your average buy cost ({cost_for_floor:.0f}) — wait for a bounce, don't realize a loss",
                ),
            }
            advice_text, reason_text = _floor_strings.get(lang, _floor_strings["ru"])
            result["state"] = "floor"
            result["recommended_price"] = breakeven_price
            result["advice"] = advice_text
            result["reasons"] = [reason_text]

    return result


def compute_advice_llm(*, orderbook=None, **inputs):
    """Synchronous LLM advisor. Falls back to rule-based advisor on any error.

    Note: safe only from synchronous callers — the blocking API call would stall
    an async event loop. Async handlers must use compute_advice_llm_async()."""
    try:
        response = _get_client().messages.create(
            model=_LLM_MODEL,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_payload(inputs, orderbook)}],
        )
        return _parse_and_floor(
            response.content[0].text,
            active_side=inputs["active_side"],
            own_avg_cost_idr=inputs.get("own_avg_cost_idr"),
            own_max_cost_idr=inputs.get("own_max_cost_idr"),
            own_open_usdt=inputs.get("own_open_usdt"),
            lang=inputs.get("lang", "ru"),
        )
    except Exception as exc:
        logger.warning("advisor_llm: falling back to rule-based (%s: %s)", type(exc).__name__, exc)
        return _rule_compute_advice(**inputs)


async def compute_advice_llm_async(*, orderbook=None, **inputs):
    """Async-safe LLM advisor for use from FastAPI handlers. Uses AsyncAnthropic so
    the call never blocks the event loop, and falls back to the deterministic
    rule-based advisor on timeout, rate-limit, parse error, or any other failure.
    `orderbook` is the live book snapshot for the model to read; it is LLM-only and
    is not forwarded to the rule-based fallback."""
    try:
        response = await _get_async_client().messages.create(
            model=_LLM_MODEL,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_payload(inputs, orderbook)}],
        )
        return _parse_and_floor(
            response.content[0].text,
            active_side=inputs["active_side"],
            own_avg_cost_idr=inputs.get("own_avg_cost_idr"),
            own_max_cost_idr=inputs.get("own_max_cost_idr"),
            own_open_usdt=inputs.get("own_open_usdt"),
            lang=inputs.get("lang", "ru"),
        )
    except Exception as exc:
        logger.warning("advisor_llm: falling back to rule-based (%s: %s)", type(exc).__name__, exc)
        return _rule_compute_advice(**inputs)
