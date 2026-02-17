#!/usr/bin/env python3
"""
Birdeye API Client — Sprint 3.3 (Lite Tier)
Deterministic Python. No LLMs.
Two modes: standalone cron job AND importable module.

Available endpoints (Lite tier):
  - /defi/v3/token/meme/list       (meme token radar)
  - /defi/token_trending            (trending Solana tokens)
  - /defi/v2/tokens/new_listing     (new token listings)
  - /defi/token_overview            (detailed token data)
  - /defi/token_security            (rug detection: top10 holder %, creator %, metadata)
  - /holder/v1/distribution         (top holder wallets + percentages)
  - /defi/token_creation_info       (token age, creator wallet)
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
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
CONFIG_ENV = BASE_DIR / "config" / ".env"
SIGNALS_DIR = BASE_DIR / "signals" / "birdeye"
WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.json"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://public-api.birdeye.so"
DEFAULT_HEADERS = {"x-chain": "solana", "accept": "application/json"}

# Rate limiting — Lite tier ~15 req/min observed safe
MAX_CALLS_PER_MINUTE = 12
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
def _get(path: str, params: dict | None = None) -> dict | list:
    _check_circuit()
    _rate_limit()
    url = f"{BASE_URL}{path}"
    headers = {**DEFAULT_HEADERS, "X-API-KEY": API_KEY}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=8)
        if resp.status_code in (401, 403):
            _log(f"API key error ({resp.status_code})")
            _record_failure()
            raise RuntimeError(f"Auth error HTTP {resp.status_code}")
        if resp.status_code == 429:
            _log("Rate limited (429) — sleeping 60s and retrying once")
            time.sleep(60)
            resp = requests.get(url, headers=headers, params=params, timeout=8)
        resp.raise_for_status()
        _reset_circuit()
        return resp.json()
    except requests.exceptions.RequestException as e:
        _record_failure()
        raise RuntimeError(f"API error on {url}: {e}") from e


# ---------------------------------------------------------------------------
# Core API functions
# ---------------------------------------------------------------------------
def get_meme_token_list(sort_by="volume_24h_usd", sort_type="desc", limit=20) -> list[dict]:
    """GET /defi/v3/token/meme/list — meme token radar.
    Valid sort_by: volume_24h_usd, market_cap, price_change_24h_percent, liquidity.
    """
    data = _get("/defi/v3/token/meme/list", params={
        "sort_by": sort_by, "sort_type": sort_type,
        "offset": 0, "limit": limit,
    })
    return (data.get("data") or {}).get("items", []) if isinstance(data, dict) else []


def get_trending_tokens(sort_by="rank", sort_type="asc", limit=20) -> list[dict]:
    """GET /defi/token_trending — trending Solana tokens."""
    data = _get("/defi/token_trending", params={
        "sort_by": sort_by, "sort_type": sort_type,
        "offset": 0, "limit": limit,
    })
    return (data.get("data") or {}).get("tokens", []) if isinstance(data, dict) else []


def get_new_listing(limit=20) -> list[dict]:
    """GET /defi/v2/tokens/new_listing — newly listed tokens."""
    data = _get("/defi/v2/tokens/new_listing", params={"limit": limit})
    return (data.get("data") or {}).get("items", []) if isinstance(data, dict) else []


def get_token_overview(address: str) -> dict:
    """GET /defi/token_overview — detailed token data."""
    data = _get("/defi/token_overview", params={"address": address})
    return data.get("data", {}) if isinstance(data, dict) else {}


def get_token_security(address: str) -> dict:
    """GET /defi/token_security — rug detection data.
    Returns: top10HolderPercent, creatorPercentage, mutableMetadata, fakeToken, etc.
    """
    data = _get("/defi/token_security", params={"address": address})
    return data.get("data", {}) if isinstance(data, dict) else {}


def get_holder_distribution(token_address: str) -> dict:
    """GET /holder/v1/distribution — top holder wallets.
    Returns: summary.percent_of_supply (top 10), holders[] with wallet, holding, percent_of_supply.
    """
    data = _get("/holder/v1/distribution", params={"token_address": token_address})
    return data.get("data", {}) if isinstance(data, dict) else {}


def get_token_creation_info(address: str) -> dict:
    """GET /defi/token_creation_info — token age, creator wallet."""
    data = _get("/defi/token_creation_info", params={"address": address})
    return data.get("data", {}) if isinstance(data, dict) else {}


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


def _normalize_meme(t: dict) -> dict:
    """Normalize meme list v3 response."""
    return {
        "address": t.get("address") or "",
        "symbol": (t.get("symbol") or "").upper(),
        "name": t.get("name") or "",
        "price": _sf(t.get("price")),
        "volume_24h": _sf(t.get("volume_24h_usd") or t.get("volume24hUSD") or t.get("v24hUSD")),
        "price_change_24h_pct": _sf(t.get("price_change_24h_percent") or t.get("price24hChangePercent") or t.get("v24hChangePercent")),
        "market_cap": _sf(t.get("market_cap") or t.get("mc") or t.get("fdv")),
        "liquidity": _sf(t.get("liquidity")),
        "holder_count": int(_sf(t.get("holder") or t.get("holder_count"))),
    }


def _normalize_trending(t: dict) -> dict:
    """Normalize trending response."""
    return {
        "address": t.get("address") or "",
        "symbol": (t.get("symbol") or "").upper(),
        "name": t.get("name") or "",
        "price": _sf(t.get("price")),
        "volume_24h": _sf(t.get("volume24hUSD") or t.get("v24hUSD")),
        "price_change_24h_pct": _sf(t.get("price24hChangePercent") or t.get("v24hChangePercent")),
        "market_cap": _sf(t.get("marketcap") or t.get("mc")),
        "liquidity": _sf(t.get("liquidity")),
        "rank": t.get("rank"),
    }


def _normalize_new_listing(t: dict) -> dict:
    """Normalize new listing v2 response."""
    return {
        "address": t.get("address") or "",
        "symbol": (t.get("symbol") or "").upper(),
        "name": t.get("name") or "",
        "liquidity": _sf(t.get("liquidity")),
        "source": t.get("source") or "",
        "listed_at": t.get("liquidityAddedAt") or "",
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
    }


def _passes_filter(t: dict) -> bool:
    """Volume > $100K, liquidity > $50K, market cap > $100K."""
    return (
        t.get("volume_24h", 0) > 100_000
        and t.get("liquidity", 0) > 50_000
        and t.get("market_cap", 0) > 100_000
    )


def _token_age_hours_from_creation(creation_data: dict) -> float | None:
    """Calculate age from creation info."""
    ts = creation_data.get("blockUnixTime")
    if not ts:
        return None
    try:
        return (time.time() - float(ts)) / 3600
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Security analysis (rug detection)
# ---------------------------------------------------------------------------
def _analyze_security(address: str) -> tuple[float | None, list[str], dict]:
    """
    Get token security data. Returns (top10_pct, rug_flags, security_data).
    """
    rug_flags: list[str] = []
    try:
        sec = get_token_security(address)
    except Exception as e:
        return None, ["security_data_unavailable"], {}

    if not sec:
        return None, ["security_data_unavailable"], {}

    top10_pct = _sf(sec.get("top10HolderPercent")) * 100  # API returns decimal
    creator_pct = _sf(sec.get("creatorPercentage")) * 100
    mutable = sec.get("mutableMetadata")
    fake = sec.get("fakeToken")

    if top10_pct > 90:
        rug_flags.append(f"skip_high_concentration: top 10 hold {top10_pct:.0f}%")
    elif top10_pct > 80:
        rug_flags.append(f"high_concentration: top 10 hold {top10_pct:.0f}%")

    if creator_pct > 10:
        rug_flags.append(f"high_creator_holding: creator holds {creator_pct:.1f}%")

    if mutable is True:
        rug_flags.append("mutable_metadata")

    if fake is True:
        rug_flags.append("skip_fake_token")

    return top10_pct, rug_flags, sec


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------
def _build_signal(
    token: dict,
    signal_type: str,
    top10_pct: float | None,
    rug_flags: list[str],
    age_hours: float | None,
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
    parts.append(f"Volume ${vol/1e6:.1f}M" if vol >= 1e6 else f"Volume ${vol/1e3:.0f}K")
    parts.append(f"liquidity ${liq/1e3:.0f}K")
    if top10_pct is not None and top10_pct > 0:
        health = "healthy" if top10_pct < 60 else "moderate" if top10_pct < 80 else "concentrated"
        parts.append(f"Holder distribution {health} (top 10 = {top10_pct:.0f}%)")
    if holders:
        parts.append(f"{holders:,} holders")
    if age_hours is not None:
        if age_hours < 24:
            parts.append(f"Token age: {age_hours:.1f}h")
        else:
            parts.append(f"Token age: {age_hours/24:.0f}d")

    # Confidence
    confidence = "low"
    skip_rug = any("skip_" in f for f in rug_flags)
    if not skip_rug:
        if vol > 500_000 and liq > 100_000 and (top10_pct is None or top10_pct < 70):
            confidence = "medium"
        if vol > 1_000_000 and liq > 200_000 and (top10_pct is None or top10_pct < 60) and holders > 1000:
            confidence = "high"

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
        "smart_money_signal": False,
        "token_age_hours": round(age_hours, 1) if age_hours else None,
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

    # --- 1. Meme token list ---
    meme_raw = []
    meme_filtered = []
    try:
        meme_raw = get_meme_token_list(sort_by="price_change_24h_percent", sort_type="desc", limit=20)
        meme_norm = [_normalize_meme(t) for t in meme_raw]
        meme_filtered = [t for t in meme_norm if _passes_filter(t)]
        _log(f"Meme tokens: {len(meme_raw)} fetched, {len(meme_filtered)} passed filters")
    except Exception as e:
        _log(f"ERROR fetching meme tokens: {e}")

    # --- 2. Trending tokens ---
    trending_raw = []
    trending_filtered = []
    try:
        trending_raw = get_trending_tokens(limit=20)
        trending_norm = [_normalize_trending(t) for t in trending_raw]
        trending_filtered = [t for t in trending_norm if _passes_filter(t)]
        _log(f"Trending: {len(trending_raw)} fetched, {len(trending_filtered)} passed filters")
    except Exception as e:
        _log(f"ERROR fetching trending: {e}")

    # --- 3. New listings ---
    new_raw = []
    new_filtered = []
    try:
        new_raw = get_new_listing(limit=20)
        new_norm = [_normalize_new_listing(t) for t in new_raw]
        new_filtered = [t for t in new_norm if t.get("liquidity", 0) > 50_000]
        _log(f"New listings: {len(new_raw)} fetched, {len(new_filtered)} passed liquidity filter")
    except Exception as e:
        _log(f"ERROR fetching new listings: {e}")

    # --- 4. Merge candidates (deduplicate by address) ---
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

    _log(f"Total candidates after dedup: {len(candidates)}")

    # --- 5. Enrich top 3 with overview + security + creation ---
    _log(f"Enriching top {min(3, len(candidates))} with overview + security + creation info...")

    signals = []
    enriched = 0

    for token, stype in candidates:
        addr = token.get("address", "")
        top10_pct = None
        rug_flags: list[str] = []
        age_hours = None

        if enriched < 3 and addr:
            # Token overview for detailed data
            try:
                overview = get_token_overview(addr)
                if overview:
                    enriched_data = _normalize_overview(overview)
                    # Merge: keep signal_type, overlay enriched fields
                    for k, v in enriched_data.items():
                        if v and (k not in token or not token[k]):
                            token[k] = v
                    # Override price/volume/etc from overview (fresher)
                    if enriched_data.get("price"):
                        token["price"] = enriched_data["price"]
                    if enriched_data.get("volume_24h"):
                        token["volume_24h"] = enriched_data["volume_24h"]
                    if enriched_data.get("market_cap"):
                        token["market_cap"] = enriched_data["market_cap"]
                    if enriched_data.get("holder_count"):
                        token["holder_count"] = enriched_data["holder_count"]
                    if enriched_data.get("price_change_1h_pct"):
                        token["price_change_1h_pct"] = enriched_data["price_change_1h_pct"]
                    if enriched_data.get("price_change_24h_pct"):
                        token["price_change_24h_pct"] = enriched_data["price_change_24h_pct"]
            except Exception as e:
                _log(f"  Overview error for {token.get('symbol', '?')}: {e}")

            # Token security (rug detection)
            try:
                top10_pct, rug_flags, _sec = _analyze_security(addr)
            except Exception as e:
                rug_flags = ["security_data_error"]
                _log(f"  Security error for {token.get('symbol', '?')}: {e}")

            # Creation info (token age)
            try:
                creation = get_token_creation_info(addr)
                if creation:
                    age_hours = _token_age_hours_from_creation(creation)
            except Exception as e:
                _log(f"  Creation info error for {token.get('symbol', '?')}: {e}")

            enriched += 1
        else:
            rug_flags = ["not_enriched"]

        # Skip fake or extremely concentrated tokens
        if any("skip_" in f for f in rug_flags):
            sym = token.get("symbol", "?")
            _log(f"  SKIPPED {sym}: {', '.join(f for f in rug_flags if 'skip_' in f)}")
            continue

        signals.append(_build_signal(token, stype, top10_pct, rug_flags, age_hours))

    # --- 6. Log results ---
    _log(f"Signals: {len(signals)} found")
    for i, s in enumerate(signals, 1):
        sym = s["token"]
        stype = s["signal_type"].lower().replace("_", " ")
        pct = s.get("price_change_24h_pct") or 0
        vol = s.get("volume_24h") or 0
        liq = s.get("liquidity_usd") or 0
        holders = s.get("holder_count") or 0
        top10 = s.get("top10_holder_pct")
        age = s.get("token_age_hours")

        vol_str = f"vol ${vol/1e6:.1f}M" if vol >= 1e6 else f"vol ${vol/1e3:.0f}K"
        liq_str = f"liq ${liq/1e3:.0f}K"
        h_str = f"holders {holders/1e3:.1f}K" if holders >= 1000 else f"holders {holders}"

        if top10 is not None and top10 > 0:
            flag = " ⛔ FLAGGED" if top10 > 80 else " ⚠️" if top10 > 60 else " ✅"
            t10_str = f"top10={top10:.0f}%{flag}"
        else:
            t10_str = "top10=N/A"

        age_str = ""
        if age is not None:
            age_str = f", age {age:.0f}h" if age < 24 else f", age {age/24:.0f}d"

        pct_str = f"{'+' if pct > 0 else ''}{pct:.0f}%"
        flags = s.get("rug_flags", [])
        flag_str = ""
        if any("high_concentration" in f for f in flags):
            flag_str = " ⛔ FLAGGED"
        elif any("high_creator" in f or "mutable" in f for f in flags):
            flag_str = " ⚠️"

        _log(f"  {i}. {sym} — {stype} {pct_str}, {vol_str}, {liq_str}, {h_str}, {t10_str}{age_str}{flag_str}")

    # --- 7. Save ---
    raw_output = {
        "timestamp": now.isoformat(),
        "tier": "lite",
        "meme_tokens_raw": meme_raw,
        "trending_raw": trending_raw,
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
