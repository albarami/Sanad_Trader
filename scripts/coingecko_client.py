#!/usr/bin/env python3
"""
CoinGecko API Client — Sprint 3.2
Deterministic Python. No LLMs.
Two modes: standalone cron job AND importable module.
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent  # /data/.openclaw/workspace/trading
CONFIG_ENV = BASE_DIR / "config" / ".env"
SIGNALS_DIR = BASE_DIR / "signals" / "coingecko"
WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.json"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
FREE_URL = "https://api.coingecko.com/api/v3"
PRO_URL = "https://pro-api.coingecko.com/api/v3"

# Rate limiting: max 30 calls/minute (free tier)
MAX_CALLS_PER_MINUTE = 30
_call_timestamps: list[float] = []

# Circuit breaker: 3 consecutive failures → stop for 5 minutes
_consecutive_failures = 0
_circuit_open_until = 0.0
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN = 300  # seconds


def _load_api_key() -> str:
    """Load COINGECKO_API_KEY from config/.env"""
    if CONFIG_ENV.exists():
        for line in CONFIG_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("COINGECKO_API_KEY="):
                return line.split("=", 1)[1].strip()
    # fallback to environment
    key = os.environ.get("COINGECKO_API_KEY", "")
    if not key:
        raise RuntimeError("COINGECKO_API_KEY not found in config/.env or environment")
    return key


API_KEY = _load_api_key()


def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[COINGECKO] {ts} {msg}" if msg.startswith("\n") == False else f"[COINGECKO] {msg}")


def _log_plain(msg: str):
    print(f"[COINGECKO] {msg}")


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
def _rate_limit():
    """Block until we have capacity within 30 calls/minute."""
    global _call_timestamps
    now = time.time()
    # Prune timestamps older than 60s
    _call_timestamps = [t for t in _call_timestamps if now - t < 60]
    if len(_call_timestamps) >= MAX_CALLS_PER_MINUTE:
        sleep_for = 60 - (now - _call_timestamps[0]) + 0.5
        if sleep_for > 0:
            _log(f"Rate limit: sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)
    _call_timestamps.append(time.time())


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
def _check_circuit():
    global _circuit_open_until
    if _consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
        if time.time() < _circuit_open_until:
            remaining = int(_circuit_open_until - time.time())
            raise RuntimeError(f"Circuit breaker OPEN — {remaining}s remaining after {CIRCUIT_BREAKER_THRESHOLD} consecutive failures")
        # cooldown expired, reset
        _reset_circuit()


def _record_failure():
    global _consecutive_failures, _circuit_open_until
    _consecutive_failures += 1
    if _consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
        _circuit_open_until = time.time() + CIRCUIT_BREAKER_COOLDOWN
        _log(f"Circuit breaker OPENED — pausing API calls for {CIRCUIT_BREAKER_COOLDOWN}s")


def _reset_circuit():
    global _consecutive_failures, _circuit_open_until
    _consecutive_failures = 0
    _circuit_open_until = 0.0


# ---------------------------------------------------------------------------
# HTTP helper — tries Pro then Free
# ---------------------------------------------------------------------------
def _get(path: str, params: dict | None = None) -> dict | list:
    _check_circuit()
    _rate_limit()

    # Try Pro first, fall back to Free
    for base_url, header_key in [
        (PRO_URL, "x-cg-pro-api-key"),
        (FREE_URL, "x-cg-demo-api-key"),
    ]:
        url = f"{base_url}{path}"
        headers = {header_key: API_KEY, "accept": "application/json"}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code in (401, 403):
                continue  # wrong tier, try next
            if resp.status_code == 429:
                _log("Rate limited (429) — sleeping 60s and retrying once")
                time.sleep(60)
                resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            _reset_circuit()
            return resp.json()
        except requests.exceptions.RequestException as e:
            _record_failure()
            _log(f"API error on {url}: {e}")
            continue

    _record_failure()
    raise RuntimeError(f"All endpoints failed for {path}")


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------
def get_trending_coins() -> list[dict]:
    """GET /search/trending — top 15 trending coins."""
    data = _get("/search/trending")
    coins = []
    for item in data.get("coins", [])[:15]:
        c = item.get("item", {})
        coins.append({
            "id": c.get("id"),
            "symbol": c.get("symbol", "").upper(),
            "name": c.get("name"),
            "market_cap_rank": c.get("market_cap_rank"),
            "price_btc": c.get("price_btc", 0),
            "score": c.get("score"),
        })
    return coins


def get_top_gainers() -> list[dict]:
    """Top 20 coins by 24h price gain, filtered by market cap > $1M and volume > $500K."""
    data = _get("/coins/markets", params={
        "vs_currency": "usd",
        "order": "price_change_percentage_24h_desc",
        "per_page": 100,  # fetch extra to filter
        "page": 1,
        "sparkline": "false",
    })
    gainers = []
    for c in data:
        mcap = c.get("market_cap") or 0
        vol = c.get("total_volume") or 0
        if mcap < 1_000_000 or vol < 500_000:
            continue
        gainers.append({
            "id": c.get("id"),
            "symbol": (c.get("symbol") or "").upper(),
            "name": c.get("name"),
            "current_price": c.get("current_price"),
            "price_change_24h_pct": c.get("price_change_percentage_24h"),
            "market_cap": mcap,
            "volume_24h": vol,
        })
        if len(gainers) >= 20:
            break
    return gainers


def get_prices(coin_ids: list[str]) -> dict:
    """Get current prices for specific coins."""
    if not coin_ids:
        return {}
    ids_str = ",".join(coin_ids)
    data = _get("/simple/price", params={
        "ids": ids_str,
        "vs_currencies": "usd",
        "include_24hr_vol": "true",
        "include_24hr_change": "true",
    })
    return {
        cid: {
            "usd": info.get("usd"),
            "usd_24h_vol": info.get("usd_24h_vol"),
            "usd_24h_change": info.get("usd_24h_change"),
        }
        for cid, info in data.items()
    }


def get_coin_data(coin_id: str) -> dict:
    """Detailed data for a specific coin."""
    data = _get(f"/coins/{coin_id}", params={
        "localization": "false",
        "tickers": "false",
        "community_data": "true",
        "developer_data": "false",
    })
    md = data.get("market_data", {})
    return {
        "id": data.get("id"),
        "symbol": (data.get("symbol") or "").upper(),
        "name": data.get("name"),
        "current_price_usd": (md.get("current_price") or {}).get("usd"),
        "market_cap_usd": (md.get("market_cap") or {}).get("usd"),
        "total_volume_usd": (md.get("total_volume") or {}).get("usd"),
        "price_change_24h_pct": md.get("price_change_percentage_24h"),
        "market_cap_rank": data.get("market_cap_rank"),
        "community_data": data.get("community_data"),
    }


def get_global_data() -> dict:
    """GET /global — market-wide stats."""
    data = _get("/global")
    gd = data.get("data", {})
    total_mcap = (gd.get("total_market_cap") or {}).get("usd", 0)
    return {
        "total_market_cap": total_mcap,
        "btc_dominance": gd.get("market_cap_percentage", {}).get("btc"),
        "eth_dominance": gd.get("market_cap_percentage", {}).get("eth"),
        "active_coins": gd.get("active_cryptocurrencies"),
        "market_cap_change_24h_pct": gd.get("market_cap_change_percentage_24h_usd"),
    }


# ---------------------------------------------------------------------------
# Watchlist loader
# ---------------------------------------------------------------------------
def _load_watchlist_symbols() -> set[str]:
    """Load watchlist symbols (e.g. BTCUSDT → BTC)."""
    if not WATCHLIST_PATH.exists():
        return set()
    try:
        wl = json.loads(WATCHLIST_PATH.read_text())
        symbols = set()
        for s in wl.get("symbols", []):
            # Strip USDT suffix
            clean = s.upper().replace("USDT", "").replace("USD", "")
            symbols.add(clean)
        return symbols
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------
def _build_signal(coin: dict, signal_type: str, trending_rank: int | None, global_data: dict) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    pct = coin.get("price_change_24h_pct") or 0
    mcap = coin.get("market_cap") or 0
    vol = coin.get("volume_24h") or 0
    price = coin.get("current_price") or 0

    # Build thesis
    parts = []
    if trending_rank is not None:
        parts.append(f"{coin['symbol']} trending #{trending_rank + 1} on CoinGecko")
    if pct:
        parts.append(f"{'+' if pct > 0 else ''}{pct:.1f}% gain in 24h")
    if vol and mcap:
        vol_ratio = vol / mcap * 100
        parts.append(f"Volume ${vol/1e6:.0f}M ({vol_ratio:.0f}% of market cap)")
    mcap_change = global_data.get("market_cap_change_24h_pct")
    if mcap_change is not None:
        parts.append(f"Market 24h: {'+' if mcap_change > 0 else ''}{mcap_change:.1f}%")
    thesis = ". ".join(parts) + "."

    # Confidence heuristic
    confidence = "low"
    if pct and pct > 20 and mcap > 10_000_000:
        confidence = "medium"
    if pct and pct > 30 and mcap > 50_000_000 and vol > 5_000_000:
        confidence = "high"

    return {
        "source": "coingecko_trending",
        "token": coin.get("symbol", ""),
        "coingecko_id": coin.get("id", ""),
        "chain": "unknown",
        "signal_type": signal_type,
        "current_price": price,
        "price_change_24h_pct": pct,
        "market_cap": mcap,
        "volume_24h": vol,
        "trending_rank": trending_rank,
        "timestamp": now,
        "thesis": thesis,
        "confidence": confidence,
    }


def run_scan():
    """Main scan — called when run as cron job."""
    now = datetime.now(timezone.utc)
    ts_label = now.strftime("%Y-%m-%d_%H-%M")
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    _log_plain(now.strftime("%Y-%m-%dT%H:%M:%S+00:00"))

    # 1. Global data
    try:
        global_data = get_global_data()
        mcap_t = global_data["total_market_cap"]
        btc_dom = global_data["btc_dominance"] or 0
        mcap_chg = global_data["market_cap_change_24h_pct"] or 0
        _log_plain(f"Global: Market cap ${mcap_t/1e12:.1f}T | BTC dom {btc_dom:.1f}% | 24h change {'+' if mcap_chg > 0 else ''}{mcap_chg:.1f}%")
    except Exception as e:
        _log_plain(f"ERROR fetching global data: {e}")
        global_data = {}

    # 2. Trending
    try:
        trending = get_trending_coins()
        _log_plain(f"Trending: {len(trending)} coins fetched")
    except Exception as e:
        _log_plain(f"ERROR fetching trending: {e}")
        trending = []

    # 3. Top gainers
    try:
        gainers = get_top_gainers()
        _log_plain(f"Top gainers: {len(gainers)} coins fetched")
    except Exception as e:
        _log_plain(f"ERROR fetching top gainers: {e}")
        gainers = []

    # 4. Watchlist
    watchlist_symbols = _load_watchlist_symbols()

    # 5. Build signal candidates
    trending_symbols = {c["symbol"] for c in trending}
    trending_by_symbol = {c["symbol"]: (i, c) for i, c in enumerate(trending)}
    gainer_by_symbol = {c["symbol"]: c for c in gainers}

    signals = []

    # a) Trending AND gainer overlap
    overlap = trending_symbols & set(gainer_by_symbol.keys())
    for sym in overlap:
        rank, _tc = trending_by_symbol[sym]
        gc = gainer_by_symbol[sym]
        signals.append(_build_signal(gc, "TRENDING_GAINER", rank, global_data))

    # b) Any coin with >30% gain AND market cap > $10M AND volume > $1M
    for g in gainers:
        pct = g.get("price_change_24h_pct") or 0
        mcap = g.get("market_cap") or 0
        vol = g.get("volume_24h") or 0
        if pct > 30 and mcap > 10_000_000 and vol > 1_000_000:
            if g["symbol"] not in overlap:  # avoid duplicates
                rank_info = trending_by_symbol.get(g["symbol"])
                rank = rank_info[0] if rank_info else None
                signals.append(_build_signal(g, "MAJOR_GAINER", rank, global_data))

    # c) Trending coins on watchlist
    watchlist_trending = trending_symbols & watchlist_symbols
    for sym in watchlist_trending:
        if sym not in overlap and sym not in {s["token"] for s in signals}:
            rank, _tc = trending_by_symbol[sym]
            # Try to get gainer data, else build from trending data
            gc = gainer_by_symbol.get(sym)
            if gc:
                signals.append(_build_signal(gc, "WATCHLIST_TRENDING", rank, global_data))
            else:
                signals.append(_build_signal({
                    "id": _tc["id"], "symbol": sym, "name": _tc["name"],
                    "current_price": _tc.get("price_btc"), "price_change_24h_pct": None,
                    "market_cap": None, "volume_24h": None,
                }, "WATCHLIST_TRENDING", rank, global_data))

    # d) Gainers on watchlist (not already captured)
    existing_tokens = {s["token"] for s in signals}
    for sym in watchlist_symbols:
        if sym in gainer_by_symbol and sym not in existing_tokens:
            gc = gainer_by_symbol[sym]
            pct = gc.get("price_change_24h_pct") or 0
            if pct > 10:  # only notable gainers
                signals.append(_build_signal(gc, "WATCHLIST_GAINER", None, global_data))

    _log_plain(f"Potential signals: {len(signals)} found")
    for i, s in enumerate(signals, 1):
        pct = s.get("price_change_24h_pct") or 0
        rank_str = f"trending #{s['trending_rank']+1}" if s.get("trending_rank") is not None else ""
        type_str = s["signal_type"].lower().replace("_", " ")
        parts = [f"{s['token']} — {type_str}"]
        if rank_str:
            parts.append(rank_str)
        if pct:
            parts.append(f"{'+' if pct > 0 else ''}{pct:.1f}%")
        _log_plain(f"  {i}. {' + '.join(parts)}")

    # 6. Save raw data
    raw_output = {
        "timestamp": now.isoformat(),
        "global": global_data,
        "trending": trending,
        "top_gainers": gainers,
        "signals": signals,
        "watchlist_symbols": sorted(watchlist_symbols),
    }
    raw_path = SIGNALS_DIR / f"{ts_label}.json"
    raw_path.write_text(json.dumps(raw_output, indent=2))
    _log_plain(f"Data saved to signals/coingecko/{ts_label}.json")

    # 7. Save global latest
    global_path = SIGNALS_DIR / "global_latest.json"
    global_path.write_text(json.dumps(global_data, indent=2))

    return signals


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        run_scan()
    except Exception as e:
        _log_plain(f"FATAL: {e}")
        sys.exit(1)
