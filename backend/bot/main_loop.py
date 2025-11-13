# run_loop.py
import time
import logging
from datetime import datetime, timedelta, timezone

# imports assume files are in same folder
import scan_ema            # provides scan_once(), log_positions_summary(), get_open_positions()
from mt5_utils import initialize_mt5, shutdown_mt5, fetch_bars, ensure_symbol
from config_loader import config
from position_manager import monitor_position_by_symbol  # blocking monitor you wrote

# Setup simple logging for the loop
logger = logging.getLogger("swingpilot.run_loop")
logger.setLevel(logging.DEBUG if config.VERBOSE else logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG if config.VERBOSE else logging.INFO)
fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
ch.setFormatter(fmt)
if not logger.handlers:
    logger.addHandler(ch)


# --- Dynamically parse timeframe ---
def timeframe_to_minutes(tf: str) -> int:
    tf = tf.upper().strip()
    if tf.startswith("M"):
        return int(tf[1:])
    if tf.startswith("H"):
        return int(tf[1:]) * 60
    if tf.startswith("D"):
        return int(tf[1:]) * 60 * 24
    raise ValueError(f"Unsupported timeframe: {tf}")


def next_candle_boundary(ts: datetime, interval_minutes: int) -> datetime:
    ts = ts.replace(second=0, microsecond=0)
    minute = ts.minute
    remainder = minute % interval_minutes
    next_minute = minute - remainder + interval_minutes
    if next_minute >= 60:
        # advance hour
        next_time = ts.replace(minute=0) + timedelta(hours=1)
    else:
        next_time = ts.replace(minute=next_minute)
    return next_time


def pd_time_to_datetime(value):
    """Convert pandas.Timestamp or datetime to UTC datetime."""
    try:
        dt = value.to_pydatetime()
    except Exception:
        dt = value
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main_loop():
    symbol = config.SYMBOL
    timeframe = config.TIMEFRAME_ENTRY  # e.g. "M5", "M15", "H1"
    interval_minutes = timeframe_to_minutes(timeframe)
    safety_buffer_seconds = 5  # to ensure candle closed
    poll_interval_for_monitor = 30 # used when invoking monitor_position_by_symbol

    logger.info(f"Starting SwingPilot loop for timeframe {timeframe} ({interval_minutes} min candles)")

    initialize_mt5()

    try:
        scan_ema.setup_logging()
    except Exception:
        logger.exception("Failed to call scan_ema.setup_logging() — continuing with current logging config.")
    try:
        ensure_symbol(symbol)
    except Exception as e:
        logger.exception(f"ensure_symbol failed: {e}")

    last_seen = None

    try:
        while True:
            try:
                df = fetch_bars(symbol, timeframe, count=2)
                if df is None or len(df) < 1:
                    logger.warning("No bars returned; sleeping 30s and retrying.")
                    time.sleep(30)
                    continue

                last_time = pd_time_to_datetime(df['time'].iloc[-1])

                # First-run initialization
                if last_seen is None:
                    last_seen = last_time
                    logger.info(f"Initial detected last {timeframe} candle: {last_seen.isoformat()}")

                    # If there is already an open position on startup, immediately monitor it
                    open_pos = scan_ema.get_open_positions(symbol)
                    if open_pos:
                        logger.info(f"Found existing open position(s) at startup ({len(open_pos)}). Entering monitor immediately.")
                        scan_ema.log_positions_summary(symbol)
                        monitor_position_by_symbol(symbol, poll_interval=poll_interval_for_monitor)
                        logger.info("Existing position(s) closed. Resuming candle-sync.")
                        # after monitor returns, refresh last_seen (get newest candle time)
                        df = fetch_bars(symbol, timeframe, count=2)
                        last_seen = pd_time_to_datetime(df['time'].iloc[-1])
                        # compute next boundary and sleep
                        next_boundary = next_candle_boundary(last_seen, interval_minutes)
                        sleep_for = (next_boundary - datetime.now(timezone.utc)).total_seconds() + safety_buffer_seconds
                        sleep_for = max(sleep_for, 5)
                        logger.info(f"Sleeping until next {timeframe} candle: {next_boundary.isoformat()} (+{safety_buffer_seconds}s buffer, {int(sleep_for)}s)")
                        time.sleep(sleep_for)
                        continue

                    # otherwise normal first-run sleep
                    next_boundary = next_candle_boundary(last_seen, interval_minutes)
                    sleep_for = (next_boundary - datetime.now(timezone.utc)).total_seconds() + safety_buffer_seconds
                    sleep_for = max(sleep_for, 5)
                    logger.info(f"Sleeping until next {timeframe} candle: {next_boundary.isoformat()} (+{safety_buffer_seconds}s buffer, {int(sleep_for)}s)")
                    time.sleep(sleep_for)
                    continue

                # New candle detected
                if last_time > last_seen:
                    logger.info(f"New {timeframe} candle detected: {last_time.isoformat()} (prev {last_seen.isoformat()})")

                    ok, res = scan_ema.scan_once()
                    logger.info(f"scan_once -> ok={ok} res={res}")

                    # If scan said a position already exists, immediately monitor it (blocking)
                    if not ok and isinstance(res, str) and res == "position_already_open":
                        logger.info("scan_once reported position_already_open — entering monitor to show live PnL.")
                        # log current positions summary then monitor (blocks)
                        scan_ema.log_positions_summary(symbol)
                        monitor_position_by_symbol(symbol, poll_interval=poll_interval_for_monitor)
                        logger.info("Monitor returned — position(s) closed. Refreshing last_seen and continuing loop.")
                        # after monitor returns, update last_seen to the latest candle to avoid re-processing the same candle
                        df = fetch_bars(symbol, timeframe, count=2)
                        last_seen = pd_time_to_datetime(df['time'].iloc[-1])
                        # sleep until next candle boundary
                        next_boundary = next_candle_boundary(last_seen, interval_minutes)
                        sleep_for = (next_boundary - datetime.now(timezone.utc)).total_seconds() + safety_buffer_seconds
                        sleep_for = max(sleep_for, 5)
                        logger.info(f"Sleeping until next {timeframe} candle: {next_boundary.isoformat()} (+{safety_buffer_seconds}s buffer, {int(sleep_for)}s)")
                        time.sleep(sleep_for)
                        continue

                    # normal flow after scan (whether it opened trade or not)
                    # update last_seen and sleep until next candle
                    last_seen = last_time
                    next_boundary = next_candle_boundary(last_seen, interval_minutes)
                    sleep_for = (next_boundary - datetime.now(timezone.utc)).total_seconds() + safety_buffer_seconds
                    sleep_for = max(sleep_for, 5)
                    logger.info(f"Sleeping until next {timeframe} candle: {next_boundary.isoformat()} (+{safety_buffer_seconds}s buffer, {int(sleep_for)}s)")
                    time.sleep(sleep_for)
                    continue

                # No new candle yet -> sleep until expected boundary
                next_boundary = next_candle_boundary(last_time, interval_minutes)
                sleep_for = (next_boundary - datetime.now(timezone.utc)).total_seconds() + safety_buffer_seconds
                sleep_for = max(sleep_for, 3)
                logger.debug(f"No new {timeframe} yet. Sleeping {int(sleep_for)}s until {next_boundary.isoformat()}")
                time.sleep(sleep_for)
                continue

            except Exception as e:
                logger.exception(f"Unexpected error in loop iteration: {e}")
                time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Run loop stopped by user (KeyboardInterrupt).")
    finally:
        shutdown_mt5()
        logger.info("Exited run loop, MT5 shutdown complete.")


if __name__ == "__main__":
    main_loop()
