# scan_ema.py
import logging
from config_loader import config
from mt5_utils import initialize_mt5, shutdown_mt5, ensure_symbol, fetch_bars, get_tick
from strategy import analyze_trend, detect_entry_15m
from order_manager import build_order_request, send_order, round_lot,cancel_pending_orders_for_symbol
import time
import MetaTrader5 as mt5
from position_manager import monitor_position_by_symbol
logger = logging.getLogger("swingpilot")
def get_open_positions(symbol):
    try:
        pos = mt5.positions_get(symbol=symbol)
        return list(pos) if pos else []
    except Exception as e:
        logger.exception(f"Error fetching positions for {symbol}: {e}")
        return []

def log_positions_summary(symbol):
    positions = get_open_positions(symbol)
    if not positions:
        logger.info(f"No open positions for {symbol}")
        return 0.0, 0.0

    total_profit = 0.0
    total_volume = 0.0
    for p in positions:
        vol = getattr(p, "volume", 0.0)
        prof = getattr(p, "profit", 0.0)
        ticket = getattr(p, "ticket", None)
        price_open = getattr(p, "price_open", None)
        pos_type = getattr(p, "type", None)
        side = "BUY" if pos_type == 0 else "SELL"
        logger.info(f" - ticket={ticket} side={side} vol={vol:.4f} open={price_open} profit={prof:.2f}")
        total_profit += prof
        total_volume += vol

    logger.info(f"Open positions for {symbol}: total_volume={total_volume:.4f} total_unrealized_profit={total_profit:.2f}")
    return total_volume, total_profit



def setup_logging():
    logger.setLevel(logging.DEBUG if config.VERBOSE else logging.INFO)
    fh = logging.FileHandler(config.LOG_FILE)
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if config.VERBOSE else logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)
    if not logger.handlers:
        logger.addHandler(fh)
        logger.addHandler(ch)
    # also set children loggers to propagate
    logging.getLogger("swingpilot.mt5").propagate = True
    logging.getLogger("swingpilot.strategy").propagate = True
    logging.getLogger("swingpilot.orders").propagate = True

def scan_once(poll_interval_seconds=40):
    symbol = config.SYMBOL
    ensure_symbol(symbol)

    # If open position exists, log and skip new entry
    positions = get_open_positions(symbol)
    if positions:
        logger.info("Open position(s) detected — skipping new entry this candle.")
        log_positions_summary(symbol)
        return False, "position_already_open"

    # No open position -> proceed with detection and entry
    df_h1 = fetch_bars(symbol, config.TIMEFRAME_TREND, count=200)
    df_entry = fetch_bars(symbol, config.TIMEFRAME_ENTRY, count=10)

    trend = analyze_trend(df_h1, config.EMAS_TREND)
    logger.info(f"Detected trend: {trend}")

    direction, reason, order_hint = detect_entry_15m(df_entry, trend)
    logger.info(f"Entry detection: {direction} — {reason}  hint={order_hint}")
    if direction not in ("long", "short"):
        return False, reason

    # get current tick for market-based values & validation
    tick = get_tick(symbol)
    if tick is None:
        return False, "no tick data"

    # compute tentative entry price from tick (used for market orders and some validations)
    entry_price_tick = float(tick.ask) if direction == "long" else float(tick.bid)

    # compute SL/TP relative to intended entry price.
    # If limit order is used, we'll use limit_price as entry reference for SL/TP (below).
    entry_price_ref = entry_price_tick

    sl_price_ref = (entry_price_ref - float(config.SL_DISTANCE_USD)) if direction == "long" else (entry_price_ref + float(config.SL_DISTANCE_USD))
    tp_price_ref = (entry_price_ref + float(config.TP_DISTANCE_USD)) if direction == "long" else (entry_price_ref - float(config.TP_DISTANCE_USD))

    # volume handling
    volume = float(config.DEFAULT_VOLUME)
    volume = round_lot(volume)
    if volume < float(config.MIN_LOT):
        logger.warning("Volume below MIN_LOT")
        return False, "volume below min lot"

    # Build request according to order_hint
    req = None
    oh_type = order_hint.get("type") if isinstance(order_hint, dict) else "market"

    if oh_type == "market":
        req = build_order_request(symbol, direction, volume, sl_price_ref, tp_price_ref, order_type="market")
        if req is None:
            logger.warning("Failed to build market request (possible volume/min lot issue).")
            return False, "request_build_failed"

    elif oh_type == "limit":
        limit_price = float(order_hint.get("price"))
        # for pending orders, SL/TP must be set relative to the pending entry price
        entry_price_ref = limit_price
        sl_price_ref = (entry_price_ref - float(config.SL_DISTANCE_USD)) if direction == "long" else (entry_price_ref + float(config.SL_DISTANCE_USD))
        tp_price_ref = (entry_price_ref + float(config.TP_DISTANCE_USD)) if direction == "long" else (entry_price_ref - float(config.TP_DISTANCE_USD))

        # Build a pending (limit) request via order_manager
        req = build_order_request(symbol, direction, volume, sl_price_ref, tp_price_ref, order_type="limit", limit_price=limit_price)

        if req is None:
            logger.warning("Limit request build failed (validation rejected).")
            return False, "limit_request_invalid"

    else:
        logger.error(f"Unknown order_hint type: {oh_type} - defaulting to market")
        req = build_order_request(symbol, direction, volume, sl_price_ref, tp_price_ref, order_type="market")
        if req is None:
            return False, "request_build_failed"

    logger.info(f"Prepared order request: {req}")

    # Send order (or simulate in DRY_RUN)
    res = send_order(req)

    # If dry-run -> do not monitor positions (no order sent)
    if bool(config.DRY_RUN):
        logger.info("DRY_RUN enabled — simulated order only. Not monitoring positions.")
        return True, res

    # If the request was a pending (limit) one, wait for fill up to timeout; else continue to monitor
    is_pending_request = False
    try:
        is_pending_request = (req.get("action") == mt5.TRADE_ACTION_PENDING)
    except Exception:
        is_pending_request = False

    if is_pending_request:
        logger.info("Pending (limit) order sent — entering pending monitor (waiting for fill).")
        max_wait = int(getattr(config, "LIMIT_PENDING_MAX_WAIT_SECONDS", 1800))
        poll_int = int(getattr(config, "LIMIT_PENDING_POLL_INTERVAL", 5))
        waited = 0
        pending_filled = False

        # try to extract pending order ticket from response (defensive)
        pending_order_ticket = None
        try:
            pending_order_ticket = getattr(res, "order", None) or (res.get("order") if isinstance(res, dict) else None)
            logger.debug(f"Pending order ticket from send response: {pending_order_ticket}")
        except Exception:
            pending_order_ticket = None

        while waited < max_wait:
            # check if pending converted to a position (filled)
            positions = mt5.positions_get(symbol=symbol)
            if positions and len(positions) > 0:
                logger.info(f"Pending order filled -> found {len(positions)} open position(s) for {symbol}.")
                pending_filled = True
                break

            # check if pending order still exists
            try:
                orders = mt5.orders_get(symbol=symbol)
            except Exception as e:
                orders = None
                logger.exception(f"orders_get() failed while monitoring pending: {e}")

            if orders is None or len(orders) == 0:
                logger.debug("No pending orders currently present for this symbol.")
            else:
                logger.debug(f"Pending orders count: {len(orders)}")

            time.sleep(poll_int)
            waited += poll_int

        if not pending_filled:
            logger.warning(f"Pending order did NOT fill within {max_wait}s for {symbol}. Will cancel pending orders and return.")
            # Cancel pending orders for this symbol (uses order_manager.cancel_pending_orders_for_symbol)
            try:
                cancelled, errors = cancel_pending_orders_for_symbol(symbol)
                logger.info(f"Cancelled {cancelled} pending orders; errors: {errors}")
            except Exception as e:
                logger.exception(f"Failed to cancel pending orders: {e}")
            return False, "pending_timeout_cancelled"

    # short wait to allow broker to register position (either market filled or pending filled)
    confirm_wait = 2.0
    time.sleep(confirm_wait)

    # Log response
    try:
        rc = getattr(res, "retcode", None) if not isinstance(res, dict) else res.get("retcode")
        comment = getattr(res, "comment", None) if not isinstance(res, dict) else res.get("comment")
    except Exception:
        rc = None
        comment = None
    logger.info(f"Order response -> retcode={rc}, comment={comment}, raw={res}")

    # Block-monitor open positions until closed
    logger.info("Blocking on monitor_position_by_symbol() until open position(s) close.")
    try:
        monitor_position_by_symbol(symbol, poll_interval=poll_interval_seconds)
    except Exception as e:
        logger.exception(f"position monitor raised exception: {e}")

    logger.info("Monitor finished (position(s) closed). scan_once() returning.")
    return True, res

def main_once():
    setup_logging()
    try:
        initialize_mt5()
        symbol = config.SYMBOL
        ensure_symbol(symbol)

        # 🟡 New: check for open positions first
        positions = get_open_positions(symbol)
        if positions:
            logger.info(f"Detected {len(positions)} running position(s) on startup. Entering monitor mode immediately.")
            log_positions_summary(symbol)
            # Block here until they close
            monitor_position_by_symbol(symbol, poll_interval=5)
            logger.info("Existing position(s) closed. Resuming normal scanning...")
            # once closed, proceed to scanning for next setup
            ok, res = scan_once()
        else:
            # no running position, proceed as usual
            ok, res = scan_once()

        logger.info(f"Scan result: ok={ok} res={res}")

    except Exception as e:
        logger.exception(f"Error in main: {e}")
    finally:
        shutdown_mt5()


if __name__ == "__main__":
    main_once()
