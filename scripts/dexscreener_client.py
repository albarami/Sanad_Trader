#!/usr/bin/env python3
"""
DexScreener API Client — Sprint 3.1
Deterministic Python. No LLMs. No API key needed.
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
BASE_DIR = SCRIPT_DIR.parent
SIGNALS_DIR = BASE_DIR / "signals" / "dexscreener"
WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.json"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://api.dexscreener.com"

# Rate limiting: max 60 calls/minute
MAX_CALLS_PER_MINUTE = 60
_call_timestamps: list[float] = []

# Circuit breaker
_consecutive_failures = 0
_circuit_open_until = 0.0
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN = 300


def _log(msg: str):
    print(f"[DEXSCREENER] {msg}")


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
def _rate_limit():
    global _call_timestamps
    now = time.time()
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
            raise RuntimeError(
                f"Circuit breaker OPEN — {remaining}s remaining after "
                f"{CIRCUIT_BREAKER_THRESHOLD} consecutive failures"
            )
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
# HTTP helper
# ---------------------------------------------------------------------------
def _get(path: str, params: dict | None = None) -> dict | list:
    _check_circuit()
    _rate_limit()
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"accept": "application/json"},
            timeout=10,
        )
        if resp.status_code == 429:
            _log("Rate limited (429) — sleeping 60s and retrying once")
            time.sleep(60)
            resp = requests.get(
                url, params=params,
                headers={"accept": "application/json"}, timeout=10,
            )
        resp.raise_for_status()
        _reset_circuit()
        return resp.json()
    except requests.exceptions.RequestException as e:
        _record_failure()
        raise RuntimeError(f"API error on {url}: {e}") from e


# ---------------------------------------------------------------------------
# Core API functions
# ---------------------------------------------------------------------------
def get_token_boosts_latest() -> list[dict]:
    """GET /token-boosts/latest/v1 — currently boosted tokens."""
    data = _get("/token-boosts/latest/v1")
    if isinstance(data, list):
        return data
    return data.get("data", data.get("tokens", []))


def get_token_boosts_top() -> list[dict]:
    """GET /token-boosts/top/v1 — top boosted tokens (most promoted)."""
    data = _get("/token-boosts/top/v1")
    if isinstance(data, list):
        return data
    return data.get("data", data.get("tokens", []))


def get_token_profiles_latest() -> list[dict]:
    """GET /token-profiles/latest/v1 — latest updated token profiles."""
    data = _get("/token-profiles/latest/v1")
    if isinstance(data, list):
        return data
    return data.get("data", data.get("tokens", []))


def get_community_takeovers() -> list[dict]:
    """GET /community-takeovers/latest/v1 — community takeover tokens."""
    data = _get("/community-takeovers/latest/v1")
    if isinstance(data, list):
        return data
    return data.get("data", data.get("tokens", []))


def search_pairs(query: str) -> list[dict]:
    """GET /latest/dex/search?q={query} — search for pairs by name/symbol/address."""
    data = _get("/latest/dex/search", params={"q": query})
    return data.get("pairs", []) if isinstance(data, dict) else []


def get_token_pairs(chain_id: str, token_address: str) -> list[dict]:
    """GET /token-pairs/v1/{chainId}/{tokenAddress}"""
    data = _get(f"/token-pairs/v1/{chain_id}/{token_address}")
    if isinstance(data, list):
        return data
    return data.get("pairs", [])


def get_tokens(chain_id: str, token_addresses: list[str]) -> list[dict]:
    """GET /tokens/v1/{chainId}/{tokenAddresses} — up to 30 addresses."""
    addrs = ",".join(token_addresses[:30])
    data = _get(f"/tokens/v1/{chain_id}/{addrs}")
    if isinstance(data, list):
        return data
    return data.get("tokens", [])


# ---------------------------------------------------------------------------
# Watchlist loader
# ---------------------------------------------------------------------------
def _load_watchlist_symbols() -> set[str]:
    if not WATCHLIST_PATH.exists():
        return set()
    try:
        wl = json.loads(WATCHLIST_PATH.read_text())
        return {
            s.upper().replace("USDT", "").replace("USD", "")
            for s in wl.get("symbols", [])
        }
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Pair data enrichment
# ---------------------------------------------------------------------------
def _enrich_with_pair_data(token_address: str) -> dict | None:
    """Search for Solana pairs for a token address and return best pair data."""
    try:
        pairs = search_pairs(token_address)
    except Exception:
        return None

    # Filter to Solana pairs only, pick highest volume
    sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
    if not sol_pairs:
        return None
    sol_pairs.sort(key=lambda p: float(p.get("volume", {}).get("h24", 0) or 0), reverse=True)
    return sol_pairs[0]


def _pair_age_hours(pair: dict) -> float | None:
    created = pair.get("pairCreatedAt")
    if not created:
        return None
    try:
        created_ts = created / 1000 if created > 1e12 else created
        return (time.time() - created_ts) / 3600
    except Exception:
        return None


def _passes_quality_filter(pair: dict) -> bool:
    """Liquidity > $50K, volume 24h > $100K, age > 1h, buys 24h >= 100."""
    liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    vol = float((pair.get("volume") or {}).get("h24", 0) or 0)
    age = _pair_age_hours(pair)
    txns = pair.get("txns") or {}
    buys_24h = (txns.get("h24") or {}).get("buys", 0) or 0

    if liq < 50_000:
        return False
    if vol < 100_000:
        return False
    if age is not None and age < 1:
        return False
    if buys_24h < 100:
        return False
    return True


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------
def _build_signal(
    token_info: dict,
    pair: dict,
    signal_type: str,
    boost_amount: int | None,
) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    base = pair.get("baseToken") or {}
    price_changes = pair.get("priceChange") or {}
    txns = pair.get("txns") or {}
    txns_24h = txns.get("h24") or {}
    liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    vol = float((pair.get("volume") or {}).get("h24", 0) or 0)
    buys = int(txns_24h.get("buys", 0) or 0)
    sells = int(txns_24h.get("sells", 0) or 0)
    bs_ratio = round(buys / sells, 2) if sells > 0 else buys
    price = float(pair.get("priceUsd") or 0)
    fdv = float(pair.get("fdv") or 0)
    age = _pair_age_hours(pair)

    pct_5m = float(price_changes.get("m5", 0) or 0)
    pct_1h = float(price_changes.get("h1", 0) or 0)
    pct_24h = float(price_changes.get("h24", 0) or 0)

    symbol = base.get("symbol", token_info.get("symbol", "UNKNOWN")).upper()
    address = base.get("address", token_info.get("tokenAddress", ""))

    # Thesis
    parts = []
    if signal_type == "BOOSTED_TOKEN" and boost_amount:
        parts.append(f"{symbol} boosted {boost_amount}x on DexScreener Solana")
    elif signal_type == "COMMUNITY_TAKEOVER":
        parts.append(f"{symbol} community takeover on DexScreener Solana")
    else:
        parts.append(f"{symbol} signal on DexScreener Solana")
    parts.append(f"Volume ${vol/1e6:.1f}M, liquidity ${liq/1e3:.0f}K")
    parts.append(f"Buy/sell ratio {bs_ratio} ({'bullish' if bs_ratio > 1.5 else 'neutral' if bs_ratio > 0.8 else 'bearish'})")
    if pct_1h:
        parts.append(f"{'+' if pct_1h > 0 else ''}{pct_1h:.0f}% in 1h")

    # Confidence
    confidence = "low"
    if vol > 500_000 and liq > 100_000 and bs_ratio > 1.5:
        confidence = "medium"
    if vol > 2_000_000 and liq > 300_000 and bs_ratio > 2.0 and pct_1h > 10:
        confidence = "high"

    return {
        "source": f"dexscreener_{'boost' if signal_type == 'BOOSTED_TOKEN' else 'cto'}",
        "token": symbol,
        "token_address": address,
        "chain": "solana",
        "signal_type": signal_type,
        "current_price": price,
        "price_change_5m_pct": pct_5m,
        "price_change_1h_pct": pct_1h,
        "price_change_24h_pct": pct_24h,
        "volume_24h": vol,
        "liquidity_usd": liq,
        "buys_24h": buys,
        "sells_24h": sells,
        "buy_sell_ratio": bs_ratio,
        "fdv": fdv,
        "pair_age_hours": round(age, 1) if age else None,
        "boost_amount": boost_amount,
        "dex_url": pair.get("url", ""),
        "timestamp": now,
        "thesis": ". ".join(parts) + ".",
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------
def run_scan():
    now = datetime.now(timezone.utc)
    ts_label = now.strftime("%Y-%m-%d_%H-%M")
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    _log(now.strftime("%Y-%m-%dT%H:%M:%S+00:00"))

    # 1. Top boosts
    try:
        boosts_top = get_token_boosts_top()
        _log(f"Top boosts: {len(boosts_top)} tokens fetched")
    except Exception as e:
        _log(f"ERROR fetching top boosts: {e}")
        boosts_top = []

    # 2. Latest profiles
    try:
        profiles = get_token_profiles_latest()
        _log(f"Latest profiles: {len(profiles)} tokens fetched")
    except Exception as e:
        _log(f"ERROR fetching latest profiles: {e}")
        profiles = []

    # 3. Community takeovers
    try:
        ctos = get_community_takeovers()
        _log(f"Community takeovers: {len(ctos)} tokens fetched")
    except Exception as e:
        _log(f"ERROR fetching community takeovers: {e}")
        ctos = []

    # 4. Filter Solana-only candidates
    sol_boosts = [
        b for b in boosts_top
        if b.get("chainId") == "solana"
        and (b.get("totalAmount") or b.get("amount") or 0) >= 50
    ]
    sol_ctos = [c for c in ctos if c.get("chainId") == "solana"]

    _log(f"Solana boosts (totalAmount>=50): {len(sol_boosts)}")
    _log(f"Solana CTOs: {len(sol_ctos)}")

    # 5. Enrich with pair data and apply quality filters
    signals = []
    seen_addresses = set()

    for b in sol_boosts:
        addr = b.get("tokenAddress", "")
        if not addr or addr in seen_addresses:
            continue
        seen_addresses.add(addr)
        pair = _enrich_with_pair_data(addr)
        if not pair:
            continue
        if not _passes_quality_filter(pair):
            continue
        boost_amt = b.get("totalAmount") or b.get("amount") or 0
        signals.append(_build_signal(b, pair, "BOOSTED_TOKEN", boost_amt))

    for c in sol_ctos:
        addr = c.get("tokenAddress", "")
        if not addr or addr in seen_addresses:
            continue
        seen_addresses.add(addr)
        pair = _enrich_with_pair_data(addr)
        if not pair:
            continue
        if not _passes_quality_filter(pair):
            continue
        signals.append(_build_signal(c, pair, "COMMUNITY_TAKEOVER", None))

    _log(f"Solana signals after filter: {len(signals)} found")
    for i, s in enumerate(signals, 1):
        boost_str = f"boosted {s['boost_amount']}x, " if s.get("boost_amount") else "CTO, "
        vol_str = f"vol ${s['volume_24h']/1e6:.1f}M" if s["volume_24h"] >= 1e6 else f"vol ${s['volume_24h']/1e3:.0f}K"
        liq_str = f"liq ${s['liquidity_usd']/1e3:.0f}K"
        pct_1h = s.get("price_change_1h_pct") or 0
        _log(f"  {i}. {s['token']} — {boost_str}{vol_str}, {liq_str}, {'+' if pct_1h > 0 else ''}{pct_1h:.0f}% 1h")

    # 6. Save raw data
    raw_output = {
        "timestamp": now.isoformat(),
        "boosts_top": boosts_top,
        "profiles_latest": profiles,
        "community_takeovers": ctos,
        "signals": signals,
    }
    raw_path = SIGNALS_DIR / f"{ts_label}.json"
    raw_path.write_text(json.dumps(raw_output, indent=2, default=str))
    _log(f"Data saved to signals/dexscreener/{ts_label}.json")

    return signals


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        run_scan()
    except Exception as e:
        _log(f"FATAL: {e}")
        sys.exit(1)
