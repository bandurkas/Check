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

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


_SYSTEM_PROMPT = """You are a Binance P2P USDT/IDR trading advisor. Give a single, concrete "what to do right now" recommendation to a trader running both a BUY and SELL ad simultaneously.

## Constants
- FEE_PCT_PER_LEG = 0.1%  (Binance charges ~0.1% on every completed order on each leg)
- ROUND_TRIP_FEE_PCT = 0.20%  (total fee for one buy+sell round trip)
- RANK_OK_MAX = 5           (rank 1-5 = visible; above 5 → fix visibility first)
- DEBT_STALE_MIN = 30       (open debt older than 30 min is costly)
- NO_FILL_MIN = 20          (no fill in 20+ min = market isn't biting)
- TIGHT_SPREAD_PCT = 0.25%  (barely past breakeven — not worth waiting)
- WIDE_SPREAD_PCT = 0.40%   (genuine post-fee margin worth holding for)
- STALE_AD_MIN = 20         (ad unchanged this long while spread is tight → stop waiting)
- FRESH_AD_MIN = 10         (ad younger than this hasn't had time to be noticed)
- MARKET_FAST_IDR_PER_MIN = 3.0  (above this → halve patience thresholds)
- FAST_MARKET_MULTIPLIER = 0.5
- TREND_AGAINST_IDR_PER_MIN = 2.0  (persistent adverse trend → act now)

## Decision priority (apply in order, stop at first match)

fast_market = (market_speed_idr_per_min is not None AND market_speed_idr_per_min > 3.0)
stale_ad_bar = 20 * 0.5 if fast_market else 20
fresh_ad_bar = 10 * 0.5 if fast_market else 10

1. PUSH — my_ad is null OR my_ad.rank > 5
   Fix visibility. recommended_price = baseline_price.

2. TREND — market_trend_idr_per_min is not None AND:
   - SELL and trend < -2.0  (market falling against seller)
   - BUY  and trend > +2.0  (market rising against buyer)
   Act now. recommended_price = aggressive_price.

3. DEBT — debt_amount > 1 AND debt_age_min > 30 AND minutes_since_fill > 20
   Stale open debt, no fills. recommended_price = aggressive_price.

4. TIGHT — spread_pct < 0.25 AND my_ad_age_min > stale_ad_bar
   Spread barely covers fees, ad stale. recommended_price = aggressive_price.

5. WIDE — spread_pct > 0.40 AND my_ad_age_min < fresh_ad_bar
   Good spread, ad fresh. Wait. recommended_price = null.

6. NEUTRAL — none of the above. recommended_price = baseline_price.

## Output — strict JSON, no markdown, no extra text
{
  "state": "push|trend|debt|tight|wide|neutral",
  "side": "<copy active_side exactly>",
  "recommended_price": <float or null>,
  "advice": "<one-sentence action in the lang from input>",
  "reasons": ["<reason 1>", "<optional reason 2>"]
}

Write advice and reasons in the language given by the "lang" field:
- "ru" → Russian
- "en" → English (default if unknown)

Be concise. Advice must state the concrete action: price to set, or explicit "wait". Never hedge.
Do NOT apply cost-basis floor protection — the caller handles that.
Respond ONLY with the JSON object."""


def compute_advice_llm(*, active_side, my_ad, my_ad_age_min, spread_pct,
                        debt_amount, debt_age_min, minutes_since_fill,
                        baseline_price, aggressive_price,
                        market_speed_idr_per_min=None, market_trend_idr_per_min=None,
                        own_avg_cost_idr=None, own_max_cost_idr=None, own_open_usdt=None,
                        lang="ru"):
    """LLM-powered advisor. Falls back to rule-based compute_advice() on any error."""
    try:
        payload = {
            "active_side": active_side,
            "my_ad": my_ad,
            "my_ad_age_min": my_ad_age_min,
            "spread_pct": spread_pct,
            "debt_amount": debt_amount,
            "debt_age_min": debt_age_min,
            "minutes_since_fill": minutes_since_fill,
            "baseline_price": baseline_price,
            "aggressive_price": aggressive_price,
            "market_speed_idr_per_min": market_speed_idr_per_min,
            "market_trend_idr_per_min": market_trend_idr_per_min,
            "own_avg_cost_idr": own_avg_cost_idr,
            "own_max_cost_idr": own_max_cost_idr,
            "own_open_usdt": own_open_usdt,
            "lang": lang,
        }

        client = _get_client()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )

        raw = response.content[0].text.strip()
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

        # Apply cost-basis floor (same logic as advisor.py wrapper)
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

    except Exception as exc:
        logger.warning("advisor_llm: falling back to rule-based (%s: %s)", type(exc).__name__, exc)
        return _rule_compute_advice(
            active_side=active_side, my_ad=my_ad, my_ad_age_min=my_ad_age_min,
            spread_pct=spread_pct, debt_amount=debt_amount, debt_age_min=debt_age_min,
            minutes_since_fill=minutes_since_fill, baseline_price=baseline_price,
            aggressive_price=aggressive_price,
            market_speed_idr_per_min=market_speed_idr_per_min,
            market_trend_idr_per_min=market_trend_idr_per_min,
            own_avg_cost_idr=own_avg_cost_idr, own_max_cost_idr=own_max_cost_idr,
            own_open_usdt=own_open_usdt, lang=lang,
        )
