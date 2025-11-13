# order_manager.py
import math
import logging
import MetaTrader5 as mt5
from config_loader import config

logger = logging.getLogger("swingpilot.orders")


def cancel_pending_orders_for_symbol(symbol):
    """
    Cancel pending orders for symbol that match our MAGIC.
    Returns (cancelled_count, errors_list).
    """
    cancelled = 0
    errors = []

    try:
        orders = mt5.orders_get(symbol=symbol)
    except Exception as e:
        logger.exception(f"orders_get() failed: {e}")
        return 0, [f"orders_get_failed:{e}"]

    if not orders:
        logger.info(f"No pending orders for {symbol}")
        return 0, []

    for o in orders:
        try:
            # Access fields safely (names vary by MT5 build)
            ticket = getattr(o, "ticket", None) or getattr(o, "order", None) or (o[0] if len(o) > 0 else None)
            magic = getattr(o, "magic", None) or getattr(o, "magic_number", None)

            # Only cancel orders that belong to this EA (magic) to be safe
            if magic is not None and int(magic) != int(config.MAGIC):
                logger.debug(f"Skipping order {ticket} (magic={magic}) not matching {config.MAGIC}")
                continue

            logger.info(f"Cancelling pending order ticket={ticket} symbol={symbol} magic={magic}")

            if bool(config.DRY_RUN):
                logger.info(f"DRY_RUN: would cancel order {ticket}")
                cancelled += 1
                continue

            # Preferred method: send TRADE_ACTION_REMOVE
            req = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": int(ticket),
                "symbol": symbol,
                "magic": int(config.MAGIC),
                "comment": "SwingPilot-cancel-pending",
            }
            res = mt5.order_send(req)
            if res is None:
                # fallback: try order_delete
                try:
                    rc = mt5.order_delete(int(ticket))
                    logger.info(f"order_delete({ticket}) -> {rc}")
                    cancelled += 1
                except Exception as e2:
                    logger.exception(f"order_delete failed for {ticket}: {e2}")
                    errors.append(f"{ticket}:delete_failed:{e2}")
            else:
                ret = getattr(res, "retcode", None)
                logger.info(f"order_send(REMOVE) -> retcode={ret} raw={res}")
                if ret == 0 or ret is None:
                    cancelled += 1
                else:
                    errors.append(f"{ticket}:retcode={ret}")
        except Exception as e:
            logger.exception(f"Exception while cancelling order {o}: {e}")
            errors.append(f"unknown:{e}")

    logger.info(f"cancel_pending_orders_for_symbol: cancelled={cancelled} errors={errors}")
    return cancelled, errors

def round_lot(volume):
    step = float(config.LOT_STEP)
    min_l = float(config.MIN_LOT)
    v = max(float(volume), min_l)
    # floor to nearest step
    v = math.floor(v / step) * step
    return round(v, 8)

def _validate_limit_price(symbol, direction, limit_price):
    """
    Validate limit price for pending orders.
    Returns (ok: bool, reason: str).
    buy limit must be < ask and distance >= MIN_STOP_DISTANCE
    sell limit must be > bid and distance >= MIN_STOP_DISTANCE
    """
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False, "no tick available"

    ask = float(tick.ask)
    bid = float(tick.bid)
    min_stop = float(config.MIN_STOP_DISTANCE)

    if direction == "long":
        if not (limit_price < ask - 1e-12):
            return False, f"buy limit {limit_price} not below ask {ask}"
        if (ask - limit_price) < min_stop:
            return False, f"buy limit too close to market (distance {(ask-limit_price):.6f} < MIN_STOP_DISTANCE={min_stop})"
    else:
        if not (limit_price > bid + 1e-12):
            return False, f"sell limit {limit_price} not above bid {bid}"
        if (limit_price - bid) < min_stop:
            return False, f"sell limit too close to market (distance {(limit_price-bid):.6f} < MIN_STOP_DISTANCE={min_stop})"

    return True, "ok"

def build_order_request(symbol, direction, volume, sl_price, tp_price, order_type="market", limit_price=None):
    """
    Build an MT5 trade request.
    - order_type: "market" or "limit"
    - limit_price: required for limit orders
    Returns: request dict or None (if invalid).
    """
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        logger.error("build_order_request: no tick for symbol")
        return None

    # normalize & round volume
    volume = float(volume)
    volume = round_lot(volume)
    if volume < float(config.MIN_LOT):
        logger.warning(f"Requested volume {volume} below MIN_LOT {config.MIN_LOT}")
        return None

    # Market order
    if order_type == "market":
        if direction == "long":
            price = float(tick.ask)
            order_type_mt5 = mt5.ORDER_TYPE_BUY
        else:
            price = float(tick.bid)
            order_type_mt5 = mt5.ORDER_TYPE_SELL

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type_mt5,
            "price": float(price),
            "sl": float(sl_price),
            "tp": float(tp_price),
            "deviation": 20,
            "magic": int(config.MAGIC),
            "comment": "SwingPilot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        return request

    # Pending limit order
    if order_type == "limit":
        if limit_price is None:
            logger.error("build_order_request: limit_price required for order_type='limit'")
            return None

        ok, reason = _validate_limit_price(symbol, direction, float(limit_price))
        if not ok:
            logger.warning(f"Limit price validation failed: {reason}")
            return None

        if direction == "long":
            order_type_mt5 = mt5.ORDER_TYPE_BUY_LIMIT
        else:
            order_type_mt5 = mt5.ORDER_TYPE_SELL_LIMIT

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type_mt5,
            "price": float(limit_price),
            "sl": float(sl_price),
            "tp": float(tp_price),
            "deviation": 20,
            "magic": int(config.MAGIC),
            "comment": "SwingPilot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        return request

    logger.error(f"Unknown order_type: {order_type}")
    return None

def send_order(request):
    """
    Send the prepared request using mt5.order_send().
    In DRY_RUN returns a simulated response dict.
    """
    if request is None:
        logger.error("send_order called with None request — skipping send.")
        return {"retcode": -1, "comment": "invalid_request", "request": None}

    if bool(config.DRY_RUN):
        logger.info("DRY_RUN enabled — not sending order. Request:")
        logger.info(request)
        return {"retcode": 0, "comment": "dry-run", "request": request}

    # quick guard: ensure terminal allows trading
    ti = mt5.terminal_info()
    if ti is None or not getattr(ti, "trade_allowed", True):
        logger.error("MT5 terminal reports trading not allowed (AutoTrading disabled). Not sending order.")
        return {"retcode": 10027, "comment": "AutoTrading disabled - prevented sending", "request": request}

    try:
        res = mt5.order_send(request)
    except Exception as e:
        logger.exception(f"exception during mt5.order_send: {e}")
        return {"retcode": -2, "comment": f"exception:{e}", "request": request}

    # log result
    try:
        rc = getattr(res, "retcode", None)
        cm = getattr(res, "comment", None)
        logger.info(f"Order send -> retcode={rc} comment={cm} raw={res}")
    except Exception:
        logger.info(f"Order send result (raw): {res}")

    return res
