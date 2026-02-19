#!/usr/bin/env python3
"""
Majors TA Scanner — Technical analysis signals for BTC/ETH/SOL spot on Binance.
Pure Python, deterministic, NO LLMs.
"""
import json
import sys
import os
import requests
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
import numpy as np
from ta.volatility import BollingerBands, AverageTrueRange
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parents[1]))
SIGNAL_DIR = BASE_DIR / "signals" / "majors"
CRON_HEALTH = BASE_DIR / "state" / "cron_health.json"
LOG_FILE = BASE_DIR / "execution-logs" / "majors_scanner.log"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

def _log(msg: str):
    """Append to log file with timestamp."""
    timestamp = datetime.now(timezone.utc).isoformat()
    line = f"[{timestamp}] {msg}\n"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line)
    print(line.strip())

def _update_cron_health(status: str = "ok"):
    """Update cron health timestamp."""
    health = {}
    if CRON_HEALTH.exists():
        with open(CRON_HEALTH) as f:
            health = json.load(f)
    
    health["majors_scanner"] = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "status": status
    }
    
    CRON_HEALTH.parent.mkdir(parents=True, exist_ok=True)
    with open(CRON_HEALTH, "w") as f:
        json.dump(health, f, indent=2)

def fetch_candles(symbol: str, interval: str = "1h", limit: int = 100) -> pd.DataFrame | None:
    """
    Fetch candlestick data from Binance public API.
    No API key required for klines.
    """
    try:
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        
        response = requests.get(BINANCE_KLINES_URL, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        
        # Binance klines format:
        # [open_time, open, high, low, close, volume, close_time, ...]
        df = pd.DataFrame(data, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore"
        ])
        
        # Convert to numeric
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col])
        
        df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms")
        
        return df[["timestamp", "open", "high", "low", "close", "volume"]]
    
    except Exception as e:
        _log(f"ERROR fetching candles for {symbol}: {e}")
        return None

def calculate_indicators(df: pd.DataFrame) -> dict:
    """
    Calculate technical indicators using ta library.
    Returns dict of current indicator values.
    """
    try:
        # Bollinger Bands
        bb = BollingerBands(close=df["close"], window=20, window_dev=2.0)
        bb_upper = bb.bollinger_hband().iloc[-1]
        bb_mid = bb.bollinger_mavg().iloc[-1]
        bb_lower = bb.bollinger_lband().iloc[-1]
        
        # RSI
        rsi = RSIIndicator(close=df["close"], window=14)
        rsi_value = rsi.rsi().iloc[-1]
        
        # EMA
        ema20 = EMAIndicator(close=df["close"], window=20).ema_indicator().iloc[-1]
        ema50 = EMAIndicator(close=df["close"], window=50).ema_indicator().iloc[-1]
        
        # MACD
        macd_ind = MACD(close=df["close"], window_slow=26, window_fast=12, window_sign=9)
        macd_line = macd_ind.macd().iloc[-1]
        macd_signal = macd_ind.macd_signal().iloc[-1]
        macd_hist = macd_ind.macd_diff().iloc[-1]
        
        # ATR
        atr = AverageTrueRange(high=df["high"], low=df["low"], close=df["close"], window=14)
        atr_value = atr.average_true_range().iloc[-1]
        
        # Current price and volume
        current_price = df["close"].iloc[-1]
        current_volume = df["volume"].iloc[-1]
        
        # Volume ratio (current vs 20-period average)
        avg_volume = df["volume"].rolling(20).mean().iloc[-1]
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0
        
        # 24h price change
        price_24h_ago = df["close"].iloc[-24] if len(df) >= 24 else df["close"].iloc[0]
        price_change_24h = ((current_price - price_24h_ago) / price_24h_ago * 100) if price_24h_ago > 0 else 0
        
        return {
            "rsi": float(rsi_value),
            "ema20": float(ema20),
            "ema50": float(ema50),
            "macd": float(macd_line),
            "macd_signal": float(macd_signal),
            "macd_hist": float(macd_hist),
            "bb_lower": float(bb_lower),
            "bb_upper": float(bb_upper),
            "bb_mid": float(bb_mid),
            "atr": float(atr_value),
            "current_price": float(current_price),
            "volume_ratio": float(volume_ratio),
            "price_change_24h": float(price_change_24h)
        }
    
    except Exception as e:
        _log(f"ERROR calculating indicators: {e}")
        return {}

def check_mean_reversion(indicators: dict, symbol: str) -> dict | None:
    """Check for mean reversion entry conditions."""
    rsi = indicators.get("rsi", 50)
    price = indicators.get("current_price", 0)
    bb_lower = indicators.get("bb_lower", 0)
    
    if rsi < 30 and price < bb_lower:
        token = symbol.replace("USDT", "")
        return {
            "token": token,
            "symbol": symbol,
            "chain": "binance",
            "source": "majors_scanner",
            "direction": "LONG",
            "strategy_hint": "mean-reversion",
            "signal_strength": min(95, int((30 - rsi) * 2.5 + 75)),
            "score": min(95, int((30 - rsi) * 2.5 + 75)),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thesis": f"{symbol} RSI={rsi:.1f} (oversold) + price below lower BB. Mean reversion entry.",
            "volume_24h": indicators.get("volume_ratio", 1.0) * 1e9,  # Approximate
            "price_change_24h": indicators.get("price_change_24h", 0),
            "indicators": indicators
        }
    return None

def check_trend_following(indicators: dict, symbol: str) -> dict | None:
    """Check for trend following entry conditions."""
    ema20 = indicators.get("ema20", 0)
    ema50 = indicators.get("ema50", 0)
    price = indicators.get("current_price", 0)
    macd_hist = indicators.get("macd_hist", 0)
    
    if ema20 > ema50 and price > ema20 and macd_hist > 0:
        token = symbol.replace("USDT", "")
        return {
            "token": token,
            "symbol": symbol,
            "chain": "binance",
            "source": "majors_scanner",
            "direction": "LONG",
            "strategy_hint": "trend-following",
            "signal_strength": 75,
            "score": 75,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thesis": f"{symbol} EMA20 > EMA50 + price above EMA20 + MACD positive. Trend following entry.",
            "volume_24h": indicators.get("volume_ratio", 1.0) * 5e8,
            "price_change_24h": indicators.get("price_change_24h", 0),
            "indicators": indicators
        }
    return None

def check_scalping(indicators: dict, symbol: str) -> dict | None:
    """Check for scalping entry conditions."""
    macd_hist = indicators.get("macd_hist", 0)
    volume_ratio = indicators.get("volume_ratio", 1.0)
    atr = indicators.get("atr", 0)
    price = indicators.get("current_price", 0)
    
    # MACD bullish crossover = histogram turned positive
    # Volume spike = volume_ratio > 2
    # ATR above minimum threshold (> 0.5% of price)
    min_atr = price * 0.005
    
    if macd_hist > 0 and volume_ratio > 2 and atr > min_atr:
        token = symbol.replace("USDT", "")
        return {
            "token": token,
            "symbol": symbol,
            "chain": "binance",
            "source": "majors_scanner",
            "direction": "LONG",
            "strategy_hint": "scalping",
            "signal_strength": 80,
            "score": 80,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thesis": f"{symbol} MACD bullish crossover + volume spike ({volume_ratio:.1f}x) + ATR > threshold. Scalping entry.",
            "volume_24h": volume_ratio * 5e9,
            "price_change_24h": indicators.get("price_change_24h", 0),
            "indicators": indicators
        }
    return None

def scan_symbol(symbol: str) -> list[dict]:
    """Scan one symbol for all strategy signals."""
    _log(f"Scanning {symbol}...")
    
    signals = []
    
    # Fetch candles
    df = fetch_candles(symbol, interval="1h", limit=100)
    if df is None or len(df) < 50:
        _log(f"  Insufficient data for {symbol}")
        return signals
    
    # Calculate indicators
    indicators = calculate_indicators(df)
    if not indicators:
        _log(f"  Failed to calculate indicators for {symbol}")
        return signals
    
    _log(f"  {symbol} RSI={indicators.get('rsi', 0):.1f} EMA20={indicators.get('ema20', 0):.0f} BB_pos={(indicators.get('current_price', 0) - indicators.get('bb_lower', 0)):.0f}")
    
    # Check all strategies
    mean_rev = check_mean_reversion(indicators, symbol)
    if mean_rev:
        signals.append(mean_rev)
        _log(f"  MEAN REVERSION SIGNAL: {symbol}")
    
    trend = check_trend_following(indicators, symbol)
    if trend:
        signals.append(trend)
        _log(f"  TREND FOLLOWING SIGNAL: {symbol}")
    
    scalp = check_scalping(indicators, symbol)
    if scalp:
        signals.append(scalp)
        _log(f"  SCALPING SIGNAL: {symbol}")
    
    return signals

def write_signal(signal: dict):
    """Write signal to signals/majors/ directory."""
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    symbol = signal.get("symbol", "UNKNOWN")
    filename = f"majors_scanner_{symbol}_{timestamp}.json"
    filepath = SIGNAL_DIR / filename
    
    with open(filepath, "w") as f:
        json.dump(signal, f, indent=2)
    
    _log(f"Signal written: {filename}")

def run_scanner(test_mode: bool = False):
    """Main scanner logic."""
    _log("=== MAJORS SCANNER START ===")
    
    all_signals = []
    
    for symbol in SYMBOLS:
        try:
            signals = scan_symbol(symbol)
            all_signals.extend(signals)
            
            if not test_mode:
                for signal in signals:
                    write_signal(signal)
        
        except Exception as e:
            _log(f"ERROR scanning {symbol}: {e}")
            continue
    
    _log(f"Generated {len(all_signals)} signals total")
    _update_cron_health("ok")
    _log("=== MAJORS SCANNER END ===")

if __name__ == "__main__":
    test_mode = "--test" in sys.argv
    
    try:
        run_scanner(test_mode=test_mode)
    except Exception as e:
        _log(f"FATAL ERROR: {e}")
        import traceback
        _log(traceback.format_exc())
        _update_cron_health("error")
        sys.exit(1)
