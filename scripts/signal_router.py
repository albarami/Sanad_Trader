#!/usr/bin/env python3
"""
Signal Router — Sprint 3.x
Reads CoinGecko + DexScreener + Birdeye signals, ranks them, feeds the best
candidate into sanad_pipeline.py. Deterministic Python. No LLMs.
Designed to run as a cron job every 15 minutes.
"""

import hashlib
import json
import os
import subprocess
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

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent  # /data/.openclaw/workspace/trading
SIGNALS_CG = BASE_DIR / "signals" / "coingecko"
SIGNALS_DEX = BASE_DIR / "signals" / "dexscreener"
SIGNALS_BE = BASE_DIR / "signals" / "birdeye"
FEAR_GREED_PATH = BASE_DIR / "signals" / "market" / "fear_greed_latest.json"
STATE_DIR = BASE_DIR / "state"
POSITIONS_PATH = STATE_DIR / "positions.json"
PORTFOLIO_PATH = STATE_DIR / "portfolio.json"
TRADE_HISTORY_PATH = STATE_DIR / "trade_history.json"
ROUTER_STATE_PATH = STATE_DIR / "signal_router_state.json"
PIPELINE_SCRIPT = SCRIPT_DIR / "sanad_pipeline.py"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_POSITIONS = 3
STALE_THRESHOLD_MIN = 30
COOLDOWN_HOURS = 2
MAX_DAILY_RUNS = 20
CROSS_SOURCE_BONUS = 25


def _log(msg: str):
    print(f"[SIGNAL ROUTER] {msg}")


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
    pos = _load_json(POSITIONS_PATH, {"positions": []})
    return {p["token"].upper() for p in pos.get("positions", []) if p.get("status") == "OPEN"}


def _load_cooldown_tokens() -> dict[str, float]:
    """Return {TOKEN: remaining_minutes} for tokens traded within cooldown period."""
    hist = _load_json(TRADE_HISTORY_PATH, {"trades": []})
    now = _now()
    cooldowns: dict[str, float] = {}
    for t in hist.get("trades", []):
        ts_str = t.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            elapsed = (now - ts).total_seconds() / 60
            remaining = COOLDOWN_HOURS * 60 - elapsed
            if remaining > 0:
                token = t.get("token", "").upper()
                cooldowns[token] = max(cooldowns.get(token, 0), remaining)
        except Exception:
            continue
    return cooldowns


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
# Main run
# ---------------------------------------------------------------------------
def run_router():
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

    def _label(name, sigs, age, path):
        if sigs:
            return f"{name} {len(sigs)} ({age:.0f}min ago)"
        elif path:
            return f"{name} 0 ({age:.0f}min ago, stale)"
        return f"{name} no files"

    _log(f"Loading signals: {_label('CoinGecko', cg_signals, cg_age, cg_path)}, "
         f"{_label('DexScreener', dex_signals, dex_age, dex_path)}, "
         f"{_label('Birdeye', be_signals, be_age, be_path)}")

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

    if not all_signals:
        _log("No actionable signals — no recent data from either source.")
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

    # --- Detect cross-source tokens (Tawatur) ---
    cg_tokens = {s.get("token", "").upper() for s in cg_signals}
    dex_tokens = {s.get("token", "").upper() for s in dex_signals}
    be_tokens = {s.get("token", "").upper() for s in be_signals}
    # Cross-source = token appears in 2+ sources
    all_source_sets = [cg_tokens, dex_tokens, be_tokens]
    cross_source_tokens: set[str] = set()
    all_unique = cg_tokens | dex_tokens | be_tokens
    for tok in all_unique:
        sources_count = sum(1 for s in all_source_sets if tok in s)
        if sources_count >= 2:
            cross_source_tokens.add(tok)
    if cross_source_tokens:
        _log(f"Cross-source (Tawatur) matches: {', '.join(sorted(cross_source_tokens))}")

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
    if fg_value is not None:
        adj_str = f"+{regime_adjustment}" if regime_adjustment > 0 else str(regime_adjustment)
        _log(f"Market regime: {fg_regime} ({fg_value}) — applying {adj_str} to all scores")
    else:
        _log("Market regime: UNKNOWN (no fear/greed data)")

    # --- Filter ---
    filtered_reasons: list[str] = []
    candidates: list[tuple[dict, float]] = []  # (signal, score)

    for s in all_signals:
        token = (s.get("token") or "").upper()
        shash = _signal_hash(s)

        if token in open_tokens:
            filtered_reasons.append(f"{token} (already open)")
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

    if filtered_reasons:
        _log(f"Filtered: {', '.join(filtered_reasons)}")

    if not candidates:
        _log("No actionable signals after filtering.")
        state["last_run"] = now_str
        state["signals_scanned"] = len(all_signals)
        state["signals_filtered"] = len(all_signals)
        _save_json_atomic(ROUTER_STATE_PATH, state)
        return

    # --- Rank ---
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

    # --- RugCheck safety gate (top 3 Solana candidates) ---
    try:
        from rugcheck_client import check_token_safety
    except ImportError:
        sys.path.insert(0, str(SCRIPT_DIR))
        from rugcheck_client import check_token_safety

    rugcheck_log_parts = []
    rugcheck_skipped: set[int] = set()
    checked_rc = 0
    for idx, (s, sc) in enumerate(candidates):
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
                candidates[idx] = (s, sc + 10)  # bonus
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
        candidates = [(s, sc) for idx, (s, sc) in enumerate(candidates) if idx not in rugcheck_skipped]
        # Re-sort after bonus adjustments
        candidates.sort(key=lambda x: x[1], reverse=True)

    if not candidates:
        _log("No candidates remaining after RugCheck safety gate.")
        state["last_run"] = now_str
        _save_json_atomic(ROUTER_STATE_PATH, state)
        return

    # --- Select top signal ---
    selected, selected_score = candidates[0]
    selected_token = selected.get("token", "?")
    _log(f"Selected: {selected_token} (score {selected_score}) → feeding to pipeline")

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

    # --- Write temp signal file ---
    tmp_dir = BASE_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    signal_file = tmp_dir / f"router_signal_{now.strftime('%Y%m%d_%H%M%S')}.json"
    signal_file.write_text(json.dumps(pipeline_signal, indent=2))

    # --- Call pipeline ---
    try:
        result = subprocess.run(
            ["python3", str(PIPELINE_SCRIPT), str(signal_file)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        # Try to parse pipeline result from stdout
        pipeline_action = "UNKNOWN"
        pipeline_reason = ""
        for line in reversed(stdout.splitlines()):
            if '"final_action"' in line:
                try:
                    # Try to parse the summary JSON block
                    pass
                except Exception:
                    pass
            if "APPROVE" in line.upper():
                pipeline_action = "APPROVE"
            elif "REJECT" in line.upper():
                pipeline_action = "REJECT"
            elif "REVISE" in line.upper():
                pipeline_action = "REVISE"

        # Better: look for the SUMMARY block
        if "SUMMARY" in stdout:
            summary_start = stdout.index("SUMMARY")
            summary_text = stdout[summary_start:]
            for line in summary_text.splitlines():
                if '"final_action"' in line:
                    if "APPROVE" in line:
                        pipeline_action = "APPROVE"
                    elif "REJECT" in line:
                        pipeline_action = "REJECT"
                    elif "REVISE" in line:
                        pipeline_action = "REVISE"
                if '"reason"' in line or '"rejection_reason"' in line:
                    pipeline_reason = line.split(":", 1)[-1].strip().strip('",')

        _log(f"Pipeline result: {pipeline_action}" + (f" ({pipeline_reason})" if pipeline_reason else ""))

        if stdout:
            # Print last 20 lines of pipeline output for visibility
            lines = stdout.splitlines()
            if len(lines) > 20:
                _log("Pipeline output (last 20 lines):")
                for l in lines[-20:]:
                    print(f"  | {l}")
            else:
                _log("Pipeline output:")
                for l in lines:
                    print(f"  | {l}")

        if stderr:
            _log(f"Pipeline stderr: {stderr[:500]}")

        if result.returncode != 0:
            _log(f"Pipeline exited with code {result.returncode}")

    except subprocess.TimeoutExpired:
        _log("Pipeline TIMEOUT (>5min) — aborting. Will not retry.")
        pipeline_action = "TIMEOUT"
        pipeline_reason = "Pipeline exceeded 5 minute timeout"
    except Exception as e:
        _log(f"Pipeline ERROR: {e}")
        pipeline_action = "ERROR"
        pipeline_reason = str(e)

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

    _save_json_atomic(ROUTER_STATE_PATH, state)

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
    try:
        run_router()
    except Exception as e:
        _log(f"FATAL: {e}")
        sys.exit(1)
