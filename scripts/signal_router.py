#!/usr/bin/env python3
"""
Signal Router — Sprint 3.x
Reads CoinGecko + DexScreener + Birdeye signals, ranks them, feeds the best
candidate into sanad_pipeline.py. Deterministic Python. No LLMs.

# Ignore SIGPIPE to prevent broken pipe crashes in cron/subprocess contexts
import signal
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
Designed to run as a cron job every 15 minutes.
"""

import hashlib
import json
import os
# import subprocess  # v3.1: removed, no longer using subprocess for pipeline
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Signal normalizer (Sprint 11.1.5) — canonical schema conversion
try:
    from signal_normalizer import normalize_signal
    HAS_NORMALIZER = True
except ImportError:
    HAS_NORMALIZER = False

# Job lease system for deterministic liveness tracking
try:
    from job_lease import acquire, release
    HAS_LEASE = True
except ImportError:
    HAS_LEASE = False

# v3.1 Hot Path imports
try:
    import fast_decision_engine
    import state_store
    import ids
    HAS_V31_HOT_PATH = True
except ImportError:
    HAS_V31_HOT_PATH = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
SIGNALS_CG = BASE_DIR / "signals" / "coingecko"
SIGNALS_DEX = BASE_DIR / "signals" / "dexscreener"
SIGNALS_BE = BASE_DIR / "signals" / "birdeye"
SIGNALS_OC = BASE_DIR / "signals" / "onchain"
FEAR_GREED_PATH = BASE_DIR / "signals" / "market" / "fear_greed_latest.json"
STATE_DIR = BASE_DIR / "state"
POSITIONS_PATH = STATE_DIR / "positions.json"
PORTFOLIO_PATH = STATE_DIR / "portfolio.json"
TRADE_HISTORY_PATH = STATE_DIR / "trade_history.json"
ROUTER_STATE_PATH = STATE_DIR / "signal_router_state.json"
CRON_HEALTH_PATH = STATE_DIR / "cron_health.json"
# PIPELINE_SCRIPT = SCRIPT_DIR / "sanad_pipeline.py"  # v3.1: removed, using fast_decision_engine

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# ── Load from config (Al-Muhasbi audit: hardcoded values were ignoring thresholds.yaml) ──
import yaml as _yaml
_THRESHOLDS_PATH = SCRIPT_DIR.parent / "config" / "thresholds.yaml"
try:
    with open(_THRESHOLDS_PATH) as _f:
        _cfg = _yaml.safe_load(_f)
    MAX_POSITIONS = _cfg.get("risk", {}).get("max_positions", 5)
    MAX_DAILY_RUNS = _cfg.get("budget", {}).get("daily_pipeline_runs", 50)
    COOLDOWN_HOURS = _cfg.get("policy_gates", {}).get("cooldown_minutes", 30) / 60  # now 30min default
    # Paper mode overrides
    try:
        with open(Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent)) / "state" / "portfolio.json") as _pf:
            _is_paper = json.load(_pf).get("mode", "paper") == "paper"
    except Exception:
        _is_paper = True
    if _is_paper:
        MAX_DAILY_RUNS = max(MAX_DAILY_RUNS, 200)  # Paper: 200 runs/day
        MAX_POSITIONS = max(MAX_POSITIONS, 10)  # Paper: 10 concurrent positions
except Exception:
    MAX_POSITIONS = 5
    MAX_DAILY_RUNS = 50
    COOLDOWN_HOURS = 0.5

STALE_THRESHOLD_MIN = 30
CROSS_SOURCE_BONUS = 25


_LOG_FILE = BASE_DIR / "logs" / "signal_router.log"


def _log(msg: str):
    line = f"[SIGNAL ROUTER] {msg}"
    print(line)
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _load_skip_list():
    """Load skip_tokens.json - tokens temporarily blocked due to issues."""
    skip_file = STATE_DIR / "skip_tokens.json"
    if not skip_file.exists():
        return []
    try:
        data = json.loads(skip_file.read_text())
        skip_list = data.get("skip_list", [])
        
        # Filter expired entries
        now = datetime.now(timezone.utc)
        active = []
        for entry in skip_list:
            expires_str = entry.get("expires_at")
            if expires_str:
                try:
                    expires = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                    if now < expires:
                        active.append(entry)
                except:
                    pass
        
        return active
    except Exception as e:
        _log(f"Skip list load error: {e}")
        return []


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# State I/O (atomic writes)
# ---------------------------------------------------------------------------
def _load_json(path: Path, default=None):
    if not path.exists():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}


def _save_json_atomic(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.rename(path)


def _append_to_jsonl(filepath: Path, record: dict):
    """Append JSON record to .jsonl file for observability."""
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        _log(f"JSONL append error: {e}")


# ---------------------------------------------------------------------------
# Load latest signal files
# ---------------------------------------------------------------------------
def _latest_signal_file(directory: Path, exclude_names: set[str] | None = None) -> tuple[Path | None, list[dict], float]:
    """Return (path, signals_list, age_minutes) for the most recent file."""
    if not directory.exists():
        return None, [], 999
    exclude = exclude_names or set()
    files = sorted(
        [f for f in directory.glob("*.json") if f.name not in exclude],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return None, [], 999
    latest = files[0]
    age_min = (time.time() - latest.stat().st_mtime) / 60
    if age_min > STALE_THRESHOLD_MIN:
        return latest, [], age_min
    data = _load_json(latest, {})
    signals = data.get("signals", [])
    return latest, signals, age_min


# ---------------------------------------------------------------------------
# Load system state
# ---------------------------------------------------------------------------
def _load_open_tokens() -> set[str]:
    """Load open tokens from SQLite (v3.1 source of truth)."""
    try:
        with state_store.get_connection() as conn:
            rows = conn.execute("SELECT token_address FROM positions WHERE status='OPEN'").fetchall()
            return {row["token_address"].upper() for row in rows}
    except state_store.DBBusyError:
        _log("DB busy loading open tokens - fail-closed (skip trading this cycle)")
        raise  # Re-raise to abort trading cycle
    except Exception as e:
        _log(f"Error loading open tokens from DB: {e}")
        return set()


def _load_cooldown_tokens() -> dict[str, float]:
    """Return {TOKEN: remaining_minutes} for tokens traded within cooldown period from SQLite."""
    try:
        with state_store.get_connection() as conn:
            # Query closed positions
            rows = conn.execute("""
                SELECT token_address, closed_at 
                FROM positions 
                WHERE status='CLOSED' AND closed_at IS NOT NULL
            """).fetchall()
            
            now = _now()
            cooldowns: dict[str, float] = {}
            
            for row in rows:
                token = row["token_address"].upper()
                closed_at_str = row["closed_at"]
                try:
                    closed_at = datetime.fromisoformat(closed_at_str.replace("Z", "+00:00"))
                    elapsed = (now - closed_at).total_seconds() / 60
                    remaining = COOLDOWN_HOURS * 60 - elapsed
                    
                    if remaining > 0:
                        cooldowns[token] = max(cooldowns.get(token, 0), remaining)
                except Exception:
                    continue
            
            return cooldowns
    except state_store.DBBusyError:
        _log("DB busy loading cooldowns - fail-closed (skip trading this cycle)")
        raise  # Re-raise to abort trading cycle
    except Exception as e:
        _log(f"Error loading cooldowns from DB: {e}")
        return {}


def _is_daily_loss_hit() -> bool:
    pf = _load_json(PORTFOLIO_PATH, {})
    # If daily PnL is worse than -5%, stop
    daily_pnl = pf.get("daily_pnl_pct", 0)
    return daily_pnl <= -5.0


def _load_router_state() -> dict:
    return _load_json(ROUTER_STATE_PATH, {
        "last_run": None,
        "processed_hashes": [],
        "daily_pipeline_runs": 0,
        "daily_reset_date": None,
    })


def _signal_hash(signal: dict) -> str:
    key = f"{signal.get('token', '')}|{signal.get('signal_type', '')}|{signal.get('source', '')}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _score_signal(signal: dict, age_minutes: float, is_cross_source: bool) -> int:
    """
    Score signals like an experienced trader, not a hype chaser.

    Priority hierarchy:
    1. CEX-listed (can actually execute and exit)        +40
    2. Volume & liquidity (can size a real position)     +30 max
    3. Cross-source confirmation (Tawatur/Mashhur)       +30
    4. Healthy fundamentals (holders, age, distribution) +25 max
    5. Momentum (catalyst-driven, not pump-driven)       +15 max
    6. Penalties: brand new, low liquidity, rug flags    -50 max
    """
    score = 0
    stype = signal.get("signal_type", "")
    source = signal.get("source", "")
    token = signal.get("token", "").upper()

    # ── 1. CEX LISTING BONUS (biggest factor) ──
    # Tokens on Binance/MEXC can actually be traded with real order books
    CEX_LISTED = {
        "BONK", "WIF", "PEPE", "FLOKI", "RAY", "ORCA", "SOL", "JUP",
        "DOGE", "SHIB", "PENGU", "TAO", "SUI", "VIRTUAL", "BTC", "ETH",
        "AAVE", "UNI", "LINK", "ATOM", "HBAR", "XRP", "INIT", "ONDO",
        "MOVE", "LDO", "RPL", "FOGO",
    }
    if token in CEX_LISTED:
        score += 40  # massive bonus — tradeable on real exchange
    else:
        score -= 10  # penalty — DEX only, execution risk

    # ── 2. VOLUME & LIQUIDITY (can we size a position?) ──
    vol = signal.get("volume_24h") or 0
    if vol > 10_000_000:
        score += 30  # deep market
    elif vol > 5_000_000:
        score += 25
    elif vol > 1_000_000:
        score += 20
    elif vol > 500_000:
        score += 10
    elif vol > 100_000:
        score += 5
    else:
        score -= 10  # can't exit this

    liq = signal.get("liquidity_usd") or 0
    if liq > 500_000:
        score += 10
    elif liq > 200_000:
        score += 5

    # ── 3. CROSS-SOURCE CONFIRMATION ──
    if is_cross_source:
        score += 30  # Tawatur = strongest conviction

    # ── 4. FUNDAMENTALS ──
    # Token age — maturity = safety
    age_hours = signal.get("token_age_hours")
    if age_hours is not None:
        if age_hours < 1:
            score -= 30  # brand new = almost certainly a rug or pump
        elif age_hours < 6:
            score -= 15  # too young, no track record
        elif age_hours < 24:
            score -= 5   # still risky
        elif age_hours > 168:  # >1 week
            score += 10  # survived — real project
        elif age_hours > 720:  # >30 days
            score += 15  # established

    # Holder distribution
    top10 = signal.get("top10_holder_pct")
    if top10 is not None and top10 > 0:
        if top10 < 25:
            score += 10  # healthy
        elif top10 < 40:
            score += 5
        elif top10 > 70:
            score -= 25  # whale-controlled
        elif top10 > 50:
            score -= 10

    # Holder count
    holder_count = signal.get("holder_count") or 0
    if holder_count > 5000:
        score += 10
    elif holder_count > 1000:
        score += 5
    elif holder_count < 100 and holder_count > 0:
        score -= 15  # ghost town

    # Rug flags penalty
    rug_flags = signal.get("rug_flags") or []
    if rug_flags and not all("not_checked" in f or "not_enriched" in f for f in rug_flags):
        score -= 25

    # Smart money signal
    if signal.get("smart_money_signal"):
        score += 20

    # ── 5. MOMENTUM (measured, not insane) ──
    pct_1h = signal.get("price_change_1h_pct") or 0
    pct_24h = signal.get("price_change_24h_pct") or 0
    momentum = pct_1h if pct_1h else pct_24h / 4

    # Good momentum: 5-50%. Over 100% = pump territory
    if 5 <= momentum <= 15:
        score += 15  # healthy momentum
    elif 15 < momentum <= 50:
        score += 10  # strong but plausible
    elif 50 < momentum <= 100:
        score += 5   # be cautious
    elif momentum > 100:
        score -= 10  # pump — will dump
    elif momentum > 1000:
        score -= 25  # obvious scam pump

    # Buy/sell ratio
    bsr = signal.get("buy_sell_ratio") or 0
    if bsr > 2.0:
        score += 10
    elif bsr > 1.5:
        score += 5

    # ── 6. SOURCE TYPE (signal quality) ──
    if source == "birdeye_meme_radar" and stype == "MEME_GAINER":
        score += 10
    elif source == "birdeye_meme_radar" and stype == "TRENDING":
        score += 10
    elif stype == "BOOSTED_TOKEN":
        score += 5   # paid boosts = questionable
    elif stype in ("TRENDING_GAINER", "MAJOR_GAINER"):
        score += 10
    elif stype == "COMMUNITY_TAKEOVER":
        score += 5
    elif stype == "NEW_LISTING":
        score += 0   # neutral — too new

    # Signal recency
    if age_minutes < 10:
        score += 5
    elif age_minutes < 20:
        score += 3

    # ── MARKET REGIME AWARENESS ──
    # In extreme fear, established tokens are bargains
    # (Fear/greed bonus applied externally by router)

    # ── SENTIMENT OVERLAY (Tier 1 wiring) ──
    # Read latest sentiment for this token if available
    try:
        sentiment_dir = BASE_DIR / "signals" / "sentiment"
        if sentiment_dir.exists():
            sent_files = sorted(sentiment_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            for sf in sent_files[:5]:  # Check last 5 sentiment files
                sent_data = _load_json(sf, {})
                sent_signals = sent_data.get("signals", [sent_data]) if "signals" in sent_data else [sent_data]
                for ss in sent_signals:
                    if ss.get("token", "").upper() == token:
                        sent_score = ss.get("sentiment_score", 50)
                        sent_trend = ss.get("trend", "stable")
                        if sent_score >= 75 and sent_trend == "rising":
                            score += 20  # Strong bullish sentiment + rising
                        elif sent_score >= 60 and sent_trend == "rising":
                            score += 10  # Moderate bullish + rising
                        elif sent_score <= 25:
                            score -= 15  # Extreme fear sentiment
                        elif sent_score <= 40 and sent_trend == "falling":
                            score -= 10  # Bearish + falling
                        raise StopIteration  # Found match, stop searching
    except StopIteration:
        pass
    except Exception:
        pass  # Sentiment unavailable — no penalty

    # ── UCB1 SOURCE WEIGHTING (from SQLite — single source of truth) ──
    # Sources with better track records get a scoring bonus
    try:
        from state_store import get_source_ucb_stats
        source_key = signal.get("source", "unknown")
        ucb_stats = get_source_ucb_stats()
        src = ucb_stats.get(source_key)
        if src and src["n"] > 0:
            win_rate = src["reward_sum"] / src["n"]
            ucb1_score = win_rate * 100
            if ucb1_score >= 80:
                score += 15  # Grade A source — proven winner
            elif ucb1_score >= 60:
                score += 5   # Grade B — reliable
            elif ucb1_score < 30:
                score -= 15  # Grade D/F — historically bad
    except Exception:
        pass  # UCB1 unavailable — no adjustment

    return max(score, 0)  # floor at 0


# ---------------------------------------------------------------------------
# Convert signal to pipeline format
# ---------------------------------------------------------------------------
def _to_pipeline_signal(signal: dict, cross_sources: list[str] | None = None) -> dict:
    # Normalize to canonical schema first (Sprint 11.1.5)
    if HAS_NORMALIZER:
        normalized = normalize_signal(signal)
        if normalized:
            # Merge normalized fields into signal (don't lose router-specific fields)
            for k, v in normalized.items():
                if v and not signal.get(k):
                    signal[k] = v
    token = signal.get("token", "UNKNOWN")
    chain = signal.get("chain", "")
    address = signal.get("token_address", "")
    stype = signal.get("signal_type", "")
    source_parts = []

    # Build human-readable source
    if "dexscreener" in signal.get("source", ""):
        boost = signal.get("boost_amount")
        if boost:
            source_parts.append(f"DexScreener boost ({boost}x)")
        elif stype == "COMMUNITY_TAKEOVER":
            source_parts.append("DexScreener community takeover")
        else:
            source_parts.append("DexScreener")

    if "coingecko" in signal.get("source", ""):
        rank = signal.get("trending_rank")
        if rank is not None:
            source_parts.append(f"CoinGecko trending #{rank + 1}")
        else:
            source_parts.append("CoinGecko top gainer")

    if "birdeye" in signal.get("source", ""):
        be_type = signal.get("signal_type", "").lower().replace("_", " ")
        top10 = signal.get("top10_holder_pct")
        if top10 is not None and top10 > 0:
            source_parts.append(f"Birdeye {be_type} (top10={top10:.0f}%)")
        else:
            source_parts.append(f"Birdeye {be_type}")

    if cross_sources:
        for cs in cross_sources:
            if cs not in " ".join(source_parts).lower():
                source_parts.append(cs)

    source_str = " + ".join(source_parts) if source_parts else signal.get("source", "unknown")

    # Determine venue
    if chain == "solana" and address:
        venue = "DEX"
        exchange = "raydium"
    else:
        venue = "CEX"
        exchange = "binance"

    result = {
        "token": token,
        "source": source_str,
        "thesis": signal.get("thesis", ""),
        "venue": venue,
        "exchange": exchange,
    }
    if chain:
        result["chain"] = chain
    if address:
        result["token_address"] = address

    return result


# ---------------------------------------------------------------------------
# Cron Health Update
# ---------------------------------------------------------------------------
def _update_cron_health(status: str = "ok"):
    """Update cron health timestamp for signal_router."""
    health = {}
    if CRON_HEALTH_PATH.exists():
        try:
            with open(CRON_HEALTH_PATH) as f:
                health = json.load(f)
        except Exception:
            pass
    
    health["signal_router"] = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "status": status
    }
    
    CRON_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CRON_HEALTH_PATH, "w") as f:
        json.dump(health, f, indent=2)


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------
def run_router():
    # Acquire lease for liveness tracking
    if HAS_LEASE:
        acquire("signal_router", ttl_seconds=720)  # 12 min (10 min timeout + 2 min grace)
    
    _update_cron_health("running")  # Mark as running at start
    
    error_occurred = False
    try:
        _run_router_impl()
    except Exception as e:
        error_occurred = True
        if HAS_LEASE:
            release("signal_router", "error", str(e))
        _update_cron_health("error")
        raise  # Re-raise for outer handler
    finally:
        # Always release lease and update cron_health
        if not error_occurred:
            if HAS_LEASE:
                release("signal_router", "ok")
            _update_cron_health("ok")

def _run_router_impl():
    now = _now()
    now_str = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _log(now_str)

    # --- Load router state ---
    state = _load_router_state()

    # Reset daily counters at midnight UTC
    today = now.strftime("%Y-%m-%d")
    if state.get("daily_reset_date") != today:
        state["daily_pipeline_runs"] = 0
        state["daily_reset_date"] = today
        state["processed_hashes"] = []  # also reset processed hashes daily

    # --- Data quality gate (Tier 2 wiring) ---
    try:
        from market_data_quality import run_all_checks
        dq = run_all_checks()
        dq_status = dq.get("status", "OK")
        if dq_status == "BLOCK":
            _log(f"Data quality BLOCK — skipping this cycle. Issues: {dq.get('checks', [])}")
            state["last_run"] = now_str
            state["data_quality_block"] = True
            _save_json_atomic(ROUTER_STATE_PATH, state)
            return
        elif dq_status == "WARN":
            _log(f"Data quality WARN — proceeding with caution")
        state["data_quality_block"] = False
    except Exception as e:
        _log(f"Data quality check failed (proceeding anyway): {e}")

    # --- Budget check ---
    if state["daily_pipeline_runs"] >= MAX_DAILY_RUNS:
        _log(f"Daily pipeline budget exhausted ({state['daily_pipeline_runs']}/{MAX_DAILY_RUNS} runs). Skipping.")
        state["last_run"] = now_str
        _save_json_atomic(ROUTER_STATE_PATH, state)
        return

    # --- Load signals ---
    cg_path, cg_signals, cg_age = _latest_signal_file(SIGNALS_CG, exclude_names={"global_latest.json"})
    dex_path, dex_signals, dex_age = _latest_signal_file(SIGNALS_DEX)
    be_path, be_signals, be_age = _latest_signal_file(SIGNALS_BE)
    oc_path, oc_signals, oc_age = _latest_signal_file(SIGNALS_OC)

    def _label(name, sigs, age, path):
        if sigs:
            return f"{name} {len(sigs)} ({age:.0f}min ago)"
        elif path:
            return f"{name} 0 ({age:.0f}min ago, stale)"
        return f"{name} no files"

    _log(f"Loading signals: {_label('CoinGecko', cg_signals, cg_age, cg_path)}, "
         f"{_label('DexScreener', dex_signals, dex_age, dex_path)}, "
         f"{_label('Birdeye', be_signals, be_age, be_path)}, "
         f"{_label('OnChain', oc_signals, oc_age, oc_path)}")

    all_signals = []
    for s in cg_signals:
        s["_source_age_min"] = cg_age
        s["_origin"] = "coingecko"
        all_signals.append(s)
    for s in dex_signals:
        s["_source_age_min"] = dex_age
        s["_origin"] = "dexscreener"
        all_signals.append(s)
    for s in be_signals:
        s["_source_age_min"] = be_age
        s["_origin"] = "birdeye"
        all_signals.append(s)
    for s in oc_signals:
        s["_source_age_min"] = oc_age
        s["_origin"] = "onchain"
        all_signals.append(s)

    # ── Source 5: Binance New Listings ──
    try:
        from binance_new_listings import check_new_listings
        new_listings = check_new_listings()
        if new_listings:
            for listing in new_listings:
                listing["source"] = "binance_new_listing"
                all_signals.append(listing)
    except Exception as e:
        print(f"  Binance new listings error: {e}")

    # ── Source 6: Pump.fun Migrations ──
    try:
        pf_dir = BASE_DIR / "signals" / "pumpfun"
        if pf_dir.exists():
            pf_files = sorted(pf_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for pf_file in pf_files[:10]:  # Last 10 signal files
                age_min = (datetime.now(timezone.utc) - datetime.fromtimestamp(pf_file.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60
                if age_min < 30:  # Only signals from last 30 minutes
                    pf_signal = json.loads(pf_file.read_text())
                    pf_signal["_source_age_min"] = age_min
                    pf_signal["_origin"] = "pumpfun"
                    if "source" not in pf_signal:
                        pf_signal["source"] = "pumpfun_monitor"
                    all_signals.append(pf_signal)
            pf_count = sum(1 for s in all_signals if s.get("_origin") == "pumpfun")
            if pf_count:
                _log(f"Pump.fun: {pf_count} migration signals loaded")
    except Exception as e:
        _log(f"Pump.fun signal load error: {e}")

    # ── Source 7: Telegram Sniffer ──
    try:
        tg_dir = BASE_DIR / "signals" / "telegram"
        if tg_dir.exists():
            tg_files = sorted(tg_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for tg_file in tg_files[:15]:
                age_min = (datetime.now(timezone.utc) - datetime.fromtimestamp(tg_file.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60
                if age_min < 60:  # Signals from last hour
                    tg_signal = json.loads(tg_file.read_text())
                    tg_signal["_source_age_min"] = age_min
                    tg_signal["_origin"] = "telegram_sniffer"
                    if "source" not in tg_signal:
                        tg_signal["source"] = "telegram_sniffer"
                    all_signals.append(tg_signal)
            tg_count = sum(1 for s in all_signals if s.get("_origin") == "telegram_sniffer")
            if tg_count:
                _log(f"Telegram sniffer: {tg_count} signals loaded")
    except Exception as e:
        _log(f"Telegram sniffer signal load error: {e}")

    # ── Source 7: Telegram Sniffer ──
    try:
        tg_dir = BASE_DIR / "signals" / "telegram"
        if tg_dir.exists():
            tg_files = sorted(tg_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for tg_file in tg_files[:15]:
                age_min = (datetime.now(timezone.utc) - datetime.fromtimestamp(tg_file.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60
                if age_min < 30:
                    tg_signal = json.loads(tg_file.read_text())
                    tg_signal["_source_age_min"] = age_min
                    tg_signal["_origin"] = "telegram_sniffer"
                    if "source" not in tg_signal:
                        tg_signal["source"] = "telegram_sniffer"
                    all_signals.append(tg_signal)
            tg_count = sum(1 for s in all_signals if s.get("_origin") == "telegram_sniffer")
            if tg_count:
                _log(f"Telegram sniffer: {tg_count} signals loaded")
    except Exception as e:
        _log(f"Telegram sniffer signal load error: {e}")

    if not all_signals:
        _log("No actionable signals — no recent data from either source.")
        state["last_run"] = now_str
        _save_json_atomic(ROUTER_STATE_PATH, state)
        return

    # --- Load rejection cooldown state (P0-3: deduplication) ---
    rejection_cooldown_path = STATE_DIR / "rejection_cooldown.json"
    rejection_cooldown = {}
    if rejection_cooldown_path.exists():
        try:
            rejection_cooldown = json.loads(rejection_cooldown_path.read_text())
        except:
            pass
    
    # Filter out recently rejected tokens (6h cooldown)
    cooldown_hours = 6
    cutoff_time = (now - timedelta(hours=cooldown_hours)).isoformat()
    pre_cooldown_count = len(all_signals)
    
    filtered_signals = []
    for sig in all_signals:
        token = sig.get("token", "").upper()
        source = sig.get("source", sig.get("_origin", "unknown"))
        cooldown_key = f"{token}:{source}"
        
        last_reject_time = rejection_cooldown.get(cooldown_key)
        if last_reject_time and last_reject_time > cutoff_time:
            _log(f"  Cooldown: skipping {token} (rejected {last_reject_time[:16]})")
            continue
        
        filtered_signals.append(sig)
    
    all_signals = filtered_signals
    if pre_cooldown_count > len(all_signals):
        _log(f"Rejection cooldown: {pre_cooldown_count} → {len(all_signals)} signals ({pre_cooldown_count - len(all_signals)} filtered)")
    
    if not all_signals:
        _log("No signals after cooldown filter.")
        state["last_run"] = now_str
        _save_json_atomic(ROUTER_STATE_PATH, state)
        return

    # --- Load system state ---
    open_tokens = _load_open_tokens()
    cooldowns = _load_cooldown_tokens()
    daily_loss = _is_daily_loss_hit()
    processed_hashes = set(state.get("processed_hashes", []))
    open_count = len(open_tokens)
    available_slots = MAX_POSITIONS - open_count

    _log(f"Open positions: {open_count} ({', '.join(sorted(open_tokens)) or 'none'}). Available slots: {available_slots}")

    if daily_loss:
        _log("Daily loss limit hit — skipping ALL signals.")
        state["last_run"] = now_str
        _save_json_atomic(ROUTER_STATE_PATH, state)
        return

    if available_slots <= 0:
        _log(f"Max positions reached ({MAX_POSITIONS}). Skipping ALL signals.")
        state["last_run"] = now_str
        _save_json_atomic(ROUTER_STATE_PATH, state)
        return

    # --- Register all signals in corroboration engine (rolling window) ---
    try:
        from corroboration_engine import register_signal, get_corroboration
        from signal_normalizer import normalize_signal
        
        normalized_count = 0
        for s in all_signals:
            # Normalize before registering (ensures signal_window has canonical fields)
            origin = s.get("_origin", "unknown")
            normalized = normalize_signal(s, origin)
            if normalized:
                register_signal(normalized)
                normalized_count += 1
            else:
                register_signal(s)  # Fallback to raw if normalization fails
        _log(f"Corroboration engine: registered {normalized_count}/{len(all_signals)} signals (normalized)")
    except Exception as e:
        _log(f"Corroboration engine registration failed: {e}")

    # --- Detect cross-source tokens (via corroboration engine) ---
    cross_source_tokens: set[str] = set()
    cross_source_data: dict[str, dict] = {}  # token → corroboration result
    all_unique_tokens = {s.get("token", "").upper() for s in all_signals if s.get("token")}
    try:
        for tok in all_unique_tokens:
            corr = get_corroboration(tok)
            if corr["cross_source_count"] >= 2:
                cross_source_tokens.add(tok)
                cross_source_data[tok] = corr
        if cross_source_tokens:
            for tok, corr in cross_source_data.items():
                _log(f"Cross-source: {tok} = {corr['corroboration_level']} ({corr['cross_source_count']} sources: {', '.join(corr['cross_sources'])})")
    except Exception as e:
        _log(f"Corroboration lookup failed: {e}")
        # Fallback to old method
        cg_tokens = {s.get("token", "").upper() for s in cg_signals}
        dex_tokens = {s.get("token", "").upper() for s in dex_signals}
        be_tokens = {s.get("token", "").upper() for s in be_signals}
        oc_tokens = {s.get("token", "").upper() for s in oc_signals}
        all_source_sets = [cg_tokens, dex_tokens, be_tokens, oc_tokens]
        for tok in (cg_tokens | dex_tokens | be_tokens | oc_tokens):
            if sum(1 for s in all_source_sets if tok in s) >= 2:
                cross_source_tokens.add(tok)

    # --- Load market regime ---
    regime_adjustment = 0
    fg = _load_json(FEAR_GREED_PATH, {})
    fg_value = fg.get("value")
    fg_regime = fg.get("regime", "UNKNOWN")
    if fg_regime == "EXTREME_GREED":
        regime_adjustment = -15
    elif fg_regime == "GREED":
        regime_adjustment = -5
    elif fg_regime == "FEAR":
        regime_adjustment = 5
    elif fg_regime == "EXTREME_FEAR":
        regime_adjustment = 15
    # Derive regime_tag for bear-market filter (BEAR_HIGH_VOL / BEAR_LOW_VOL)
    regime_tag = "UNKNOWN"
    if fg_regime in ("EXTREME_FEAR", "FEAR"):
        regime_tag = "BEAR_HIGH_VOL"  # conservative: treat fear as bear
    elif fg_regime in ("EXTREME_GREED", "GREED"):
        regime_tag = "BULL"
    elif fg_regime == "NEUTRAL":
        regime_tag = "NEUTRAL"

    if fg_value is not None:
        adj_str = f"+{regime_adjustment}" if regime_adjustment > 0 else str(regime_adjustment)
        _log(f"Market regime: {fg_regime} ({fg_value}) — applying {adj_str} to all scores")
    else:
        _log("Market regime: UNKNOWN (no fear/greed data)")

    # --- Load skip list (toxic tokens) ---
    skip_list = _load_skip_list()
    skip_tokens = {entry["token"].upper(): entry["reason"] for entry in skip_list}
    
    # --- Filter ---
    filtered_reasons: list[str] = []
    candidates: list[tuple[dict, float]] = []  # (signal, score)

    for s in all_signals:
        token = (s.get("token") or "").upper()
        shash = _signal_hash(s)

        if token in open_tokens:
            filtered_reasons.append(f"{token} (already open)")
            continue
        if token in skip_tokens:
            filtered_reasons.append(f"{token} (skip list: {skip_tokens[token][:50]})")
            continue
        if token in cooldowns:
            filtered_reasons.append(f"{token} (cooldown {cooldowns[token]:.0f}min remaining)")
            continue
        if shash in processed_hashes:
            continue  # silently skip already-processed

        is_cross = token in cross_source_tokens
        age = s.get("_source_age_min", 30)
        score = _score_signal(s, age, is_cross) + regime_adjustment
        candidates.append((s, score))

    # ── Bear market quality filter (live mode only) ──
    # In paper mode, let memes through to learn which ones get correctly rejected
    router_cfg = _cfg.get("router", {})
    if regime_tag in ("BEAR_HIGH_VOL", "BEAR_LOW_VOL") and router_cfg.get("bear_market_cex_only", False) and not _is_paper:
        min_vol = router_cfg.get("min_volume_24h_usd", 1_000_000)
        min_liq = router_cfg.get("min_liquidity_usd", 500_000)
        pre_filter = len(candidates)
        candidates = [
            (s, sc) for s, sc in candidates
            if (s.get("volume_24h", 0) or 0) >= min_vol or sc >= 50  # high score = CEX listed
        ]
        dropped = pre_filter - len(candidates)
        if dropped:
            _log(f"Bear market filter: dropped {dropped} low-quality signals (min vol ${min_vol:,.0f})")

    if filtered_reasons:
        _log(f"Filtered: {', '.join(filtered_reasons)}")

    if not candidates:
        _log("No actionable signals after filtering.")
        state["last_run"] = now_str
        state["signals_scanned"] = len(all_signals)
        state["signals_filtered"] = len(all_signals)
        _save_json_atomic(ROUTER_STATE_PATH, state)
        return

    # --- Strategy pre-filter (Tier 2 wiring) ---
    # Bonus for signals that match an active strategy
    try:
        from strategy_registry import match_signal_to_strategies
        for idx, (s, sc) in enumerate(candidates):
            matches = match_signal_to_strategies(s)
            if matches:
                strategy_names = [m["strategy"] for m in matches]
                candidates[idx] = (s, sc + 15)  # Strategy match bonus
                s["_matched_strategies"] = strategy_names
        _log(f"Strategy filter: {sum(1 for s,_ in candidates if s.get('_matched_strategies'))} signals match active strategies")
    except Exception as e:
        _log(f"Strategy pre-filter failed (proceeding without): {e}")

    # --- Rank by base score first ---
    candidates.sort(key=lambda x: x[1], reverse=True)
    
    # --- Tradeability scoring (Phase 3) - ONLY for top 20 candidates ---
    # Optimize: expensive scoring only on promising signals
    try:
        from tradeability_scorer import score_tradeability
        top_n = min(20, len(candidates))
        for idx in range(top_n):
            s, sc = candidates[idx]
            t_score = score_tradeability(s)
            s["_tradeability_score"] = t_score
            # Add (tradeability_score // 10) to ranking score
            candidates[idx] = (s, sc + (t_score // 10))
        _log(f"Tradeability scored: top {top_n} candidates")
    except Exception as e:
        _log(f"Tradeability scoring failed (proceeding without): {e}")

    # --- Re-rank with tradeability ---
    candidates.sort(key=lambda x: x[1], reverse=True)
    _log(f"Scoring {len(candidates)} candidates...")

    for i, (s, score) in enumerate(candidates[:10], 1):
        token = s.get("token", "?")
        stype = s.get("signal_type", "")
        origin = s.get("_origin", "")
        is_cross = token.upper() in cross_source_tokens
        extras = []
        if s.get("boost_amount"):
            extras.append(f"boost {s['boost_amount']}x")
        if s.get("trending_rank") is not None:
            extras.append(f"trending #{s['trending_rank'] + 1}")
        top10 = s.get("top10_holder_pct")
        if top10 is not None and top10 > 0:
            extras.append(f"top10={top10:.0f}%")
        if s.get("holder_count") and s["holder_count"] > 0:
            hc = s["holder_count"]
            extras.append(f"{hc/1e3:.1f}K holders" if hc >= 1000 else f"{hc} holders")
        age_h = s.get("token_age_hours")
        if age_h is not None:
            extras.append(f"age {age_h:.0f}h" if age_h < 24 else f"age {age_h/24:.0f}d")
        pct_1h = s.get("price_change_1h_pct")
        pct_24h = s.get("price_change_24h_pct")
        if pct_1h:
            extras.append(f"{'+' if pct_1h > 0 else ''}{pct_1h:.0f}% 1h")
        elif pct_24h:
            extras.append(f"{'+' if pct_24h > 0 else ''}{pct_24h:.1f}% 24h")
        vol = s.get("volume_24h") or 0
        if vol >= 1e6:
            extras.append(f"vol ${vol/1e6:.1f}M")
        elif vol > 0:
            extras.append(f"vol ${vol/1e3:.0f}K")
        detail = ", ".join(extras)
        cross_tag = " ← CROSS-SOURCE" if is_cross else ""
        _log(f"  {i}. {token} — score {score} ({origin} {stype}, {detail}){cross_tag}")

    # --- PREFILTER: DexScreener boost signals (100% block rate = waste) ---
    prefiltered = []
    dex_prefilter_skipped = 0
    
    for idx, (s, sc) in enumerate(candidates):
        origin = s.get("_origin", "")
        boost = s.get("boost_amount") or 0
        
        # Only prefilter DexScreener boost signals
        if origin == "dexscreener" and boost > 0:
            token_name = s.get("token", "?")
            liquidity = s.get("liquidity") or 0
            token_age_h = s.get("token_age_hours") or 0
            holder_count = s.get("holder_count") or 0
            rugcheck_score = s.get("rugcheck_score") or 0
            
            # REQUIRE ALL: liquidity $100K+, age 6h+, holders 500+, rugcheck 40+
            # EXPERIMENT: Lowered thresholds to test borderline tokens
            reasons = []
            if liquidity < 100000:
                reasons.append(f"liquidity ${liquidity/1000:.0f}K < $100K")
            if token_age_h < 6:
                reasons.append(f"age {token_age_h:.1f}h < 6h")
            if holder_count < 500:
                reasons.append(f"holders {holder_count} < 500")
            if rugcheck_score > 0 and rugcheck_score < 40:
                reasons.append(f"rugcheck {rugcheck_score} < 40")
            
            if reasons:
                dex_prefilter_skipped += 1
                _log(f"Prefiltered: {token_name} (DexScreener boost failed: {', '.join(reasons)})")
                continue
        
        prefiltered.append((s, sc))
    
    if dex_prefilter_skipped > 0:
        _log(f"DexScreener prefilter: {dex_prefilter_skipped} signals skipped (low quality)")
    
    candidates = prefiltered
    
    # --- RugCheck safety gate (top 3 Solana candidates) ---
    try:
        from rugcheck_client import check_token_safety
    except ImportError:
        sys.path.insert(0, str(SCRIPT_DIR))
        from rugcheck_client import check_token_safety

    rugcheck_log_parts = []
    rugcheck_skipped: set[int] = set()
    checked_rc = 0
    for idx, (s, sc) in enumerate(prefiltered):
        addr = s.get("token_address", "")
        chain = s.get("chain", "")
        if not addr or chain != "solana" or checked_rc >= 3:
            continue
        checked_rc += 1
        token_name = s.get("token", "?")
        try:
            safety = check_token_safety(addr)
            rc_score = safety.get("rugcheck_score")
            rc_level = safety.get("risk_level", "?")
            rc_safe = safety.get("safe_to_trade", False)

            if rc_score is not None and rc_score > 70:
                prefiltered[idx] = (s, sc + 10)  # bonus
            if rc_score is not None and rc_score < 30:
                rugcheck_skipped.add(idx)
                rugcheck_log_parts.append(f"{token_name} score {rc_score} ({rc_level}) ⛔ SKIPPED")
                continue
            if not rc_safe:
                rugcheck_skipped.add(idx)
                rugcheck_log_parts.append(f"{token_name} score {rc_score} ({rc_level}) UNSAFE ⛔ SKIPPED")
                continue

            flag = "✅" if rc_level == "Good" else "⚠️"
            rugcheck_log_parts.append(f"{token_name} score {rc_score} ({rc_level}) {flag}")
            # Store rugcheck data in signal for pipeline
            s["rugcheck_score"] = rc_score
            s["rugcheck_level"] = rc_level
            s["rugcheck_risks"] = safety.get("risks", [])
        except Exception as e:
            rugcheck_log_parts.append(f"{token_name} ERROR: {e}")

    if rugcheck_log_parts:
        _log(f"RugCheck: {' | '.join(rugcheck_log_parts)}")

    # Remove skipped candidates
    if rugcheck_skipped:
        prefiltered = [(s, sc) for idx, (s, sc) in enumerate(prefiltered) if idx not in rugcheck_skipped]
    
    # Re-sort after bonus adjustments and prefiltering
    candidates = prefiltered
    candidates.sort(key=lambda x: x[1], reverse=True)

    if not candidates:
        _log("No candidates remaining after RugCheck safety gate.")
        state["last_run"] = now_str
        _save_json_atomic(ROUTER_STATE_PATH, state)
        return

    # --- Select top signals (batch up to 3 for paper mode) ---
    try:
        with open(STATE_DIR / "portfolio.json") as _pf:
            _portfolio = json.load(_pf)
        is_paper_mode = _portfolio.get("mode", "paper") == "paper"
    except Exception:
        is_paper_mode = True

    batch_size = 2 if is_paper_mode else 1  # Capped at 2 to stay under 600s timeout
    # Deduplicate: no two signals for the same token in one batch
    batch = []
    seen_tokens = set()
    for s, sc in candidates:
        tok = s.get("token", "").upper()
        if tok in seen_tokens:
            continue
        seen_tokens.add(tok)
        batch.append((s, sc))
        if len(batch) >= batch_size:
            break

    _log(f"Batch: {len(batch)} signal(s) selected for pipeline" + (" (paper mode)" if is_paper_mode else ""))

    # Initialize pipeline_action in case all signals are skipped
    pipeline_action = "NO_SIGNALS"
    
    # Update last_run timestamp at start (so watchdog knows router is running)
    now = datetime.now(timezone.utc)
    now_str = now.isoformat().replace("+00:00", "+00:00")
    state["last_run"] = now_str
    _save_json_atomic(ROUTER_STATE_PATH, state)
    
    for batch_idx, (selected, selected_score) in enumerate(batch):
        # Check budget before each run
        if state.get("daily_pipeline_runs", 0) >= MAX_DAILY_RUNS:
            _log(f"Daily pipeline budget exhausted after batch item {batch_idx}. Stopping.")
            break

        selected_token = selected.get("token", "?")
        _log(f"[{batch_idx+1}/{len(batch)}] Selected: {selected_token} (score {selected_score}) → feeding to pipeline")

        # --- Tradeability Gate (Phase 3) ---
        try:
            # Enrich signal with real-time market data before scoring
            from market_data_enricher import enrich_signal
            selected = enrich_signal(selected)
            
            from tradeability_scorer import score_tradeability
            t_score = score_tradeability(selected)
            selected['tradeability_score'] = t_score
            _log(f"  Tradeability: {t_score}/100")
            
            # Regime-adaptive threshold from config
            regime_tag = selected.get('regime_tag', 'UNKNOWN')
            tradeability_cfg = _cfg.get('tradeability', {}).get('gate_threshold', {})
            
            if regime_tag in ['BEAR_HIGH_VOL', 'BEAR_LOW_VOL']:
                threshold = tradeability_cfg.get('BEAR_HIGH_VOL', 35)
            elif regime_tag == 'UNKNOWN':
                threshold = tradeability_cfg.get('UNKNOWN', 40)
            else:  # BULL or NEUTRAL
                threshold = tradeability_cfg.get('BULL', 55)
            
            if t_score < threshold:
                _log(f"  SKIP {selected_token}: tradeability={t_score} (below {threshold} for {regime_tag})")
                continue
        except Exception as e:
            _log(f"  Tradeability scoring failed: {e}")
            # Continue without tradeability score (don't block on error)

        # --- Record signal in UCB1 ---
        try:
            from ucb1_scorer import record_signal
            source_key = selected.get("source", selected.get("_origin", "unknown"))
            record_signal(source_key)
        except Exception as e:
            _log(f"UCB1 record_signal failed: {e}")

        # --- Convert to pipeline format ---
        cross_labels = []
        if selected_token.upper() in cross_source_tokens:
            # Find cross-source info
            for s in all_signals:
                if s.get("token", "").upper() == selected_token.upper() and s.get("_origin") != selected.get("_origin"):
                    if s.get("_origin") == "coingecko":
                        rank = s.get("trending_rank")
                        cross_labels.append(f"CoinGecko trending #{rank + 1}" if rank is not None else "CoinGecko gainer")
                    elif s.get("_origin") == "dexscreener":
                        boost = s.get("boost_amount")
                        cross_labels.append(f"DexScreener boost ({boost}x)" if boost else "DexScreener")
                    elif s.get("_origin") == "birdeye":
                        be_type = s.get("signal_type", "")
                        cross_labels.append(f"Birdeye {be_type.lower().replace('_', ' ')}")

        pipeline_signal = _to_pipeline_signal(selected, cross_labels)
        pipeline_signal["router_score"] = selected_score  # Pass to pipeline for tier routing

        # --- Validate required fields before sending to pipeline ---
        # Note: Empty string "" should NOT count as missing (some signals have minimal thesis)
        required_fields = ["token", "source", "venue", "exchange"]  # thesis optional for cross-source
        missing_fields = [f for f in required_fields if not pipeline_signal.get(f)]
        # Warn but don't block on empty thesis (DexScreener cross-source signals may lack narrative)
        if not pipeline_signal.get("thesis"):
            _log(f"  WARN {selected_token}: thesis is empty (cross-source signal?)")
        if missing_fields:
            _log(f"  SKIP {selected_token}: missing required fields: {', '.join(missing_fields)}")
            # Record as rejected counterfactual
            try:
                from counterfactual import record_rejection
                record_rejection(
                    token=selected_token,
                    price_usd=selected.get("price_usd") or selected.get("price") or selected.get("current_price_usd", 0),
                    reason=f"Missing required field: {missing_fields[0]}",
                    gate="ROUTER_VALIDATION"
                )
            except Exception as e:
                _log(f"Counterfactual record failed: {e}")
            continue

        # --- Inject corroboration data for Sanad verifier ---
        tok_upper = selected_token.upper()
        if tok_upper in cross_source_data:
            corr = cross_source_data[tok_upper]
        else:
            try:
                corr = get_corroboration(tok_upper)
            except Exception:
                corr = {"cross_source_count": 1, "cross_sources": [], "corroboration_level": "AHAD"}
        pipeline_signal["cross_source_count"] = corr["cross_source_count"]
        pipeline_signal["cross_sources"] = corr["cross_sources"]
        pipeline_signal["corroboration_level"] = corr["corroboration_level"]
        pipeline_signal["corroboration_quality"] = corr.get("corroboration_quality", "STRONG")

        # --- Active Solscan enrichment (on-chain verification) ---
        if pipeline_signal.get("contract_address") or pipeline_signal.get("address") or pipeline_signal.get("token_address"):
            try:
                from solscan_client import enrich_signal_with_solscan
                pipeline_signal = enrich_signal_with_solscan(pipeline_signal)
                _log(f"Solscan enrichment applied: holders={pipeline_signal.get('solscan_holder_count', 'N/A')}, verified={pipeline_signal.get('solscan_verified', False)}")
                
                # Re-calculate corroboration with Solscan as 5th source
                if pipeline_signal.get("solscan_holder_count", 0) > 0:
                    # Add solscan to cross_sources if enrichment succeeded
                    cross_sources_list = list(corr["cross_sources"])
                    if "solscan" not in cross_sources_list:
                        cross_sources_list.append("solscan")
                        cross_source_count = len(cross_sources_list)
                        
                        # Update corroboration level
                        if cross_source_count >= 4:
                            pipeline_signal["corroboration_level"] = "TAWATUR_QAWIY"
                        elif cross_source_count == 3:
                            pipeline_signal["corroboration_level"] = "TAWATUR"
                        elif cross_source_count == 2:
                            pipeline_signal["corroboration_level"] = "MASHHUR"
                        
                        pipeline_signal["cross_source_count"] = cross_source_count
                        pipeline_signal["cross_sources"] = cross_sources_list
                        _log(f"Corroboration upgraded with Solscan: {pipeline_signal['corroboration_level']} ({cross_source_count} sources)")
                        
            except Exception as e:
                _log(f"Solscan enrichment failed: {e}")

        # --- v3.1 Hot Path: Call fast_decision_engine directly ---
        _log(f"Calling v3.1 Hot Path (fast_decision_engine)...")
        pipeline_start = time.time()
        
        POLICY_VERSION = "v3.1.0"
        try:
            # Load portfolio state
            portfolio = _load_json(PORTFOLIO_PATH, default={
                "cash_balance_usd": 10000,
                "open_position_count": 0,
                "total_exposure_pct": 0,
                "mode": "paper"
            })
            
            # Build runtime state — stats from SQLite (single source of truth)
            runtime_state = {
                "min_score": 40,
                "regime_tag": "NEUTRAL",  # TODO: integrate regime_classifier
                "kill_switch": False,
            }
            
            # Load UCB1 source grades from DB (replaces JSON file reads)
            try:
                from state_store import get_source_ucb_stats, get_bandit_stats
                
                # UCB1 grades: compute from source_ucb_stats table
                ucb_raw = get_source_ucb_stats()
                ucb1_grades = {}
                for src_id, stats in ucb_raw.items():
                    n = stats["n"]
                    if n == 0:
                        ucb1_grades[src_id] = {"grade": "C", "score": 50, "cold_start": True}
                    else:
                        win_rate = stats["reward_sum"] / n
                        # UCB1 score: win_rate * 100 (simplified — no exploration bonus in hot path)
                        score = win_rate * 100
                        grade = "A" if score >= 80 else "B" if score >= 60 else "C" if score >= 40 else "D"
                        ucb1_grades[src_id] = {"grade": grade, "score": score, "cold_start": False, "n": n}
                runtime_state["ucb1_grades"] = ucb1_grades
                
                # Thompson state: from bandit_strategy_stats table
                bandit_raw = get_bandit_stats()
                thompson_state = {}
                for (strat_id, regime), stats in bandit_raw.items():
                    thompson_state.setdefault(strat_id, {})[regime] = {
                        "alpha": stats["alpha"], "beta": stats["beta"], "n": stats["n"]
                    }
                runtime_state["thompson_state"] = thompson_state
                
            except Exception as e:
                _log(f"WARNING: Failed to load DB stats for hot path: {e}")
                runtime_state["ucb1_grades"] = {}
                runtime_state["thompson_state"] = {}
            
            # Call v3.1 Hot Path
            if HAS_V31_HOT_PATH:
                decision_record = fast_decision_engine.evaluate_signal_fast(
                    signal=pipeline_signal,
                    portfolio=portfolio,
                    runtime_state=runtime_state,
                    policy_version=POLICY_VERSION
                )
                
                pipeline_duration = time.time() - pipeline_start
                _log(f"Hot Path completed in {pipeline_duration:.1f}s")
                
                # Extract result (v3.1 format)
                pipeline_action = decision_record.get("result")  # EXECUTE/SKIP/BLOCK
                pipeline_reason = decision_record.get("reason_code", "")
                decision_data = decision_record
                
                # Persist SKIP/BLOCK decisions to DB
                if pipeline_action in ("SKIP", "BLOCK"):
                    try:
                        state_store.insert_decision(decision_record)
                        _log(f"Decision persisted to DB: {pipeline_action} - {pipeline_reason}")
                    except state_store.DBBusyError:
                        _log(f"DB busy, logging to JSONL fallback: {decision_record.get('decision_id', '?')}")
                        _append_to_jsonl(BASE_DIR / "logs" / "decisions.jsonl", decision_record)
                    except Exception as e:
                        _log(f"Decision insert error: {e}, using JSONL fallback")
                        _append_to_jsonl(BASE_DIR / "logs" / "decisions.jsonl", decision_record)
                
                # EXECUTE: position already created by try_open_position_atomic() in engine
                elif pipeline_action == "EXECUTE":
                    execution_data = decision_record.get("execution", {})
                    position_id = execution_data.get("position_id", "?")
                    entry_price = execution_data.get("entry_price", 0)
                    size_usd = execution_data.get("size_usd", 0)
                    
                    _log(f"Decision: EXECUTE - Position {position_id} opened @ ${entry_price:.6f} (${size_usd})")
                    
                    # Update portfolio counters
                    portfolio["open_position_count"] += 1
                    portfolio["daily_trades"] = portfolio.get("daily_trades", 0) + 1
                    _save_json_atomic(PORTFOLIO_PATH, portfolio)
                    
                    pipeline_reason = f"position={position_id} price=${entry_price:.6f}"
                
                # Always append to JSONL for observability
                _append_to_jsonl(BASE_DIR / "logs" / "decisions.jsonl", decision_record)
            
            else:
                # v3.1 Hot Path not available - this should not happen
                _log("ERROR: v3.1 Hot Path not available (HAS_V31_HOT_PATH=False)")
                pipeline_action = "ERROR"
                pipeline_reason = "v3.1 Hot Path modules not imported"
                decision_data = None
        
        except state_store.DBBusyError as e:
            _log(f"DB busy during Hot Path: {e}")
            pipeline_action = "SKIP"
            pipeline_reason = "SKIP_DB_BUSY"
        except Exception as e:
            _log(f"Hot Path ERROR: {e}")
            import traceback
            _log(traceback.format_exc())
            pipeline_action = "ERROR"
            pipeline_reason = f"Hot Path exception: {str(e)[:100]}"

        # --- Update state ---            pipeline_reason = str(e)

        # --- Update state ---
        shash = _signal_hash(selected)
        processed = list(processed_hashes)
        processed.append(shash)
        state["last_run"] = now_str
        state["signals_scanned"] = len(all_signals)
        state["signals_filtered"] = len(all_signals) - len(candidates)
        state["signal_selected"] = {
            "token": selected_token,
            "score": selected_score,
            "source": selected.get("source", ""),
            "signal_type": selected.get("signal_type", ""),
        }
        state["pipeline_result"] = pipeline_action
        state["pipeline_reason"] = pipeline_reason
        state["processed_hashes"] = processed
        state["daily_pipeline_runs"] = state.get("daily_pipeline_runs", 0) + 1
        daily_runs = state["daily_pipeline_runs"]
        _log(f"Daily runs: {daily_runs}/{MAX_DAILY_RUNS}")

        # --- Record rejection in cooldown state (P0-3) ---
        if pipeline_action in ("REJECT", "REVISE", "TIMEOUT", "ERROR"):
            try:
                cooldown_key = f"{selected_token}:{selected.get('source', selected.get('_origin', 'unknown'))}"
                rejection_cooldown[cooldown_key] = now_str
                _save_json_atomic(rejection_cooldown_path, rejection_cooldown)
            except Exception as e:
                _log(f"Rejection cooldown update failed: {e}")
        
        # --- Counterfactual tracking for rejections ---
        # Record rejected signals so we can check later what we missed
        if pipeline_action in ("REJECT", "REVISE", "TIMEOUT", "ERROR"):
            try:
                cf_path = STATE_DIR / "counterfactual_rejections.json"
                cf_data = _load_json(cf_path, {"rejections": []})
                # Get current price from signal (prioritize enriched fields)
                current_price = (
                    selected.get("price_usd") or 
                    selected.get("price") or 
                    selected.get("current_price_usd") or 
                    selected.get("current_price")
                )
                
                cf_entry = {
                    "token": selected_token,
                    "symbol": selected.get("symbol", f"{selected_token}USDT"),
                    "rejected_at": now_str,
                    "rejection_reason": pipeline_reason or pipeline_action,
                    "router_score": selected_score,
                    "source": selected.get("source", ""),
                    "signal_type": selected.get("signal_type", ""),
                    "price_at_rejection": current_price,
                    "price_24h_later": None,
                    "counterfactual_pnl_pct": None,
                    "checked": False,
                }
                # Get current price for rejection snapshot
                try:
                    sys.path.insert(0, str(SCRIPT_DIR))
                    from binance_client import get_price
                    price = get_price(cf_entry["symbol"])
                    if price:
                        cf_entry["price_at_rejection"] = float(price)
                except Exception:
                    pass
                cf_data["rejections"].append(cf_entry)
                # Keep last 200 rejections
                cf_data["rejections"] = cf_data["rejections"][-200:]
                _save_json_atomic(cf_path, cf_data)
                _log(f"Counterfactual: recorded rejection of {selected_token} @ ${cf_entry.get('price_at_rejection', '?')}")
            except Exception as e:
                _log(f"Counterfactual recording failed: {e}")

    _save_json_atomic(ROUTER_STATE_PATH, state)

    # cron_health now updated in outer wrapper (finally block)

    # Cleanup temp file
    try:
        signal_file.unlink()
    except Exception:
        pass

    return pipeline_action


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import signal
    
    def timeout_handler(signum, frame):
        _log("GLOBAL TIMEOUT: Router exceeded 10 minute hard limit - forcing exit")
        sys.exit(124)  # Exit code 124 = timeout
    
    # Set global 10-minute timeout (dead man's switch)
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(600)  # 10 minutes total for entire router run
    
    try:
        run_router()
        signal.alarm(0)  # Cancel alarm if completed successfully
    except Exception as e:
        signal.alarm(0)  # Cancel alarm on error
        _log(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
