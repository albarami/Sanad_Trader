#!/usr/bin/env python3
"""
Cost Tracker — API Call Logging & Daily Budget Monitoring

Tracks all LLM API calls with token usage and cost calculation.
Maintains both detailed append-only log and daily aggregated summary.

Usage:
    from cost_tracker import log_api_call
    
    # After any API call:
    log_api_call(
        model="claude-opus-4-6",
        input_tokens=1500,
        output_tokens=800,
        stage="sanad_verification",
        token_symbol="BTC",
        extra={"correlation_id": "abc123"}
    )
    
    # Test mode:
    python3 cost_tracker.py --test

Files:
    state/api_costs.jsonl      — Append-only log (timestamp, model, tokens, cost, stage)
    state/daily_cost.json      — Daily aggregated totals by model and stage
"""

import os
import sys
import json
import tempfile
import shutil
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
STATE_DIR = BASE_DIR / "state"
COSTS_LOG = STATE_DIR / "api_costs.jsonl"
DAILY_COST_FILE = STATE_DIR / "daily_cost.json"

# Pricing (per million tokens)
PRICING = {
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
    "gpt-5.2": {"flat": 0.03},  # flat per call
    "perplexity/sonar-pro": {"flat": 0.02},
    "perplexity/sonar-deep-research": {"flat": 0.15},
}

# Alias mapping for model name variations
MODEL_ALIASES = {
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "sonar-pro": "perplexity/sonar-pro",
    "sonar-deep-research": "perplexity/sonar-deep-research",
    "gpt-5.3": "gpt-5.2",  # fallback to same pricing
}


# ─────────────────────────────────────────────
# Cost Calculation
# ─────────────────────────────────────────────

def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Calculate cost in USD for an API call.
    
    Args:
        model: Model identifier (e.g., "claude-opus-4-6", "gpt-5.2")
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
    
    Returns:
        Cost in USD (float)
    """
    # Normalize model name
    model = MODEL_ALIASES.get(model, model)
    
    if model not in PRICING:
        print(f"[cost_tracker] Warning: Unknown model '{model}', assuming $0 cost")
        return 0.0
    
    pricing = PRICING[model]
    
    # Flat-rate models
    if "flat" in pricing:
        return pricing["flat"]
    
    # Token-based models
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    
    return round(input_cost + output_cost, 6)


# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

def log_api_call(
    model: str,
    input_tokens: int,
    output_tokens: int,
    stage: str,
    token_symbol: str = "",
    extra: dict = None
):
    """
    Log an API call to both append-only log and daily summary.
    
    Args:
        model: Model name (e.g., "claude-opus-4-6")
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        stage: Pipeline stage (e.g., "sanad_verification", "bull_debate")
        token_symbol: Trading token symbol (optional)
        extra: Additional metadata (optional)
    """
    timestamp = datetime.now(timezone.utc)
    cost_usd = calculate_cost(model, input_tokens, output_tokens)
    
    # Normalize model name for storage
    model = MODEL_ALIASES.get(model, model)
    
    # 1. Append to detailed log
    record = {
        "timestamp": timestamp.isoformat(),
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "stage": stage,
        "token_symbol": token_symbol,
    }
    
    if extra:
        record["extra"] = extra
    
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    
    with open(COSTS_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
    
    # 2. Update daily summary
    _update_daily_summary(timestamp, model, input_tokens, output_tokens, cost_usd, stage)


def _update_daily_summary(timestamp: datetime, model: str, input_tokens: int, output_tokens: int, cost: float, stage: str):
    """
    Update daily_cost.json with running totals.
    Handles date rollover (new day = reset totals).
    Uses atomic writes.
    """
    today = timestamp.date().isoformat()
    
    # Load existing daily summary
    if DAILY_COST_FILE.exists():
        with open(DAILY_COST_FILE, "r") as f:
            daily = json.load(f)
    else:
        daily = {
            "date": today,
            "total_usd": 0.0,
            "by_model": {},
            "by_stage": {},
            "updated_at": timestamp.isoformat()
        }
    
    # Check for date rollover
    if daily.get("date") != today:
        daily = {
            "date": today,
            "total_usd": 0.0,
            "by_model": {},
            "by_stage": {},
            "updated_at": timestamp.isoformat()
        }
    
    # Update totals
    daily["total_usd"] = round(daily["total_usd"] + cost, 6)
    daily["updated_at"] = timestamp.isoformat()
    
    # Update by_model
    if model not in daily["by_model"]:
        daily["by_model"][model] = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": 0.0
        }
    
    daily["by_model"][model]["calls"] += 1
    daily["by_model"][model]["input_tokens"] += input_tokens
    daily["by_model"][model]["output_tokens"] += output_tokens
    daily["by_model"][model]["cost"] = round(daily["by_model"][model]["cost"] + cost, 6)
    
    # Update by_stage
    if stage not in daily["by_stage"]:
        daily["by_stage"][stage] = {
            "calls": 0,
            "cost": 0.0
        }
    
    daily["by_stage"][stage]["calls"] += 1
    daily["by_stage"][stage]["cost"] = round(daily["by_stage"][stage]["cost"] + cost, 6)
    
    # Atomic write
    _atomic_write_json(DAILY_COST_FILE, daily)


def _atomic_write_json(path: Path, data: dict):
    """Write JSON file atomically using temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with tempfile.NamedTemporaryFile(
        mode='w',
        dir=path.parent,
        prefix=f".{path.name}.tmp.",
        delete=False
    ) as tmp:
        json.dump(data, tmp, indent=2)
        tmp_path = tmp.name
    
    shutil.move(tmp_path, path)


# ─────────────────────────────────────────────
# Query / Display
# ─────────────────────────────────────────────

def get_daily_summary() -> dict:
    """Load and return current daily_cost.json."""
    if not DAILY_COST_FILE.exists():
        return {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "total_usd": 0.0,
            "by_model": {},
            "by_stage": {},
        }
    
    with open(DAILY_COST_FILE, "r") as f:
        return json.load(f)


def print_daily_summary():
    """Print human-readable daily cost summary."""
    summary = get_daily_summary()
    
    print(f"\n{'='*60}")
    print(f"Daily API Cost Summary — {summary['date']}")
    print(f"{'='*60}")
    print(f"Total Cost: ${summary['total_usd']:.2f}")
    print(f"\nBy Model:")
    
    for model, stats in sorted(summary["by_model"].items(), key=lambda x: x[1]["cost"], reverse=True):
        print(f"  {model}:")
        print(f"    Calls: {stats['calls']}")
        print(f"    Tokens: {stats['input_tokens']:,} in / {stats['output_tokens']:,} out")
        print(f"    Cost: ${stats['cost']:.4f}")
    
    print(f"\nBy Stage:")
    for stage, stats in sorted(summary["by_stage"].items(), key=lambda x: x[1]["cost"], reverse=True):
        print(f"  {stage}: {stats['calls']} calls, ${stats['cost']:.4f}")
    
    print(f"\nUpdated: {summary.get('updated_at', 'N/A')}")
    print(f"{'='*60}\n")


# ─────────────────────────────────────────────
# Test Mode
# ─────────────────────────────────────────────

def run_test():
    """Test mode: Log some fake calls and print summary."""
    print("[cost_tracker] TEST MODE — Logging fake API calls...\n")
    
    # Simulate various calls
    test_calls = [
        ("claude-opus-4-6", 2000, 500, "sanad_verification", "BTC"),
        ("claude-opus-4-6", 1800, 600, "bull_debate", "ETH"),
        ("claude-opus-4-6", 1700, 550, "bear_debate", "ETH"),
        ("gpt-5.2", 0, 0, "judge", "ETH"),
        ("claude-haiku-4-5-20251001", 800, 200, "execution", "SOL"),
        ("perplexity/sonar-pro", 0, 0, "sanad_verification", "PEPE"),
        ("perplexity/sonar-deep-research", 0, 0, "deep_research", ""),
    ]
    
    for model, input_tok, output_tok, stage, symbol in test_calls:
        cost = calculate_cost(model, input_tok, output_tok)
        print(f"  Logging: {model} ({stage}) — ${cost:.4f}")
        log_api_call(model, input_tok, output_tok, stage, symbol, extra={"test": True})
    
    print("\n[cost_tracker] Test calls logged successfully.")
    print_daily_summary()


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if "--test" in sys.argv:
        run_test()
    else:
        print_daily_summary()
