# mt5_utils.py
import MetaTrader5 as mt5
from datetime import datetime, timezone
import logging
import pandas as pd
from config_loader import config

logger = logging.getLogger("swingpilot.mt5")

TF_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H2": mt5.TIMEFRAME_H2,
}

def initialize_mt5():
    if not mt5.initialize():
        logger.error(f"mt5.initialize() failed: {mt5.last_error()}")
        raise RuntimeError("MT5 initialize failed")
    if config.MT5_LOGIN is not None:
        ok = mt5.login(config.MT5_LOGIN, password=config.MT5_PASSWORD, server=config.MT5_SERVER)
        if not ok:
            logger.warning(f"MT5 login attempt returned: {mt5.last_error()}")
    logger.info("MT5 initialized")

def shutdown_mt5():
    try:
        mt5.shutdown()
        logger.info("MT5 shutdown")
    except Exception:
        pass

def ensure_symbol(symbol: str):
    info = mt5.symbol_info(symbol)
    if info is None:
        logger.error(f"Symbol {symbol} not found on server.")
        raise ValueError(f"Symbol {symbol} not found")
    if not info.visible:
        mt5.symbol_select(symbol, True)
        logger.info(f"Selected {symbol} in Market Watch.")
    return info

def fetch_bars(symbol: str, timeframe_str: str, count: int = None) -> pd.DataFrame:
    if count is None:
        count = int(config.FETCH_BARS)
    tf = TF_MAP.get(timeframe_str)
    if tf is None:
        raise ValueError(f"Unknown timeframe: {timeframe_str}")
    utc_to = datetime.now(timezone.utc)
    rates = mt5.copy_rates_from(symbol, tf, utc_to, int(count))
    if rates is None:
        err = mt5.last_error()
        logger.error(f"Failed to fetch bars for {symbol} {timeframe_str}: {err}")
        raise RuntimeError("Failed to fetch bars")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df

def get_tick(symbol: str):
    return mt5.symbol_info_tick(symbol)
