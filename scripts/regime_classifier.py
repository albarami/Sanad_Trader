#!/usr/bin/env python3
"""
Regime Classifier — Sprint 5.3
Deterministic Python. No LLMs.

Classifies the current crypto market regime based on:
1. BTC 30-day price trend (SMA slope)
2. BTC short-term volatility (14-day ATR as % of price)
3. Fear & Greed Index (level + trend)
4. BTC drawdown from recent high

Regime outputs:
  Primary: BULL | BEAR | SIDEWAYS
  Volatility: HIGH_VOL | LOW_VOL | NORMAL_VOL
  Combined tag: e.g. "BULL_HIGH_VOL", "BEAR_LOW_VOL", "SIDEWAYS_NORMAL_VOL"

Used by:
- post_trade_analyzer.py: tags every trade with regime at entry/exit
- signal_router.py: regime-weighted signal scoring
- Genius Memory RAG: filter past trades by matching regime

Runs on-demand (imported) and as standalone for cron/testing.
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
FEAR_GREED_PATH = BASE_DIR / "signals" / "market" / "fear_greed_latest.json"
REGIME_DIR = BASE_DIR / "genius-memory" / "regime-data"
REGIME_LATEST = REGIME_DIR / "latest.json"
REGIME_HISTORY = REGIME_DIR / "history.jsonl"

BINANCE_BASE = "https://api.binance.com"
BTC_SYMBOL = "BTCUSDT"

# ─────────────────────────────────────────────────────────
# Thresholds (tuned for crypto)
# ─────────────────────────────────────────────────────────
# Trend classification: 30-day SMA slope as daily % change
BULL_SLOPE_THRESHOLD = 0.15    # >0.15% per day avg = bull
BEAR_SLOPE_THRESHOLD = -0.15   # <-0.15% per day avg = bear
# Between -0.15% and +0.15% = sideways

# Volatility: 14-day ATR as % of price
HIGH_VOL_THRESHOLD = 3.5   # >3.5% daily ATR = high vol
LOW_VOL_THRESHOLD = 1.5    # <1.5% daily ATR = low vol
# Between 1.5% and 3.5% = normal vol

# Drawdown from 30-day high
DRAWDOWN_BEAR_THRESHOLD = 0.15  # >15% from high reinforces bear
DRAWDOWN_CORRECTION = 0.08     # >8% = meaningful correction

# Fear & Greed
FG_EXTREME_FEAR = 20
FG_FEAR = 35
FG_GREED = 65
FG_EXTREME_GREED = 80

# Circuit breaker
_consecutive_failures = 0
CIRCUIT_BREAKER_THRESHOLD = 3


def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[REGIME] {ts} {msg}", flush=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────
def _load_json(path: Path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json_atomic(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        _log(f"ERROR saving {path}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _append_jsonl(path: Path, record: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        _log(f"ERROR appending to {path}: {e}")


# ─────────────────────────────────────────────────────────
# Data Fetching
# ─────────────────────────────────────────────────────────
def _fetch_btc_daily_klines(days: int = 30) -> list | None:
    """Fetch BTC daily klines from Binance."""
    global _consecutive_failures
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={
                "symbol": BTC_SYMBOL,
                "interval": "1d",
                "limit": days + 1,  # +1 for current incomplete day
            },
            timeout=15,
        )
        resp.raise_for_status()
        _consecutive_failures = 0
        return resp.json()
    except Exception as e:
        _consecutive_failures += 1
        _log(f"ERROR fetching BTC klines: {e}")
        if _consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
            _log("CIRCUIT BREAKER — too many Binance failures")
        return None


def _fetch_btc_current_price() -> float | None:
    """Fetch current BTC price."""
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/api/v3/ticker/price",
            params={"symbol": BTC_SYMBOL},
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as e:
        _log(f"ERROR fetching BTC price: {e}")
        return None


def _load_fear_greed() -> dict:
    """Load Fear & Greed data."""
    data = _load_json(FEAR_GREED_PATH, {})
    if not data:
        return {"value": 50, "regime": "NEUTRAL", "trend_7d": "stable"}
    return data


# ─────────────────────────────────────────────────────────
# Analysis Functions
# ─────────────────────────────────────────────────────────
def _calc_sma_slope(closes: list[float], period: int = 30) -> float:
    """Calculate SMA slope as average daily % change.
    Positive = uptrend, Negative = downtrend.
    Uses linear regression slope over the period.
    """
    if len(closes) < period:
        return 0.0

    recent = closes[-period:]
    n = len(recent)

    # Simple linear regression: slope of price over time
    x_mean = (n - 1) / 2
    y_mean = sum(recent) / n

    numerator = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(recent))
    denominator = sum((i - x_mean) ** 2 for i in range(n))

    if denominator == 0 or y_mean == 0:
        return 0.0

    slope = numerator / denominator
    # Convert to daily % change
    slope_pct = (slope / y_mean) * 100
    return round(slope_pct, 4)


def _calc_atr_pct(klines: list, period: int = 14) -> float:
    """Calculate Average True Range as % of price.
    Each kline: [open_time, open, high, low, close, volume, close_time, ...]
    """
    if len(klines) < period + 1:
        return 0.0

    true_ranges = []
    for i in range(1, len(klines)):
        try:
            high = float(klines[i][2])
            low = float(klines[i][3])
            prev_close = float(klines[i - 1][4])
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
            true_ranges.append(tr)
        except (ValueError, IndexError):
            continue

    if len(true_ranges) < period:
        return 0.0

    # Use most recent `period` values
    recent_tr = true_ranges[-period:]
    atr = sum(recent_tr) / len(recent_tr)

    # As % of current price
    try:
        current_price = float(klines[-1][4])
        if current_price <= 0:
            return 0.0
        return round((atr / current_price) * 100, 4)
    except (ValueError, IndexError):
        return 0.0


def _calc_drawdown(closes: list[float], lookback: int = 30) -> float:
    """Calculate drawdown from highest close in lookback period."""
    if len(closes) < 2:
        return 0.0
    recent = closes[-lookback:]
    peak = max(recent)
    current = closes[-1]
    if peak <= 0:
        return 0.0
    return round((peak - current) / peak, 4)


def _calc_volatility_trend(klines: list) -> str:
    """Is volatility increasing, decreasing, or stable?
    Compare last 7-day ATR vs prior 7-day ATR.
    """
    if len(klines) < 16:
        return "stable"

    def atr_for_range(k_range):
        trs = []
        for i in range(1, len(k_range)):
            try:
                h = float(k_range[i][2])
                l = float(k_range[i][3])
                pc = float(k_range[i - 1][4])
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            except (ValueError, IndexError):
                continue
        return sum(trs) / len(trs) if trs else 0

    recent_atr = atr_for_range(klines[-8:])   # Last 7 days
    prior_atr = atr_for_range(klines[-15:-7])  # Prior 7 days

    if prior_atr <= 0:
        return "stable"

    change = (recent_atr - prior_atr) / prior_atr
    if change > 0.20:
        return "increasing"
    elif change < -0.20:
        return "decreasing"
    return "stable"


# ─────────────────────────────────────────────────────────
# Regime Classification
# ─────────────────────────────────────────────────────────
def classify_regime() -> dict:
    """Main regime classification function.
    Returns a dict with regime info, or None on failure.
    """
    now = _now()
    _log("=== REGIME CLASSIFICATION ===")

    # Fetch data
    klines = _fetch_btc_daily_klines(30)
    if not klines or len(klines) < 15:
        _log("ERROR: Insufficient BTC kline data")
        # Return last known regime if available
        last = _load_json(REGIME_LATEST)
        if last:
            _log(f"Using cached regime: {last.get('regime_tag', 'UNKNOWN')}")
            return last
        return {"regime_tag": "UNKNOWN", "error": "no_data"}

    btc_price = _fetch_btc_current_price()
    fg_data = _load_fear_greed()

    # Extract close prices
    closes = []
    for k in klines:
        try:
            closes.append(float(k[4]))
        except (ValueError, IndexError):
            continue

    if len(closes) < 15:
        _log("ERROR: Too few valid close prices")
        return {"regime_tag": "UNKNOWN", "error": "insufficient_closes"}

    # ── Calculate metrics ──
    sma_slope = _calc_sma_slope(closes, min(30, len(closes)))
    atr_pct = _calc_atr_pct(klines, min(14, len(klines) - 1))
    drawdown = _calc_drawdown(closes, min(30, len(closes)))
    vol_trend = _calc_volatility_trend(klines)
    fg_value = fg_data.get("value", 50)
    fg_trend = fg_data.get("trend_7d", "stable")

    _log(f"BTC price: ${btc_price:,.0f}" if btc_price else "BTC price: unavailable")
    _log(f"SMA slope: {sma_slope:+.4f}%/day")
    _log(f"ATR(14): {atr_pct:.2f}%")
    _log(f"Drawdown from 30d high: {drawdown*100:.1f}%")
    _log(f"Vol trend: {vol_trend}")
    _log(f"Fear & Greed: {fg_value} ({fg_data.get('regime', 'N/A')}) trend={fg_trend}")

    # ── PRIMARY REGIME: BULL / BEAR / SIDEWAYS ──
    primary = "SIDEWAYS"
    primary_confidence = 0.5

    if sma_slope >= BULL_SLOPE_THRESHOLD:
        primary = "BULL"
        primary_confidence = min(0.5 + (sma_slope / 1.0), 0.95)
        # Drawdown override: if we're in a deep drawdown, trend is weakening
        if drawdown >= DRAWDOWN_BEAR_THRESHOLD:
            primary = "BEAR"
            primary_confidence = 0.6
            _log(f"  Override: slope says bull but {drawdown*100:.1f}% drawdown → BEAR")
    elif sma_slope <= BEAR_SLOPE_THRESHOLD:
        primary = "BEAR"
        primary_confidence = min(0.5 + (abs(sma_slope) / 1.0), 0.95)
    else:
        primary = "SIDEWAYS"
        primary_confidence = 0.5

    # F&G reinforcement
    if primary == "BULL" and fg_value < FG_FEAR:
        _log(f"  Conflict: slope says BULL but F&G={fg_value} (fear)")
        primary_confidence *= 0.8
    elif primary == "BEAR" and fg_value > FG_GREED:
        _log(f"  Conflict: slope says BEAR but F&G={fg_value} (greed)")
        primary_confidence *= 0.8

    # ── VOLATILITY REGIME: HIGH / LOW / NORMAL ──
    if atr_pct >= HIGH_VOL_THRESHOLD:
        vol_regime = "HIGH_VOL"
    elif atr_pct <= LOW_VOL_THRESHOLD:
        vol_regime = "LOW_VOL"
    else:
        vol_regime = "NORMAL_VOL"

    # ── Combined tag ──
    regime_tag = f"{primary}_{vol_regime}"

    # ── Trading implications ──
    implications = _derive_implications(primary, vol_regime, fg_value, drawdown)

    result = {
        "regime_tag": regime_tag,
        "primary": primary,
        "volatility": vol_regime,
        "confidence": round(primary_confidence, 3),
        "metrics": {
            "btc_price": btc_price,
            "sma_slope_pct_per_day": sma_slope,
            "atr_14d_pct": atr_pct,
            "drawdown_from_30d_high": round(drawdown, 4),
            "volatility_trend": vol_trend,
            "fear_greed_value": fg_value,
            "fear_greed_trend": fg_trend,
        },
        "implications": implications,
        "timestamp": now.isoformat(),
        "data_points": len(closes),
    }

    _log(f"REGIME: {regime_tag} (confidence={primary_confidence:.0%})")
    _log(f"Implications: {implications.get('risk_adjustment', 'normal')}")

    # Save latest
    _save_json_atomic(REGIME_LATEST, result)

    # Append to history
    _append_jsonl(REGIME_HISTORY, {
        "timestamp": now.isoformat(),
        "regime_tag": regime_tag,
        "primary": primary,
        "volatility": vol_regime,
        "confidence": round(primary_confidence, 3),
        "btc_price": btc_price,
        "sma_slope": sma_slope,
        "atr_pct": atr_pct,
        "drawdown": round(drawdown, 4),
        "fg_value": fg_value,
    })

    _log("=== CLASSIFICATION COMPLETE ===")
    return result


def _derive_implications(
    primary: str, vol_regime: str, fg_value: int, drawdown: float
) -> dict:
    """Derive trading implications from the regime."""
    implications = {
        "risk_adjustment": "normal",
        "position_size_modifier": 1.0,
        "preferred_strategies": [],
        "avoid_strategies": [],
        "notes": [],
    }

    # Primary regime implications
    if primary == "BULL":
        implications["preferred_strategies"] = ["meme-momentum", "whale-following"]
        implications["notes"].append("Trending markets favor momentum plays")
        if vol_regime == "LOW_VOL":
            implications["notes"].append("Low vol bull = breakout accumulation phase")
            implications["position_size_modifier"] = 1.1
        elif vol_regime == "HIGH_VOL":
            implications["notes"].append("High vol bull = use tighter stops")
            implications["position_size_modifier"] = 0.8

    elif primary == "BEAR":
        implications["risk_adjustment"] = "defensive"
        implications["position_size_modifier"] = 0.5
        implications["preferred_strategies"] = ["sentiment-divergence"]
        implications["avoid_strategies"] = ["meme-momentum", "early-launch"]
        implications["notes"].append("Bear market — reduce exposure, tighten stops")
        if drawdown >= DRAWDOWN_BEAR_THRESHOLD:
            implications["notes"].append(
                f"Deep drawdown ({drawdown*100:.1f}%) — capital preservation mode"
            )
            implications["position_size_modifier"] = 0.3

    elif primary == "SIDEWAYS":
        implications["preferred_strategies"] = ["sentiment-divergence", "cex-listing-play"]
        implications["notes"].append("Range-bound — mean reversion strategies preferred")
        if vol_regime == "HIGH_VOL":
            implications["risk_adjustment"] = "cautious"
            implications["position_size_modifier"] = 0.7
            implications["notes"].append("High vol chop — dangerous for momentum")

    # F&G extremes
    if fg_value <= FG_EXTREME_FEAR:
        implications["notes"].append("Extreme fear — contrarian opportunities possible but risky")
    elif fg_value >= FG_EXTREME_GREED:
        implications["notes"].append("Extreme greed — distribution risk high, reduce new entries")
        implications["position_size_modifier"] = min(
            implications["position_size_modifier"], 0.6
        )

    return implications


# ─────────────────────────────────────────────────────────
# Convenience: get current regime (for imports)
# ─────────────────────────────────────────────────────────
def get_current_regime() -> dict:
    """Get the current regime. Uses cache if fresh (<1h), else reclassifies."""
    cached = _load_json(REGIME_LATEST)
    if cached:
        try:
            ts = datetime.fromisoformat(cached["timestamp"])
            age_min = (_now() - ts).total_seconds() / 60
            if age_min < 60:  # Cache valid for 1 hour
                return cached
        except (KeyError, ValueError):
            pass
    return classify_regime()


def get_regime_tag() -> str:
    """Quick helper: returns just the regime tag string."""
    regime = get_current_regime()
    return regime.get("regime_tag", "UNKNOWN")


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        result = classify_regime()
        print(f"\n{'='*50}")
        print(f"  REGIME: {result.get('regime_tag', 'UNKNOWN')}")
        print(f"  Confidence: {result.get('confidence', 0):.0%}")
        print(f"{'='*50}")
        metrics = result.get("metrics", {})
        print(f"  BTC Price:   ${metrics.get('btc_price', 0):,.0f}")
        print(f"  SMA Slope:   {metrics.get('sma_slope_pct_per_day', 0):+.4f}%/day")
        print(f"  ATR(14):     {metrics.get('atr_14d_pct', 0):.2f}%")
        print(f"  Drawdown:    {metrics.get('drawdown_from_30d_high', 0)*100:.1f}%")
        print(f"  Vol Trend:   {metrics.get('volatility_trend', 'N/A')}")
        print(f"  Fear & Greed: {metrics.get('fear_greed_value', 'N/A')}")
        impl = result.get("implications", {})
        print(f"  Risk Adjust: {impl.get('risk_adjustment', 'N/A')}")
        print(f"  Size Modifier: {impl.get('position_size_modifier', 1.0):.1f}x")
        if impl.get("preferred_strategies"):
            print(f"  Preferred:   {', '.join(impl['preferred_strategies'])}")
        if impl.get("avoid_strategies"):
            print(f"  Avoid:       {', '.join(impl['avoid_strategies'])}")
        if impl.get("notes"):
            for note in impl["notes"]:
                print(f"  Note: {note}")
    except Exception as e:
        _log(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
