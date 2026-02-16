#!/usr/bin/env python3
"""
Pattern Extraction — Sprint 5.1.9

Uses Claude Opus to analyze last 20 trades for recurring patterns.
Runs weekly (Sunday 06:00 QAT) as part of deep analysis.
Can also run standalone.

Reads: genius-memory/wins/, genius-memory/losses/, state/trade_history.json
Writes: genius-memory/patterns/weekly_YYYYMMDD.json
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
WINS_DIR = BASE_DIR / "genius-memory" / "wins"
LOSSES_DIR = BASE_DIR / "genius-memory" / "losses"
PATTERNS_DIR = BASE_DIR / "genius-memory" / "patterns"
TRADE_HISTORY = BASE_DIR / "state" / "trade_history.json"
STRATEGY_DIR = BASE_DIR / "genius-memory" / "strategy-evolution"
REGIME_LATEST = BASE_DIR / "genius-memory" / "regime-data" / "latest.json"
MEME_LIFECYCLE = BASE_DIR / "genius-memory" / "meme-coin-lifecycle.md"


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[PATTERN] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _load_text(path):
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def _get_recent_trades(n: int = 20) -> list:
    """Get the last N completed trades."""
    history = _load_json(TRADE_HISTORY, [])
    trades = history if isinstance(history, list) else history.get("trades", [])
    completed = [t for t in trades if t.get("status") in ("closed", "CLOSED", None)]
    return completed[-n:]


def _get_trade_logs() -> str:
    """Load recent win/loss post-mortems."""
    logs = []
    for folder in [WINS_DIR, LOSSES_DIR]:
        if folder.exists():
            files = sorted(folder.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
            for f in files[:10]:  # Last 10 from each
                try:
                    content = f.read_text()
                    if len(content) > 100:
                        logs.append(f"--- {f.name} ---\n{content[:1500]}")
                except Exception:
                    pass
    return "\n".join(logs[:15])  # Cap total context


def _get_strategy_stats() -> str:
    """Load strategy evolution stats."""
    stats = []
    if STRATEGY_DIR.exists():
        for f in sorted(STRATEGY_DIR.iterdir()):
            if f.suffix == ".json":
                data = _load_json(f)
                if data:
                    stats.append(f"--- {f.stem} ---\n{json.dumps(data, indent=2, default=str)[:800]}")
    return "\n".join(stats[:5])


def build_prompt(trades: list, trade_logs: str, strategy_stats: str) -> str:
    """Build the Opus analysis prompt."""
    regime = _load_json(REGIME_LATEST, {})
    regime_tag = regime.get("combined_tag", "UNKNOWN")

    trades_summary = json.dumps(trades, indent=2, default=str)[:4000]

    return f"""You are the Genius Memory pattern extraction engine for Sanad Trader, an autonomous cryptocurrency trading system.

CURRENT MARKET REGIME: {regime_tag}

Analyze the following trade data and identify:

1. **RECURRING WINNING PATTERNS** — What do winning trades have in common?
   - Entry timing, market conditions, signal sources, strategies
   - Specific setups that consistently work

2. **RECURRING LOSING PATTERNS** — What do losing trades have in common?
   - Common mistakes, bad timing, misleading signals
   - Conditions where the system should NOT trade

3. **NON-OBVIOUS CORRELATIONS** — Patterns a human might miss:
   - Time-of-day effects, regime-specific edges
   - Source reliability patterns, strategy-regime mismatches
   - Volume/sentiment leading indicators

4. **ACTIONABLE RECOMMENDATIONS** — Specific, testable changes:
   - Strategy parameter adjustments (with evidence)
   - New entry/exit rules to test
   - Sources to upgrade or downgrade

IMPORTANT: Only identify patterns with at least 3 supporting data points.
Do NOT suggest changes based on 1-2 trades. Statistical significance matters.

Return your analysis as JSON with this structure:
{{
  "winning_patterns": [
    {{"pattern": "...", "evidence_count": N, "confidence": "high/medium/low", "details": "..."}}
  ],
  "losing_patterns": [...],
  "correlations": [...],
  "recommendations": [
    {{"action": "...", "param": "...", "current": "...", "proposed": "...", "evidence": "...", "priority": "high/medium/low"}}
  ],
  "summary": "2-3 sentence overview"
}}

=== RECENT TRADES ({len(trades)} trades) ===
{trades_summary}

=== TRADE POST-MORTEMS ===
{trade_logs[:3000]}

=== STRATEGY STATS ===
{strategy_stats[:2000]}
"""


def call_opus(prompt: str) -> dict | None:
    """Call Claude Opus via OpenClaw's /opus command or API."""
    _log("Calling Opus for pattern extraction...")

    try:
        # Method 1: Direct Anthropic API call
        import requests

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            env_path = BASE_DIR / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if line.startswith("ANTHROPIC_API_KEY="):
                        api_key = line.split("=", 1)[1].strip()

        if not api_key:
            _log("No ANTHROPIC_API_KEY — trying OpenClaw relay")
            return _call_via_openclaw(prompt)

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-opus-4-6",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )

        if resp.status_code == 200:
            data = resp.json()
            text = data["content"][0]["text"]
            # Parse JSON from response
            return _extract_json(text)
        else:
            _log(f"Opus API error {resp.status_code}: {resp.text[:200]}")
            return None

    except Exception as e:
        _log(f"Opus call failed: {e}")
        return None


def _call_via_openclaw(prompt: str) -> dict | None:
    """Fallback: call via OpenClaw CLI."""
    try:
        # Write prompt to temp file, invoke via CLI
        tmp = BASE_DIR / "state" / "pattern_prompt.tmp"
        tmp.write_text(prompt)
        _log("OpenClaw relay not implemented — skipping LLM call")
        return None
    except Exception:
        return None


def _extract_json(text: str) -> dict | None:
    """Extract JSON from LLM response text."""
    import re

    # Try to find JSON block
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    # Try stripping markdown code fences
    cleaned = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        _log("Could not parse JSON from Opus response")
        return {"raw_response": text[:2000], "parse_error": True}


def run():
    """Main pattern extraction run."""
    _log("=== PATTERN EXTRACTION (Opus) ===")

    trades = _get_recent_trades(20)
    if len(trades) < 3:
        _log(f"Only {len(trades)} trades — need at least 3 for pattern analysis")
        return None

    trade_logs = _get_trade_logs()
    strategy_stats = _get_strategy_stats()

    prompt = build_prompt(trades, trade_logs, strategy_stats)
    _log(f"Analyzing {len(trades)} trades, prompt size: {len(prompt)} chars")

    result = call_opus(prompt)

    if result:
        # Save pattern file
        PATTERNS_DIR.mkdir(parents=True, exist_ok=True)
        date_str = _now().strftime("%Y%m%d")

        output = {
            "date": date_str,
            "trades_analyzed": len(trades),
            "regime": _load_json(REGIME_LATEST, {}).get("combined_tag", "UNKNOWN"),
            "analysis": result,
            "generated_at": _now().isoformat(),
        }

        _save_json(PATTERNS_DIR / f"weekly_{date_str}.json", output)

        summary = result.get("summary", "No summary")
        winning = len(result.get("winning_patterns", []))
        losing = len(result.get("losing_patterns", []))
        recs = len(result.get("recommendations", []))

        _log(f"Found {winning} winning patterns, {losing} losing patterns, {recs} recommendations")
        _log(f"Summary: {summary[:200]}")
        return output
    else:
        _log("No result from Opus — check API key or connectivity")
        return None


if __name__ == "__main__":
    run()
