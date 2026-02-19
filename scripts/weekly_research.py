#!/usr/bin/env python3
"""
Weekly Deep Research — Sprint 6.1.15
Runs Sunday 08:00 QAT (05:00 UTC).
Macro crypto trends via Perplexity or Opus web search.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
REPORTS_DIR = BASE_DIR / "reports" / "weekly-research"

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[RESEARCH] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def research():
    _log("=== WEEKLY DEEP RESEARCH ===")

    import env_loader

    prompt = """You are a crypto market researcher. Provide a concise weekly macro analysis:

1. BTC/ETH price action and key levels this week
2. Major regulatory developments
3. Notable DeFi/meme coin trends
4. Upcoming catalysts (FOMC, ETF decisions, unlocks, launches)
5. On-chain metrics summary (exchange flows, whale activity, stablecoin supply)
6. Risk factors for the coming week
7. Meme coin sector: which narratives are gaining/losing momentum

Format as a structured brief. Be specific with data points where possible.
Focus on actionable intelligence for an autonomous trading system."""

    result = None

    # Try Perplexity first (best for current research)
    pplx_key = env_loader.get_key("PERPLEXITY_API_KEY")
    if pplx_key:
        try:
            import requests
            resp = requests.post(
                "https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {pplx_key}", "Content-Type": "application/json"},
                json={
                    "model": "sonar-pro",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 3000,
                },
                timeout=60,
            )
            if resp.status_code == 200:
                result = resp.json()["choices"][0]["message"]["content"]
                _log("Research via Perplexity OK")
        except Exception as e:
            _log(f"Perplexity failed: {e}")

    # Fallback to Anthropic
    if not result:
        api_key = env_loader.get_key("ANTHROPIC_API_KEY")
        if api_key:
            try:
                import requests
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 3000,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=60,
                )
                if resp.status_code == 200:
                    resp_data = resp.json()
                    result = resp_data["content"][0]["text"]
                    # Track cost
                    usage = resp_data.get("usage", {})
                    if usage:
                        from cost_tracker import log_api_call
                        log_api_call(
                            model="claude-haiku-4-5-20251001",
                            input_tokens=usage.get("input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0),
                            stage="weekly_research",
                            extra={"script": "weekly_research"}
                        )
                    _log("Research via Anthropic OK (Haiku)")
            except Exception as e:
                _log(f"Anthropic failed: {e}")

    if not result:
        _log("No API available for research")
        return None

    # Save
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = _now().strftime("%Y%m%d")
    report = {
        "date": date_str,
        "research": result,
        "generated_at": _now().isoformat(),
    }
    _save_json(REPORTS_DIR / f"{date_str}.json", report)

    # Notify
    try:
        import notifier
        brief = result[:3000] if len(result) > 3000 else result
        notifier.send(brief, notifier.AlertLevel.NORMAL, title="Weekly Market Research")
    except Exception as e:
        _log(f"Notification failed: {e}")

    _log(f"Research saved ({len(result)} chars)")
    _log("=== RESEARCH COMPLETE ===")
    return report


if __name__ == "__main__":
    research()
