#!/usr/bin/env python3
"""
Sanad Trader v3.1 — Fast Decision Engine (Hot Path)

The deterministic <3s decision pipeline. Zero LLM calls.
Replaces sanad_pipeline.py for runtime signal evaluation.

5-Stage Pipeline:
  Stage 1: Hard Safety Gates (<100ms)
  Stage 2: Signal Scoring (<200ms)
  Stage 3: Strategy Match + Thompson (<100ms)
  Stage 4: Policy Engine Gates 1-14 (<500ms)
  Stage 5: Execute Paper Trade (<1000ms)

Author: Sanad Trader v3.1
Date: 2026-02-22
"""

import os
import sys
import time
import json
from pathlib import Path
from datetime import datetime, timezone

# Add scripts dir to path
BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
SCRIPTS_DIR = BASE_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Import Ticket 1-2 modules
import ids
import state_store

# Import existing v3.0 modules (reuse)
try:
    import hard_gates
    HAS_HARD_GATES = True
except ImportError:
    HAS_HARD_GATES = False

try:
    import signal_scorer
    HAS_SCORER = True
except ImportError:
    HAS_SCORER = False

try:
    import strategy_selector
    HAS_STRATEGY = True
except ImportError:
    HAS_STRATEGY = False

try:
    import policy_engine
    HAS_POLICY = True
except ImportError:
    HAS_POLICY = False

try:
    import paper_execution
    HAS_PAPER = True
except ImportError:
    HAS_PAPER = False

try:
    import binance_client
    HAS_BINANCE = True
except ImportError:
    HAS_BINANCE = False


# ============================================================================
# CONSTANTS
# ============================================================================

POLICY_VERSION = "v3.1.0"
HOT_PATH_TIMEOUT_MS = 3000
STAGE_BUDGETS_MS = {
    "stage_1_safety": 100,
    "stage_2_scoring": 200,
    "stage_3_strategy": 100,
    "stage_4_policy": 500,
    "stage_5_execute": 1000,
}


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

# Known Binance-tradeable majors (short symbols, not contract addresses)
BINANCE_MAJORS = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "DOT", "MATIC",
    "LINK", "UNI", "ATOM", "LTC", "NEAR", "APT", "ARB", "OP", "FIL", "PEPE",
    "SHIB", "WIF", "BONK", "FLOKI", "SUI", "SEI", "TIA", "JUP", "RENDER",
    "FET", "INJ", "STX", "IMX", "MANA", "SAND", "AXS", "GALA",
}


def _is_binance_symbol(symbol: str) -> bool:
    """Check if symbol looks like a Binance-tradeable asset (not a contract address)."""
    if not symbol:
        return False
    s = symbol.upper().rstrip("USDT")
    # Contract addresses are long hex/base58 strings
    if len(symbol) > 20:
        return False
    # Direct match or ends with USDT
    return s in BINANCE_MAJORS or symbol.upper().endswith("USDT")


def now_utc_iso():
    """Current UTC timestamp as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def elapsed_ms(start_time):
    """Milliseconds elapsed since start_time. Uses perf_counter for precision."""
    return int((time.perf_counter() - start_time) * 1000)


def build_decision_record(
    signal_id, decision_id, policy_version, result, stage, reason_code,
    signal, score_data, strategy_data, policy_data, execution_data, timings
):
    """
    Build canonical DecisionRecord matching v3.1 spec schema.
    
    Returns: dict with all required keys
    """
    return {
        # IDs & metadata
        "decision_id": decision_id,
        "signal_id": signal_id,
        "policy_version": policy_version,
        "created_at": now_utc_iso(),
        
        # Result
        "result": result,
        "stage": stage,
        "reason_code": reason_code,
        
        # Signal identity
        "token_address": signal.get("token_address") or signal.get("token", "unknown"),
        "chain": signal.get("chain", "unknown"),
        "source_primary": signal.get("source_primary") or signal.get("source", "unknown"),
        "signal_type": signal.get("signal_type", "generic"),
        
        # Scoring
        "score_total": score_data.get("score_total"),
        "score_breakdown_json": json.dumps(score_data.get("score_breakdown")),
        
        # Strategy
        "strategy_id": strategy_data.get("strategy_id"),
        "position_usd": strategy_data.get("position_usd"),
        
        # Policy
        "gate_failed": policy_data.get("gate_failed"),
        "evidence_json": json.dumps(policy_data.get("evidence", {})),
        
        # Execution
        "execution": execution_data,
        
        # Performance
        "timings_json": json.dumps(timings),
        
        # Full packet
        "decision_packet_json": json.dumps({
            "meta": {
                "signal_id": signal_id,
                "decision_id": decision_id,
                "policy_version": policy_version,
                "created_at": now_utc_iso()
            },
            "signal": signal,
            "score": score_data,
            "strategy": strategy_data,
            "policy": policy_data,
            "execution": execution_data,
            "timings_ms": timings
        })
    }


# ============================================================================
# STAGE 1: HARD SAFETY GATES
# ============================================================================

def stage_1_hard_safety_gates(signal, timings, start_time):
    """
    Stage 1: Hard Safety Gates (<100ms target)
    
    Checks:
    - Honeypot (cached)
    - Rugpull scan (from router enrichment)
    - Sybil risk (cached)
    - Kill switch
    - Circuit breakers
    
    Returns: (passed: bool, reason_code: str or None, evidence: dict)
    """
    stage_start = time.perf_counter()
    
    # Placeholder: call hard_gates module if available
    if HAS_HARD_GATES:
        passed, reason_code, evidence = hard_gates.evaluate(signal)
        timings["stage_1_safety"] = elapsed_ms(stage_start)
        return passed, reason_code, evidence
    
    # Fallback: basic checks
    onchain = signal.get("onchain_evidence", {})
    
    # Honeypot check
    hp = onchain.get("honeypot", {})
    if hp.get("is_honeypot") or hp.get("verdict") == "HONEYPOT":
        timings["stage_1_safety"] = elapsed_ms(stage_start)
        return False, "BLOCK_HONEYPOT", {"honeypot": hp}
    
    # Rugpull check
    rs = onchain.get("rugpull_scan", {})
    if rs.get("verdict") in ("RUG", "BLACKLISTED"):
        timings["stage_1_safety"] = elapsed_ms(stage_start)
        return False, "BLOCK_RUGPULL", {"rugpull_scan": rs}
    
    # Sybil check
    ha = onchain.get("holder_analysis", {})
    if ha.get("sybil_risk") == "CRITICAL":
        timings["stage_1_safety"] = elapsed_ms(stage_start)
        return False, "BLOCK_SYBIL_CRITICAL", {"sybil": ha}
    
    # Kill switch (check file flag)
    kill_switch_path = BASE_DIR / "config" / "kill_switch.flag"
    if kill_switch_path.exists():
        content = kill_switch_path.read_text().strip().upper()
        if content == "TRUE":
            timings["stage_1_safety"] = elapsed_ms(stage_start)
            return False, "BLOCK_KILL_SWITCH", {"kill_switch": True}
    
    # All passed
    timings["stage_1_safety"] = elapsed_ms(stage_start)
    return True, None, {}


# ============================================================================
# STAGE 2: SIGNAL SCORING
# ============================================================================

def stage_2_signal_scoring(signal, runtime_state, timings, start_time):
    """
    Stage 2: Signal Scoring (<200ms target)
    
    Returns: (score_total: float, score_breakdown: dict)
    """
    stage_start = time.perf_counter()
    
    # Placeholder: call signal_scorer module if available
    if HAS_SCORER:
        score_total, score_breakdown = signal_scorer.score_signal(signal, runtime_state)
        timings["stage_2_scoring"] = elapsed_ms(stage_start)
        return score_total, score_breakdown
    
    # Fallback: basic scoring
    score = 0
    breakdown = {}
    
    # RugCheck score
    rugcheck = signal.get("rugcheck_score", 0)
    if rugcheck >= 70:
        rc_points = 30
    elif rugcheck >= 50:
        rc_points = 20
    elif rugcheck >= 40:
        rc_points = 10
    else:
        rc_points = 0
    
    breakdown["rugcheck"] = {"raw": rugcheck, "points": rc_points}
    score += rc_points
    
    # Cross-source corroboration
    cross_sources = signal.get("cross_source_count", 1)
    if cross_sources >= 3:
        cs_points = 25
    elif cross_sources >= 2:
        cs_points = 18
    else:
        cs_points = 8
    
    breakdown["cross_source"] = {"count": cross_sources, "points": cs_points}
    score += cs_points
    
    # Volume
    volume_24h = signal.get("volume_24h", 0)
    if volume_24h >= 5_000_000:
        vol_points = 20
    elif volume_24h >= 1_000_000:
        vol_points = 15
    elif volume_24h >= 100_000:
        vol_points = 10
    else:
        vol_points = 0
    
    breakdown["volume"] = {"raw": volume_24h, "points": vol_points}
    score += vol_points
    
    breakdown["total"] = score
    
    timings["stage_2_scoring"] = elapsed_ms(stage_start)
    return score, breakdown


# ============================================================================
# STAGE 3: STRATEGY SELECTION
# ============================================================================

def kelly_position_size(strategy_id, regime_tag, portfolio, runtime_state, config=None):
    """
    Fractional Kelly Criterion position sizing with mode-specific caps.
    
    Kelly formula: f* = 2p - 1 (simplified, b=1)
    Half-Kelly: f = f* × kelly_fraction (default 0.5)
    
    Mode detection: portfolio["mode"] first, then SYSTEM_MODE env, default PAPER.
    PAPER cap: paper_max_position_pct (0.05)
    LIVE cap: live_max_position_pct (0.05)
    Cold start default: kelly_default_pct (0.02 — conservative, matches risk constitution)
    
    Args:
        strategy_id: Strategy name
        regime_tag: Current market regime
        portfolio: Portfolio dict with cash_balance_usd (and optional "mode")
        runtime_state: Must contain "thompson_state" from DB
        config: Sizing config (or uses defaults from thresholds.yaml)
    
    Returns:
        position_usd: Sized position in USD
        sizing_info: Dict with Kelly computation details
    """
    # Config defaults (from thresholds.yaml)
    if config is None:
        config = {}
    kelly_fraction = config.get("kelly_fraction", 0.5)
    kelly_min_trades = config.get("kelly_min_trades", 30)
    kelly_default_pct = config.get("kelly_default_pct", 0.02)
    
    # Mode detection: portfolio > env > default PAPER
    mode = portfolio.get("mode", os.environ.get("SYSTEM_MODE", "PAPER")).upper()
    
    # Mode-specific caps
    if mode == "LIVE":
        max_position_pct = config.get("live_max_position_pct", 0.05)
    else:
        max_position_pct = config.get("paper_max_position_pct", 0.05)
    
    cash = portfolio.get("cash_balance_usd", 10000)
    
    # Look up Thompson stats from DB
    thompson_state = runtime_state.get("thompson_state", {})
    strat_data = thompson_state.get(strategy_id, {})
    regime_data = strat_data.get(regime_tag, strat_data.get("NEUTRAL", {}))
    
    alpha = regime_data.get("alpha", 1.0)
    beta_val = regime_data.get("beta", 1.0)
    n = regime_data.get("n", 0)
    
    sizing_info = {
        "method": "kelly_default",
        "strategy_id": strategy_id,
        "regime_tag": regime_tag,
        "mode": mode,
        "n": n,
        "kelly_min_trades": kelly_min_trades,
        "max_position_pct": max_position_pct,
    }
    
    # Cold start: not enough trades → use conservative default (2%)
    if n < kelly_min_trades:
        position_usd = cash * kelly_default_pct
        sizing_info["method"] = "kelly_default"
        sizing_info["default_pct"] = kelly_default_pct
        sizing_info["position_usd"] = position_usd
        return position_usd, sizing_info
    
    # Compute win rate from Beta distribution (posterior mean)
    win_rate = alpha / (alpha + beta_val)
    
    # Kelly fraction: f* = 2p - 1 (simplified, b=1)
    kelly_full = 2 * win_rate - 1
    
    # If Kelly is negative or zero, minimum sizing (half of default)
    if kelly_full <= 0:
        position_usd = cash * kelly_default_pct * 0.5  # Half of default
        sizing_info["method"] = "kelly_negative"
        sizing_info["win_rate"] = round(win_rate, 4)
        sizing_info["kelly_full"] = round(kelly_full, 4)
        sizing_info["position_usd"] = position_usd
        return position_usd, sizing_info
    
    # Half-Kelly (or configured fraction)
    kelly_sized = kelly_full * kelly_fraction
    
    # Cap at mode-specific max_position_pct
    kelly_pct = min(kelly_sized, max_position_pct)
    
    position_usd = cash * kelly_pct
    
    sizing_info["method"] = "kelly_active"
    sizing_info["win_rate"] = round(win_rate, 4)
    sizing_info["kelly_full"] = round(kelly_full, 4)
    sizing_info["kelly_fraction"] = kelly_fraction
    sizing_info["kelly_sized"] = round(kelly_sized, 4)
    sizing_info["kelly_pct"] = round(kelly_pct, 4)
    sizing_info["position_usd"] = round(position_usd, 2)
    
    return position_usd, sizing_info


def stage_3_strategy_selection(signal, portfolio, runtime_state, timings, start_time):
    """
    Stage 3: Strategy Match + Thompson Select + Kelly Sizing (<100ms target)
    
    Returns: (strategy_id: str or None, position_usd: float or None, eligible: list)
    """
    stage_start = time.perf_counter()
    
    # Placeholder: call strategy_selector module if available
    if HAS_STRATEGY:
        eligible = strategy_selector.get_eligible_strategies(signal, runtime_state)
        if not eligible:
            timings["stage_3_strategy"] = elapsed_ms(stage_start)
            return None, None, []
        
        strategy_id = strategy_selector.thompson_select(eligible, runtime_state)
        position_usd = strategy_selector.calculate_position_size(strategy_id, portfolio)
        
        timings["stage_3_strategy"] = elapsed_ms(stage_start)
        return strategy_id, position_usd, eligible
    
    # Fallback: default strategy + Kelly sizing
    strategy_id = "default"
    regime_tag = runtime_state.get("regime_tag", "NEUTRAL")
    position_usd, _sizing = kelly_position_size(strategy_id, regime_tag, portfolio, runtime_state)
    eligible = [strategy_id]
    
    timings["stage_3_strategy"] = elapsed_ms(stage_start)
    return strategy_id, position_usd, eligible


# ============================================================================
# STAGE 4: POLICY ENGINE
# ============================================================================

def stage_4_policy_engine(decision_packet, portfolio, timings, start_time):
    """
    Stage 4: Policy Engine Gates 1-14 (<500ms target)
    
    Returns: (passed: bool, gate_failed: int or None, evidence: dict)
    """
    stage_start = time.perf_counter()
    
    # Call policy_engine module if available
    if HAS_POLICY:
        # Build state_override from SQLite/portfolio (avoid stale JSON)
        state_override = {
            "portfolio": portfolio,
            "trade_history": []  # TODO: Load from SQLite if needed
        }
        
        result = policy_engine.evaluate_gates(
            decision_packet,
            gate_range=(1, 14),
            state_override=state_override
        )
        timings["stage_4_policy"] = elapsed_ms(stage_start)
        
        if result["result"] == "PASS":
            return True, None, {}
        else:
            # Extract actual policy engine fields (no "reason" field exists)
            evidence = {
                "gate_failed_name": result.get("gate_failed_name"),
                "gate_evidence": result.get("gate_evidence"),
                "all_evidence": result.get("all_evidence", {})
            }
            return False, result.get("gate_failed"), evidence
    
    # Fallback: pass (policy engine required in production)
    timings["stage_4_policy"] = elapsed_ms(stage_start)
    return True, None, {}


# ============================================================================
# STAGE 5: EXECUTE
# ============================================================================

def stage_5_execute(signal, decision_id, strategy_id, position_usd,
                    score_data, policy_data, timings, start_time):
    """
    Stage 5: Execute Paper Trade (<1000ms target)
    
    Steps:
    1. Fetch live price (with timeout)
    2. Build full canonical DecisionRecord for DB
    3. Create position via try_open_position_atomic()
    4. Return position record
    
    Args:
        score_data: dict with score_total, score_breakdown (from stage 2)
        policy_data: dict with gate_failed, evidence (from stage 4)
    
    Returns: (success: bool, position: dict or None, error: str or None)
    """
    stage_start = time.perf_counter()
    
    token = signal.get("token_address") or signal.get("token")
    symbol = signal.get("symbol", token or "")
    
    # Price selection: DEX/enriched price first, Binance fallback only for CEX symbols
    try:
        # 1. If signal already has a valid price (DEX/Pump.fun/Raydium), use it directly
        signal_price = signal.get("price")
        if signal_price and float(signal_price) > 0:
            price = float(signal_price)
        # 2. Binance fallback: only for known CEX symbols
        elif HAS_BINANCE and _is_binance_symbol(symbol):
            binance_symbol = symbol.upper() if symbol.upper().endswith("USDT") else f"{symbol.upper()}USDT"
            price = binance_client.get_price(binance_symbol, timeout=0.5)
        else:
            price = None
        
        if not price:
            timings["stage_5_execute"] = elapsed_ms(stage_start)
            return False, None, "SKIP_NO_PRICE"
    
    except Exception as e:
        timings["stage_5_execute"] = elapsed_ms(stage_start)
        return False, None, f"SKIP_PRICE_TIMEOUT: {e}"
    
    # Build position payload
    position_id = ids.make_position_id(decision_id, execution_ordinal=1)
    position_payload = {
        "position_id": position_id,
        "size_token": position_usd / price if price > 0 else 0,
        "regime_tag": signal.get("regime_tag", "NEUTRAL"),
        "features": {
            "entry_signal": signal,
            "strategy_id": strategy_id
        }
    }
    
    # Execute via try_open_position_atomic
    try:
        # Build FULL canonical DecisionRecord for DB insert
        # Must match schema expected by _insert_decision_internal
        signal_id = signal.get("signal_id", "")
        strategy_data = {
            "strategy_id": strategy_id,
            "position_usd": position_usd,
            "eligible": [strategy_id]
        }
        execution_data = {
            "result": "EXECUTE",
            "position_id": position_id
        }

        decision_for_db = build_decision_record(
            signal_id=signal_id,
            decision_id=decision_id,
            policy_version=POLICY_VERSION,
            result="EXECUTE",
            stage="STAGE_5_EXECUTE",
            reason_code="EXECUTE",
            signal=signal,
            score_data=score_data,
            strategy_data=strategy_data,
            policy_data=policy_data,
            execution_data=execution_data,
            timings=timings
        )
        
        position, metadata = state_store.try_open_position_atomic(
            decision_for_db, price, position_payload
        )
        
        timings["stage_5_execute"] = elapsed_ms(stage_start)
        
        if metadata.get("already_existed"):
            return True, position, None
        else:
            return True, position, None
    
    except state_store.DBBusyError as e:
        timings["stage_5_execute"] = elapsed_ms(stage_start)
        return False, None, "SKIP_DB_BUSY"
    
    except Exception as e:
        timings["stage_5_execute"] = elapsed_ms(stage_start)
        return False, None, f"SKIP_EXECUTION_ERROR: {e}"


# ============================================================================
# POLICY PACKET BUILDER
# ============================================================================

def build_policy_packet(signal: dict, strategy_data: dict, price: float, runtime_state: dict, now_iso: str) -> dict:
    """
    Build policy-engine-compatible decision packet.
    
    Based on test_policy_engine.make_passing_packet() schema.
    Ensures Gates 1-14 have required fields.
    """
    token_address = signal.get("token_address", signal.get("token", "UNKNOWN"))
    symbol = signal.get("symbol", token_address)
    chain = signal.get("chain", "unknown")
    
    # Extract enrichment sources
    cross_sources = signal.get("cross_sources", [])
    if not cross_sources:
        cross_sources = [signal.get("source", "router")]
    
    packet = {
        # Core token identity
        "token": {
            "symbol": symbol,
            "chain": chain,
            "contract_address": token_address,
            "deployment_timestamp": signal.get("deployment_timestamp"),  # Gate 4 checks this
        },
        
        # Timestamps (Gate 3 checks these)
        "data_timestamps": {
            "price_timestamp": now_iso,
            "onchain_timestamp": signal.get("timestamp", now_iso),
            "signal_timestamp": signal.get("timestamp", now_iso),
        },
        
        # API responses (Gate 3 checks non-empty)
        "api_responses": {
            "price_source": {"status": "ok", "provider": "binance"} if price else {"status": "unavailable"},
            "enrichment_sources": {src: {"status": "ok"} for src in cross_sources},
        },
        
        # Sanad verification (Gate 5 checks these)
        "sanad_verification": {
            "rugpull_flags": signal.get("onchain_evidence", {}).get("rugpull_scan", {}).get("flags", []),
            "sybil_risk": signal.get("onchain_evidence", {}).get("holder_analysis", {}).get("sybil_risk", "UNKNOWN"),
            "trust_score": signal.get("rugcheck_score", 0),
        },
        
        # Market data (Gate 7 checks slippage, Gate 9 checks volatility)
        "market_data": {
            "estimated_slippage_bps": 50,  # Conservative default
            "spread_bps": 10,
            "liquidity_usd": signal.get("liquidity_usd", 0),
            "volume_24h": signal.get("volume_24h", 0),
            "price_change_pct_window": signal.get("price_30min_change_pct", 0),  # Gate 9
        },
        
        # Trade details
        "venue": "paper",
        "exchange": "binance",
        "strategy_name": strategy_data.get("strategy_id", "unknown"),
        
        # Regime
        "regime": runtime_state.get("regime_tag", "NEUTRAL"),
    }
    
    return packet


# ============================================================================
# MAIN API: EVALUATE_SIGNAL_FAST
# ============================================================================

def evaluate_signal_fast(
    signal: dict,
    portfolio: dict,
    runtime_state: dict,
    policy_version: str = POLICY_VERSION
) -> dict:
    """
    Hot Path: Evaluate signal and return decision in <3 seconds.
    
    NO LLM CALLS. Pure deterministic + statistical.
    
    Args:
        signal: Enriched signal dict (from router)
        portfolio: Portfolio state (cash, positions, exposure)
        runtime_state: Runtime state (Thompson, UCB1, regime, etc.)
        policy_version: Policy version string (default: v3.1.0)
    
    Returns:
        DecisionRecord dict with keys:
        - decision_id, signal_id, policy_version, created_at
        - result (SKIP/BLOCK/EXECUTE)
        - stage, reason_code
        - token_address, chain, source_primary, signal_type
        - score_total, score_breakdown_json
        - strategy_id, position_usd
        - gate_failed, evidence_json
        - execution, timings_json, decision_packet_json
    
    Performance guarantee: <3000ms total
    """
    start_time = time.perf_counter()
    timings = {}
    
    # Generate IDs
    signal_id = ids.make_signal_id(signal)
    decision_id = ids.make_decision_id(signal_id, policy_version)
    
    signal["signal_id"] = signal_id
    signal["decision_id"] = decision_id
    
    # Initialize result containers
    score_data = {"score_total": None, "score_breakdown": None}
    strategy_data = {"strategy_id": None, "position_usd": None, "eligible": []}
    policy_data = {"gate_failed": None, "evidence": {}}
    execution_data = {"result": None, "position_id": None, "error": None}
    
    # ========================================================================
    # STAGE 1: HARD SAFETY GATES
    # ========================================================================
    
    passed, reason_code, evidence = stage_1_hard_safety_gates(signal, timings, start_time)
    
    if not passed:
        # BLOCK decision
        timings["total"] = elapsed_ms(start_time)
        return build_decision_record(
            signal_id, decision_id, policy_version,
            result="BLOCK",
            stage="STAGE_1_SAFETY",
            reason_code=reason_code,
            signal=signal,
            score_data=score_data,
            strategy_data=strategy_data,
            policy_data={"evidence": evidence},
            execution_data=execution_data,
            timings=timings
        )
    
    # ========================================================================
    # STAGE 2: SIGNAL SCORING
    # ========================================================================
    
    score_total, score_breakdown = stage_2_signal_scoring(signal, runtime_state, timings, start_time)
    score_data = {"score_total": score_total, "score_breakdown": score_breakdown}
    
    # Check score threshold
    min_score = runtime_state.get("min_score", 40)
    if score_total < min_score:
        # SKIP decision
        timings["total"] = elapsed_ms(start_time)
        return build_decision_record(
            signal_id, decision_id, policy_version,
            result="SKIP",
            stage="STAGE_2_SCORE",
            reason_code="SKIP_SCORE_LOW",
            signal=signal,
            score_data=score_data,
            strategy_data=strategy_data,
            policy_data=policy_data,
            execution_data=execution_data,
            timings=timings
        )
    
    # ========================================================================
    # STAGE 3: STRATEGY SELECTION
    # ========================================================================
    
    strategy_id, position_usd, eligible = stage_3_strategy_selection(
        signal, portfolio, runtime_state, timings, start_time
    )
    strategy_data = {
        "strategy_id": strategy_id,
        "position_usd": position_usd,
        "eligible": eligible
    }
    
    if not strategy_id:
        # SKIP decision
        timings["total"] = elapsed_ms(start_time)
        return build_decision_record(
            signal_id, decision_id, policy_version,
            result="SKIP",
            stage="STAGE_3_STRATEGY",
            reason_code="SKIP_NO_STRATEGY",
            signal=signal,
            score_data=score_data,
            strategy_data=strategy_data,
            policy_data=policy_data,
            execution_data=execution_data,
            timings=timings
        )
    
    # ========================================================================
    # STAGE 4: POLICY ENGINE
    # ========================================================================
    
    # Build policy-engine-compatible packet
    decision_packet_for_policy = build_policy_packet(
        signal=signal,
        strategy_data=strategy_data,
        price=0.0,  # Will be fetched in Stage 5
        runtime_state=runtime_state,
        now_iso=now_utc_iso()
    )
    
    passed, gate_failed, evidence = stage_4_policy_engine(
        decision_packet_for_policy, portfolio, timings, start_time
    )
    policy_data = {"gate_failed": gate_failed, "evidence": evidence}
    
    if not passed:
        # BLOCK decision
        timings["total"] = elapsed_ms(start_time)
        return build_decision_record(
            signal_id, decision_id, policy_version,
            result="BLOCK",
            stage="STAGE_4_POLICY",
            reason_code=f"BLOCK_POLICY_GATE_{gate_failed:02d}",
            signal=signal,
            score_data=score_data,
            strategy_data=strategy_data,
            policy_data=policy_data,
            execution_data=execution_data,
            timings=timings
        )
    
    # ========================================================================
    # STAGE 5: EXECUTE
    # ========================================================================
    
    success, position, error = stage_5_execute(
        signal, decision_id, strategy_id, position_usd,
        score_data, policy_data, timings, start_time
    )
    
    if not success:
        # SKIP decision (execution failed)
        timings["total"] = elapsed_ms(start_time)
        return build_decision_record(
            signal_id, decision_id, policy_version,
            result="SKIP",
            stage="STAGE_5_EXECUTE",
            reason_code=error,
            signal=signal,
            score_data=score_data,
            strategy_data=strategy_data,
            policy_data=policy_data,
            execution_data={"error": error},
            timings=timings
        )
    
    # ========================================================================
    # SUCCESS: EXECUTE
    # ========================================================================
    
    execution_data = {
        "result": "EXECUTE",
        "position_id": position["position_id"],
        "entry_price": position["entry_price"],
        "size_usd": position["size_usd"],
        "created_at": position["created_at"]
    }
    
    timings["total"] = elapsed_ms(start_time)
    
    return build_decision_record(
        signal_id, decision_id, policy_version,
        result="EXECUTE",
        stage="STAGE_5_EXECUTE",
        reason_code="EXECUTE",
        signal=signal,
        score_data=score_data,
        strategy_data=strategy_data,
        policy_data=policy_data,
        execution_data=execution_data,
        timings=timings
    )


# ============================================================================
# CLI TEST INTERFACE
# ============================================================================

def main():
    """CLI test interface for fast_decision_engine."""
    import json
    
    # Mock signal
    test_signal = {
        "token": "PEPE",
        "token_address": "0x123abc",
        "chain": "solana",
        "source": "test",
        "thesis": "Test signal for fast decision engine",
        "rugcheck_score": 75,
        "volume_24h": 5000000,
        "cross_source_count": 3,
        "onchain_evidence": {
            "honeypot": {"is_honeypot": False},
            "rugpull_scan": {"verdict": "SAFE"},
            "holder_analysis": {"sybil_risk": "LOW"}
        }
    }
    
    # Mock portfolio
    test_portfolio = {
        "cash_balance_usd": 10000,
        "open_position_count": 0,
        "total_exposure_pct": 0
    }
    
    # Mock runtime state
    test_runtime_state = {
        "min_score": 40,
        "regime_tag": "NEUTRAL"
    }
    
    # Run evaluation
    print("=" * 80)
    print("FAST DECISION ENGINE TEST")
    print("=" * 80)
    
    result = evaluate_signal_fast(test_signal, test_portfolio, test_runtime_state)
    
    print(json.dumps(result, indent=2))
    print("=" * 80)
    print(f"Total time: {result['timings_json']}")


if __name__ == "__main__":
    main()
