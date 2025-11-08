import json, time, os, logging
from datetime import datetime
import MetaTrader5 as mt5
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, "config.json")) as f:
    cfg = json.load(f)

SYMBOL = cfg["symbol"]
TF_MAP = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1,
    "H2": mt5.TIMEFRAME_H2, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1
}
TIMEFRAME = TF_MAP[cfg["timeframe"]]
EMA_LEN = cfg["ema_length"]
POLL = cfg["poll_interval_seconds"]

def initialize():
    if not mt5.initialize():
        logging.error("MT5 init failed")
        quit()
    acc = mt5.account_info()
    logging.info(f"Connected to MT5: {acc.login} ({acc.server})")

def fetch(symbol, tf, n=200):
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, n)
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)
    return df

def detect_cross(df):
    df["ema"] = df["close"].ewm(span=EMA_LEN, adjust=False).mean()
    if len(df) < 2: return False
    prev_c, prev_e = df["close"].iloc[-2], df["ema"].iloc[-2]
    last_c, last_e = df["close"].iloc[-1], df["ema"].iloc[-1]
    cross = prev_c <= prev_e and last_c > last_e
    logging.info(f"Last close {last_c:.2f} / EMA {last_e:.2f} | Crossed={cross}")
    return cross

def main():
    initialize()
    while True:
        df = fetch(SYMBOL, TIMEFRAME)
        detect_cross(df)
        time.sleep(POLL)

if __name__ == "__main__":
    main()
