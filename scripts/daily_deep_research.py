#!/usr/bin/env python3
"""
Daily Deep Research — Self-Improvement Component 1

Uses Perplexity sonar-deep-research via OpenRouter to gather three intelligence reports daily:
1. Alpha Discovery — High-return strategies, momentum setups, meme narratives, whale patterns
2. Regime Intelligence — Current market conditions, BTC trends, altcoin rotation, funding rates
3. Risk Radar — Upcoming threats, token unlocks, regulatory risks, smart contract vulnerabilities

Output:
- Full response: reports/daily-research/YYYY-MM-DD.json
- Condensed latest: genius-memory/research/latest.json (overwrite daily)
- Telegram L2 notification with highlights

Usage:
    python3 daily_deep_research.py          # Run full research
    python3 daily_deep_research.py --test   # Dry run (no API calls, no save, no Telegram)
"""

import os
import sys
import json
import argparse
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────
# Environment Setup
# ─────────────────────────────────────────────

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
SCRIPTS_DIR = BASE_DIR / "scripts"
REPORTS_DIR = BASE_DIR / "reports"
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

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")

# ─────────────────────────────────────────────
# Query Templates
# ─────────────────────────────────────────────

QUERIES = {
    "alpha_discovery": """What cryptocurrency trading strategies are producing the highest returns this week? Focus on: momentum setups, mean reversion patterns, meme coin narratives gaining traction, whale accumulation patterns, and any emerging DeFi opportunities. Be specific with token names, timeframes, and entry conditions.""",
    
    "regime_intelligence": """Current crypto market conditions analysis: BTC trend direction and key support/resistance levels, altcoin rotation patterns, funding rates across major exchanges, stablecoin flows, Fear and Greed index trend, and institutional positioning. What regime are we in and what historically works best in this regime?""",
    
    "risk_radar": """What are the biggest risks in crypto markets right now? Upcoming token unlocks, regulatory threats, exchange solvency concerns, smart contract vulnerabilities discovered this week, and any macro events (FOMC, CPI, etc.) that could cause volatility. Include specific dates and tokens affected."""
}

# ─────────────────────────────────────────────
# API Calls
# ─────────────────────────────────────────────

def call_perplexity_deep_research(query: str, test_mode: bool = False) -> dict:
    """Call Perplexity sonar-deep-research via OpenRouter."""
    if test_mode:
        print(f"    [TEST MODE] Would call Perplexity with query: {query[:100]}...")
        return {
            "response": f"[TEST] Mock response for query: {query[:50]}...",
            "model": "perplexity/sonar-deep-research",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY not set in environment")
    
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    }
    
    body = json.dumps({
        "model": "perplexity/sonar-deep-research",
        "messages": [
            {
                "role": "system",
                "content": "You are a crypto intelligence analyst. Provide detailed, factual, actionable insights with specific token names, dates, and data points. Include sources when possible."
            },
            {
                "role": "user",
                "content": query
            }
        ],
        "max_tokens": 3000,
    })
    
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            choices = result.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "")
                return {
                    "response": text,
                    "model": "perplexity/sonar-deep-research",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "usage": result.get("usage", {})
                }
        raise ValueError("No response from Perplexity")
    except Exception as e:
        raise RuntimeError(f"Perplexity API call failed: {e}")


# ─────────────────────────────────────────────
# Research Pipeline
# ─────────────────────────────────────────────

def run_daily_research(test_mode: bool = False):
    """Execute all three research queries and save results."""
    print(f"{'='*60}")
    print(f"Daily Deep Research — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}\n")
    
    if test_mode:
        print("⚠️  TEST MODE: No API calls, no file saves, no Telegram\n")
    
    results = {}
    
    # Run each query
    for query_name, query_text in QUERIES.items():
        print(f"📊 {query_name.replace('_', ' ').title()}...")
        try:
            result = call_perplexity_deep_research(query_text, test_mode)
            results[query_name] = result
            print(f"    ✅ Complete ({len(result['response'])} chars)\n")
        except Exception as e:
            print(f"    ❌ Failed: {e}\n")
            results[query_name] = {
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
    
    # Build full report
    report = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "queries": results,
        "test_mode": test_mode
    }
    
    if not test_mode:
        # Save full report
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_research_dir = REPORTS_DIR / "daily-research"
        daily_research_dir.mkdir(parents=True, exist_ok=True)
        
        report_path = daily_research_dir / f"{today}.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"💾 Full report saved: {report_path}")
        
        # Save condensed version to genius-memory
        condensed = _condense_report(results)
        genius_research_dir = GENIUS_DIR / "research"
        genius_research_dir.mkdir(parents=True, exist_ok=True)
        
        latest_path = genius_research_dir / "latest.json"
        with open(latest_path, "w") as f:
            json.dump(condensed, f, indent=2)
        print(f"💾 Condensed saved: {latest_path}")
        
        # Send Telegram notification
        if HAS_NOTIFIER:
            highlights = _extract_highlights(results)
            msg = f"*Daily Deep Research Complete*\n\n{highlights}"
            try:
                notifier.send(msg, level='L2')
                print(f"📱 Telegram notification sent")
            except Exception as e:
                print(f"⚠️  Telegram send failed: {e}")
    
    print(f"\n{'='*60}")
    print(f"✅ Daily research complete")
    print(f"{'='*60}")
    
    return report


def _condense_report(results: dict) -> dict:
    """Extract key points from full research for genius-memory."""
    condensed = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "summary": {}
    }
    
    for query_name, result in results.items():
        if "error" in result:
            condensed["summary"][query_name] = {"error": result["error"]}
        else:
            # Take first 500 chars as summary
            text = result.get("response", "")
            condensed["summary"][query_name] = {
                "preview": text[:500] + "..." if len(text) > 500 else text,
                "timestamp": result.get("timestamp")
            }
    
    return condensed


def _extract_highlights(results: dict) -> str:
    """Extract key highlights for Telegram notification."""
    highlights = []
    
    for query_name, result in results.items():
        if "error" in result:
            highlights.append(f"❌ {query_name.replace('_', ' ').title()}: Error")
        else:
            text = result.get("response", "")
            # Extract first 200 chars
            preview = text[:200].replace("\n", " ")
            if len(text) > 200:
                preview += "..."
            highlights.append(f"✅ {query_name.replace('_', ' ').title()}:\n{preview}\n")
    
    return "\n".join(highlights)


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Daily Deep Research")
    parser.add_argument("--test", action="store_true", help="Dry run (no API calls, no saves)")
    args = parser.parse_args()
    
    try:
        run_daily_research(test_mode=args.test)
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
