#!/usr/bin/env python3
"""
Pattern Extractor — Self-Improvement Component 2

Analyzes closed trades to extract winning/losing patterns, agent accuracy, and strategy performance.
Runs every 6 hours. Only triggers analysis when 20+ new closed trades since last run.

Process:
1. Load state/trade_history.json
2. Check if 20+ new closed trades since last analysis
3. Send trade batch to Claude Opus for pattern extraction
4. Validate patterns with GPT for statistical significance
5. Save to genius-memory/patterns/batch_NNN.json
6. Send Telegram notification with key findings

Usage:
    python3 pattern_extractor.py          # Run analysis
    python3 pattern_extractor.py --test   # Dry run (no API calls, no save, no Telegram)
"""

import os
import sys
import json
import argparse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────
# Environment Setup
# ─────────────────────────────────────────────

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
SCRIPTS_DIR = BASE_DIR / "scripts"
STATE_DIR = BASE_DIR / "state"
GENIUS_DIR = BASE_DIR / "genius-memory"

sys.path.insert(0, str(SCRIPTS_DIR))

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / "config" / ".env")
except Exception:
    pass

try:
    import notifier
    HAS_NOTIFIER = True
except ImportError:
    HAS_NOTIFIER = False

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

STATE_FILE = STATE_DIR / "pattern_extractor_state.json"
TRADE_HISTORY_FILE = STATE_DIR / "trade_history.json"
BATCH_THRESHOLD = 20  # Minimum new trades to trigger analysis

# ─────────────────────────────────────────────
# State Management
# ─────────────────────────────────────────────

def load_state() -> dict:
    """Load pattern extractor state."""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "last_analyzed_count": 0,
        "last_analysis_timestamp": None,
        "batch_count": 0
    }


def save_state(state: dict):
    """Save pattern extractor state."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def load_trade_history() -> list:
    """Load closed trades from state/trade_history.json."""
    if not TRADE_HISTORY_FILE.exists():
        return []
    
    with open(TRADE_HISTORY_FILE, "r") as f:
        data = json.load(f)
    
    # Handle both list and dict formats
    if isinstance(data, list):
        trades = data
    elif isinstance(data, dict):
        trades = data.get("trades", [])
    else:
        trades = []
    
    # Filter closed trades only
    closed = [t for t in trades if isinstance(t, dict) and t.get("status") == "closed"]
    return closed


# ─────────────────────────────────────────────
# API Calls
# ─────────────────────────────────────────────

def call_claude(system_prompt: str, user_message: str, model: str = "claude-haiku-4-5-20251001", test_mode: bool = False) -> str:
    """Call Claude API (direct or via OpenRouter fallback). Uses Haiku for cost efficiency."""
    if test_mode:
        print(f"    [TEST MODE] Would call Claude {model}")
        return json.dumps({
            "winning_patterns": ["[TEST] Momentum trades in bull regime"],
            "losing_patterns": ["[TEST] Mean reversion in high volatility"],
            "bull_agent_accuracy": 72.5,
            "bear_agent_accuracy": 68.3,
            "source_reliability": {"coingecko": 0.65, "onchain": 0.78},
            "strategy_performance": {"momentum": 0.58, "mean_reversion": 0.42},
            "regime_insights": "[TEST] Bull trends favor momentum",
            "recommended_changes": ["Increase momentum weight in bull regimes"]
        })
    
    # Try direct Anthropic API first
    if ANTHROPIC_API_KEY:
        try:
            url = "https://api.anthropic.com/v1/messages"
            headers = {
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            }
            body = json.dumps({
                "model": model,
                "max_tokens": 4000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            })
            
            req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("content") and len(result["content"]) > 0:
                    # Track cost
                    usage = result.get("usage", {})
                    if usage:
                        from cost_tracker import log_api_call
                        log_api_call(
                            model=model,
                            input_tokens=usage.get("input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0),
                            stage="pattern_extraction",
                            extra={"script": "pattern_extractor"}
                        )
                    return result["content"][0].get("text", "")
        except Exception as e:
            print(f"    [Claude direct failed: {e}]")
    
    # Fallback to OpenRouter
    if OPENROUTER_API_KEY:
        try:
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            }
            body = json.dumps({
                "model": f"anthropic/{model}",
                "max_tokens": 4000,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            })
            
            req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                choices = result.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "")
        except Exception as e:
            print(f"    [OpenRouter Claude failed: {e}]")
    
    raise RuntimeError("Claude API call failed (both direct and OpenRouter)")


def call_openai(system_prompt: str, user_message: str, model: str = "gpt-5.2", test_mode: bool = False) -> str:
    """Call OpenAI API (direct or via OpenRouter fallback)."""
    if test_mode:
        print(f"    [TEST MODE] Would call OpenAI {model}")
        return json.dumps({
            "validated_patterns": ["Momentum trades in bull regime (p<0.05)"],
            "insufficient_data": ["Mean reversion (n=5, need n>=10)"],
            "statistical_confidence": "moderate",
            "sample_size_warnings": ["Bear agent only 12 trades, need 20+"]
        })
    
    # Try direct OpenAI API first
    if OPENAI_API_KEY:
        try:
            url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENAI_API_KEY}",
            }
            body = json.dumps({
                "model": model,
                "max_completion_tokens": 2000,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            })
            
            req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=90) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                choices = result.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "")
        except Exception as e:
            print(f"    [OpenAI direct failed: {e}]")
    
    # Fallback to OpenRouter
    if OPENROUTER_API_KEY:
        try:
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            }
            body = json.dumps({
                "model": f"openai/{model}",
                "max_tokens": 2000,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            })
            
            req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=90) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                choices = result.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "")
        except Exception as e:
            print(f"    [OpenRouter OpenAI failed: {e}]")
    
    raise RuntimeError("OpenAI API call failed (both direct and OpenRouter)")


# ─────────────────────────────────────────────
# Pattern Extraction
# ─────────────────────────────────────────────

def extract_patterns(trades: list, test_mode: bool = False) -> dict:
    """Use Claude Opus to extract patterns from trade batch."""
    print(f"📊 Extracting patterns from {len(trades)} trades...")
    
    # Build trade summary for Claude
    trade_summary = []
    for t in trades:
        trade_summary.append({
            "token": t.get("token"),
            "entry_price": t.get("entry_price"),
            "exit_price": t.get("exit_price"),
            "pnl_pct": t.get("pnl_pct", t.get("pnl_percent")),
            "pnl_usd": t.get("pnl_usd", t.get("net_pnl_usd")),
            "hold_time_hours": t.get("hold_time_hours", 0),
            "source": t.get("source"),
            "strategy": t.get("strategy"),
            "trust_score": t.get("trust_score", t.get("sanad_trust_score")),
            "regime": t.get("regime"),
            "bull_conviction": t.get("bull_conviction"),
            "bear_conviction": t.get("bear_conviction"),
        })
    
    system_prompt = """You are a quantitative trading analyst specializing in crypto pattern recognition.
Analyze the provided trade batch and extract actionable patterns.

Return JSON with:
- winning_patterns: List of patterns that consistently produce profits
- losing_patterns: List of patterns that consistently produce losses
- bull_agent_accuracy: % of bull predictions that were profitable
- bear_agent_accuracy: % of bear rejections that saved money
- source_reliability: Dict of source → win rate
- strategy_performance: Dict of strategy → win rate
- regime_insights: How different regimes affected performance
- recommended_changes: Specific actionable changes to improve performance

Be quantitative. Include sample sizes and confidence levels."""
    
    user_message = f"Analyze these {len(trades)} closed trades:\n\n{json.dumps(trade_summary, indent=2)}"
    
    try:
        response = call_claude(system_prompt, user_message, test_mode=test_mode)
        # Extract JSON from response
        # Try to find JSON block
        if "```json" in response:
            json_start = response.find("```json") + 7
            json_end = response.find("```", json_start)
            response = response[json_start:json_end].strip()
        elif "```" in response:
            json_start = response.find("```") + 3
            json_end = response.find("```", json_start)
            response = response[json_start:json_end].strip()
        
        patterns = json.loads(response)
        print(f"    ✅ Pattern extraction complete")
        return patterns
    except Exception as e:
        print(f"    ❌ Pattern extraction failed: {e}")
        raise


def validate_patterns(patterns: dict, test_mode: bool = False) -> dict:
    """Use GPT to validate statistical significance of patterns."""
    print(f"🔬 Validating statistical significance...")
    
    system_prompt = """You are a statistical analyst. Review the trading patterns extracted by another agent.
Check for:
- Sample size adequacy (need n>=10 for confidence)
- Statistical significance (p<0.05)
- Overfitting risks
- Confounding variables

Return JSON with:
- validated_patterns: Patterns with sufficient statistical confidence
- insufficient_data: Patterns that need more data
- statistical_confidence: overall confidence level (low/moderate/high)
- sample_size_warnings: Specific warnings about sample sizes"""
    
    user_message = f"Validate these patterns:\n\n{json.dumps(patterns, indent=2)}"
    
    try:
        response = call_openai(system_prompt, user_message, test_mode=test_mode)
        # Extract JSON from response
        if "```json" in response:
            json_start = response.find("```json") + 7
            json_end = response.find("```", json_start)
            response = response[json_start:json_end].strip()
        elif "```" in response:
            json_start = response.find("```") + 3
            json_end = response.find("```", json_start)
            response = response[json_start:json_end].strip()
        
        validation = json.loads(response)
        print(f"    ✅ Validation complete")
        return validation
    except Exception as e:
        print(f"    ❌ Validation failed: {e}")
        raise


# ─────────────────────────────────────────────
# Main Pipeline
# ─────────────────────────────────────────────

def run_pattern_extraction(test_mode: bool = False):
    """Main pattern extraction pipeline."""
    print(f"{'='*60}")
    print(f"Pattern Extractor — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}\n")
    
    if test_mode:
        print("⚠️  TEST MODE: No API calls, no file saves, no Telegram\n")
    
    # Load state
    state = load_state()
    last_count = state.get("last_analyzed_count", 0)
    batch_count = state.get("batch_count", 0)
    
    # Load trades
    print("📂 Loading trade history...")
    trades = load_trade_history()
    total_trades = len(trades)
    print(f"    Total closed trades: {total_trades}")
    print(f"    Previously analyzed: {last_count}")
    
    new_trades = total_trades - last_count
    print(f"    New trades since last analysis: {new_trades}\n")
    
    # Check threshold
    if new_trades < BATCH_THRESHOLD:
        print(f"⏸️  Only {new_trades} new trades (need {BATCH_THRESHOLD})")
        print(f"   Waiting for more trades before analysis.\n")
        return None
    
    # Get new trades
    new_trade_batch = trades[last_count:] if last_count > 0 else trades
    
    print(f"🔍 Analyzing batch of {len(new_trade_batch)} new trades...\n")
    
    # Extract patterns
    try:
        patterns = extract_patterns(new_trade_batch, test_mode)
    except Exception as e:
        print(f"\n❌ Pattern extraction failed: {e}")
        return None
    
    # Validate patterns
    try:
        validation = validate_patterns(patterns, test_mode)
    except Exception as e:
        print(f"\n❌ Validation failed: {e}")
        validation = {"error": str(e)}
    
    # Build final report
    report = {
        "batch_number": batch_count + 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trades_analyzed": len(new_trade_batch),
        "total_trades_to_date": total_trades,
        "patterns": patterns,
        "validation": validation,
        "test_mode": test_mode
    }
    
    if not test_mode:
        # Save to genius-memory
        patterns_dir = GENIUS_DIR / "patterns"
        patterns_dir.mkdir(parents=True, exist_ok=True)
        
        batch_file = patterns_dir / f"batch_{batch_count + 1:03d}.json"
        with open(batch_file, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n💾 Saved: {batch_file}")
        
        # Update state
        state["last_analyzed_count"] = total_trades
        state["last_analysis_timestamp"] = datetime.now(timezone.utc).isoformat()
        state["batch_count"] = batch_count + 1
        save_state(state)
        print(f"💾 State updated")
        
        # Send Telegram notification
        if HAS_NOTIFIER:
            msg = _build_notification(report)
            try:
                notifier.send(msg, level='L2')
                print(f"📱 Telegram notification sent")
            except Exception as e:
                print(f"⚠️  Telegram send failed: {e}")
    
    print(f"\n{'='*60}")
    print(f"✅ Pattern extraction complete")
    print(f"{'='*60}")
    
    return report


def _build_notification(report: dict) -> str:
    """Build Telegram notification message."""
    batch_num = report.get("batch_number", 0)
    trades = report.get("trades_analyzed", 0)
    patterns = report.get("patterns", {})
    
    winning = patterns.get("winning_patterns", [])
    losing = patterns.get("losing_patterns", [])
    changes = patterns.get("recommended_changes", [])
    
    msg = f"*Pattern Analysis Batch #{batch_num}*\n\n"
    msg += f"📊 Analyzed {trades} trades\n\n"
    
    if winning:
        msg += f"✅ *Winning Patterns:*\n"
        for p in winning[:3]:  # Top 3
            msg += f"  • {p}\n"
        msg += "\n"
    
    if losing:
        msg += f"❌ *Losing Patterns:*\n"
        for p in losing[:3]:  # Top 3
            msg += f"  • {p}\n"
        msg += "\n"
    
    if changes:
        msg += f"💡 *Recommended Changes:*\n"
        for c in changes[:3]:  # Top 3
            msg += f"  • {c}\n"
    
    return msg


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pattern Extractor")
    parser.add_argument("--test", action="store_true", help="Dry run (no API calls, no saves)")
    args = parser.parse_args()
    
    try:
        run_pattern_extraction(test_mode=args.test)
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
