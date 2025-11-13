# position_manager.py
import time
import logging
from datetime import datetime, timezone
import MetaTrader5 as mt5
from config_loader import config
# Optional: enable requests to push updates to backend (uncomment + configure)
# import requests

logger = logging.getLogger("swingpilot.position_manager")

# Optional backend config (uncomment and set if you want live dashboard pushes)
# BACKEND_URL = "http://localhost:8000"
# API_KEY = "change_this_to_a_strong_key"

def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def _pos_side(p):
    # MetaTrader5 position type: 0 = buy, 1 = sell
    try:
        return "BUY" if getattr(p, "type", None) == 0 else "SELL"
    except Exception:
        return "UNKNOWN"

def _current_price(symbol, side):
    """Return a best current price (float) for symbol depending on side."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    # for buys current price = ask (what you'd pay), for sells = bid (what you'd get)
    return float(tick.ask if side == "BUY" else tick.bid)

def _format_pos_line(p):
    ticket = getattr(p, "ticket", None)
    vol = float(getattr(p, "volume", 0.0))
    price_open = float(getattr(p, "price_open", 0.0))
    side = _pos_side(p)
    cur_price = _current_price(getattr(p, "symbol", ""), side) or 0.0
    unreal = float(getattr(p, "profit", 0.0))
    age = getattr(p, "time", None)  # sometimes position has time field; not guaranteed
    return {
        "ticket": ticket,
        "side": side,
        "volume": vol,
        "open": price_open,
        "current": cur_price,
        "unreal": unreal,
        "age": age
    }

def _log_snapshot(symbol, positions):
    """Log a formatted snapshot of all positions for symbol."""
    ts = _now_iso()
    total_profit = 0.0
    total_volume = 0.0
    logger.info(f"[{ts}] Position snapshot for {symbol} (count={len(positions)})")
    for p in positions:
        info = _format_pos_line(p)
        total_profit += info["unreal"]
        total_volume += info["volume"]
        logger.info(f"[{ts}]  ticket={info['ticket']} side={info['side']} vol={info['volume']:.4f} open={info['open']} cur={info['current']} unreal={info['unreal']:.2f}")
    logger.info(f"[{ts}]  TOTAL vol={total_volume:.4f} total_unreal={total_profit:.2f}")

    # Optional: push to backend for dashboard (uncomment to enable)
    # try:
    #     payload = {"type":"position_snapshot", "payload":{"symbol":symbol, "ts":ts, "total_unreal": total_profit, "total_vol": total_volume, "positions":[_format_pos_line(p) for p in positions]}}
    #     headers = {"x-api-key": API_KEY}
    #     requests.post(BACKEND_URL + "/api/events", json=payload, headers=headers, timeout=2)
    # except Exception as e:
    #     logger.debug(f"Failed to push position snapshot to backend: {e}")

_tightened_positions = set()

def _safe_getattr(obj, name, default=None):
    try:
        return getattr(obj, name, default)
    except Exception:
        try:
            return obj.get(name, default)
        except Exception:
            return default

def _modify_position_sl(ticket, new_sl, symbol, current_tp=None):
    """
    Try to modify SL of the given position ticket.
    Uses mt5.order_modify() if available; falls back to mt5.order_send() with TRADE_ACTION_SLTP.
    Returns (ok: bool, info: object/res or error)
    """
    if bool(config.DRY_RUN):
        logger.info(f"DRY_RUN: would modify SL for ticket={ticket} to {new_sl} (symbol={symbol})")
        return True, {"dry_run": True}

    # Try mt5.order_modify (signature differs between builds; we attempt safe calls)
    try:
        # Many examples use: mt5.order_modify(ticket, price, sl, tp, deviation)
        # We'll attempt a few variants defensively.
        try:
            # most common:
            res = mt5.order_modify(int(ticket), 0.0, float(new_sl), float(current_tp) if current_tp else 0.0, 0)
            rc = getattr(res, "retcode", None)
            logger.info(f"order_modify(ticket={ticket}) -> retcode={rc} raw={res}")
            if rc == 0 or rc is None:
                return True, res
        except Exception as e1:
            logger.debug(f"order_modify variant 1 failed: {e1}")

        # Fallback: try sending a SL/TP update request using TRADE_ACTION_SLTP if available
        try:
            req = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": int(ticket),
                "sl": float(new_sl),
                "tp": float(current_tp) if current_tp else 0.0,
                "magic": int(config.MAGIC),
                "comment": "SwingPilot-SL-tighten"
            }
            res2 = mt5.order_send(req)
            rc2 = getattr(res2, "retcode", None)
            logger.info(f"order_send(TRAIL SL) ticket={ticket} -> retcode={rc2} raw={res2}")
            if rc2 == 0 or rc2 is None:
                return True, res2
            return False, res2
        except Exception as e2:
            logger.exception(f"order_send(TRAIL SL) failed for ticket={ticket}: {e2}")
            return False, e2

    except Exception as e:
        logger.exception(f"Unexpected exception modifying SL for ticket={ticket}: {e}")
        return False, e

    # If reached, treat as failure
    logger.warning(f"Could not modify SL for ticket={ticket} (no working API path).")
    return False, None


def monitor_position_by_symbol(symbol, poll_interval=5):
    """
    Monitor open positions for `symbol`. Logs profit periodically until the position closes.
    Additionally, applies a one-time SL tightening when the trade reaches TRAIL_TRIGGER_RR.
    This is blocking and intended to be run in the main thread (or a dedicated thread).
    """
    logger.info(f"Starting monitor for {symbol} (poll_interval={poll_interval}s)")
    try:
        while True:
            positions = mt5.positions_get(symbol=symbol)
            if not positions or len(positions) == 0:
                logger.info(f"No open positions for {symbol}. Monitor exiting.")
                break

            # If multiple positions exist, iterate each and sum profits
            total_profit = 0.0
            total_volume = 0.0
            for p in positions:
                # defensive attribute extraction
                ticket = _safe_getattr(p, "ticket", None) or _safe_getattr(p, "order", None)
                vol = float(_safe_getattr(p, "volume", 0.0))
                prof = float(_safe_getattr(p, "profit", 0.0))
                price_open = float(_safe_getattr(p, "price_open", _safe_getattr(p, "price", 0.0)))
                pos_type = int(_safe_getattr(p, "type", 0))   # 0=BUY,1=SELL
                sl_price = _safe_getattr(p, "sl", None)
                tp_price = _safe_getattr(p, "tp", None)

                # live market price for calculation
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    logger.debug("No tick info available while monitoring; skipping price-based checks this round.")
                    current_price = price_open
                else:
                    # For longs, use bid; for shorts use ask as current effective price
                    current_price = float(tick.bid) if pos_type == 0 else float(tick.ask)

                side = "BUY" if pos_type == 0 else "SELL"
                logger.info(f" - ticket={ticket} side={side} vol={vol:.4f} open={price_open} profit={prof:.2f}")

                total_profit += prof
                total_volume += vol

                # --- SL tightening logic ---
                try:
                    # Only consider tightening for our EA-managed positions (we assumed single symbol per EA).
                    # Make sure we haven't tightened this ticket already in this session.
                    if ticket is None:
                        logger.debug("Position missing ticket id; skipping tighten check.")
                    elif ticket in _tightened_positions:
                        logger.debug(f"Ticket {ticket} already tightened earlier; skipping.")
                    else:
                        # compute initial SL distance in price units
                        if sl_price is None or sl_price == 0:
                            # if SL missing in position object, fall back to configured SL distance relative to entry
                            initial_sl_distance = abs(float(config.SL_DISTANCE_USD))
                        else:
                            initial_sl_distance = abs(price_open - float(sl_price))
                            if initial_sl_distance <= 0:
                                initial_sl_distance = abs(float(config.SL_DISTANCE_USD))

                        if initial_sl_distance <= 0:
                            logger.debug("initial_sl_distance evaluated to zero — skipping tighten check.")
                        else:
                            # compute current RR (price-distance based)
                            if pos_type == 0:  # BUY
                                price_move = current_price - price_open
                            else:  # SELL
                                price_move = price_open - current_price

                            current_rr = price_move / initial_sl_distance
                            logger.debug(f"Ticket {ticket} current_rr={current_rr:.3f} (price_move={price_move:.6f} init_sl={initial_sl_distance:.6f})")

                            # check trigger >= configured RR
                            trigger_rr = float(getattr(config, "TRAIL_TRIGGER_RR", 2.0))
                            if current_rr >= trigger_rr:
                                # compute new SL price so remaining price-distance (from current price) equals REDUCED_RISK_USD
                                reduced_risk = float(getattr(config, "REDUCED_RISK_USD", 3.0))
                                entry_price = float(price_open)

                                # desired SL measured from ENTRY (user requested)
                                if pos_type == 0:  # BUY
                                    desired_new_sl = entry_price - reduced_risk
                                else:  # SELL
                                    desired_new_sl = entry_price + reduced_risk

                                # Round for broker precision safety
                                desired_new_sl = round(desired_new_sl, 5)

                                # Safety checks:
                                # - Must actually reduce risk (move SL *towards* entry)
                                # - Must not be placed on the wrong side of the market (would close immediately)
                                current_sl_val = float(sl_price) if (sl_price is not None and sl_price != 0) else None

                                # Determine whether desired_new_sl is an improvement (closer to entry)
                                should_attempt = False
                                if current_sl_val is None:
                                    # no SL present — only allow if desired_new_sl is sensible relative to current price
                                    should_attempt = True
                                else:
                                    if pos_type == 0:  # BUY: closer to entry means desired_new_sl > current_sl_val
                                        if desired_new_sl > current_sl_val:
                                            should_attempt = True
                                    else:  # SELL: closer to entry means desired_new_sl < current_sl_val
                                        if desired_new_sl < current_sl_val:
                                            should_attempt = True

                                # Ensure desired_new_sl does not cross/lie on the wrong side of current market
                                # For BUY: desired_new_sl must be strictly < current_price (not >=)
                                # For SELL: desired_new_sl must be strictly > current_price
                                if should_attempt:
                                    if pos_type == 0 and not (desired_new_sl < current_price):
                                        logger.warning(f"Desired SL {desired_new_sl} would be >= current_price {current_price}; skipping to avoid immediate close.")
                                        should_attempt = False
                                    if pos_type == 1 and not (desired_new_sl > current_price):
                                        logger.warning(f"Desired SL {desired_new_sl} would be <= current_price {current_price}; skipping to avoid immediate close.")
                                        should_attempt = False

                                if should_attempt:
                                    logger.info(f"Attempting SL tighten for ticket={ticket}: desired_new_sl (from ENTRY) -> {desired_new_sl} (reduced risk ${reduced_risk})")
                                    ok, info = _modify_position_sl(ticket, desired_new_sl, symbol, current_tp=tp_price)
                                    if ok:
                                        logger.info(f"Successfully tightened SL for ticket={ticket} -> {desired_new_sl}")
                                        _tightened_positions.add(ticket)
                                    else:
                                        logger.warning(f"Failed to tighten SL for ticket={ticket}. info={info}")
                                else:
                                    logger.debug(f"Skipping SL tighten for ticket={ticket}. desired_new_sl={desired_new_sl}, current_sl={current_sl_val}, current_price={current_price}")
                except Exception as e:
                    logger.exception(f"Error during SL tighten check for ticket={ticket}: {e}")

            logger.info(f"Position monitor summary for {symbol}: total_volume={total_volume:.4f} total_unrealized_profit={total_profit:.2f}")

            # sleep then continue
            time.sleep(poll_interval)
    except Exception as e:
        logger.exception(f"Exception in monitor_position_by_symbol: {e}")