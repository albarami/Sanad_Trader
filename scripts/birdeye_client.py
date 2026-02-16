#!/usr/bin/env python3
"""
Birdeye API Client — Sprint 3.3
Deterministic Python. No LLMs.
Two modes: standalone cron job AND importable module.

Free tier endpoints available:
  - /defi/token_trending  (trending Solana tokens)
  - /defi/token_overview   (detailed token data per address)

Paid tier endpoints (implemented but gracefully degrade if 404):
  - /defi/meme_token_list
  - /defi/token_new_listing
  - /defi/smart_money/token_list
  - /defi/token_holder_distribution
  - /defi/token_creation_info
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
CONFIG_ENV = BASE_DIR / "config" / ".env"
SIGNALS_DIR = BASE_DIR / "signals" / "birdeye"
WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.json"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://public-api.birdeye.so"
DEFAULT_HEADERS = {"x-chain": "solana", "accept": "application/json"}

# Rate limiting — Birdeye free tier is very strict (~10 req/min observed)
MAX_CALLS_PER_MINUTE = 10
_call_timestamps: list[float] = []

# Circuit breaker
_consecutive_failures = 0
_circuit_open_until = 0.0
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN = 300


def _load_api_key() -> str:
    if CONFIG_ENV.exists():
        for line in CONFIG_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("BIRDEYE_API_KEY="):
                return line.split("=", 1)[1].strip()
    key = os.environ.get("BIRDEYE_API_KEY", "")
    if not key:
        raise RuntimeError("BIRDEYE_API_KEY not found in config/.env or environment")
    return key


API_KEY = _load_api_key()


def _log(msg: str):
    print(f"[BIRDEYE] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
def _rate_limit():
    global _call_timestamps
    now = time.time()
    _call_timestamps = [t for t in _call_timestamps if now - t < 60]
    if len(_call_timestamps) >= MAX_CALLS_PER_MINUTE:
        sleep_for = 60 - (now - _call_timestamps[0]) + 1.0
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
            raise RuntimeError(f"Circuit breaker OPEN — {remaining}s remaining")
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
def _get(path: str, params: dict | None = None, allow_404: bool = False) -> dict | list | None:
    """
    GET request to Birdeye API.
    If allow_404=True, returns None on 404 (paid-tier endpoint).
    """
    _check_circuit()
    _rate_limit()
    url = f"{BASE_URL}{path}"
    headers = {**DEFAULT_HEADERS, "X-API-KEY": API_KEY}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code in (401, 403):
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            msg = body.get("message", f"HTTP {resp.status_code}")
            if "upgrade" in msg.lower() or "permissions" in msg.lower():
                if allow_404:
                    return None
            _log(f"API key error ({resp.status_code}): {msg}")
            _record_failure()
            raise RuntimeError(f"Auth error: {msg}")
        if resp.status_code == 429:
            _log("Rate limited (429) — sleeping 60s and retrying once")
            time.sleep(60)
            resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # Birdeye returns {"success": false, "message": "Not found"} as 200 sometimes
        if isinstance(data, dict) and data.get("success") is False:
            msg = data.get("message", "")
            if "not found" in msg.lower() or "permissions" in msg.lower() or "upgrade" in msg.lower():
                if allow_404:
                    return None
                raise RuntimeError(f"API error: {msg}")
        _reset_circuit()
        return data
    except requests.exceptions.RequestException as e:
        _record_failure()
        raise RuntimeError(f"API error on {url}: {e}") from e


# ---------------------------------------------------------------------------
# Core API functions — FREE TIER
# ---------------------------------------------------------------------------
def get_trending_tokens(sort_by="rank", sort_type="asc", limit=20) -> list[dict]:
    """GET /defi/token_trending — trending Solana tokens (FREE)."""
    data = _get("/defi/token_trending", params={
        "sort_by": sort_by, "sort_type": sort_type,
        "offset": 0, "limit": limit,
    })
    if not data or not isinstance(data, dict):
        return []
    tokens = (data.get("data") or {}).get("tokens", [])
    # Fallback: data.data.items
    if not tokens:
        tokens = (data.get("data") or {}).get("items", [])
    return tokens


def get_token_overview(address: str) -> dict:
    """GET /defi/token_overview — detailed token data (FREE)."""
    data = _get("/defi/token_overview", params={"address": address})
    if not data or not isinstance(data, dict):
        return {}
    return data.get("data", {})


# ---------------------------------------------------------------------------
# Core API functions — PAID TIER (gracefully degrade)
# ---------------------------------------------------------------------------
def get_meme_token_list(sort_by="v24hUSD", sort_type="desc", limit=20) -> list[dict] | None:
    """GET /defi/meme_token_list — meme token radar (PAID)."""
    data = _get("/defi/meme_token_list", params={
        "sort_by": sort_by, "sort_type": sort_type,
        "offset": 0, "limit": limit,
    }, allow_404=True)
    if data is None:
        return None
    return (data.get("data") or {}).get("items", [])


def get_new_listing(sort_by="createdAt", sort_type="desc", limit=20) -> list[dict] | None:
    """GET /defi/token_new_listing — newly listed tokens (PAID)."""
    data = _get("/defi/token_new_listing", params={
        "sort_by": sort_by, "sort_type": sort_type,
        "offset": 0, "limit": limit,
    }, allow_404=True)
    if data is None:
        return None
    return (data.get("data") or {}).get("items", [])


def get_token_holder_distribution(address: str) -> dict | None:
    """GET /defi/token_holder_distribution — rug detection (PAID)."""
    data = _get("/defi/token_holder_distribution", params={"address": address}, allow_404=True)
    if data is None:
        return None
    return data.get("data", {})


def get_smart_money_token_list(sort_by="v24hUSD", sort_type="desc", limit=20) -> list[dict] | None:
    """GET /defi/smart_money/token_list — whale tracking (PAID)."""
    data = _get("/defi/smart_money/token_list", params={
        "sort_by": sort_by, "sort_type": sort_type,
        "offset": 0, "limit": limit,
    }, allow_404=True)
    if data is None:
        return None
    return (data.get("data") or {}).get("items", [])


def get_token_creation_info(address: str) -> dict | None:
    """GET /defi/token_creation_info — token age (PAID)."""
    data = _get("/defi/token_creation_info", params={"address": address}, allow_404=True)
    if data is None:
        return None
    return data.get("data", {})


# ---------------------------------------------------------------------------
# Watchlist
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
# Helpers
# ---------------------------------------------------------------------------
def _sf(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _normalize_trending(t: dict) -> dict:
    """Normalize trending token response to standard fields."""
    return {
        "address": t.get("address") or "",
        "symbol": (t.get("symbol") or "").upper(),
        "name": t.get("name") or "",
        "price": _sf(t.get("price")),
        "volume_24h": _sf(t.get("volume24hUSD") or t.get("v24hUSD")),
        "price_change_24h_pct": _sf(t.get("price24hChangePercent") or t.get("v24hChangePercent")),
        "volume_change_24h_pct": _sf(t.get("volume24hChangePercent") or t.get("v24hChangePercent")),
        "market_cap": _sf(t.get("marketcap") or t.get("mc") or t.get("realMc")),
        "liquidity": _sf(t.get("liquidity") or t.get("liquidityUsd")),
        "fdv": _sf(t.get("fdv")),
        "rank": t.get("rank"),
    }


def _normalize_overview(o: dict) -> dict:
    """Normalize token overview response."""
    return {
        "address": o.get("address") or "",
        "symbol": (o.get("symbol") or "").upper(),
        "name": o.get("name") or "",
        "price": _sf(o.get("price")),
        "volume_24h": _sf(o.get("v24hUSD") or o.get("volume24hUSD")),
        "price_change_24h_pct": _sf(o.get("priceChange24hPercent") or o.get("v24hChangePercent")),
        "price_change_1h_pct": _sf(o.get("priceChange1hPercent")),
        "market_cap": _sf(o.get("marketCap") or o.get("mc") or o.get("realMc")),
        "liquidity": _sf(o.get("liquidity")),
        "holder_count": int(_sf(o.get("holder") or o.get("holderCount"))),
        "trade_count_24h": int(_sf(o.get("trade24h") or o.get("uniqueWallet24h"))),
        "fdv": _sf(o.get("fdv")),
        "last_trade_time": o.get("lastTradeUnixTime"),
    }


def _passes_filter(t: dict) -> bool:
    """Volume > $100K, liquidity > $50K, market cap > $100K."""
    return (
        t.get("volume_24h", 0) > 100_000
        and t.get("liquidity", 0) > 50_000
        and t.get("market_cap", 0) > 100_000
    )


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------
def _build_signal(
    token: dict,
    signal_type: str,
    smart_money: bool,
    top10_pct: float | None,
    rug_flags: list[str],
) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    sym = token.get("symbol", "UNKNOWN")
    vol = token.get("volume_24h", 0)
    liq = token.get("liquidity", 0)
    mc = token.get("market_cap", 0)
    pct24 = token.get("price_change_24h_pct", 0)
    pct1h = token.get("price_change_1h_pct")
    holders = token.get("holder_count", 0)
    trades = token.get("trade_count_24h", 0)

    # Thesis
    parts = [f"{sym} {signal_type.lower().replace('_', ' ')} on Birdeye"]
    if pct24:
        parts.append(f"{'+' if pct24 > 0 else ''}{pct24:.0f}% 24h")
    if smart_money:
        parts.append("Smart money active")
    parts.append(f"Volume ${vol/1e6:.1f}M" if vol >= 1e6 else f"Volume ${vol/1e3:.0f}K")
    parts.append(f"liquidity ${liq/1e3:.0f}K")
    if top10_pct is not None:
        health = "healthy" if top10_pct < 60 else "moderate" if top10_pct < 80 else "concentrated"
        parts.append(f"Holder distribution {health} (top 10 = {top10_pct:.0f}%)")
    if holders:
        parts.append(f"{holders:,} holders")

    # Confidence
    confidence = "low"
    skip_rug = any("skip_" in f for f in rug_flags)
    if not skip_rug:
        if vol > 500_000 and liq > 100_000 and (top10_pct is None or top10_pct < 70):
            confidence = "medium"
        if vol > 1_000_000 and liq > 200_000 and (top10_pct is None or top10_pct < 60):
            confidence = "high"
        if smart_money and confidence != "high":
            confidence = "medium"

    return {
        "source": "birdeye_meme_radar",
        "token": sym,
        "token_address": token.get("address", ""),
        "chain": "solana",
        "signal_type": signal_type,
        "current_price": token.get("price", 0),
        "price_change_1h_pct": pct1h,
        "price_change_24h_pct": pct24,
        "market_cap": mc,
        "volume_24h": vol,
        "liquidity_usd": liq,
        "holder_count": holders,
        "top10_holder_pct": top10_pct,
        "smart_money_signal": smart_money,
        "token_age_hours": None,
        "trade_count_24h": trades,
        "timestamp": now,
        "thesis": ". ".join(parts) + ".",
        "confidence": confidence,
        "rug_flags": rug_flags,
    }


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------
def run_scan():
    now = datetime.now(timezone.utc)
    ts_label = now.strftime("%Y-%m-%d_%H-%M")
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    _log(now.strftime("%Y-%m-%dT%H:%M:%S+00:00"))

    paid_available = True  # will be set False if first paid call returns None

    # --- 1. Meme token list (PAID) ---
    meme_raw = None
    meme_filtered = []
    try:
        meme_raw = get_meme_token_list(sort_by="v24hChangePercent", sort_type="desc", limit=20)
        if meme_raw is None:
            _log("Meme tokens: SKIPPED (paid tier endpoint)")
            paid_available = False
        else:
            meme_norm = [_normalize_trending(t) for t in meme_raw]
            meme_filtered = [t for t in meme_norm if _passes_filter(t)]
            _log(f"Meme tokens: {len(meme_raw)} fetched, {len(meme_filtered)} passed filters")
    except Exception as e:
        _log(f"ERROR fetching meme tokens: {e}")

    # --- 2. Trending tokens (FREE) ---
    trending_raw = []
    trending_filtered = []
    try:
        trending_raw = get_trending_tokens(limit=20)
        trending_norm = [_normalize_trending(t) for t in trending_raw]
        trending_filtered = [t for t in trending_norm if _passes_filter(t)]
        _log(f"Trending: {len(trending_raw)} fetched, {len(trending_filtered)} passed filters")
    except Exception as e:
        _log(f"ERROR fetching trending: {e}")

    # --- 3. Smart money (PAID) ---
    smart_raw = None
    smart_addresses: set[str] = set()
    if paid_available:
        try:
            smart_raw = get_smart_money_token_list(limit=20)
            if smart_raw is None:
                _log("Smart money: SKIPPED (paid tier endpoint)")
            else:
                smart_addresses = {t.get("address", "") for t in smart_raw if t.get("address")}
                _log(f"Smart money: {len(smart_raw)} fetched")
        except Exception as e:
            _log(f"ERROR fetching smart money: {e}")
    else:
        _log("Smart money: SKIPPED (paid tier)")

    # --- 4. New listings (PAID) ---
    new_raw = None
    new_filtered = []
    if paid_available:
        try:
            new_raw = get_new_listing(limit=20)
            if new_raw is None:
                _log("New listings: SKIPPED (paid tier endpoint)")
            else:
                new_norm = [_normalize_trending(t) for t in new_raw]
                new_filtered = [t for t in new_norm if t.get("volume_24h", 0) > 50_000 and t.get("liquidity", 0) > 50_000]
                _log(f"New listings: {len(new_raw)} fetched, {len(new_filtered)} passed filters")
        except Exception as e:
            _log(f"ERROR fetching new listings: {e}")
    else:
        _log("New listings: SKIPPED (paid tier)")

    # --- 5. Merge candidates (deduplicate by address) ---
    seen: set[str] = set()
    candidates: list[tuple[dict, str]] = []

    for t in meme_filtered:
        addr = t.get("address", "")
        if addr and addr not in seen:
            seen.add(addr)
            candidates.append((t, "MEME_GAINER"))

    for t in trending_filtered:
        addr = t.get("address", "")
        if addr and addr not in seen:
            seen.add(addr)
            candidates.append((t, "TRENDING"))

    for t in new_filtered:
        addr = t.get("address", "")
        if addr and addr not in seen:
            seen.add(addr)
            candidates.append((t, "NEW_LISTING"))

    # --- 6. Enrich top 5 with token_overview (FREE) ---
    _log(f"Enriching top {min(5, len(candidates))} candidates with token_overview...")
    enriched_candidates: list[tuple[dict, str]] = []

    for i, (token, stype) in enumerate(candidates):
        addr = token.get("address", "")
        if i < 5 and addr:
            try:
                overview = get_token_overview(addr)
                if overview:
                    enriched = _normalize_overview(overview)
                    # Keep original signal_type
                    enriched_candidates.append((enriched, stype))
                    continue
            except Exception as e:
                _log(f"  Overview error for {token.get('symbol', '?')}: {e}")
        enriched_candidates.append((token, stype))

    # --- 7. Holder distribution for top 5 (PAID — graceful degrade) ---
    holder_checked = False
    if paid_available:
        _log("Checking holder distribution for top candidates...")
    signals = []
    checked = 0

    for token, stype in enriched_candidates:
        addr = token.get("address", "")
        is_smart = addr in smart_addresses

        top10_pct = None
        rug_flags: list[str] = []

        # Try holder distribution for top 5 (PAID)
        if checked < 5 and addr and paid_available:
            try:
                dist = get_token_holder_distribution(addr)
                if dist is None:
                    rug_flags.append("holder_data_unavailable (paid tier)")
                    if not holder_checked:
                        _log("Holder distribution: SKIPPED (paid tier endpoint)")
                        paid_available = False  # stop trying
                else:
                    holder_checked = True
                    # Parse distribution
                    holders_list = dist if isinstance(dist, list) else dist.get("items") or dist.get("holders") or []
                    if isinstance(holders_list, list) and holders_list:
                        top10 = holders_list[:10]
                        top10_pct = sum(
                            _sf(h.get("pct") or h.get("percentage") or h.get("uiAmountPercent"))
                            for h in top10
                        )
                    if top10_pct and top10_pct > 90:
                        rug_flags.append(f"skip_high_concentration: top 10 hold {top10_pct:.0f}%")
                    elif top10_pct and top10_pct > 80:
                        rug_flags.append(f"high_concentration: top 10 hold {top10_pct:.0f}%")
            except Exception:
                rug_flags.append("holder_data_unavailable")
            checked += 1
        elif addr:
            rug_flags.append("holder_data_not_checked")

        # Low holder count flag
        hc = token.get("holder_count", 0)
        if 0 < hc < 200:
            rug_flags.append(f"low_holder_count: only {hc} holders")

        # Skip if top10 > 90%
        if any("skip_" in f for f in rug_flags):
            continue

        signals.append(_build_signal(token, stype, is_smart, top10_pct, rug_flags))

    # --- 8. Log results ---
    _log(f"Signals: {len(signals)} found")
    for i, s in enumerate(signals, 1):
        sym = s["token"]
        stype = s["signal_type"].lower().replace("_", " ")
        pct = s.get("price_change_24h_pct") or 0
        vol = s.get("volume_24h") or 0
        liq = s.get("liquidity_usd") or 0
        holders = s.get("holder_count") or 0
        top10 = s.get("top10_holder_pct")
        smart = " 🐋" if s.get("smart_money_signal") else ""

        vol_str = f"vol ${vol/1e6:.1f}M" if vol >= 1e6 else f"vol ${vol/1e3:.0f}K"
        liq_str = f"liq ${liq/1e3:.0f}K"
        h_str = f"holders {holders/1e3:.1f}K" if holders >= 1000 else f"holders {holders}"

        if top10 is not None and top10 > 0:
            flag = " ⛔ FLAGGED" if top10 > 80 else " ⚠️" if top10 > 60 else " ✅"
            t10_str = f"top10={top10:.0f}%{flag}"
        else:
            t10_str = "top10=N/A"

        pct_str = f"{'+' if pct > 0 else ''}{pct:.0f}%"
        _log(f"  {i}. {sym} — {stype} {pct_str}, {vol_str}, {liq_str}, {h_str}, {t10_str}{smart}")

    # --- 9. Save ---
    raw_output = {
        "timestamp": now.isoformat(),
        "tier": "free" if not holder_checked and meme_raw is None else "paid",
        "trending_raw": trending_raw,
        "meme_tokens_raw": meme_raw,
        "smart_money_raw": smart_raw,
        "new_listings_raw": new_raw,
        "signals": signals,
    }
    raw_path = SIGNALS_DIR / f"{ts_label}.json"
    raw_path.write_text(json.dumps(raw_output, indent=2, default=str))
    _log(f"Data saved to signals/birdeye/{ts_label}.json")

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
