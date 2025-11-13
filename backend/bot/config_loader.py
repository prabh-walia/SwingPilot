# config_loader.py
import json
from types import SimpleNamespace
from pathlib import Path

DEFAULT_CFG = {
    "SYMBOL": "XAUUSDm",
    "TIMEFRAME_ENTRY": "M15",
    "TIMEFRAME_TREND": "H1",
    "EMAS_TREND": [9, 20, 50],
    "SL_DISTANCE_USD": 8.0,
    "TP_DISTANCE_USD": 30.0,
    "RISK_PCT": 0.01,
    "MIN_LOT": 0.01,
    "LOT_STEP": 0.01,
    "DEFAULT_VOLUME": 0.01,
    "DRY_RUN": False,
    "MAGIC": 987654,
    "LOG_FILE": "swingpilot.log",
    "MT5_LOGIN": None,
    "MT5_PASSWORD": None,
    "MT5_SERVER": None,
    "VERBOSE": True,
    "FETCH_BARS": 100,
    "MIN_STOP_DISTANCE": 0.1
}

def load_config(path: str = "config.json"):
    p = Path(path)
    if p.exists():
        with open(p, "r") as f:
            cfg_dict = json.load(f)
    else:
        cfg_dict = DEFAULT_CFG
    # fill missing keys
    for k, v in DEFAULT_CFG.items():
        if k not in cfg_dict:
            cfg_dict[k] = v
    # ensure types
    cfg_dict["EMAS_TREND"] = [int(x) for x in cfg_dict.get("EMAS_TREND", [9,20,50])]
    cfg = SimpleNamespace(**cfg_dict)
    return cfg

# single config instance (import config_loader.config)
config = load_config()
