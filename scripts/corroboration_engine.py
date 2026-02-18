#!/usr/bin/env python3
"""
Cross-Source Corroboration Engine — v3.0

Maintains a rolling window of recent signals and enriches new signals
with cross-source corroboration data BEFORE Sanad verification.

This is the #1 mechanism for pushing signals from Ahad (single-source, ~60 trust)
to Mashhur/Tawatur (multi-source, 70+ trust) without lowering any thresholds.
"""
import fcntl
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
STATE_DIR = BASE_DIR / "state"
WINDOW_PATH = STATE_DIR / "signal_window.json"
LOCK_PATH = STATE_DIR / "signal_window.lock"

# Rolling window: signals older than this are pruned
WINDOW_MINUTES = 60

# Source independence mapping — multiple sub-sources from same provider = 1 source
SOURCE_PROVIDERS = {
    "coingecko": "coingecko",
    "coingecko_trending": "coingecko",
    "coingecko_gainers": "coingecko",
    "birdeye": "birdeye",
    "birdeye_meme_list": "birdeye",
    "birdeye_trending": "birdeye",
    "birdeye_new_listing": "birdeye",
    "birdeye_smart_money": "smart_money",
    "smart_money": "smart_money",
    "dexscreener": "dexscreener",
    "dexscreener_boost": "dexscreener",
    "dexscreener_cto": "dexscreener",
    "telegram": "telegram",
    "telegram_sniffer": "telegram",
    "sentiment": "sentiment",
    "perplexity": "sentiment",
    "social_sentiment": "sentiment",
    "onchain": "onchain",
    "onchain_analytics": "onchain",
    "whale_alert": "onchain",
    "glassnode": "onchain",
    "solscan": "solscan",
    "solscan_onchain": "solscan",
}

# Corroboration levels (maps to Sanad trust score bonus)
# Ahad (1 source) = 10 corroboration points
# Mashhur (2 sources) = 18 points
# Tawatur (3+ sources) = 25 points
# Tawatur Qawiy (4+ sources) = 30 points (maximum trust)
CORROBORATION_LEVELS = {
    1: "AHAD",
    2: "MASHHUR",
    3: "TAWATUR",
}
# 4+ = TAWATUR_QAWIY (strongest corroboration)


def _load_window():
    try:
        with open(WINDOW_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"signals": [], "updated_at": None}


def _save_window(window):
    """Atomic write: temp file + os.replace to prevent corruption on concurrent access."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WINDOW_PATH.with_suffix(f".tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        json.dump(window, f, indent=2, default=str)
    os.replace(tmp, WINDOW_PATH)


def _normalize_provider(source_str: str) -> str:
    """Map a source string to its independent provider."""
    source_lower = source_str.lower().strip()
    # Direct match
    if source_lower in SOURCE_PROVIDERS:
        return SOURCE_PROVIDERS[source_lower]
    # Substring match
    for key, provider in SOURCE_PROVIDERS.items():
        if key in source_lower:
            return provider
    return source_lower  # Unknown source = treat as independent


def _normalize_token(token: str) -> str:
    """Normalize token symbol for matching."""
    return token.upper().strip().replace("$", "")


def _prune_window(window: dict, now: datetime) -> dict:
    """Remove signals older than WINDOW_MINUTES."""
    cutoff = (now - timedelta(minutes=WINDOW_MINUTES)).isoformat()
    window["signals"] = [
        s for s in window.get("signals", [])
        if s.get("timestamp", "") >= cutoff
    ]
    return window


def _build_result(providers_seen: set, source_labels: list) -> dict:
    """Build corroboration result with quality assessment."""
    count = len(providers_seen)

    if count >= 4:
        level = "TAWATUR_QAWIY"  # 4+ sources = maximum trust
    elif count == 3:
        level = "TAWATUR"
    elif count == 2:
        level = "MASHHUR"
    else:
        level = "AHAD"

    # Quality: STRONG requires at least one non-hype evidence source
    # Hype sources = trending/boost lists (same hype everywhere ≠ real corroboration)
    # Evidence sources = on-chain analytics, sentiment analysis, telegram sniffer, solscan, smart money
    HYPE_SOURCES = {"coingecko", "birdeye", "dexscreener"}
    EVIDENCE_SOURCES = {"onchain", "sentiment", "telegram", "solscan", "smart_money"}
    has_evidence = bool(providers_seen & EVIDENCE_SOURCES)
    all_hype = providers_seen and providers_seen.issubset(HYPE_SOURCES)

    if count >= 2 and all_hype and not has_evidence:
        quality = "WEAK"  # Same hype appearing everywhere — no independent evidence
    else:
        quality = "STRONG"

    return {
        "cross_source_count": count,
        "cross_sources": sorted(source_labels),
        "corroboration_level": level,
        "corroboration_quality": quality,
    }


def register_signal(signal: dict) -> dict:
    """
    Register a new signal in the rolling window and return corroboration data.
    File-locked to prevent lost updates from concurrent router runs.

    Args:
        signal: dict with at least 'token' and 'source' keys

    Returns:
        dict with:
            cross_source_count: int (number of independent sources)
            cross_sources: list[str] (provider names)
            corroboration_level: str (AHAD/MASHHUR/TAWATUR)
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        try:
            return _register_signal_locked(signal)
        finally:
            fcntl.flock(lockfile, fcntl.LOCK_UN)


def _register_signal_locked(signal: dict) -> dict:
    now = datetime.now(timezone.utc)
    window = _load_window()
    window = _prune_window(window, now)

    token = _normalize_token(signal.get("token", ""))
    source = signal.get("source", signal.get("_origin", "unknown"))
    provider = _normalize_provider(source)
    address = signal.get("token_address", signal.get("address", ""))

    if not token:
        return {"cross_source_count": 0, "cross_sources": [], "corroboration_level": "AHAD_DAIF"}

    # Add this signal to the window
    entry = {
        "token": token,
        "provider": provider,
        "source": source,
        "address": address,
        "chain": signal.get("chain", ""),
        "timestamp": now.isoformat(),
    }
    window["signals"].append(entry)
    window["updated_at"] = now.isoformat()
    _save_window(window)

    # Find all independent providers for this token in the window
    # Primary key: (chain, address) when available; fallback: symbol
    chain = signal.get("chain", "")
    providers_seen = set()
    source_labels = []
    for s in window["signals"]:
        match = False
        # Primary match: contract address (same chain)
        s_addr = s.get("address", "")
        s_chain = s.get("chain", "")
        if address and s_addr and address == s_addr:
            if not chain or not s_chain or chain == s_chain:
                match = True
        # Fallback: symbol match (only if neither has an address, or addresses match)
        if not match and _normalize_token(s.get("token", "")) == token:
            # Avoid false match: if both have addresses but they differ, skip
            if address and s_addr and address != s_addr:
                match = False
            else:
                match = True

        if match:
            p = s["provider"]
            if p not in providers_seen:
                providers_seen.add(p)
                source_labels.append(p)

    return _build_result(providers_seen, source_labels)


def get_corroboration(token: str, address: str = "", chain: str = "") -> dict:
    """
    Check corroboration for a token WITHOUT registering a new signal.
    File-locked for consistency with concurrent register_signal calls.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_SH)  # Shared lock for reads
        try:
            return _get_corroboration_locked(token, address, chain)
        finally:
            fcntl.flock(lockfile, fcntl.LOCK_UN)


def _get_corroboration_locked(token: str, address: str = "", chain: str = "") -> dict:
    now = datetime.now(timezone.utc)
    window = _load_window()
    window = _prune_window(window, now)

    token_norm = _normalize_token(token)
    providers_seen = set()
    source_labels = []

    for s in window["signals"]:
        match = False
        s_addr = s.get("address", "")
        s_chain = s.get("chain", "")
        if address and s_addr and address == s_addr:
            if not chain or not s_chain or chain == s_chain:
                match = True
        if not match and _normalize_token(s.get("token", "")) == token_norm:
            if address and s_addr and address != s_addr:
                match = False
            else:
                match = True
        if match:
            p = s["provider"]
            if p not in providers_seen:
                providers_seen.add(p)
                source_labels.append(p)

    return _build_result(providers_seen, source_labels)


def get_window_stats() -> dict:
    """Return current window statistics."""
    now = datetime.now(timezone.utc)
    window = _load_window()
    window = _prune_window(window, now)

    tokens = {}
    for s in window["signals"]:
        tok = s.get("token", "?")
        if tok not in tokens:
            tokens[tok] = set()
        tokens[tok].add(s["provider"])

    multi_source = {t: sorted(p) for t, p in tokens.items() if len(p) >= 2}

    return {
        "total_signals": len(window["signals"]),
        "unique_tokens": len(tokens),
        "multi_source_tokens": multi_source,
        "window_minutes": WINDOW_MINUTES,
    }


if __name__ == "__main__":
    stats = get_window_stats()
    print(json.dumps(stats, indent=2))
