#!/usr/bin/env python3
"""
Meme Radar — Sprint 3.4
Deterministic Python. No LLMs. No opinions.

Combines CoinGecko trending + Binance volume spikes + Fear & Greed context
into composite scored meme coin signals.

Designed to run every 5 minutes as a cron job.
Outputs signals to signals/meme_radar/ for signal_router.py to consume.

Scoring:
- CoinGecko trending rank (0-25 pts)
- Binance 24h volume multiple (0-25 pts)
- Price momentum (1h/24h) (0-20 pts)
- Market cap sweet spot (0-15 pts)
- Fear & Greed context (0-15 pts)
Total: 0-100. Minimum to emit signal: 55.
"""

import json
import os
import sys
import time
import hashlib
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent  # /data/.openclaw/workspace/trading
CONFIG_ENV = BASE_DIR / "config" / ".env"
WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.json"

# Signal directories (inputs)
SIGNALS_CG = BASE_DIR / "signals" / "coingecko"
FEAR_GREED_PATH = BASE_DIR / "signals" / "market" / "fear_greed_latest.json"

# Output directory
OUTPUT_DIR = BASE_DIR / "signals" / "meme_radar"

# State
STATE_DIR = BASE_DIR / "state"
RADAR_STATE_PATH = STATE_DIR / "meme_radar_state.json"

# ─────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────
MINIMUM_SIGNAL_SCORE = 55       # Don't emit below this
VOLUME_SPIKE_THRESHOLD = 2.0    # 2x average = noteworthy
SIGNAL_COOLDOWN_MINUTES = 30    # Don't re-emit same token within 30 min
MAX_SIGNALS_PER_RUN = 3         # Cap signals per execution
BINANCE_BASE = "https://api.binance.com"

# Market cap sweet spots (USD)
MCAP_SWEET_LOW = 5_000_000      # $5M — below is too risky
MCAP_SWEET_HIGH = 500_000_000   # $500M — above is too slow for meme plays
MCAP_IDEAL_LOW = 10_000_000     # $10M
MCAP_IDEAL_HIGH = 100_000_000   # $100M — ideal meme territory

# Circuit breaker
_consecutive_failures = 0
_circuit_open_until = 0.0
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN = 300  # 5 minutes


def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[MEME_RADAR] {ts} {msg}", flush=True)


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


def _signal_hash(token: str, source: str) -> str:
    raw = f"{token}:{source}:{_now().strftime('%Y-%m-%d-%H')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _check_circuit_breaker() -> bool:
    global _circuit_open_until
    if time.time() < _circuit_open_until:
        remaining = int(_circuit_open_until - time.time())
        _log(f"CIRCUIT BREAKER OPEN — {remaining}s remaining")
        return False
    return True


def _record_failure():
    global _consecutive_failures, _circuit_open_until
    _consecutive_failures += 1
    if _consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
        _circuit_open_until = time.time() + CIRCUIT_BREAKER_COOLDOWN
        _log(f"CIRCUIT BREAKER TRIPPED — {CIRCUIT_BREAKER_COOLDOWN}s cooldown")


def _record_success():
    global _consecutive_failures
    _consecutive_failures = 0


# ─────────────────────────────────────────────────────────
# Data Sources
# ─────────────────────────────────────────────────────────
def _load_coingecko_trending() -> list[dict]:
    """Load latest CoinGecko trending data from signal files."""
    if not SIGNALS_CG.exists():
        _log("No CoinGecko signals directory")
        return []

    # Find the most recent signal file
    files = sorted(SIGNALS_CG.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        _log("No CoinGecko signal files found")
        return []

    latest = files[0]
    age_min = (_now() - datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60

    if age_min > 15:
        _log(f"CoinGecko data stale ({age_min:.0f}min old) — skipping")
        return []

    data = _load_json(latest, {})

    # Handle both formats: direct list or nested under 'trending'/'tokens'
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Try common keys
        for key in ("trending", "tokens", "coins", "data"):
            if key in data and isinstance(data[key], list):
                return data[key]
        # If dict has token-like entries, wrap them
        if "symbol" in data or "token" in data:
            return [data]

    return []


def _load_fear_greed() -> dict:
    """Load latest Fear & Greed data."""
    data = _load_json(FEAR_GREED_PATH, {})
    if not data:
        _log("No Fear & Greed data — using neutral defaults")
        return {"value": 50, "regime": "NEUTRAL", "trend_7d": "stable"}
    return data


def _get_binance_ticker(symbol: str) -> dict | None:
    """Fetch 24h ticker from Binance for a symbol."""
    if not _check_circuit_breaker():
        return None
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/api/v3/ticker/24hr",
            params={"symbol": symbol},
            timeout=10
        )
        if resp.status_code == 400:
            # Symbol not on Binance
            return None
        resp.raise_for_status()
        _record_success()
        return resp.json()
    except requests.RequestException as e:
        _record_failure()
        _log(f"Binance ticker error for {symbol}: {e}")
        return None


def _get_binance_klines(symbol: str, interval: str = "1h", limit: int = 24) -> list | None:
    """Fetch klines for volume comparison."""
    if not _check_circuit_breaker():
        return None
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )
        if resp.status_code == 400:
            return None
        resp.raise_for_status()
        _record_success()
        return resp.json()
    except requests.RequestException as e:
        _record_failure()
        _log(f"Binance klines error for {symbol}: {e}")
        return None


def _load_watchlist() -> set[str]:
    """Load watchlist tokens."""
    data = _load_json(WATCHLIST_PATH, {})
    if isinstance(data, list):
        return {t.get("symbol", t.get("token", "")).upper() for t in data if isinstance(t, dict)}
    if isinstance(data, dict):
        tokens = data.get("tokens", data.get("symbols", []))
        if isinstance(tokens, list):
            return {t.upper() if isinstance(t, str) else t.get("symbol", "").upper() for t in tokens}
    return set()


def _load_radar_state() -> dict:
    """Load meme radar state (cooldowns, history)."""
    return _load_json(RADAR_STATE_PATH, {
        "last_run": None,
        "signals_emitted": {},
        "total_signals_today": 0,
        "last_reset_date": _now().strftime("%Y-%m-%d"),
    })


# ─────────────────────────────────────────────────────────
# Scoring Functions
# ─────────────────────────────────────────────────────────
def _score_trending_rank(token_data: dict) -> int:
    """Score based on CoinGecko trending position (0-25 pts).
    Rank 1 = 25pts, Rank 5 = 15pts, Rank 10 = 8pts, >15 = 3pts.
    """
    rank = token_data.get("rank", token_data.get("market_cap_rank", 999))

    # For trending list, position matters
    idx = token_data.get("_trending_index", 99)

    if idx <= 2:
        return 25
    elif idx <= 5:
        return 20
    elif idx <= 8:
        return 15
    elif idx <= 12:
        return 10
    elif idx <= 15:
        return 8
    else:
        return 3


def _score_volume_spike(ticker_24h: dict | None, klines: list | None) -> tuple[int, float]:
    """Score based on volume anomaly (0-25 pts).
    Returns (score, volume_multiple).
    Compare current 24h volume to average of prior days.
    """
    if not ticker_24h:
        return 0, 0.0

    try:
        vol_24h = float(ticker_24h.get("quoteVolume", 0))
    except (ValueError, TypeError):
        return 0, 0.0

    if vol_24h <= 0:
        return 0, 0.0

    # Calculate average volume from klines
    if klines and len(klines) >= 12:
        # Each kline: [open_time, open, high, low, close, volume, close_time, quote_vol, ...]
        try:
            historical_vols = [float(k[7]) for k in klines[:-1]]  # quote volume, exclude latest
            avg_vol = sum(historical_vols) / len(historical_vols) if historical_vols else 0
            # Annualize to 24h equivalent
            hours = len(historical_vols)
            avg_24h = (avg_vol / hours) * 24 if hours > 0 else 0
        except (ValueError, IndexError):
            avg_24h = 0
    else:
        avg_24h = 0

    if avg_24h <= 0:
        # No baseline — give moderate score if volume is meaningful
        if vol_24h > 1_000_000:  # > $1M
            return 10, 0.0
        return 5, 0.0

    multiple = vol_24h / avg_24h

    if multiple >= 5.0:
        return 25, multiple
    elif multiple >= 3.0:
        return 20, multiple
    elif multiple >= 2.0:
        return 15, multiple
    elif multiple >= 1.5:
        return 10, multiple
    elif multiple >= 1.0:
        return 5, multiple
    else:
        return 0, multiple


def _score_momentum(ticker_24h: dict | None) -> int:
    """Score based on price momentum (0-20 pts).
    Combines 24h change percentage.
    """
    if not ticker_24h:
        return 0

    try:
        pct_24h = float(ticker_24h.get("priceChangePercent", 0))
    except (ValueError, TypeError):
        return 0

    # Sweet spot: strong positive momentum but not overextended
    if 5 <= pct_24h <= 15:
        return 20  # Ideal entry zone — momentum confirmed, not overbought
    elif 15 < pct_24h <= 30:
        return 15  # Strong but might be getting late
    elif 30 < pct_24h <= 50:
        return 10  # Late but could still run
    elif 2 <= pct_24h < 5:
        return 12  # Early momentum building
    elif pct_24h > 50:
        return 5   # Already pumped — high risk of being exit liquidity
    elif 0 <= pct_24h < 2:
        return 5   # Flat — no momentum yet
    elif -5 <= pct_24h < 0:
        return 3   # Slight pullback — could be accumulation
    else:
        return 0   # Dumping — avoid


def _score_market_cap(token_data: dict) -> int:
    """Score based on market cap sweet spot (0-15 pts).
    Ideal meme territory: $10M-$100M.
    """
    mcap = token_data.get("market_cap", token_data.get("mcap", 0))
    if not mcap or mcap <= 0:
        return 5  # Unknown — neutral

    if MCAP_IDEAL_LOW <= mcap <= MCAP_IDEAL_HIGH:
        return 15  # Sweet spot
    elif MCAP_SWEET_LOW <= mcap < MCAP_IDEAL_LOW:
        return 10  # Small but viable
    elif MCAP_IDEAL_HIGH < mcap <= MCAP_SWEET_HIGH:
        return 10  # Larger but still meme-capable
    elif mcap < MCAP_SWEET_LOW:
        return 3   # Micro-cap — extremely risky
    else:
        return 5   # Too large for meme plays


def _score_fear_greed(fg_data: dict) -> int:
    """Score based on Fear & Greed context (0-15 pts).
    Best for meme coins: greed (but not extreme) + rising trend.
    """
    value = fg_data.get("value", 50)
    trend = fg_data.get("trend_7d", "stable")

    base = 0

    # F&G value scoring
    if 55 <= value <= 75:
        base = 12  # Greed zone — meme coins thrive
    elif 40 <= value <= 55:
        base = 8   # Neutral — okay
    elif 75 < value <= 85:
        base = 6   # Getting overheated
    elif 25 <= value < 40:
        base = 4   # Fear — memes usually suffer
    elif value > 85:
        base = 3   # Extreme greed — reversal risk
    else:
        base = 2   # Extreme fear — memes crash

    # Trend bonus
    if trend == "rising" and value < 80:
        base = min(base + 3, 15)
    elif trend == "falling":
        base = max(base - 2, 0)

    return base


# ─────────────────────────────────────────────────────────
# Main Radar Logic
# ─────────────────────────────────────────────────────────
def run_radar():
    """Main radar execution."""
    now = _now()
    _log(f"=== MEME RADAR SCAN ===")
    _log(now.strftime("%Y-%m-%dT%H:%M:%S+00:00"))

    # Load state
    state = _load_radar_state()

    # Reset daily counter
    today = now.strftime("%Y-%m-%d")
    if state.get("last_reset_date") != today:
        state["total_signals_today"] = 0
        state["last_reset_date"] = today

    # Load data sources
    trending = _load_coingecko_trending()
    fg_data = _load_fear_greed()
    watchlist = _load_watchlist()

    _log(f"Trending tokens: {len(trending)}")
    _log(f"Fear & Greed: {fg_data.get('value', '?')} ({fg_data.get('regime', '?')})")
    _log(f"Watchlist size: {len(watchlist)}")

    if not trending:
        _log("No trending data — nothing to scan")
        state["last_run"] = now.isoformat()
        _save_json_atomic(RADAR_STATE_PATH, state)
        return []

    # Score each candidate
    candidates = []

    for idx, token in enumerate(trending):
        # Extract token info
        symbol = (
            token.get("symbol", "")
            or token.get("token", "")
            or token.get("id", "")
        ).upper()

        if not symbol:
            continue

        # Add trending index for scoring
        token["_trending_index"] = idx

        # Build Binance symbol (try USDT pair)
        binance_symbol = f"{symbol}USDT"

        # Fetch Binance data (rate-limited by circuit breaker)
        ticker = _get_binance_ticker(binance_symbol)
        klines = None
        if ticker:
            klines = _get_binance_klines(binance_symbol, "1h", 24)
            time.sleep(0.1)  # Gentle rate limiting

        # Calculate all scores
        score_trending = _score_trending_rank(token)
        score_volume, vol_multiple = _score_volume_spike(ticker, klines)
        score_momentum = _score_momentum(ticker)
        score_mcap = _score_market_cap(token)
        score_fg = _score_fear_greed(fg_data)

        total = score_trending + score_volume + score_momentum + score_mcap + score_fg

        # Build candidate record
        candidate = {
            "token": symbol,
            "binance_symbol": binance_symbol if ticker else None,
            "total_score": total,
            "scores": {
                "trending": score_trending,
                "volume": score_volume,
                "momentum": score_momentum,
                "market_cap": score_mcap,
                "fear_greed": score_fg,
            },
            "data": {
                "volume_multiple": round(vol_multiple, 2) if vol_multiple else None,
                "price_change_24h": float(ticker.get("priceChangePercent", 0)) if ticker else None,
                "current_price": float(ticker.get("lastPrice", 0)) if ticker else None,
                "volume_usd_24h": float(ticker.get("quoteVolume", 0)) if ticker else None,
                "market_cap": token.get("market_cap", token.get("mcap")),
                "trending_index": idx,
                "on_watchlist": symbol in watchlist,
            },
            "fear_greed": {
                "value": fg_data.get("value"),
                "regime": fg_data.get("regime"),
            },
            "timestamp": now.isoformat(),
        }
        candidates.append(candidate)

        _log(f"  {symbol}: {total}/100 "
             f"(trend={score_trending} vol={score_volume} "
             f"mom={score_momentum} mcap={score_mcap} fg={score_fg})"
             f"{' [WATCHLIST]' if symbol in watchlist else ''}")

    # Sort by total score descending
    candidates.sort(key=lambda c: c["total_score"], reverse=True)

    # Filter: minimum score + cooldown check
    signals_emitted = state.get("signals_emitted", {})
    emitted = []

    for candidate in candidates:
        if len(emitted) >= MAX_SIGNALS_PER_RUN:
            break

        token = candidate["token"]
        score = candidate["total_score"]

        # Score gate
        if score < MINIMUM_SIGNAL_SCORE:
            continue

        # Must have Binance data (we need price for trading)
        if not candidate["binance_symbol"]:
            _log(f"  SKIP {token}: Not on Binance")
            continue

        # Cooldown check
        last_emitted = signals_emitted.get(token)
        if last_emitted:
            try:
                last_dt = datetime.fromisoformat(last_emitted)
                if (now - last_dt).total_seconds() < SIGNAL_COOLDOWN_MINUTES * 60:
                    _log(f"  SKIP {token}: Cooldown ({SIGNAL_COOLDOWN_MINUTES}min)")
                    continue
            except (ValueError, TypeError):
                pass

        # Watchlist bonus: tokens on our watchlist get priority logging
        if candidate["data"]["on_watchlist"]:
            _log(f"  >>> WATCHLIST MATCH: {token} score={score}")

        # Build thesis string
        vol_mult = candidate["data"]["volume_multiple"]
        pct_chg = candidate["data"]["price_change_24h"]
        thesis_parts = [f"{token} trending #{candidate['data']['trending_index']+1} on CoinGecko"]
        if vol_mult and vol_mult >= VOLUME_SPIKE_THRESHOLD:
            thesis_parts.append(f"with {vol_mult}x volume spike")
        if pct_chg is not None:
            thesis_parts.append(f"24h change {pct_chg:+.1f}%")
        thesis_parts.append(f"Market sentiment: {fg_data.get('regime', 'NEUTRAL')}")
        thesis_str = ", ".join(thesis_parts) + "."

        # Emit signal
        signal = {
            "token": token,
            "symbol": candidate["binance_symbol"],
            "source": "meme_radar",
            "source_detail": "CoinGecko trending + Binance volume + Fear&Greed composite",
            "thesis": thesis_str,
            "signal_score": score,
            "scores_breakdown": candidate["scores"],
            "current_price": candidate["data"]["current_price"],
            "volume_usd_24h": candidate["data"]["volume_usd_24h"],
            "volume_multiple": candidate["data"]["volume_multiple"],
            "market_cap": candidate["data"]["market_cap"],
            "fear_greed_value": fg_data.get("value"),
            "on_watchlist": candidate["data"]["on_watchlist"],
            "timestamp": now.isoformat(),
            "signal_id": _signal_hash(token, "meme_radar"),
        }

        # Save signal file
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{now.strftime('%Y%m%d_%H%M')}_{token}.json"
        signal_path = OUTPUT_DIR / filename
        _save_json_atomic(signal_path, signal)

        # Update cooldown
        signals_emitted[token] = now.isoformat()
        emitted.append(signal)
        _log(f"  SIGNAL EMITTED: {token} score={score} -> {filename}")

    # Save all candidates report (for debugging / analysis)
    report_path = OUTPUT_DIR / "latest_scan.json"
    report = {
        "scan_time": now.isoformat(),
        "candidates_scanned": len(candidates),
        "signals_emitted": len(emitted),
        "fear_greed": fg_data,
        "top_candidates": candidates[:10],  # Top 10 for review
    }
    _save_json_atomic(report_path, report)

    # Update state
    state["last_run"] = now.isoformat()
    state["signals_emitted"] = signals_emitted
    state["total_signals_today"] = state.get("total_signals_today", 0) + len(emitted)
    _save_json_atomic(RADAR_STATE_PATH, state)

    _log(f"=== SCAN COMPLETE: {len(emitted)} signals from {len(candidates)} candidates ===")
    return emitted


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        signals = run_radar()
        if signals:
            _log(f"  Signals emitted:")
            for s in signals:
                _log(f"    {s['token']}: score={s['signal_score']} price=${s.get('current_price', 0):,.6f}")
        else:
            _log("  No signals met threshold this scan.")
    except Exception as e:
        _log(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
