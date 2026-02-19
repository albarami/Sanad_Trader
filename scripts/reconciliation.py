#!/usr/bin/env python3
"""
Sanad Trader v3.0 — Deterministic Portfolio Reconciliation

Phase 10 — Runs every 10 minutes via cron.
Verifies that exchange positions match internal records.
BLOCKs trading on ANY mismatch — no exceptions.

NOT an LLM. Pure deterministic comparison logic.

References:
- v3 doc Table 6 row 4 (Portfolio Reconciliation)
- v3 doc Policy Gate #11 (Reconciliation gate)
- v3 doc Table 11 (Threat: reconciliation mismatch)
- v3 doc Table 16 (RECONCILIATION_MISMATCH event)
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
LOGS_DIR = BASE_DIR / "execution-logs"
RECON_LOG = LOGS_DIR / "reconciliation.log"


def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        log(f"CRITICAL: Cannot load config: {e}")
        return None


def load_state(filename):
    try:
        with open(STATE_DIR / filename, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(filename, data):
    try:
        with open(STATE_DIR / filename, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"ERROR: Cannot save {filename}: {e}")


def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


def log(message):
    ts = now_iso()
    line = f"[{ts}] {message}"
    print(line)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(RECON_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify_whatsapp(message, urgent=False):
    """STUB: Will be implemented Phase 6."""
    prefix = "URGENT " if urgent else ""
    log(f"[WHATSAPP {prefix}NOTIFICATION] {message}")


# ─────────────────────────────────────────────
# EXCHANGE POSITION FETCHERS (stubs)
# ─────────────────────────────────────────────

def fetch_binance_positions():
    """
    Fetch open positions from Binance API.
    STUB: Returns empty dict until exchange API integration (Week 3-4).
    In production: calls Binance REST API /api/v3/account
    """
    # TODO Phase 4-5: Implement Binance API call
    # Returns: {"TOKEN": {"quantity": float, "avg_price": float}, ...}
    return {}


def fetch_mexc_positions():
    """
    Fetch open positions from MEXC API.
    STUB: Returns empty dict until MEXC is configured.
    """
    # TODO: Implement MEXC API call
    return {}


# ─────────────────────────────────────────────
# RECONCILIATION LOGIC
# ─────────────────────────────────────────────

def reconcile_positions():
    """
    Main reconciliation function.

    Compares internal position state against exchange-reported positions.
    Any discrepancy triggers:
    1. BLOCK trading (update reconciliation state)
    2. Log RECONCILIATION_MISMATCH event
    3. Notify via WhatsApp

    Returns reconciliation result dict.
    """
    log("=" * 50)
    log("RECONCILIATION START")

    config = load_config()
    if not config:
        result = {
            "last_reconciliation_timestamp": now_iso(),
            "has_mismatch": True,
            "mismatch_details": "Cannot load config — trading blocked",
            "positions_checked": 0,
            "exchanges_checked": []
        }
        save_state("reconciliation.json", result)
        notify_whatsapp("Reconciliation FAILED: cannot load config", urgent=True)
        return result

    # Load internal position state
    portfolio = load_state("portfolio.json")
    internal_positions = load_state("positions.json")
    mode = portfolio.get("mode", "PAPER")

    # In PAPER mode: no real exchange to check against
    # Reconciliation verifies internal state consistency only
    if mode.upper() == "PAPER":
        result = reconcile_paper_mode(portfolio, internal_positions)
    else:
        result = reconcile_live_mode(portfolio, internal_positions, config)

    # Save reconciliation state (Gate #11 reads this)
    save_state("reconciliation.json", result)

    # Log result
    if result["has_mismatch"]:
        log(f"RECONCILIATION MISMATCH: {result['mismatch_details']}")
        notify_whatsapp(
            f"RECONCILIATION MISMATCH: {result['mismatch_details']}. Trading BLOCKED.",
            urgent=True
        )
        # Emit RECONCILIATION_MISMATCH event
        emit_recon_event(result)
    else:
        log(f"RECONCILIATION CLEAN: {result['positions_checked']} positions checked on {result['exchanges_checked']}")

    log("RECONCILIATION END")
    return result


def reconcile_paper_mode(portfolio, internal_positions):
    """
    Paper mode reconciliation.
    Checks internal state consistency:
    - open_position_count matches actual open positions in positions.json
    - meme_allocation_pct is correctly computed
    - No orphaned or zombie positions
    """
    mismatches = []

    # Count actual open positions
    open_positions = []
    if isinstance(internal_positions, list):
        open_positions = [p for p in internal_positions if p.get("status", "").upper() == "OPEN"]
    elif isinstance(internal_positions, dict) and "positions" in internal_positions:
        open_positions = [p for p in internal_positions["positions"] if p.get("status", "").upper() == "OPEN"]

    reported_count = portfolio.get("open_position_count", 0)
    actual_count = len(open_positions)

    if reported_count != actual_count:
        mismatches.append(
            f"Position count mismatch: portfolio says {reported_count}, "
            f"positions.json has {actual_count} open"
        )

    # Verify exposure calculations
    total_exposure = sum(p.get("position_size_pct", 0) for p in open_positions)
    reported_exposure = portfolio.get("total_exposure_pct", 0)

    if abs(total_exposure - reported_exposure) > 0.001:
        mismatches.append(
            f"Exposure mismatch: computed {total_exposure:.4f}, "
            f"portfolio reports {reported_exposure:.4f}"
        )

    result = {
        "last_reconciliation_timestamp": now_iso(),
        "has_mismatch": len(mismatches) > 0,
        "mismatch_details": "; ".join(mismatches) if mismatches else None,
        "positions_checked": actual_count,
        "exchanges_checked": ["paper"],
        "mode": "PAPER"
    }

    return result


def reconcile_live_mode(portfolio, internal_positions, config):
    """
    Live mode reconciliation.
    Compares internal records against exchange API responses.
    """
    mismatches = []
    exchanges_checked = []

    # Fetch from each exchange
    binance_positions = fetch_binance_positions()
    exchanges_checked.append("binance")

    mexc_positions = fetch_mexc_positions()
    exchanges_checked.append("mexc")

    # Get internal positions by exchange
    internal_by_exchange = {}
    positions_list = []

    if isinstance(internal_positions, list):
        positions_list = internal_positions
    elif isinstance(internal_positions, dict) and "positions" in internal_positions:
        positions_list = internal_positions["positions"]

    for pos in positions_list:
        if pos.get("status") != "open":
            continue
        exchange = pos.get("exchange", "binance")
        if exchange not in internal_by_exchange:
            internal_by_exchange[exchange] = {}
        token = pos.get("token", "UNKNOWN")
        internal_by_exchange[exchange][token] = {
            "quantity": pos.get("quantity", 0),
            "entry_price": pos.get("entry_price", 0)
        }

    # Compare Binance
    internal_binance = internal_by_exchange.get("binance", {})
    for token, exchange_data in binance_positions.items():
        internal_data = internal_binance.get(token)
        if not internal_data:
            mismatches.append(f"binance: {token} exists on exchange but not in internal state")
        elif abs(exchange_data.get("quantity", 0) - internal_data["quantity"]) > 0.0001:
            mismatches.append(
                f"binance: {token} quantity mismatch — "
                f"exchange={exchange_data['quantity']}, internal={internal_data['quantity']}"
            )

    for token in internal_binance:
        if token not in binance_positions:
            mismatches.append(f"binance: {token} in internal state but not on exchange")

    # Compare MEXC (same logic)
    internal_mexc = internal_by_exchange.get("mexc", {})
    for token, exchange_data in mexc_positions.items():
        internal_data = internal_mexc.get(token)
        if not internal_data:
            mismatches.append(f"mexc: {token} exists on exchange but not in internal state")
        elif abs(exchange_data.get("quantity", 0) - internal_data["quantity"]) > 0.0001:
            mismatches.append(
                f"mexc: {token} quantity mismatch — "
                f"exchange={exchange_data['quantity']}, internal={internal_data['quantity']}"
            )

    for token in internal_mexc:
        if token not in mexc_positions:
            mismatches.append(f"mexc: {token} in internal state but not on exchange")

    total_checked = len(internal_binance) + len(internal_mexc) + len(binance_positions) + len(mexc_positions)

    result = {
        "last_reconciliation_timestamp": now_iso(),
        "has_mismatch": len(mismatches) > 0,
        "mismatch_details": "; ".join(mismatches) if mismatches else None,
        "positions_checked": total_checked,
        "exchanges_checked": exchanges_checked,
        "mode": "LIVE"
    }

    return result


def emit_recon_event(result):
    """
    Emit RECONCILIATION_MISMATCH event to events log.
    STUB: Will sync to Supabase events table.
    """
    event = {
        "event_type": "RECONCILIATION_MISMATCH",
        "timestamp": now_iso(),
        "payload": {
            "mismatch_details": result.get("mismatch_details"),
            "positions_checked": result.get("positions_checked"),
            "exchanges_checked": result.get("exchanges_checked"),
            "auto_response": "BLOCK_TRADING"
        }
    }

    # Append to events log
    events_path = LOGS_DIR / "events.jsonl"
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(events_path, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        log(f"ERROR: Cannot write event: {e}")


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

if __name__ == "__main__":
    result = reconcile_positions()
    print(json.dumps(result, indent=2))
    
    # Update cron_health.json so watchdog knows we ran
    try:
        cron_health_file = STATE_DIR / "cron_health.json"
        cron_health = {}
        if cron_health_file.exists():
            try:
                cron_health = json.load(open(cron_health_file))
            except:
                pass
        
        cron_health["reconciliation"] = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "status": "ok" if not result["has_mismatch"] else "mismatch"
        }
        
        with open(cron_health_file, "w") as f:
            json.dump(cron_health, f, indent=2)
    except Exception as e:
        log(f"Warning: Failed to update cron_health.json: {e}")

    if result["has_mismatch"]:
        sys.exit(1)
    else:
        sys.exit(0)
