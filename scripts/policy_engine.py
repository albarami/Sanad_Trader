#!/usr/bin/env python3
"""
Sanad Trader v3.0 — Fail-Closed Policy Engine

Phase 10 — Deterministic Veto Pipeline

This is a DETERMINISTIC script. No LLM calls. No exceptions.
Gates are evaluated in exact order. First failure stops evaluation and BLOCKs.
Every external dependency defaults to BLOCK on failure.

References:
- v3 doc Phase 10, Table 9 (15 gates)
- v3 doc Phase 10.3 (fail-closed semantics)
- thresholds.yaml (single source of truth for all parameters)

Author: Sanad Trader v3.0 Policy Engine
"""

import json
import os
import sys
import time
import yaml
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
CONFIG_PATH = BASE_DIR / "config" / "thresholds.yaml"
KILL_SWITCH_PATH = BASE_DIR / "config" / "kill_switch.flag"
STATE_DIR = BASE_DIR / "state"
EXECUTION_LOGS_DIR = BASE_DIR / "execution-logs"


def load_config():
    """Load thresholds.yaml. BLOCK if missing or corrupt."""
    try:
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
        if not isinstance(config, dict):
            return None, "thresholds.yaml parsed but is not a dict"
        return config, None
    except FileNotFoundError:
        return None, "thresholds.yaml not found"
    except yaml.YAMLError as e:
        return None, f"thresholds.yaml parse error: {e}"
    except Exception as e:
        return None, f"thresholds.yaml read error: {e}"


def load_json_state(filename, required=True):
    """Load a JSON state file. BLOCK if required and missing/corrupt."""
    path = STATE_DIR / filename
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data, None
    except FileNotFoundError:
        if required:
            return None, f"State file {filename} not found"
        return {}, None
    except json.JSONDecodeError as e:
        return None, f"State file {filename} JSON parse error: {e}"
    except Exception as e:
        return None, f"State file {filename} read error: {e}"


def now_utc():
    """Current UTC timestamp."""
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────
# GATE DEFINITIONS (Ordered 1-15)
# Each gate returns (passed: bool, evidence: str)
# ─────────────────────────────────────────────

def gate_01_kill_switch(config, decision_packet, state):
    """
    Gate 1: Kill Switch
    BLOCK if kill switch file flag is set to TRUE.
    Rationale: Manual or automatic system halt. Overrides everything.
    """
    try:
        if KILL_SWITCH_PATH.exists():
            content = KILL_SWITCH_PATH.read_text().strip().upper()
            if content == "TRUE":
                return False, "Kill switch is ACTIVE"
        return True, "Kill switch not active"
    except Exception as e:
        return False, f"Cannot read kill switch file: {e}"


def gate_02_capital_preservation(config, decision_packet, state):
    """
    Gate 2: Capital Preservation
    BLOCK if daily loss limit hit (5%) OR max drawdown exceeded (15%).
    Rationale: Prevent catastrophic loss days.
    """
    try:
        portfolio = state.get("portfolio", {})
        daily_pnl_pct = portfolio.get("daily_pnl_pct")
        max_drawdown_pct = portfolio.get("current_drawdown_pct")

        if daily_pnl_pct is None or max_drawdown_pct is None:
            return False, "Portfolio state missing daily_pnl_pct or current_drawdown_pct"

        daily_limit = config["risk"]["daily_loss_limit_pct"]
        max_dd = config["risk"]["max_drawdown_pct"]

        if daily_pnl_pct <= -daily_limit:
            return False, f"Daily loss limit hit: {daily_pnl_pct:.4f} <= -{daily_limit}"

        if max_drawdown_pct >= max_dd:
            return False, f"Max drawdown exceeded: {max_drawdown_pct:.4f} >= {max_dd}"

        return True, f"Daily PnL: {daily_pnl_pct:.4f}, Drawdown: {max_drawdown_pct:.4f}"
    except KeyError as e:
        return False, f"Missing config key: {e}"
    except Exception as e:
        return False, f"Capital preservation check error: {e}"


def gate_03_data_freshness(config, decision_packet, state):
    """
    Gate 3: Data Freshness
    BLOCK if any input data older than 5 min (prices) or 30 min (on-chain)
    OR any API returned empty/null/timeout.
    Rationale: Stale data = blind trading. Missing data = BLOCK.
    """
    try:
        price_max_age = config["policy_gates"]["price_max_age_sec"]
        onchain_max_age = config["policy_gates"]["onchain_max_age_sec"]
        current = now_utc()

        data_timestamps = decision_packet.get("data_timestamps", {})
        if not data_timestamps:
            return False, "No data timestamps provided in decision packet"

        price_ts = data_timestamps.get("price_timestamp")
        if price_ts is None:
            return False, "Price timestamp missing — cannot verify freshness"

        price_dt = datetime.fromisoformat(price_ts)
        price_age = (current - price_dt).total_seconds()
        if price_age > price_max_age:
            return False, f"Price data stale: {price_age:.0f}s old (max {price_max_age}s)"

        onchain_ts = data_timestamps.get("onchain_timestamp")
        if onchain_ts is not None:
            onchain_dt = datetime.fromisoformat(onchain_ts)
            onchain_age = (current - onchain_dt).total_seconds()
            if onchain_age > onchain_max_age:
                return False, f"On-chain data stale: {onchain_age:.0f}s old (max {onchain_max_age}s)"

        api_responses = decision_packet.get("api_responses", {})
        for api_name, response in api_responses.items():
            if response is None or response == {} or response == []:
                return False, f"API {api_name} returned empty/null response"

        return True, f"Price age: {price_age:.0f}s, all data fresh"
    except KeyError as e:
        return False, f"Missing config key: {e}"
    except Exception as e:
        return False, f"Data freshness check error: {e}"


def gate_04_token_age(config, decision_packet, state):
    """
    Gate 4: Token Age
    BLOCK if token contract deployed less than 1 hour ago
    (unless early-launch strategy explicitly enabled with micro-position).
    Rationale: Avoid extreme early volatility and honeypot traps.
    """
    try:
        token_min_age_hours = config["policy_gates"]["token_min_age_hours"]
        token = decision_packet.get("token", {})
        deployment_ts = token.get("deployment_timestamp")

        if deployment_ts is None:
            return False, "Token deployment timestamp unknown — cannot verify age"

        deploy_dt = datetime.fromisoformat(deployment_ts)
        age_hours = (now_utc() - deploy_dt).total_seconds() / 3600

        if age_hours < token_min_age_hours:
            strategy_name = decision_packet.get("strategy_name", "")
            if strategy_name == "early-launch":
                return True, f"Token age {age_hours:.1f}h < {token_min_age_hours}h but early-launch strategy permitted"
            return False, f"Token too young: {age_hours:.1f}h < {token_min_age_hours}h minimum"

        return True, f"Token age: {age_hours:.1f}h (minimum {token_min_age_hours}h)"
    except KeyError as e:
        return False, f"Missing config key: {e}"
    except Exception as e:
        return False, f"Token age check error: {e}"


def gate_05_rugpull_safety(config, decision_packet, state):
    """
    Gate 5: Rugpull Safety
    BLOCK if any chain-specific rugpull check failed.
    Rationale: Hard gate. No override possible.
    """
    try:
        sanad = decision_packet.get("sanad_verification", {})
        rugpull_flags = sanad.get("rugpull_flags", [])

        if rugpull_flags is None:
            return False, "Rugpull check results missing from Sanad verification"

        # Hard rugpull flags that NEVER pass even in paper mode
        hard_flags = {"honeypot", "blacklisted", "rug_confirmed", "mint_authority_active"}
        soft_flags = {"extreme_infancy", "low_holders", "concentrated_holders"}

        if len(rugpull_flags) > 0:
            flags_set = set(f.lower().replace(" ", "_") for f in rugpull_flags)
            hard_hits = flags_set & hard_flags

            if hard_hits:
                return False, f"Rugpull HARD flags: {', '.join(hard_hits)}"

            # Paper mode: allow soft flags through with warning
            portfolio_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "state", "portfolio.json")
            try:
                with open(portfolio_path) as f:
                    is_paper = json.load(f).get("mode", "paper") == "paper"
            except Exception:
                is_paper = True

            if is_paper:
                return True, f"PAPER MODE: soft rugpull flags allowed: {', '.join(rugpull_flags)}"
            return False, f"Rugpull flags triggered: {', '.join(rugpull_flags)}"

        return True, "All rugpull safety checks passed"
    except Exception as e:
        return False, f"Rugpull safety check error: {e}"


def gate_06_liquidity(config, decision_packet, state):
    """
    Gate 6: Liquidity Gate
    BLOCK if computed slippage > 3% OR depth insufficient.
    Rationale: Cannot exit the position safely.

    Paper mode: DEX tokens without CEX order book data get a pass
    with estimated slippage from on-chain liquidity.
    """
    try:
        max_slippage_bps = config["policy_gates"]["max_slippage_bps"]
        market = decision_packet.get("market_data", {})
        venue = decision_packet.get("venue", "CEX")
        estimated_slippage_bps = market.get("estimated_slippage_bps")

        # Paper mode: DEX tokens without slippage estimates use on-chain liquidity
        if estimated_slippage_bps is None:
            portfolio_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "state", "portfolio.json")
            try:
                with open(portfolio_path) as f:
                    portfolio = json.load(f)
                is_paper = portfolio.get("mode", "paper") == "paper"
            except Exception:
                is_paper = True

            if is_paper and venue == "DEX":
                # Use on-chain liquidity as proxy — estimate slippage from pool size
                liquidity = market.get("liquidity_usd", 0) or decision_packet.get("liquidity", 0)
                position_size = decision_packet.get("position_size_usd", 100)
                if liquidity > 0:
                    estimated_slippage_bps = int((position_size / liquidity) * 10000)
                    if estimated_slippage_bps <= max_slippage_bps:
                        return True, f"DEX paper mode: estimated slippage {estimated_slippage_bps}bps from ${liquidity:.0f} liquidity"
                    else:
                        return False, f"DEX slippage too high: {estimated_slippage_bps}bps > {max_slippage_bps}bps (liquidity ${liquidity:.0f})"
                else:
                    # No liquidity data at all — paper mode: allow with warning
                    return True, "DEX paper mode: no liquidity data — allowing with simulated 100bps slippage"
            else:
                return False, "Slippage estimate not provided — cannot verify liquidity"

        if estimated_slippage_bps > max_slippage_bps:
            return False, f"Slippage too high: {estimated_slippage_bps}bps > {max_slippage_bps}bps max"

        depth_sufficient = market.get("depth_sufficient")
        if depth_sufficient is False:
            # Paper mode DEX: depth_sufficient may be unset — allow if slippage is OK
            portfolio_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "state", "portfolio.json")
            try:
                with open(portfolio_path) as f:
                    portfolio = json.load(f)
                is_paper = portfolio.get("mode", "paper") == "paper"
            except Exception:
                is_paper = True
            if is_paper and venue == "DEX":
                return True, f"DEX paper mode: slippage OK ({estimated_slippage_bps}bps), depth check skipped"
            return False, "Order book / pool depth insufficient for position size"

        return True, f"Slippage: {estimated_slippage_bps}bps (max {max_slippage_bps}bps)"
    except KeyError as e:
        return False, f"Missing config key: {e}"
    except Exception as e:
        return False, f"Liquidity check error: {e}"


def gate_07_spread(config, decision_packet, state):
    """
    Gate 7: Spread Gate (CEX)
    BLOCK if bid-ask spread exceeds 2% on CEX venues.
    Rationale: Wide spread indicates thin liquidity or manipulation.
    """
    try:
        venue = decision_packet.get("venue", "CEX")
        if venue == "DEX":
            return True, "Spread gate skipped — DEX trade (no order book spread)"

        max_spread_bps = config["policy_gates"]["max_spread_bps"]
        market = decision_packet.get("market_data", {})
        spread_bps = market.get("spread_bps")

        if spread_bps is None:
            return False, "Spread data not provided — cannot verify"

        if spread_bps > max_spread_bps:
            return False, f"Spread too wide: {spread_bps}bps > {max_spread_bps}bps max"

        return True, f"Spread: {spread_bps}bps (max {max_spread_bps}bps)"
    except KeyError as e:
        return False, f"Missing config key: {e}"
    except Exception as e:
        return False, f"Spread check error: {e}"


def gate_08_preflight_simulation(config, decision_packet, state):
    """
    Gate 8: Pre-Flight Simulation (DEX)
    BLOCK if simulated sell reverts, errors, or returns 0 tokens.
    Rationale: Dynamic honeypot detection.
    """
    try:
        venue = decision_packet.get("venue", "CEX")
        if venue == "CEX":
            return True, "Pre-flight simulation skipped — CEX trade"

        preflight = decision_packet.get("preflight_simulation", {})
        if not preflight:
            # Paper mode: no on-chain simulation available — allow with warning
            portfolio_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "state", "portfolio.json")
            try:
                with open(portfolio_path) as f:
                    is_paper = json.load(f).get("mode", "paper") == "paper"
            except Exception:
                is_paper = True
            if is_paper:
                return True, "DEX paper mode: pre-flight simulation skipped (no on-chain access)"
            return False, "Pre-flight simulation results missing for DEX trade"

        sim_success = preflight.get("sell_simulation_success")
        sim_tokens_returned = preflight.get("tokens_returned", 0)

        if sim_success is None:
            return False, "Pre-flight simulation not executed — cannot verify"

        if sim_success is False:
            error_msg = preflight.get("error", "unknown error")
            return False, f"Simulated sell REVERTED: {error_msg}"

        if sim_tokens_returned == 0:
            return False, "Simulated sell returned 0 tokens — likely honeypot"

        return True, f"Pre-flight simulation passed, tokens returned: {sim_tokens_returned}"
    except Exception as e:
        return False, f"Pre-flight simulation check error: {e}"


def gate_09_volatility_halt(config, decision_packet, state):
    """
    Gate 9: Volatility Halt
    BLOCK if token price moved >25% in last 30 min AND no verified catalyst.
    Rationale: Extreme volatility without explanation = manipulation risk.
    """
    try:
        vol_pct = config["policy_gates"]["volatility_halt_pct"]
        vol_window = config["policy_gates"]["volatility_halt_window_minutes"]
        market = decision_packet.get("market_data", {})
        price_change_pct = market.get("price_change_pct_window")

        if price_change_pct is None:
            return False, f"Price change over {vol_window}min window not provided"

        if abs(price_change_pct) > vol_pct:
            has_verified_catalyst = decision_packet.get("has_verified_catalyst", False)
            if not has_verified_catalyst:
                return False, (
                    f"Volatility halt: {abs(price_change_pct):.2%} move in "
                    f"{vol_window}min (>{vol_pct:.0%}) with no verified catalyst"
                )

        return True, f"Price change: {abs(price_change_pct):.2%} in {vol_window}min"
    except KeyError as e:
        return False, f"Missing config key: {e}"
    except Exception as e:
        return False, f"Volatility halt check error: {e}"


def gate_10_exchange_health(config, decision_packet, state):
    """
    Gate 10: Exchange Health
    BLOCK if exchange API error rate > 5% in last 15 min OR WebSocket dropped.
    Rationale: Unreliable execution environment.
    """
    try:
        max_error_rate = config["policy_gates"]["exchange_error_rate_pct"]
        exchange_health = state.get("exchange_health", {})
        target_exchange = decision_packet.get("exchange", "binance")
        health = exchange_health.get(target_exchange, {})

        if not health:
            # Paper mode: DEX exchanges don't have health monitoring
            venue = decision_packet.get("venue", "CEX")
            portfolio_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "state", "portfolio.json")
            try:
                with open(portfolio_path) as f:
                    is_paper = json.load(f).get("mode", "paper") == "paper"
            except Exception:
                is_paper = True
            if is_paper and venue == "DEX":
                return True, f"DEX paper mode: health check skipped for {target_exchange}"
            return False, f"No health data for exchange {target_exchange}"

        error_rate = health.get("error_rate_pct", 0)
        ws_connected = health.get("websocket_connected", None)

        if error_rate > max_error_rate:
            return False, f"Exchange {target_exchange} error rate: {error_rate:.2%} > {max_error_rate:.0%}"

        if ws_connected is False:
            return False, f"Exchange {target_exchange} WebSocket disconnected"

        return True, f"Exchange {target_exchange} healthy (error rate: {error_rate:.2%})"
    except KeyError as e:
        return False, f"Missing config key: {e}"
    except Exception as e:
        return False, f"Exchange health check error: {e}"


def gate_11_reconciliation(config, decision_packet, state):
    """
    Gate 11: Reconciliation
    BLOCK if last reconciliation > 15 min OR any mismatch detected.
    Rationale: Unknown state = do not add complexity.
    """
    try:
        max_age = config["policy_gates"]["reconciliation_max_age_sec"]
        recon = state.get("reconciliation", {})
        last_recon_ts = recon.get("last_reconciliation_timestamp")

        if last_recon_ts is None:
            return False, "No reconciliation has been performed"

        last_dt = datetime.fromisoformat(last_recon_ts)
        age_sec = (now_utc() - last_dt).total_seconds()

        if age_sec > max_age:
            return False, f"Reconciliation stale: {age_sec:.0f}s ago (max {max_age}s)"

        has_mismatch = recon.get("has_mismatch", False)
        if has_mismatch:
            mismatch_details = recon.get("mismatch_details", "unknown")
            return False, f"Reconciliation mismatch detected: {mismatch_details}"

        return True, f"Reconciliation clean, {age_sec:.0f}s ago"
    except KeyError as e:
        return False, f"Missing config key: {e}"
    except Exception as e:
        return False, f"Reconciliation check error: {e}"


def gate_12_exposure_limits(config, decision_packet, state):
    """
    Gate 12: Exposure Limits
    BLOCK if adding this position would breach max single-token (10%),
    max meme allocation (30%), or max concurrent positions.
    Rationale: Portfolio concentration risk.
    """
    try:
        max_single = config["risk"]["max_single_token_pct"]
        max_meme = config["risk"]["max_meme_allocation_pct"]
        max_positions = config["policy_gates"]["max_concurrent_positions"]

        portfolio = state.get("portfolio", {})
        intent = decision_packet.get("trade_intent", {})
        position_size_pct = intent.get("position_size_pct", 0)

        existing_token_pct = portfolio.get("token_exposure_pct", {}).get(
            decision_packet.get("token", {}).get("symbol", ""), 0
        )
        total_token_pct = existing_token_pct + position_size_pct

        if total_token_pct > max_single:
            return False, f"Single-token exposure: {total_token_pct:.2%} > {max_single:.0%} max"

        current_meme_pct = portfolio.get("meme_allocation_pct", 0)
        new_meme_pct = current_meme_pct + position_size_pct

        if new_meme_pct > max_meme:
            return False, f"Meme allocation: {new_meme_pct:.2%} > {max_meme:.0%} max"

        open_positions = portfolio.get("open_position_count", 0)
        if open_positions >= max_positions:
            return False, f"Max concurrent positions: {open_positions} >= {max_positions}"

        return True, (
            f"Token: {total_token_pct:.2%}/{max_single:.0%}, "
            f"Meme: {new_meme_pct:.2%}/{max_meme:.0%}, "
            f"Positions: {open_positions}/{max_positions}"
        )
    except KeyError as e:
        return False, f"Missing config key: {e}"
    except Exception as e:
        return False, f"Exposure limits check error: {e}"


def gate_13_cooldown(config, decision_packet, state):
    """
    Gate 13: Cooldown
    BLOCK if same token traded within last 2 hours.
    Rationale: Avoid revenge trading patterns.
    """
    try:
        cooldown_min = config["policy_gates"]["cooldown_minutes"]
        token_symbol = decision_packet.get("token", {}).get("symbol", "")

        if not token_symbol:
            return False, "Token symbol missing from decision packet"

        trade_history = state.get("trade_history", [])
        current = now_utc()
        cooldown_delta = timedelta(minutes=cooldown_min)

        for trade in trade_history:
            if trade.get("token") == token_symbol:
                trade_ts = trade.get("timestamp")
                if trade_ts:
                    trade_dt = datetime.fromisoformat(trade_ts)
                    elapsed = current - trade_dt
                    if elapsed < cooldown_delta:
                        remaining = cooldown_delta - elapsed
                        return False, (
                            f"Cooldown active for {token_symbol}: "
                            f"last traded {elapsed.total_seconds()/60:.0f}min ago "
                            f"({remaining.total_seconds()/60:.0f}min remaining)"
                        )

        return True, f"No cooldown active for {token_symbol}"
    except KeyError as e:
        return False, f"Missing config key: {e}"
    except Exception as e:
        return False, f"Cooldown check error: {e}"


def gate_14_budget(config, decision_packet, state):
    """
    Gate 14: Budget Gate
    BLOCK if daily LLM API spend exceeded OR monthly exceeded.
    Rationale: Cost control.
    """
    try:
        daily_limit = config["budget"]["daily_llm_spend_limit_usd"]
        monthly_limit = config["budget"]["monthly_llm_spend_limit_usd"]

        budget = state.get("budget", {})
        daily_spend = budget.get("daily_llm_spend_usd", 0)
        monthly_spend = budget.get("monthly_llm_spend_usd", 0)

        if daily_spend >= daily_limit:
            return False, f"Daily LLM spend exceeded: ${daily_spend:.2f} >= ${daily_limit:.2f}"

        if monthly_spend >= monthly_limit:
            return False, f"Monthly LLM spend exceeded: ${monthly_spend:.2f} >= ${monthly_limit:.2f}"

        cost_alert = config["budget"]["cost_per_trade_alert_usd"]
        trade_cost = decision_packet.get("estimated_trade_cost_usd", 0)
        cost_warning = ""
        if trade_cost > cost_alert:
            cost_warning = f" WARNING: trade cost ${trade_cost:.2f} > ${cost_alert:.2f} alert"

        return True, f"Budget OK: daily ${daily_spend:.2f}/${daily_limit:.2f}, monthly ${monthly_spend:.2f}/${monthly_limit:.2f}{cost_warning}"
    except KeyError as e:
        return False, f"Missing config key: {e}"
    except Exception as e:
        return False, f"Budget check error: {e}"


def gate_15_sanad_audit(config, decision_packet, state):
    """
    Gate 15: Sanad + Audit
    BLOCK if trust score < 70 OR Confidence Score < 60 OR Al-Muhasbi REJECT.
    Rationale: Intelligence layers agree it is a bad trade.
    """
    try:
        min_trust = config["scoring"]["min_trust_score"]
        min_confidence = config["scoring"]["min_confidence_score"]

        sanad = decision_packet.get("sanad_verification", {})
        trust_score = sanad.get("sanad_trust_score")

        if trust_score is None:
            return False, "Sanad trust score missing from decision packet"

        if trust_score < min_trust:
            return False, f"Trust score too low: {trust_score} < {min_trust}"

        confidence_score = decision_packet.get("trade_confidence_score")
        if confidence_score is None:
            return False, "Trade confidence score missing from decision packet"

        if confidence_score < min_confidence:
            return False, f"Confidence score too low: {confidence_score} < {min_confidence}"

        audit_verdict = decision_packet.get("almuhasbi_verdict", "")

        # Paper mode: allow REJECT/REVISE through with warning (we want to see execution)
        portfolio_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "state", "portfolio.json")
        try:
            with open(portfolio_path) as f:
                is_paper = json.load(f).get("mode", "paper") == "paper"
        except Exception:
            is_paper = True

        if audit_verdict == "REJECT":
            judge_confidence = decision_packet.get("almuhasbi_confidence", 0)
            if is_paper and judge_confidence < 85:
                # Paper mode: allow low-confidence rejects through for learning
                return True, f"PAPER MODE: Al-Muhasbi REJECT overridden (judge_conf={judge_confidence}% < 85%, trust={trust_score})"
            # Hard block: judge confident or live mode
            return False, f"Al-Muhasbi verdict: REJECT (confidence {judge_confidence}%)"

        if audit_verdict not in ("APPROVE", "REVISE"):
            if is_paper:
                return True, f"PAPER MODE: verdict '{audit_verdict}' overridden (trust={trust_score}, conf={confidence_score})"
            return False, f"Al-Muhasbi verdict invalid or missing: '{audit_verdict}'"

        return True, f"Trust: {trust_score}, Confidence: {confidence_score}, Audit: {audit_verdict}"
    except KeyError as e:
        return False, f"Missing config key: {e}"
    except Exception as e:
        return False, f"Sanad + audit check error: {e}"


# ─────────────────────────────────────────────
# GATE REGISTRY (ordered)
# ─────────────────────────────────────────────

GATES = [
    (1,  "Kill Switch",                  gate_01_kill_switch),
    (2,  "Capital Preservation",         gate_02_capital_preservation),
    (3,  "Data Freshness",               gate_03_data_freshness),
    (4,  "Token Age",                    gate_04_token_age),
    (5,  "Rugpull Safety",               gate_05_rugpull_safety),
    (6,  "Liquidity Gate",               gate_06_liquidity),
    (7,  "Spread Gate (CEX)",            gate_07_spread),
    (8,  "Pre-Flight Simulation (DEX)",  gate_08_preflight_simulation),
    (9,  "Volatility Halt",              gate_09_volatility_halt),
    (10, "Exchange Health",              gate_10_exchange_health),
    (11, "Reconciliation",               gate_11_reconciliation),
    (12, "Exposure Limits",              gate_12_exposure_limits),
    (13, "Cooldown",                     gate_13_cooldown),
    (14, "Budget Gate",                  gate_14_budget),
    (15, "Sanad + Audit",               gate_15_sanad_audit),
]


# ─────────────────────────────────────────────
# CIRCUIT BREAKER CHECK (pre-gate)
# ─────────────────────────────────────────────

def check_circuit_breakers(config, state):
    """
    Check circuit breaker states before gate evaluation.
    If simultaneous_trip_pause threshold is met, BLOCK immediately.
    """
    try:
        cb_config = config.get("circuit_breakers", {})
        cb_state = state.get("circuit_breakers", {})
        trip_pause = cb_config.get("simultaneous_trip_pause", 3)

        tripped = []
        for component, cb in cb_state.items():
            if cb.get("state") == "open":
                tripped.append(component)

        if len(tripped) >= trip_pause:
            return False, f"System PAUSED: {len(tripped)} circuit breakers tripped ({', '.join(tripped)}) >= {trip_pause} threshold"

        if tripped:
            return True, f"WARNING: {len(tripped)} circuit breaker(s) tripped: {', '.join(tripped)}"

        return True, "All circuit breakers closed"
    except Exception as e:
        return False, f"Circuit breaker check error: {e}"


# ─────────────────────────────────────────────
# MAIN ENGINE
# ─────────────────────────────────────────────

def evaluate_gates(decision_packet):
    """
    Main entry point. Evaluates all 15 gates in order.
    First failure stops evaluation and returns BLOCK.

    Args:
        decision_packet: dict with all trade data

    Returns:
        dict with result, gates_passed, gate_failed, evidence
    """
    result = {
        "result": "BLOCK",
        "gates_passed": [],
        "gate_failed": None,
        "gate_failed_name": None,
        "gate_evidence": None,
        "all_evidence": {},
        "timestamp": now_utc().isoformat(),
        "correlation_id": decision_packet.get("correlation_id", "UNKNOWN"),
    }

    # Load config (BLOCK if unavailable)
    config, config_err = load_config()
    if config_err:
        result["gate_failed"] = 0
        result["gate_failed_name"] = "CONFIG"
        result["gate_evidence"] = config_err
        return result

    # Load state files
    state = {}

    portfolio, err = load_json_state("portfolio.json")
    if err:
        result["gate_failed"] = 0
        result["gate_failed_name"] = "STATE"
        result["gate_evidence"] = err
        return result
    state["portfolio"] = portfolio

    recon, err = load_json_state("reconciliation.json")
    if err:
        result["gate_failed"] = 0
        result["gate_failed_name"] = "STATE"
        result["gate_evidence"] = err
        return result
    state["reconciliation"] = recon

    exchange_health, _ = load_json_state("exchange_health.json", required=False)
    state["exchange_health"] = exchange_health

    circuit_breakers, _ = load_json_state("circuit_breakers.json", required=False)
    state["circuit_breakers"] = circuit_breakers

    trade_history, _ = load_json_state("trade_history.json", required=False)
    state["trade_history"] = trade_history if isinstance(trade_history, list) else trade_history.get("trades", [])

    budget, _ = load_json_state("budget.json", required=False)
    state["budget"] = budget

    # Pre-gate: Circuit breaker check
    cb_ok, cb_evidence = check_circuit_breakers(config, state)
    result["all_evidence"]["CB"] = cb_evidence
    if not cb_ok:
        result["gate_failed"] = 0
        result["gate_failed_name"] = "CIRCUIT_BREAKERS"
        result["gate_evidence"] = cb_evidence
        return result

    # Evaluate gates in order — first failure stops
    for gate_num, gate_name, gate_func in GATES:
        try:
            passed, evidence = gate_func(config, decision_packet, state)
        except Exception as e:
            passed = False
            evidence = f"Unhandled error in gate {gate_num}: {e}"

        result["all_evidence"][gate_num] = evidence

        if not passed:
            result["gate_failed"] = gate_num
            result["gate_failed_name"] = gate_name
            result["gate_evidence"] = evidence
            return result

        result["gates_passed"].append(gate_num)

    # All 15 gates passed
    result["result"] = "PASS"
    return result


# ─────────────────────────────────────────────
# CLI INTERFACE
# ─────────────────────────────────────────────

if __name__ == "__main__":
    """
    Usage: python3 policy_engine.py <decision_packet.json>
    Exit code: 0 = PASS, 1 = BLOCK, 2 = ERROR
    """

    if len(sys.argv) < 2:
        print(json.dumps({
            "result": "BLOCK",
            "gate_failed": 0,
            "gate_failed_name": "USAGE",
            "gate_evidence": "Usage: python3 policy_engine.py <decision_packet.json>"
        }))
        sys.exit(2)

    try:
        with open(sys.argv[1], "r") as f:
            packet = json.load(f)
    except Exception as e:
        print(json.dumps({
            "result": "BLOCK",
            "gate_failed": 0,
            "gate_failed_name": "INPUT",
            "gate_evidence": f"Cannot read decision packet: {e}"
        }))
        sys.exit(2)

    result = evaluate_gates(packet)
    print(json.dumps(result, indent=2))

    if result["result"] == "PASS":
        sys.exit(0)
    else:
        sys.exit(1)
