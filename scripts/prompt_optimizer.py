#!/usr/bin/env python3
"""
Prompt Optimizer — Self-Improvement Component 3

Analyzes pattern analyses, current prompts, and trade outcomes to propose prompt improvements.
Runs weekly or every 50 closed trades. DOES NOT auto-apply changes — human approval required.

Process:
1. Load pattern analyses from genius-memory/patterns/
2. Load current prompts from prompts/
3. Load recent trade outcomes where agents were wrong
4. Send to Claude Opus for prompt revision proposals
5. Save proposals to genius-memory/strategy-evolution/prompt_update_NNN.json
6. Version prompts in genius-memory/strategy-evolution/prompt_versions/
7. Send diff to Telegram

Usage:
    python3 prompt_optimizer.py                 # Generate new proposals
    python3 prompt_optimizer.py --test          # Dry run (no API calls, no saves)
    python3 prompt_optimizer.py --apply NNN     # Apply specific update NNN
    python3 prompt_optimizer.py --revert        # Rollback to previous version
"""

import os
import sys
import json
import argparse
import shutil
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────
# Environment Setup
# ─────────────────────────────────────────────

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
SCRIPTS_DIR = BASE_DIR / "scripts"
PROMPTS_DIR = BASE_DIR / "prompts"
GENIUS_DIR = BASE_DIR / "genius-memory"
STATE_DIR = BASE_DIR / "state"

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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

EVOLUTION_DIR = GENIUS_DIR / "strategy-evolution"
VERSIONS_DIR = EVOLUTION_DIR / "prompt_versions"
STATE_FILE = STATE_DIR / "prompt_optimizer_state.json"

PROMPT_FILES = [
    "sanad-verifier.md",
    "bull-albaqarah.md",
    "bear-aldahhak.md",
    "judge-almuhasbi.md"
]

# ─────────────────────────────────────────────
# State Management
# ─────────────────────────────────────────────

def load_state() -> dict:
    """Load prompt optimizer state."""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "last_optimization_timestamp": None,
        "update_count": 0,
        "applied_updates": [],
        "version_history": []
    }


def save_state(state: dict):
    """Save prompt optimizer state."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ─────────────────────────────────────────────
# Data Loading
# ─────────────────────────────────────────────

def load_pattern_analyses() -> list:
    """Load recent pattern analyses."""
    patterns_dir = GENIUS_DIR / "patterns"
    if not patterns_dir.exists():
        return []
    
    # Get all batch files
    batch_files = sorted(patterns_dir.glob("batch_*.json"))
    
    # Load last 5 batches
    analyses = []
    for batch_file in batch_files[-5:]:
        try:
            with open(batch_file, "r") as f:
                analyses.append(json.load(f))
        except Exception:
            pass
    
    return analyses


def load_prompts() -> dict:
    """Load current prompts."""
    prompts = {}
    for filename in PROMPT_FILES:
        path = PROMPTS_DIR / filename
        if path.exists():
            with open(path, "r") as f:
                prompts[filename] = f.read()
    return prompts


def load_wrong_predictions() -> list:
    """Load trades where agents were wrong."""
    trade_history_file = STATE_DIR / "trade_history.json"
    if not trade_history_file.exists():
        return []
    
    with open(trade_history_file, "r") as f:
        data = json.load(f)
    
    trades = data.get("trades", data) if isinstance(data, dict) else data
    
    # Find trades where prediction was wrong
    wrong = []
    for t in trades:
        if not isinstance(t, dict) or t.get("status") != "closed":
            continue
        
        pnl = float(t.get("pnl_usd", t.get("net_pnl_usd", 0)) or 0)
        bull_conviction = t.get("bull_conviction", 0)
        bear_conviction = t.get("bear_conviction", 0)
        
        # Bull was wrong if high conviction but lost money
        if bull_conviction > 70 and pnl < -10:
            wrong.append({
                "token": t.get("token"),
                "agent": "bull",
                "conviction": bull_conviction,
                "pnl_usd": pnl,
                "reason": t.get("bull_thesis", ""),
                "timestamp": t.get("exit_time")
            })
        
        # Bear was right if high conviction and we avoided a loss
        # (This is harder to track since we don't record rejected trades in history)
    
    # Return last 20 wrong predictions
    return wrong[-20:]


# ─────────────────────────────────────────────
# API Calls
# ─────────────────────────────────────────────

def call_claude(system_prompt: str, user_message: str, model: str = "claude-haiku-4-5-20251001", test_mode: bool = False) -> str:
    """Call Claude API (direct or via OpenRouter fallback). Uses Haiku for cost efficiency."""
    if test_mode:
        print(f"    [TEST MODE] Would call Claude {model}")
        return json.dumps({
            "proposed_changes": {
                "bull-albaqarah.md": {
                    "change_summary": "[TEST] Add more skepticism for meme coins",
                    "rationale": "[TEST] Bull agent over-optimistic on memes",
                    "diff": "[TEST] + Check liquidity depth before bullish call"
                }
            },
            "priority": "medium",
            "expected_impact": "[TEST] Reduce false positives by ~15%"
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
                "max_tokens": 8000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            })
            
            req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=180) as resp:
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
                            stage="prompt_optimization",
                            extra={"script": "prompt_optimizer"}
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
                "max_tokens": 8000,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            })
            
            req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                choices = result.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "")
        except Exception as e:
            print(f"    [OpenRouter Claude failed: {e}]")
    
    raise RuntimeError("Claude API call failed (both direct and OpenRouter)")


# ─────────────────────────────────────────────
# Prompt Optimization
# ─────────────────────────────────────────────

def generate_prompt_proposals(test_mode: bool = False) -> dict:
    """Generate prompt revision proposals."""
    print(f"📊 Loading data for prompt optimization...")
    
    # Load all data
    patterns = load_pattern_analyses()
    print(f"    Loaded {len(patterns)} pattern analyses")
    
    prompts = load_prompts()
    print(f"    Loaded {len(prompts)} prompts")
    
    wrong = load_wrong_predictions()
    print(f"    Loaded {len(wrong)} wrong predictions\n")
    
    # Build context for Claude
    context = {
        "pattern_analyses": patterns,
        "current_prompts": {k: v[:500] + "..." for k, v in prompts.items()},  # Truncate for context
        "wrong_predictions": wrong
    }
    
    system_prompt = """You are a prompt engineering expert specializing in trading system optimization.

Your task: Analyze trading system performance data and propose specific improvements to agent prompts.

Focus on:
1. Patterns that consistently fail
2. Agent mistakes (over-optimism, under-skepticism)
3. Missing risk checks
4. Regime-specific weaknesses

Return JSON with:
- proposed_changes: Dict of {filename: {change_summary, rationale, diff}}
- priority: low/medium/high
- expected_impact: Quantitative estimate of improvement

Be specific with changes. Include exact text to add/remove/modify.
DO NOT make changes that would compromise safety or rigor."""
    
    user_message = f"""Here's the trading system performance data:

PATTERN ANALYSES (last 5 batches):
{json.dumps([p.get('patterns', {}) for p in patterns], indent=2)}

WRONG PREDICTIONS (where agents were wrong):
{json.dumps(wrong, indent=2)}

CURRENT PROMPTS (truncated):
{json.dumps(context['current_prompts'], indent=2)}

Propose specific prompt improvements to reduce errors and improve accuracy."""
    
    print(f"🤖 Sending to Claude Opus for prompt analysis...")
    try:
        response = call_claude(system_prompt, user_message, test_mode=test_mode)
        
        # Extract JSON from response
        if "```json" in response:
            json_start = response.find("```json") + 7
            json_end = response.find("```", json_start)
            response = response[json_start:json_end].strip()
        elif "```" in response:
            json_start = response.find("```") + 3
            json_end = response.find("```", json_start)
            response = response[json_start:json_end].strip()
        
        proposals = json.loads(response)
        print(f"    ✅ Proposals generated")
        return proposals
    except Exception as e:
        print(f"    ❌ Failed: {e}")
        raise


def save_proposals(proposals: dict, state: dict, test_mode: bool = False):
    """Save proposals to genius-memory."""
    if test_mode:
        print(f"    [TEST MODE] Would save proposals")
        return None
    
    update_num = state.get("update_count", 0) + 1
    
    EVOLUTION_DIR.mkdir(parents=True, exist_ok=True)
    
    proposal_file = EVOLUTION_DIR / f"prompt_update_{update_num:03d}.json"
    
    full_proposal = {
        "update_number": update_num,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "proposals": proposals,
        "status": "pending",
        "applied_at": None
    }
    
    with open(proposal_file, "w") as f:
        json.dump(full_proposal, f, indent=2)
    
    print(f"💾 Proposals saved: {proposal_file}")
    
    # Update state
    state["update_count"] = update_num
    state["last_optimization_timestamp"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    
    return update_num


def apply_update(update_num: int):
    """Apply a specific prompt update."""
    print(f"🔧 Applying update #{update_num}...")
    
    # Load proposal
    proposal_file = EVOLUTION_DIR / f"prompt_update_{update_num:03d}.json"
    if not proposal_file.exists():
        raise ValueError(f"Update #{update_num} not found")
    
    with open(proposal_file, "r") as f:
        proposal = json.load(f)
    
    if proposal.get("status") == "applied":
        print(f"⚠️  Update #{update_num} already applied")
        return
    
    # Version current prompts
    version_prompts()
    
    # Apply changes (this is simplified - real implementation would parse diffs)
    proposed_changes = proposal.get("proposals", {}).get("proposed_changes", {})
    
    print(f"⚠️  AUTO-APPLY NOT IMPLEMENTED")
    print(f"   Proposed changes for {len(proposed_changes)} files:")
    for filename, change in proposed_changes.items():
        print(f"   - {filename}: {change.get('change_summary', 'N/A')}")
    
    print(f"\n💡 Manual step: Review proposals and edit prompts manually")
    print(f"   Then mark as applied with: UPDATE STATUS IN JSON")
    
    # Mark as applied
    proposal["status"] = "applied"
    proposal["applied_at"] = datetime.now(timezone.utc).isoformat()
    
    with open(proposal_file, "w") as f:
        json.dump(proposal, f, indent=2)
    
    print(f"✅ Update #{update_num} marked as applied")


def version_prompts():
    """Version current prompts before applying changes."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    version_dir = VERSIONS_DIR / timestamp
    version_dir.mkdir(parents=True, exist_ok=True)
    
    for filename in PROMPT_FILES:
        src = PROMPTS_DIR / filename
        if src.exists():
            dst = version_dir / filename
            shutil.copy2(src, dst)
    
    print(f"📦 Prompts versioned: {version_dir}")


def revert_to_previous():
    """Rollback to previous prompt version."""
    print(f"⏮️  Reverting to previous version...")
    
    # Find latest version
    versions = sorted(VERSIONS_DIR.glob("*"))
    if not versions:
        print(f"❌ No versions found")
        return
    
    latest = versions[-1]
    print(f"   Reverting to: {latest.name}")
    
    # Copy back
    for filename in PROMPT_FILES:
        src = latest / filename
        if src.exists():
            dst = PROMPTS_DIR / filename
            shutil.copy2(src, dst)
            print(f"   ✅ Reverted {filename}")
    
    print(f"✅ Revert complete")


# ─────────────────────────────────────────────
# Main Pipeline
# ─────────────────────────────────────────────

def run_optimization(test_mode: bool = False):
    """Main prompt optimization pipeline."""
    print(f"{'='*60}")
    print(f"Prompt Optimizer — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}\n")
    
    if test_mode:
        print("⚠️  TEST MODE: No API calls, no file saves, no Telegram\n")
    
    state = load_state()
    
    # Generate proposals
    try:
        proposals = generate_prompt_proposals(test_mode)
    except Exception as e:
        print(f"\n❌ Failed to generate proposals: {e}")
        return None
    
    # Save proposals
    update_num = save_proposals(proposals, state, test_mode)
    
    if not test_mode and update_num:
        # Send Telegram notification
        if HAS_NOTIFIER:
            msg = _build_notification(proposals, update_num)
            try:
                notifier.send(msg, level='L2')
                print(f"📱 Telegram notification sent")
            except Exception as e:
                print(f"⚠️  Telegram send failed: {e}")
    
    print(f"\n{'='*60}")
    print(f"✅ Optimization complete")
    if update_num:
        print(f"   Proposals saved as update #{update_num}")
        print(f"   Review and apply with: --apply {update_num}")
    print(f"{'='*60}")
    
    return proposals


def _build_notification(proposals: dict, update_num: int) -> str:
    """Build Telegram notification with diff."""
    proposed_changes = proposals.get("proposed_changes", {})
    priority = proposals.get("priority", "unknown")
    impact = proposals.get("expected_impact", "unknown")
    
    msg = f"*Prompt Optimization Update #{update_num}*\n\n"
    msg += f"📊 Priority: {priority.upper()}\n"
    msg += f"💡 Expected Impact: {impact}\n\n"
    
    msg += f"*Proposed Changes:*\n"
    for filename, change in list(proposed_changes.items())[:3]:  # Top 3
        summary = change.get("change_summary", "N/A")
        msg += f"• {filename}: {summary}\n"
    
    msg += f"\n⚠️ Review required before applying"
    
    return msg


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Prompt Optimizer")
    parser.add_argument("--test", action="store_true", help="Dry run (no API calls, no saves)")
    parser.add_argument("--apply", type=int, metavar="NNN", help="Apply specific update NNN")
    parser.add_argument("--revert", action="store_true", help="Rollback to previous version")
    args = parser.parse_args()
    
    try:
        if args.apply:
            apply_update(args.apply)
        elif args.revert:
            revert_to_previous()
        else:
            run_optimization(test_mode=args.test)
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
