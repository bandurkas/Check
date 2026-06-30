"""Rule-based "что делать с заявкой прямо сейчас" — see ADVISOR_DESIGN.md.

Pure function: signals in, advice + reasons out. Thresholds are rough
starting points per the design doc ("подбираются по факту") — tune here
once we have a few days of real observations, no need to touch main.py.
"""

# Binance P2P charges ~0.1% commission in the crypto asset on EVERY completed
# order, both legs (confirmed 2026-06-30 from the API's own `commission` field,
# not visible anywhere in price/orderbook data) - a cost the advisor was
# completely blind to until today. Across the 2026-06-30 session this ate
# Rp 289,669 against Rp 171,278 of price-only "profit" - net real result was
# a LOSS of about Rp 118,391, despite every individual FIFO match showing
# positive. Most of that day's margin (avg ~12bps/cycle) never even reached
# the ~20bps breakeven line below - it just looked like profit because
# nothing upstream of this constant knew fees existed.
FEE_PCT_PER_LEG = 0.1
ROUND_TRIP_FEE_PCT = FEE_PCT_PER_LEG * 2

RANK_OK_MAX = 5           # rank 1..5 = visible enough, beyond that fix visibility first
DEBT_STALE_MIN = 30       # open rebuy/resell debt older than this = costing real time
NO_FILL_MIN = 20          # no trade in this long = market isn't biting at current price
# Both retuned 2026-06-30 around ROUND_TRIP_FEE_PCT - the old 0.12/0.25 pair
# predates knowing fees exist, and sat entirely inside the loss zone.
TIGHT_SPREAD_PCT = ROUND_TRIP_FEE_PCT + 0.05   # 0.25% - barely past breakeven, still not worth squeezing for
STALE_AD_MIN = 20         # ad price unchanged this long while spread is tight = stop waiting
WIDE_SPREAD_PCT = ROUND_TRIP_FEE_PCT + 0.20    # 0.40% - genuine post-fee margin worth waiting for
FRESH_AD_MIN = 10         # ad younger than this hasn't had time to be noticed yet

# ADVISOR_DESIGN.md item 4: a fast-moving market makes a parked ad go stale
# quicker - halve the patience bars when the price is clearly on the move.
MARKET_FAST_IDR_PER_MIN = 3.0
FAST_MARKET_MULTIPLIER = 0.5

# Directional trend, signed. If the price you'd compete at is moving against
# your side persistently (not just one outlier tick), every minute you wait
# costs more than the spread you're holding out for - dump now, debt age and
# spread thresholds don't matter. This is what would have caught the
# 2026-06-30 loss: bought 17945, market kept falling, sold 17939 six minutes too late.
TREND_AGAINST_IDR_PER_MIN = 2.0

# All user-facing text, keyed by rule id, so compute_advice stays a thin
# decision tree and translation is just a dict swap - no string parsing
# anywhere downstream (main.py/frontend read `state`/numbers, never grep text).
STRINGS = {
    "ru": {
        "advice_floor": "Держи на {price:.0f} (своя себестоимость) — не отдавай ниже",
        "reason_floor": "Рынок ниже твоей средней цены покупки ({cost:.0f}) — жди отскок, не сливай в убыток",
        "rank_with": "ранг #{rank} из {total}",
        "rank_without": "не найдена в выдаче",
        "price_set": "Выставь {price:.0f}",
        "price_set_lc": "выставь {price:.0f}",
        "no_price": "нет данных о цене",
        "dir_falling": "падает",
        "dir_rising": "растёт",
        "advice_push": "Подвинь цену ближе к топу — {price_text}",
        "reason_visibility": "Видимость хуже топ-5 ({rank_text}) — пока тебя не видят, тактика ожидания/скидки вторична",
        "advice_trend": "Скинь сейчас — {price_text} — рынок {direction} против тебя",
        "reason_trend": "Цена {direction} ~{speed:.1f} IDR/мин за последние ~10 мин",
        "reason_trend_wait": "Ждать тут только хуже — долг и спред не важны, пока тренд против тебя",
        "advice_debt": "Скинь цену — {price_text} — время сейчас дороже",
        "reason_debt": "Открытый долг {amount:.0f} USDT висит {age:.0f} мин",
        "reason_last_fill": "Последняя сделка была {mins:.0f} мин назад",
        "advice_tight": "Скинь немного — {price_text} — дальше ждать почти нет смысла",
        "reason_tight_spread": "Спред узкий ({pct:.3f}%, ниже {threshold}%)",
        "reason_stale_ad": "Заявка не двигалась {age:.0f} мин (порог {bar:.0f})",
        "speed_note": "рынок быстро двигается (~{speed:.1f} IDR/мин) — терпение урезано вдвое",
        "advice_wide": "Жди — запас по спреду есть",
        "reason_wide_spread": "Спред широкий ({pct:.3f}%, выше {threshold}%)",
        "reason_fresh_ad": "Заявка свежая ({age:.0f} мин, порог {bar:.0f})",
        "advice_neutral": "Нейтрально — можно подождать, можно скинуть{price_suffix}",
        "neutral_price_suffix": " (можно встать в {price:.0f})",
        "reason_neutral": "Ни один сигнал явно не доминирует — разница небольшая",
    },
    "en": {
        "advice_floor": "Hold at {price:.0f} (your own cost) — don't give it away below that",
        "reason_floor": "Market is below your average buy cost ({cost:.0f}) — wait for a bounce, don't realize a loss",
        "rank_with": "rank #{rank} of {total}",
        "rank_without": "not found in listings",
        "price_set": "Post at {price:.0f}",
        "price_set_lc": "post at {price:.0f}",
        "no_price": "no price data",
        "dir_falling": "falling",
        "dir_rising": "rising",
        "advice_push": "Move closer to the top — {price_text}",
        "reason_visibility": "Visibility worse than top-5 ({rank_text}) — nobody sees you yet, wait/drop tactics are secondary",
        "advice_trend": "Drop now — {price_text} — market is {direction} against you",
        "reason_trend": "Price {direction} ~{speed:.1f} IDR/min over the last ~10 min",
        "reason_trend_wait": "Waiting only makes it worse — debt age and spread don't matter while the trend is against you",
        "advice_debt": "Drop the price — {price_text} — time is costing you more now",
        "reason_debt": "Open debt {amount:.0f} USDT has been open {age:.0f} min",
        "reason_last_fill": "Last fill was {mins:.0f} min ago",
        "advice_tight": "Drop slightly — {price_text} — waiting longer barely matters now",
        "reason_tight_spread": "Spread is tight ({pct:.3f}%, below {threshold}%)",
        "reason_stale_ad": "Ad hasn't moved in {age:.0f} min (threshold {bar:.0f})",
        "speed_note": "market is moving fast (~{speed:.1f} IDR/min) — patience halved",
        "advice_wide": "Wait — there's spread to spare",
        "reason_wide_spread": "Spread is wide ({pct:.3f}%, above {threshold}%)",
        "reason_fresh_ad": "Ad is fresh ({age:.0f} min, threshold {bar:.0f})",
        "advice_neutral": "Neutral — fine to wait or drop{price_suffix}",
        "neutral_price_suffix": " (could post at {price:.0f})",
        "reason_neutral": "No signal clearly dominates — the difference is small",
    },
}


def compute_advice(*, active_side, my_ad, my_ad_age_min, spread_pct,
                    debt_amount, debt_age_min, minutes_since_fill,
                    baseline_price, aggressive_price,
                    market_speed_idr_per_min=None, market_trend_idr_per_min=None,
                    own_avg_cost_idr=None, own_open_usdt=None,
                    lang="ru"):
    """Wraps _compute_advice_raw with a fee-aware cost-basis floor on the SELL
    side: whatever state/price the raw rules land on, never recommend selling
    below break-even on currently-held inventory - own avg buy cost PLUS
    ROUND_TRIP_FEE_PCT, since Binance's ~0.1%-per-leg commission (own_avg_cost_idr,
    own_open_usdt - both from PnLTracker's open FIFO lots) is real money that
    a raw price comparison misses entirely. Selling at exactly your buy price
    nets a real loss once both legs' commission is counted - confirmed
    2026-06-30: a whole session of FIFO-"profitable" trades (Rp 171,278 on
    paper) was actually a Rp 118,391 net loss after fees. own_open_usdt below
    ~1 is treated as "nothing real to protect" so a stale historical average
    can't clamp a position that's effectively empty.

    See _compute_advice_raw for the rest of the parameters and the underlying
    push/trend/debt/tight/wide/neutral decision tree this wraps."""
    S = STRINGS.get(lang, STRINGS["ru"])
    result = _compute_advice_raw(
        active_side=active_side, my_ad=my_ad, my_ad_age_min=my_ad_age_min, spread_pct=spread_pct,
        debt_amount=debt_amount, debt_age_min=debt_age_min, minutes_since_fill=minutes_since_fill,
        baseline_price=baseline_price, aggressive_price=aggressive_price,
        market_speed_idr_per_min=market_speed_idr_per_min, market_trend_idr_per_min=market_trend_idr_per_min,
        lang=lang,
    )
    has_position = own_open_usdt is not None and own_open_usdt > 1
    if active_side == "SELL" and has_position and own_avg_cost_idr is not None:
        breakeven_price = own_avg_cost_idr * (1 + ROUND_TRIP_FEE_PCT / 100)
        if result["recommended_price"] is not None and result["recommended_price"] < breakeven_price:
            result["state"] = "floor"
            result["recommended_price"] = breakeven_price
            result["advice"] = S["advice_floor"].format(price=breakeven_price)
            result["reasons"] = [S["reason_floor"].format(cost=own_avg_cost_idr)]
    return result


def _compute_advice_raw(*, active_side, my_ad, my_ad_age_min, spread_pct,
                         debt_amount, debt_age_min, minutes_since_fill,
                         baseline_price, aggressive_price,
                         market_speed_idr_per_min=None, market_trend_idr_per_min=None,
                         lang="ru"):
    """active_side: "BUY" or "SELL" — which of your two ads matters right now
    (the side carrying open debt, or chosen by raw balance if no debt).

    baseline_price: price that ties the current top of the book (one tick).
    aggressive_price: a deliberately more aggressive price, for when speed
    matters more than the extra fraction of spread.
    market_speed_idr_per_min: how fast the competing top price has been moving
    over the last ~10 min, unsigned (None until enough history has accumulated).
    market_trend_idr_per_min: same window, signed - positive means rising.
    lang: "ru" or "en" - selects the STRINGS table; falls back to "ru".

    Returns a dict including "state" (push|trend|debt|tight|wide|neutral) -
    callers should branch on this, never on the localized "advice" text."""
    S = STRINGS.get(lang, STRINGS["ru"])

    fast_market = (market_speed_idr_per_min is not None
                   and market_speed_idr_per_min > MARKET_FAST_IDR_PER_MIN)
    stale_ad_bar = STALE_AD_MIN * FAST_MARKET_MULTIPLIER if fast_market else STALE_AD_MIN
    fresh_ad_bar = FRESH_AD_MIN * FAST_MARKET_MULTIPLIER if fast_market else FRESH_AD_MIN
    speed_note = S["speed_note"].format(speed=market_speed_idr_per_min) if fast_market else None

    if my_ad is None or my_ad["rank"] > RANK_OK_MAX:
        rank_text = S["rank_with"].format(rank=my_ad["rank"], total=my_ad["total"]) if my_ad else S["rank_without"]
        price_text = S["price_set"].format(price=baseline_price) if baseline_price is not None else S["no_price"]
        return {
            "advice": S["advice_push"].format(price_text=price_text),
            "state": "push",
            "side": active_side,
            "recommended_price": baseline_price,
            "reasons": [S["reason_visibility"].format(rank_text=rank_text)],
        }

    trend_against = (market_trend_idr_per_min is not None and (
        (active_side == "SELL" and market_trend_idr_per_min < -TREND_AGAINST_IDR_PER_MIN)
        or (active_side == "BUY" and market_trend_idr_per_min > TREND_AGAINST_IDR_PER_MIN)
    ))
    if trend_against:
        price_text = S["price_set_lc"].format(price=aggressive_price) if aggressive_price is not None else S["no_price"]
        direction = S["dir_falling"] if active_side == "SELL" else S["dir_rising"]
        return {
            "advice": S["advice_trend"].format(price_text=price_text, direction=direction),
            "state": "trend",
            "side": active_side,
            "recommended_price": aggressive_price,
            "reasons": [
                S["reason_trend"].format(direction=direction, speed=abs(market_trend_idr_per_min)),
                S["reason_trend_wait"],
            ],
        }

    if (debt_amount > 1 and debt_age_min is not None and debt_age_min > DEBT_STALE_MIN
            and minutes_since_fill is not None and minutes_since_fill > NO_FILL_MIN):
        price_text = S["price_set_lc"].format(price=aggressive_price) if aggressive_price is not None else S["no_price"]
        return {
            "advice": S["advice_debt"].format(price_text=price_text),
            "state": "debt",
            "side": active_side,
            "recommended_price": aggressive_price,
            "reasons": [
                S["reason_debt"].format(amount=debt_amount, age=debt_age_min),
                S["reason_last_fill"].format(mins=minutes_since_fill),
            ],
        }

    if (spread_pct is not None and spread_pct < TIGHT_SPREAD_PCT
            and my_ad_age_min is not None and my_ad_age_min > stale_ad_bar):
        price_text = S["price_set_lc"].format(price=aggressive_price) if aggressive_price is not None else S["no_price"]
        reasons = [
            S["reason_tight_spread"].format(pct=spread_pct, threshold=TIGHT_SPREAD_PCT),
            S["reason_stale_ad"].format(age=my_ad_age_min, bar=stale_ad_bar),
        ]
        if speed_note:
            reasons.append(speed_note)
        return {
            "advice": S["advice_tight"].format(price_text=price_text),
            "state": "tight",
            "side": active_side,
            "recommended_price": aggressive_price,
            "reasons": reasons,
        }

    if (spread_pct is not None and spread_pct > WIDE_SPREAD_PCT
            and my_ad_age_min is not None and my_ad_age_min < fresh_ad_bar):
        reasons = [
            S["reason_wide_spread"].format(pct=spread_pct, threshold=WIDE_SPREAD_PCT),
            S["reason_fresh_ad"].format(age=my_ad_age_min, bar=fresh_ad_bar),
        ]
        if speed_note:
            reasons.append(speed_note)
        return {
            "advice": S["advice_wide"],
            "state": "wide",
            "side": active_side,
            "recommended_price": None,
            "reasons": reasons,
        }

    price_suffix = S["neutral_price_suffix"].format(price=baseline_price) if baseline_price is not None else ""
    return {
        "advice": S["advice_neutral"].format(price_suffix=price_suffix),
        "state": "neutral",
        "side": active_side,
        "recommended_price": baseline_price,
        "reasons": [S["reason_neutral"]],
    }
