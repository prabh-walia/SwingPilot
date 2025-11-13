# strategy.py
from indicators import ema
import numpy as np
import logging
from datetime import datetime, timezone
import pandas as pd
from config_loader import config

logger = logging.getLogger("swingpilot.strategy")


def get_last_two_closed(df, timeframe, safety_seconds=1):
    """
    Robust: return (prev, last) = the two most recent FULLY CLOSED candles.

    Approach:
    - compute bar end times = open_time + timeframe
    - check whether the final row is still forming
    - if forming -> last closed is df.iloc[-2], prev is df.iloc[-3]
    - else last closed is df.iloc[-1], prev is df.iloc[-2]
    """
    tf = timeframe.upper()
    if tf.startswith("M"):
        mins = int(tf[1:])
    elif tf.startswith("H"):
        mins = int(tf[1:]) * 60
    else:
        raise ValueError("Unsupported timeframe")

    now = datetime.now(timezone.utc)

    # Parse timestamps
    times = pd.to_datetime(df["time"])
    if times.dt.tz is None:
        times = times.dt.tz_localize(timezone.utc)
    else:
        times = times.dt.tz_convert(timezone.utc)

    # Candle close = open + interval
    ends = times + pd.to_timedelta(mins, unit="m")

    if len(df) < 3:
        # fallback — try selecting closed bars only
        closed_mask = ends <= (pd.Timestamp(now) - pd.Timedelta(seconds=safety_seconds))
        closed_df = df[closed_mask]
        if len(closed_df) >= 2:
            return closed_df.iloc[-2], closed_df.iloc[-1]
        return None, None

    # Check if last bar is forming
    last_end = ends.iloc[-1]
    forming_threshold = pd.Timestamp(now) - pd.Timedelta(seconds=safety_seconds)
    is_forming = last_end > forming_threshold

    if is_forming:
        # last row is forming → use -3 and -2
        if len(df) >= 3:
            prev = df.iloc[-3]
            last = df.iloc[-2]
            return prev, last
        return None, None
    else:
        # last row is closed → use -2 and -1
        prev = df.iloc[-2]
        last = df.iloc[-1]
        return prev, last


def analyze_trend(df_h1, emas_trend):
    """
    Return one of: 'true_bull','true_bear','weak_bull','weak_bear','neutral'
    Uses last H1 candle's EMA values.
    IMPORTANT: df_h1 should contain closed H1 candles (no forming bar).
    """
    df = df_h1.copy().reset_index(drop=True)
    for p in emas_trend:
        df[f"ema{p}"] = ema(df["close"], int(p))
    last = df.iloc[-1]
    e = {}
    for p in emas_trend:
        e[p] = float(last.get(f"ema{p}", np.nan))
    e9 = e.get(9, np.nan)
    e20 = e.get(20, np.nan)
    e50 = e.get(50, np.nan)
    logger.debug(f"H1 EMAs: {e}")
    if e9 > e20 > e50:
        return "true_bull"
    if e9 < e20 < e50:
        return "true_bear"
    if e9 < e20 and e20 > e50:
        return "weak_bull"
    if e9 > e20 and e20 < e50:
        return "weak_bear"
    return "neutral"


def candle_is_bull(c):
    return float(c["close"]) > float(c["open"])


def candle_is_bear(c):
    return float(c["close"]) < float(c["open"])


def detect_entry_15m(df_m15, trend):
    """
    Detect entry (long/short) and return:
        direction, reason, order_hint

    order_hint:
        {"type": "market"}
        {"type": "limit", "price": float}
    """
    tol = 1e-12

    prev, last = get_last_two_closed(df_m15, "M15")
    if prev is None or last is None:
        return None, "not enough closed bars", {"type": "market"}

    # DEBUG log (keeps your existing verbose info)
    logger.info(
        f"[CandleCheck] prev_close={float(prev['close']):.6f} last_close={float(last['close']):.6f} "
        f"prev_low={float(prev['low']):.6f} last_low={float(last['low']):.6f} "
        f"prev_high={float(prev['high']):.6f} last_high={float(last['high']):.6f}"
    )

    # config values (make sure these keys are present in config.json)
    big_threshold = float(getattr(config, "LARGE_CANDLE_THRESHOLD_USD", 7.0))
    pullback_pct = float(getattr(config, "LIMIT_PULLBACK_PCT", 0.30))

    last_high = float(last["high"])
    last_low = float(last["low"])
    last_open = float(last["open"])
    last_close = float(last["close"])

    last_range = last_high - last_low
    body = abs(last_close - last_open)

    # -------------------------------------------
    # BULLISH ENTRY
    # -------------------------------------------
    if trend in ("true_bull", "weak_bull"):
        if candle_is_bear(prev) and candle_is_bull(last):

            prev_low = float(prev["low"])
            prev_close = float(prev["close"])

            # Condition for market entry
            valid_market = (
                last_low <= prev_low + tol
                and last_close >= prev_close - tol
            )

            # If big candle → use buy-limit
            if last_range >= big_threshold and body > tol:
                limit_price = last_close - pullback_pct * (last_close - last_open)
                logger.info(
                    f"[BullLimit] Big candle detected (range={last_range:.6f}). Using BUY LIMIT at {limit_price:.6f}"
                )
                return "long", "bull entry with limit (big candle)", {
                    "type": "limit",
                    "price": float(limit_price)
                }

            # Normal market entry
            if valid_market:
                return "long", "bull entry pattern valid", {"type": "market"}

            if not (last_low <= prev_low + tol):
                return None, "green low above prev low", {"type": "market"}
            return None, "green close below prev close", {"type": "market"}

        return None, "no red->green pattern", {"type": "market"}

    # -------------------------------------------
    # BEARISH ENTRY
    # -------------------------------------------
    if trend in ("true_bear", "weak_bear"):
        if candle_is_bull(prev) and candle_is_bear(last):

            prev_high = float(prev["high"])
            prev_close = float(prev["close"])

            valid_market = (
                last_high >= prev_high - tol
                and last_close <= prev_close + tol
            )

            # If big candle → use sell-limit
            if last_range >= big_threshold and body > tol:
                limit_price = last_close + pullback_pct * (last_open - last_close)
                logger.info(
                    f"[BearLimit] Big candle detected (range={last_range:.6f}). Using SELL LIMIT at {limit_price:.6f}"
                )
                return "short", "bear entry with limit (big candle)", {
                    "type": "limit",
                    "price": float(limit_price)
                }

            # Market entry
            if valid_market:
                return "short", "bear entry pattern valid", {"type": "market"}

            if not (last_high >= prev_high - tol):
                return None, "red high below prev high", {"type": "market"}
            return None, "red close above prev close", {"type": "market"}

        return None, "no green->red pattern", {"type": "market"}

    return None, "trend not supporting entry", {"type": "market"}
