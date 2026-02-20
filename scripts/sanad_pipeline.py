#!/usr/bin/env python3
import signal as _signal
_signal.signal(_signal.SIGPIPE, _signal.SIG_DFL)
"""
Sanad Trader v3.0 — Intelligence Pipeline Orchestrator

Phases 2-3: End-to-end signal processing

The 7-stage pipeline (from pipeline.md):
1. Signal Intake — Receive and validate raw signal
2. Sanad Verification — Verify signal sources (trust score, grade)
3. Strategy Match — Select best strategy for this signal
4. Bull/Bear Debate — Argue FOR and AGAINST the trade
5. Al-Muhasbi Judge — Independent review, final verdict
6. Policy Engine — 15 deterministic gates (fail-closed)
7. Execute/Log — Paper trade or log rejection

All LLM calls use structured prompts from trading/prompts/.
All decisions logged to Supabase with full decision packet.
Fail-closed: any stage failure → BLOCK, log reason, notify.

References:
- pipeline.md (7-stage flow)
- sanad-verifier.md (Takhrij process)
- bull-albaqarah.md (Bull case)
- bear-aldahhak.md (Bear case)
- judge-almuhasbi.md (Al-Muhasbi verdict)
- thresholds.yaml (all thresholds)
- policy_engine.py (15 gates)
"""

import os
import sys
import json
import time
import uuid
import hashlib
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Load environment
BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
SCRIPTS_DIR = BASE_DIR / "scripts"
PROMPTS_DIR = BASE_DIR / "prompts"
STATE_DIR = BASE_DIR / "state"
CONFIG_DIR = BASE_DIR / "config"
LOGS_DIR = BASE_DIR / "execution-logs"

sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv(CONFIG_DIR / ".env")
except Exception:
    pass

import yaml
import requests  # Better timeout handling than urllib
import binance_client
try:
    import notifier
    HAS_NOTIFIER = True
except ImportError:
    HAS_NOTIFIER = False

# v3.0 imports
from token_profile import (
    build_token_profile, meme_safety_gate, get_eligible_strategies,
    TIER_MAP, lint_prompt, validate_evidence, PRE_TRADE_MUHASABA
)
from tier_prompts import get_bull_prompt, get_bear_prompt

# Load thresholds
with open(CONFIG_DIR / "thresholds.yaml", "r") as f:
    THRESHOLDS = yaml.safe_load(f)

# Load agent prompts
def _load_prompt(filename):
    path = PROMPTS_DIR / filename
    with open(path, "r") as f:
        return f.read()

SANAD_PROMPT = _load_prompt("sanad-verifier.md")
BULL_PROMPT = _load_prompt("bull-albaqarah.md")
BEAR_PROMPT = _load_prompt("bear-aldahhak.md")
JUDGE_PROMPT = _load_prompt("judge-almuhasbi.md")
PIPELINE_PROMPT = _load_prompt("pipeline.md")


# ─────────────────────────────────────────────
# LLM CALLER (Direct APIs + OpenRouter Fallback)
# ─────────────────────────────────────────────

import urllib.request
import urllib.parse

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")


def call_claude(system_prompt, user_message, model="claude-haiku-4-5-20251001", max_tokens=2000, stage="unknown", token_symbol=""):
    """
    Call Claude via direct Anthropic API.
    
    Args:
        system_prompt: System prompt string
        user_message: User message string
        model: Model name (default: claude-haiku-4-5-20251001)
        max_tokens: Max tokens for response
        stage: Pipeline stage for cost tracking (default: "unknown")
        token_symbol: Trading token symbol for cost tracking (default: "")
    
    Returns:
        Response text string, or None on failure
    """
    
    # Direct Anthropic
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    })

    try:
        # Use requests with proper timeout (connect + read)
        response = requests.post(
            url,
            headers=headers,
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            },
            timeout=(10, 60)  # (connect_timeout, read_timeout)
        )
        response.raise_for_status()
        result = response.json()
        
        if result.get("content") and len(result["content"]) > 0:
            text = result["content"][0].get("text", "")
            if text:
                print(f"    [Claude direct OK — {model}]")
                
                # Log cost
                usage = result.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                try:
                    from cost_tracker import log_api_call
                    log_api_call(model, input_tokens, output_tokens, stage, token_symbol)
                except Exception as e:
                    print(f"    [Cost tracking failed: {e}]")
                
                return text
        return None
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"    [Claude direct TIMEOUT/CONNECTION: {e}]")
        print(f"    [Falling back to OpenRouter Claude...]")
        return _fallback_openrouter(system_prompt, user_message, f"anthropic/{model}", max_tokens, stage, token_symbol)
    except Exception as e:
        print(f"    [Claude direct FAILED: {e}]")
        print(f"    [Falling back to OpenRouter Claude...]")
        return _fallback_openrouter(system_prompt, user_message, f"anthropic/{model}", max_tokens, stage, token_symbol)


def call_openai(system_prompt, user_message, model="gpt-5.2", max_tokens=2000, stage="unknown", token_symbol=""):
    """
    Call OpenAI API directly.
    Primary for: Al-Muhasbi Judge (GPT-5.2).
    Fallback: OpenRouter GPT.
    
    Args:
        system_prompt: System prompt string
        user_message: User message string
        model: Model name (default: gpt-5.2)
        max_tokens: Max tokens for response
        stage: Pipeline stage for cost tracking (default: "unknown")
        token_symbol: Trading token symbol for cost tracking (default: "")
    
    Returns:
        Response text string, or None on failure
    """
    if not OPENAI_API_KEY:
        print(f"    [OpenAI key missing — falling back to OpenRouter]")
        return _fallback_openrouter(system_prompt, user_message, f"openai/{model}", max_tokens, stage, token_symbol)

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }
    body = json.dumps({
        "model": model,
        "max_completion_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    })

    try:
        # Use requests with proper timeout (connect + read)
        response = requests.post(
            url,
            headers=headers,
            json={
                "model": model,
                "max_completion_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            },
            timeout=(10, 60)  # (connect_timeout, read_timeout)
        )
        response.raise_for_status()
        result = response.json()
        choices = result.get("choices", [])
        if choices:
            text = choices[0].get("message", {}).get("content", "")
            if text:
                print(f"    [OpenAI direct OK — {model}]")
                
                # Log cost
                usage = result.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                try:
                    from cost_tracker import log_api_call
                    log_api_call(model, input_tokens, output_tokens, stage, token_symbol)
                except Exception as e:
                    print(f"    [Cost tracking failed: {e}]")
                
                return text
        return None
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"    [OpenAI direct TIMEOUT/CONNECTION: {e}]")
        print(f"    [Falling back to OpenRouter GPT...]")
        return _fallback_openrouter(system_prompt, user_message, f"openai/{model}", max_tokens, stage, token_symbol)
    except Exception as e:
        print(f"    [OpenAI direct FAILED: {e}]")
        print(f"    [Falling back to OpenRouter GPT...]")
        return _fallback_openrouter(system_prompt, user_message, f"openai/{model}", max_tokens, stage, token_symbol)


def call_openai_responses(system_prompt, user_message, model="gpt-5.2-pro", max_tokens=8000, stage="unknown", token_symbol=""):
    """
    Call OpenAI Responses API for models that require it (e.g. gpt-5.2-pro).
    
    Args:
        system_prompt: System prompt string
        user_message: User message string
        model: Model name (default: gpt-5.2-pro)
        max_tokens: Max tokens for response
        stage: Pipeline stage for cost tracking
        token_symbol: Trading token symbol for cost tracking
    
    Returns:
        Response text string, or None on failure
    """
    if not OPENAI_API_KEY:
        print(f"    [OpenAI key missing — cannot use Responses API]")
        return None

    url = "https://api.openai.com/v1/responses"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json={
                "model": model,
                "input": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "max_output_tokens": max_tokens,
            },
            timeout=(10, 120)  # Longer timeout for deep reasoning
        )
        response.raise_for_status()
        result = response.json()
        
        # Extract text from Responses API structure
        for item in result.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        text = content.get("text", "")
                        if text:
                            print(f"    [OpenAI Responses API OK — {model}]")
                            
                            # Log cost
                            usage = result.get("usage", {})
                            input_tokens = usage.get("input_tokens", 0)
                            output_tokens = usage.get("output_tokens", 0)
                            try:
                                from cost_tracker import log_api_call
                                log_api_call(model, input_tokens, output_tokens, stage, token_symbol)
                            except Exception as e:
                                print(f"    [Cost tracking failed: {e}]")
                            
                            return text
        
        print(f"    [OpenAI Responses API returned unexpected structure: {str(result)[:200]}]")
        return None
        
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"    [OpenAI Responses API TIMEOUT/CONNECTION: {e}]")
        return None
    except Exception as e:
        print(f"    [OpenAI Responses API FAILED: {e}]")
        return None


def call_perplexity(query, model="sonar-pro", stage="unknown", token_symbol=""):
    """
    Call Perplexity API directly for real-time intelligence.
    Primary for: Sanad Verifier source research.
    Fallback: OpenRouter Perplexity.
    
    Args:
        query: Query string
        model: Model name (default: sonar-pro)
        stage: Pipeline stage for cost tracking (default: "unknown")
        token_symbol: Trading token symbol for cost tracking (default: "")
    
    Returns:
        Response text string, or None on failure
    """
    if not PERPLEXITY_API_KEY:
        print(f"    [Perplexity key missing — falling back to OpenRouter]")
        return _fallback_openrouter(
            "You are a real-time crypto intelligence agent. Return factual, sourced information only.",
            query,
            f"perplexity/{model}",
            1500,
            stage,
            token_symbol,
        )

    url = "https://api.perplexity.ai/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
    }
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a real-time crypto intelligence agent. Return factual, sourced information only. Include source URLs when possible."},
            {"role": "user", "content": query},
        ],
    })

    try:
        # Use requests with proper timeout (connect + read)
        response = requests.post(
            url,
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a real-time crypto intelligence agent. Return factual, sourced information only. Include source URLs when possible."},
                    {"role": "user", "content": query},
                ],
            },
            timeout=(10, 30)  # (connect_timeout, read_timeout) - faster for search
        )
        response.raise_for_status()
        result = response.json()
        choices = result.get("choices", [])
        if choices:
            text = choices[0].get("message", {}).get("content", "")
            if text:
                print(f"    [Perplexity direct OK — {model}]")
                
                # Log cost (flat rate, no token counting)
                try:
                    from cost_tracker import log_api_call
                    log_api_call(f"perplexity/{model}", 0, 0, stage, token_symbol)
                except Exception as e:
                    print(f"    [Cost tracking failed: {e}]")
                
                return text
        return None
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"    [Perplexity direct TIMEOUT/CONNECTION: {e}]")
        print(f"    [Falling back to OpenRouter Perplexity...]")
        return _fallback_openrouter(
            "You are a real-time crypto intelligence agent. Return factual, sourced information only.",
            query,
            f"perplexity/{model}",
            1500,
            stage,
            token_symbol,
        )
    except Exception as e:
        print(f"    [Perplexity direct FAILED: {e}]")
        print(f"    [Falling back to OpenRouter Perplexity...]")
        return _fallback_openrouter(
            "You are a real-time crypto intelligence agent. Return factual, sourced information only.",
            query,
            f"perplexity/{model}",
            1500,
            stage,
            token_symbol,
        )


def _fallback_openrouter(system_prompt, user_message, model, max_tokens=2000, stage="unknown", token_symbol=""):
    """
    Fallback: Route any model through OpenRouter.
    Used when direct API calls fail.
    """
    if not OPENROUTER_API_KEY:
        print(f"    [OpenRouter key also missing — no fallback available]")
        return None

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    }
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    })

    try:
        # Use requests with proper timeout (connect + read)
        response = requests.post(
            url,
            headers=headers,
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            },
            timeout=(10, 90)  # (connect_timeout, read_timeout)
        )
        response.raise_for_status()
        result = response.json()
        choices = result.get("choices", [])
        if choices:
            text = choices[0].get("message", {}).get("content", "")
            if text:
                print(f"    [OpenRouter fallback OK — {model}]")
                
                # Log cost (best-effort token counting from OpenRouter)
                usage = result.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                try:
                    from cost_tracker import log_api_call
                    log_api_call(model, input_tokens, output_tokens, stage, token_symbol)
                except Exception as e:
                    print(f"    [Cost tracking failed: {e}]")
                
                return text
        return None
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"    [OpenRouter fallback TIMEOUT/CONNECTION: {e}]")
        return None
    except Exception as e:
        print(f"    [OpenRouter fallback FAILED: {e}]")
        return None
        return None


# ─────────────────────────────────────────────
# STAGE 1: SIGNAL INTAKE
# ─────────────────────────────────────────────

def stage_1_signal_intake(signal):
    """
    Validate and normalize incoming signal.
    Required fields: token, source, thesis, timestamp.
    Optional: exchange, chain, url, raw_data.
    """
    print(f"\n{'='*60}")
    print(f"STAGE 1: SIGNAL INTAKE")
    print(f"{'='*60}")

    required = ["token", "source", "thesis"]
    for field in required:
        if field not in signal or not signal[field]:
            return None, f"Missing required field: {field}"

    # Add metadata
    signal["correlation_id"] = str(uuid.uuid4())[:12]
    signal["pipeline_start"] = datetime.now(timezone.utc).isoformat()

    if "timestamp" not in signal:
        signal["timestamp"] = datetime.now(timezone.utc).isoformat()

    # Check signal freshness
    max_age = THRESHOLDS["sanad"]["signal_max_age_minutes"]
    try:
        signal_time = datetime.fromisoformat(signal["timestamp"])
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)
        age_minutes = (datetime.now(timezone.utc) - signal_time).total_seconds() / 60
        if age_minutes > max_age:
            return None, f"Signal too old: {age_minutes:.0f}min > {max_age}min max"
    except Exception:
        pass  # If timestamp parse fails, continue (freshness checked later by Gate #3)

    print(f"  Token: {signal['token']}")
    print(f"  Source: {signal['source']}")
    print(f"  Thesis: {signal['thesis'][:80]}...")
    print(f"  Correlation ID: {signal['correlation_id']}")

    return signal, None


# ─────────────────────────────────────────────
# ON-CHAIN ENRICHMENT (pre-Stage 2)
# ─────────────────────────────────────────────

def enrich_signal_with_onchain_data(signal: dict) -> dict:
    """
    For Solana tokens with a token_address, fetch Birdeye security + RugCheck data
    and append it to the signal as on-chain verification evidence.
    """
    import time as _time

    address = signal.get("token_address") or signal.get("address") or ""
    chain = signal.get("chain", "").lower()
    if not address or chain != "solana":
        return signal

    print("  [2pre] Enriching with on-chain verification data...")
    onchain_evidence = {}

    # 1. RugCheck safety
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from rugcheck_client import check_token_safety
        safety = check_token_safety(address)
        onchain_evidence["rugcheck"] = {
            "score": safety.get("rugcheck_score", 0),
            "risk_level": safety.get("risk_level", "Unknown"),
            "risks": safety.get("risks", []),
            "lp_locked_pct": safety.get("lp_locked_pct", 0),
            "safe_to_trade": safety.get("safe_to_trade", False),
        }
        print(f"    RugCheck: score {safety.get('rugcheck_score')}/100 ({safety.get('risk_level')})")
    except Exception as e:
        onchain_evidence["rugcheck"] = {"error": str(e)}
        print(f"    RugCheck: error — {e}")

    _time.sleep(2)  # respect Birdeye rate limits

    # 2. Birdeye security + overview + creation
    try:
        from birdeye_client import get_token_security, get_token_overview, get_token_creation_info
        security = get_token_security(address)
        if security:
            onchain_evidence["birdeye_security"] = {
                "top10_holder_pct": round(security.get("top10HolderPercent", 0) * 100, 2) if security.get("top10HolderPercent") else None,
                "creator_pct": round(security.get("creatorPercentage", 0) * 100, 4) if security.get("creatorPercentage") else None,
                "mutable_metadata": security.get("mutableMetadata"),
                "is_fake_token": security.get("fakeToken"),
            }
            print(f"    Birdeye security: top10={onchain_evidence['birdeye_security']['top10_holder_pct']}%")

        _time.sleep(2)

        overview = get_token_overview(address)
        if overview:
            onchain_evidence["birdeye_overview"] = {
                "holder_count": overview.get("holder"),
                "volume_24h": overview.get("v24hUSD"),
                "liquidity": overview.get("liquidity"),
                "market_cap": overview.get("mc") or overview.get("marketCap"),
                "trade_count_24h": overview.get("trade24h"),
                "unique_wallets_24h": overview.get("uniqueWallet24h"),
            }
            print(f"    Birdeye overview: holders={overview.get('holder')}, liq=${overview.get('liquidity', 0):,.0f}")

        _time.sleep(2)

        creation = get_token_creation_info(address)
        if creation:
            created_ts = creation.get("blockUnixTime", 0)
            age_hours = ((_time.time() - created_ts) / 3600) if created_ts else None
            onchain_evidence["token_creation"] = {
                "created_at": creation.get("blockHumanTime"),
                "creator_wallet": creation.get("creator"),
                "age_hours": round(age_hours, 1) if age_hours else None,
            }
            print(f"    Token age: {age_hours:.1f}h" if age_hours else "    Token age: unknown")
    except Exception as e:
        onchain_evidence["birdeye_error"] = str(e)
        print(f"    Birdeye: error — {e}")

    # 3. Build verification summary
    rc = onchain_evidence.get("rugcheck", {})
    bs = onchain_evidence.get("birdeye_security", {})
    bo = onchain_evidence.get("birdeye_overview", {})
    tc = onchain_evidence.get("token_creation", {})

    def _fmt(v, prefix="", suffix="", fmt_str="{}", default="N/A"):
        if v is None or v == "":
            return default
        return f"{prefix}{fmt_str.format(v)}{suffix}"

    evidence_summary = f"""
ON-CHAIN VERIFICATION DATA (treat as credible primary sources):

Source 1 — RugCheck (rugcheck.xyz, independent Solana token auditor):
  Safety Score: {_fmt(rc.get('score'), suffix='/100')}  (higher = safer)
  Risk Level: {rc.get('risk_level', 'N/A')}
  Risks Found: {', '.join(rc.get('risks', [])) or 'None'}
  LP Locked: {_fmt(rc.get('lp_locked_pct'), suffix='%')}
  Safe to Trade: {rc.get('safe_to_trade', 'N/A')}

Source 2 — Birdeye (birdeye.so, Solana chain analytics):
  Top 10 Holders: {_fmt(bs.get('top10_holder_pct'), suffix='% of supply')}
  Creator Holdings: {_fmt(bs.get('creator_pct'), suffix='%')}
  Mutable Metadata: {bs.get('mutable_metadata', 'N/A')}
  Fake Token Flag: {bs.get('is_fake_token', 'N/A')}
  Holders: {_fmt(bo.get('holder_count'))}
  24h Volume: {_fmt(bo.get('volume_24h'), prefix='$', fmt_str='{:,.0f}')}
  Liquidity: {_fmt(bo.get('liquidity'), prefix='$', fmt_str='{:,.0f}')}
  Market Cap: {_fmt(bo.get('market_cap'), prefix='$', fmt_str='{:,.0f}')}
  24h Trades: {_fmt(bo.get('trade_count_24h'))}
  Unique Wallets 24h: {_fmt(bo.get('unique_wallets_24h'))}

Source 3 — Token Creation (on-chain):
  Created: {tc.get('created_at', 'N/A')}
  Age: {_fmt(tc.get('age_hours'), suffix=' hours')}
  Creator Wallet: {tc.get('creator_wallet', 'N/A')}

IMPORTANT FOR SANAD SCORING:
- RugCheck and Birdeye are independent on-chain verification sources (count as 2 separate sources)
- If RugCheck score > 70 AND Birdeye shows healthy holder distribution (top 10 < 50%), this token has 2 credible verifications
- On-chain data is MORE reliable than news for meme tokens — it shows what's actually happening, not what's being reported
"""

    # 4. Holder Concentration Analysis (Sprint 7.2.3 — replaces BubbleMaps)
    try:
        from holder_analyzer import analyze_concentration
        holder_result = analyze_concentration(address)
        if holder_result.get("status") == "analyzed":
            onchain_evidence["holder_analysis"] = {
                "top_5_pct": holder_result.get("top_5_pct"),
                "top_10_pct": holder_result.get("top_10_pct"),
                "top_20_pct": holder_result.get("top_20_pct"),
                "hhi": holder_result.get("hhi"),
                "gini": holder_result.get("gini"),
                "dev_wallet_pct": holder_result.get("dev_wallet_pct"),
                "sybil_risk": holder_result.get("sybil_risk"),
                "risk_score": holder_result.get("risk_score"),
            }
            print(f"    Holder analysis: top10={holder_result.get('top_10_pct')}%, "
                  f"sybil={holder_result.get('sybil_risk')}, risk={holder_result.get('risk_score')}/100")
        else:
            onchain_evidence["holder_analysis"] = {"status": holder_result.get("status", "no_data")}
            print(f"    Holder analysis: {holder_result.get('status', 'no_data')}")
    except Exception as e:
        onchain_evidence["holder_analysis"] = {"error": str(e)}
        print(f"    Holder analysis: error — {e}")

    _time.sleep(1)

    # 5. Honeypot Detection (Sprint 7.1.7)
    try:
        from honeypot_detector import check_honeypot
        honeypot_result = check_honeypot(address, signal.get("token", ""))
        onchain_evidence["honeypot"] = {
            "is_honeypot": honeypot_result.get("is_honeypot", False),
            "verdict": honeypot_result.get("verdict", "UNKNOWN"),
            "buy_possible": honeypot_result.get("buy_possible"),
            "sell_possible": honeypot_result.get("sell_possible"),
            "round_trip_loss_pct": honeypot_result.get("round_trip_loss_pct", 0),
            "checks": honeypot_result.get("checks", []),
        }
        print(f"    Honeypot: {honeypot_result.get('verdict')} "
              f"(round-trip loss: {honeypot_result.get('round_trip_loss_pct', 0):.1f}%)")
    except Exception as e:
        onchain_evidence["honeypot"] = {"error": str(e)}
        print(f"    Honeypot: error — {e}")

    # 6. Rugpull Scanner (Sprint 7.5.1-7.5.4)
    try:
        from rugpull_scanner import scan_token, is_blacklisted, record_prediction
        if is_blacklisted(address):
            onchain_evidence["rugpull_scan"] = {
                "verdict": "BLACKLISTED",
                "risk_score": 100,
                "flags": ["previously_blacklisted"],
            }
            print(f"    Rugpull scan: BLACKLISTED (known scam)")
        else:
            scan_result = scan_token(
                address,
                token_name=signal.get("token", ""),
                metadata=None,  # will fetch via helius
                holders=onchain_evidence.get("holder_analysis"),
            )
            onchain_evidence["rugpull_scan"] = {
                "verdict": scan_result.get("verdict"),
                "risk_score": scan_result.get("risk_score"),
                "flags": scan_result.get("flags", []),
            }
            print(f"    Rugpull scan: {scan_result.get('verdict')} "
                  f"(score: {scan_result.get('risk_score')}/100, "
                  f"flags: {len(scan_result.get('flags', []))})")
    except Exception as e:
        onchain_evidence["rugpull_scan"] = {"error": str(e)}
        print(f"    Rugpull scan: error — {e}")

    # 3. Build verification summary
    rc = onchain_evidence.get("rugcheck", {})
    bs = onchain_evidence.get("birdeye_security", {})
    bo = onchain_evidence.get("birdeye_overview", {})
    tc = onchain_evidence.get("token_creation", {})
    ha = onchain_evidence.get("holder_analysis", {})
    hp = onchain_evidence.get("honeypot", {})
    rs = onchain_evidence.get("rugpull_scan", {})

    evidence_summary += f"""

Source 4 — Holder Concentration (Helius DAS, deterministic):
  Top 5 Holders: {_fmt(ha.get('top_5_pct'), suffix='%')}
  Top 10 Holders: {_fmt(ha.get('top_10_pct'), suffix='%')}
  Top 20 Holders: {_fmt(ha.get('top_20_pct'), suffix='%')}
  HHI (concentration index): {_fmt(ha.get('hhi'))}  (>2500 = concentrated)
  Gini Coefficient: {_fmt(ha.get('gini'))}  (>0.8 = very unequal)
  Dev Wallet: {_fmt(ha.get('dev_wallet_pct'), suffix='%')}
  Sybil Risk: {ha.get('sybil_risk', 'N/A')}
  Holder Risk Score: {_fmt(ha.get('risk_score'), suffix='/100')}

Source 5 — Honeypot Detection (Jupiter simulation, deterministic):
  Verdict: {hp.get('verdict', 'N/A')}
  Buy Possible: {hp.get('buy_possible', 'N/A')}
  Sell Possible: {hp.get('sell_possible', 'N/A')}
  Round-Trip Loss: {_fmt(hp.get('round_trip_loss_pct'), suffix='%')}
  Checks: {', '.join(hp.get('checks', [])) or 'None'}

Source 6 — Rugpull Scanner (deterministic pattern matching):
  Verdict: {rs.get('verdict', 'N/A')}
  Risk Score: {_fmt(rs.get('risk_score'), suffix='/100')}
  Flags: {', '.join(rs.get('flags', [])) or 'None'}

HARD GATES (deterministic, override LLM):
- If honeypot verdict = HONEYPOT → BLOCK (no trade)
- If rugpull scan verdict = RUG or BLACKLISTED → BLOCK (no trade)
- If holder sybil_risk = CRITICAL → BLOCK (no trade)
- If holder risk_score > 80 → strong BLOCK signal
"""

    signal["onchain_evidence"] = onchain_evidence
    signal["onchain_evidence_summary"] = evidence_summary.strip()
    return signal


# ─────────────────────────────────────────────
# HELPER: CANONICALIZE SOURCE
# ─────────────────────────────────────────────

def _canonicalize_source(raw_source: str) -> str:
    """Convert raw source string to canonical source_key for UCB1."""
    try:
        from signal_normalizer import canonical_source
        return canonical_source(raw_source)["source_key"]
    except Exception:
        return raw_source.lower().replace(" ", "_")


# ─────────────────────────────────────────────
# STAGE 2: SANAD VERIFICATION
# ─────────────────────────────────────────────

def stage_2_sanad_verification(signal):
    """
    Run Takhrij process on the signal.
    Uses Claude Opus for deep verification + Perplexity for real-time data.
    Returns: trust_score (0-100), grade (Tawatur/Mashhur/Ahad), recommendation.
    HARD RULE: score < 70 → BLOCK.
    """
    print(f"\n{'='*60}")
    print(f"STAGE 2: SANAD VERIFICATION (Takhrij)")
    print(f"{'='*60}")

    # Step 0a: Load learned source grades (UCB1 feedback)
    source_grades_file = STATE_DIR / "source_grades.json"
    source_grades = {}
    if source_grades_file.exists():
        try:
            source_grades = json.load(open(source_grades_file))
        except:
            pass
    
    signal_source = signal.get("source", "unknown").lower()
    learned_grade = source_grades.get(signal_source, None)
    
    if learned_grade:
        print(f"  📊 UCB1 Source Grade: {signal_source} = {learned_grade} (learned from past trades)")

    # Step 0: Enrich with on-chain data for Solana tokens
    signal = enrich_signal_with_onchain_data(signal)

    # Step 0b: HARD GATES — deterministic blocks before LLM (Sprint 7.2.3)
    onchain = signal.get("onchain_evidence", {})
    hp = onchain.get("honeypot", {})
    rs = onchain.get("rugpull_scan", {})
    ha = onchain.get("holder_analysis", {})

    hard_block_reason = None
    if hp.get("is_honeypot") or hp.get("verdict") == "HONEYPOT":
        hard_block_reason = f"HONEYPOT detected: {', '.join(hp.get('checks', []))}"
    elif rs.get("verdict") in ("RUG", "BLACKLISTED"):
        hard_block_reason = f"Rugpull scan: {rs.get('verdict')} — flags: {', '.join(rs.get('flags', []))}"
    elif ha.get("sybil_risk") == "CRITICAL":
        hard_block_reason = f"CRITICAL Sybil risk (holder risk score: {ha.get('risk_score', '?')}/100)"

    if hard_block_reason:
        print(f"  ⛔ HARD GATE BLOCK: {hard_block_reason}")
        print(f"  Skipping LLM verification — deterministic BLOCK")
        return {
            "trust_score": 0,
            "grade": "BLOCKED",
            "recommendation": "BLOCK",
            "reasoning": f"Deterministic hard gate: {hard_block_reason}",
            "hard_gate": True,
            "hard_gate_reason": hard_block_reason,
            "honeypot": hp,
            "rugpull_scan": rs,
            "holder_analysis": ha,
        }, None

    # Step 1: Gather real-time intelligence via Perplexity
    print("  [2a] Gathering real-time intelligence via Perplexity...")
    intel_query = f"""Research the cryptocurrency token {signal['token']}:
1. Current price, 24h volume, market cap
2. Recent news in the last 24 hours
3. Social media sentiment (Twitter/X, Telegram)
4. Any rugpull warnings or scam reports
5. Team/project credibility
6. On-chain activity (whale movements, liquidity changes)

Signal thesis: {signal['thesis']}
Signal source: {signal['source']}"""

    perplexity_intel = call_perplexity(intel_query, stage="sanad_verification", token_symbol=signal.get("token", ""))
    if not perplexity_intel:
        print("  WARNING: Perplexity unavailable — proceeding with limited data")
        perplexity_intel = "Real-time data unavailable."

    # Step 2: Get current price data from Binance
    symbol = signal.get("symbol", signal["token"] + "USDT")
    price_data = binance_client.get_ticker_24h(symbol)
    price_context = ""
    if price_data:
        price_context = f"""
Binance 24h data:
- Price: ${price_data.get('lastPrice', 'N/A')}
- 24h Change: {price_data.get('priceChangePercent', 'N/A')}%
- 24h Volume (USDT): {price_data.get('quoteVolume', 'N/A')}
- 24h High: ${price_data.get('highPrice', 'N/A')}
- 24h Low: ${price_data.get('lowPrice', 'N/A')}"""
    else:
        price_context = f"Binance data unavailable for {symbol}."

    # Step 3: Run Sanad Verifier (Claude Opus)
    print("  [2b] Running Sanad Verification (Claude Opus)...")
    
    # Add learned source grade to prompt
    source_grade_context = ""
    if learned_grade:
        grade_points = {"A": 15, "B": 10, "C": 5, "D": 0}
        points = grade_points.get(learned_grade, 5)
        source_grade_context = f"""
SOURCE PERFORMANCE (learned from past trades):
- This source has been graded {learned_grade} based on historical win rate
- Source trust bonus: +{points} points
- Use this grade as a starting point for source_ucb1_score in your calculation
"""
    
    verification_prompt = f"""SIGNAL TO VERIFY:
Token: {signal['token']}
Source: {signal['source']}
Thesis: {signal['thesis']}
Timestamp: {signal.get('timestamp', 'unknown')}
{source_grade_context}
CROSS-SOURCE CORROBORATION (pre-verified by corroboration engine):
- Independent sources confirming this token: {signal.get('cross_source_count', 1)}
- Sources: {', '.join(signal.get('cross_sources', [])) or 'single source only'}
- Corroboration level: {signal.get('corroboration_level', 'AHAD')}
NOTE: Use this corroboration data directly in your trust score calculation.
If cross_source_count >= 2, corroboration_level is at least MASHHUR (18 points).
If cross_source_count >= 3, corroboration_level is TAWATUR (25 points).
If cross_source_count >= 4, corroboration_level is TAWATUR_QAWIY (30 points — maximum trust).

REAL-TIME INTELLIGENCE (from Perplexity):
{perplexity_intel}

EXCHANGE DATA:
{price_context}

{signal.get('onchain_evidence_summary', '')}

Execute the full 6-step Takhrij process as specified in your instructions.
Use the explicit Trust Score Formula to calculate the score deterministically.
Return your analysis as valid JSON with these exact keys:
{{
  "trust_score": <0-100, calculated using the weighted formula>,
  "grade": "<Tawatur|Mashhur|Ahad>",
  "source_grade": "<A|B|C|D|F>",
  "source_ucb1_score": <UCB1 value or 50 if new>,
  "chain_length": <number of independent confirmations>,
  "chain_integrity": "<CONNECTED|BROKEN|PARTIAL>",
  "content_consistency": "<CONSISTENT|CONTRADICTIONS_FOUND|UNVERIFIABLE>",
  "corroboration_level": "<TAWATUR_QAWIY|TAWATUR|MASHHUR|AHAD_SAHIH|AHAD_DAIF>",
  "recency_decay_points": <0 to -15>,
  "rugpull_flags": ["<flag1>", "<flag2>"] or [],
  "sybil_risk": "<LOW|MEDIUM|HIGH>",
  "sybil_evidence": "<description if MEDIUM/HIGH>",
  "key_findings": ["<finding1>", "<finding2>", "<finding3>"],
  "recommendation": "<PROCEED|CAUTION|BLOCK>",
  "source_count": <number of independent sources found>,
  "reasoning": "<3-5 sentence detailed explanation>"
}}"""

    # RAG: retrieve similar past trades for context
    rag_context = ""
    try:
        from vector_db import query_regime_weighted
        similar = query_regime_weighted(f"{signal['token']} {signal.get('signal_type', '')}", n_results=3)
        if similar:
            rag_lines = []
            for s in similar:
                rag_lines.append(f"- {s.get('token', '?')}: {s.get('outcome', '?')} ({s.get('pnl_pct', 0)*100:.1f}%), regime={s.get('regime', '?')}")
            rag_context = "\n\nSIMILAR PAST TRADES:\n" + "\n".join(rag_lines)
    except Exception as e:
        pass  # RAG is optional enhancement

    sanad_response = call_claude(
        system_prompt=SANAD_PROMPT,
        user_message=verification_prompt + rag_context,
        model="claude-haiku-4-5-20251001",  # Haiku for paper trading (30x cheaper than Opus)
        max_tokens=8000,
        stage="sanad_verification",
        token_symbol=signal.get("token", ""),
    )

    if not sanad_response:
        print("  FAIL-CLOSED: Sanad Verifier returned no response → BLOCK")
        return {
            "trust_score": 0,
            "grade": "FAILED",
            "recommendation": "BLOCK",
            "reasoning": "Sanad Verifier API call failed — fail closed",
            "perplexity_intel": perplexity_intel,
            "price_context": price_context,
        }, "Sanad Verifier API failure"

    # Parse JSON response
    sanad_result = _parse_json_response(sanad_response)
    if not sanad_result:
        print("  FAIL-CLOSED: Could not parse Sanad response → BLOCK")
        print(f"  DEBUG raw response (first 1000 chars):\n{sanad_response[:1000] if sanad_response else 'EMPTY'}")
        return {
            "trust_score": 0,
            "grade": "FAILED",
            "recommendation": "BLOCK",
            "reasoning": "Sanad Verifier response not parseable — fail closed",
            "raw_response": sanad_response[:500],
            "perplexity_intel": perplexity_intel,
        }, "Sanad response parse failure"

    # Add supplementary data
    sanad_result["perplexity_intel"] = perplexity_intel[:500]
    sanad_result["price_context"] = price_context

    # DETERMINISTIC CORROBORATION OVERRIDE
    # Don't trust LLM to apply corroboration points correctly — override from engine
    engine_count = signal.get("cross_source_count", 1)
    engine_level = signal.get("corroboration_level", "AHAD")
    engine_quality = signal.get("corroboration_quality", "WEAK")  # fail closed: no tag = no full boost
    llm_level = sanad_result.get("corroboration_level", "AHAD").upper().strip()

    # Corroboration points: AHAD=10, MASHHUR=18, TAWATUR=25, TAWATUR_QAWIY=30
    # WEAK quality gets partial credit: AHAD=10, MASHHUR=14, TAWATUR=18, TAWATUR_QAWIY=22
    CORR_POINTS_STRONG = {"AHAD": 10, "AHAD_SAHIH": 10, "AHAD_DAIF": 0, "MASHHUR": 18, "TAWATUR": 25, "TAWATUR_QAWIY": 30}
    CORR_POINTS_WEAK = {"AHAD": 10, "AHAD_SAHIH": 10, "AHAD_DAIF": 0, "MASHHUR": 14, "TAWATUR": 18, "TAWATUR_QAWIY": 22}
    points_table = CORR_POINTS_WEAK if engine_quality == "WEAK" else CORR_POINTS_STRONG
    llm_points = CORR_POINTS_STRONG.get(llm_level, 10)  # LLM always scored on STRONG scale
    engine_points = points_table.get(engine_level, 10)

    # Signed delta — corrects BOTH directions (boost or subtract)
    corr_delta = engine_points - llm_points
    if corr_delta != 0:
        sanad_result["trust_score"] = max(0, min(100, sanad_result.get("trust_score", 0) + corr_delta))
        sanad_result["corroboration_override"] = {
            "llm_level": llm_level, "engine_level": engine_level,
            "delta": corr_delta, "quality": engine_quality,
        }
        direction = f"+{corr_delta}" if corr_delta > 0 else str(corr_delta)
        print(f"  ⚡ Corroboration override: {llm_level}→{engine_level} ({direction} trust points){' [WEAK partial]' if engine_quality == 'WEAK' else ''}")

    # Overwrite corroboration fields from engine (code > LLM)
    sanad_result["source_count"] = engine_count
    sanad_result["corroboration_level"] = engine_level
    sanad_result["cross_sources"] = signal.get("cross_sources", [])
    sanad_result["chain_length"] = engine_count  # align with source_count
    # Recompute grade from engine source count
    if engine_count >= 3:
        sanad_result["grade"] = "Tawatur"
    elif engine_count >= 2:
        sanad_result["grade"] = "Mashhur"
    else:
        sanad_result["grade"] = "Ahad"

    trust_score = sanad_result.get("trust_score", 0)
    grade = sanad_result.get("grade", "FAILED")
    min_score = THRESHOLDS["sanad"]["minimum_trade_score"]

    # DETERMINISTIC RECOMMENDATION — derived from final trust_score
    # Hard blocks override everything
    rugpull_flags = sanad_result.get("rugpull_flags", [])
    sybil_risk = sanad_result.get("sybil_risk", "LOW")
    if rugpull_flags:
        recommendation = "BLOCK"
    elif sybil_risk == "HIGH":
        recommendation = "BLOCK"
    elif trust_score >= 80:
        recommendation = "PROCEED"
    elif trust_score >= min_score:
        recommendation = "CAUTION"
    else:
        recommendation = "BLOCK"
    sanad_result["recommendation"] = recommendation

    print(f"  Trust Score: {trust_score}/100{f' (corr delta: {corr_delta:+d})' if corr_delta else ''}")
    print(f"  Grade: {grade}")
    print(f"  Source Grade: {sanad_result.get('source_grade', 'N/A')}")
    print(f"  Chain Integrity: {sanad_result.get('chain_integrity', 'N/A')}")
    print(f"  Corroboration: {engine_level} ({engine_count} sources){' [WEAK — partial credit]' if engine_quality == 'WEAK' else ''}")
    print(f"  Recency Decay: {sanad_result.get('recency_decay_points', 'N/A')}")
    print(f"  Sybil Risk: {sybil_risk}")
    print(f"  Rugpull Flags: {rugpull_flags}")
    print(f"  Recommendation: {recommendation}")
    print(f"  Source Count: {sanad_result.get('source_count', 'N/A')}")
    print(f"  Reasoning: {sanad_result.get('reasoning', 'N/A')[:200]}")

    # HARD RULE: score < threshold → BLOCK (redundant safety net)
    if trust_score < min_score:
        _funnel("sanad_blocked")
        print(f"  BLOCKED: Trust score {trust_score} < {min_score} minimum")
        sanad_result["recommendation"] = "BLOCK"

    return sanad_result, None


# ─────────────────────────────────────────────
# STAGE 2.5: TOKEN PROFILE & TIER CLASSIFICATION (v3.0)
# ─────────────────────────────────────────────

def stage_2_5_token_profile(signal, sanad_result):
    """
    Build TokenProfile and classify asset tier.
    For TIER_3 (memes/microcaps), run meme_safety_gate BEFORE LLM processing.
    This saves API credits by rejecting obvious scams deterministically.
    """
    print(f"\n{'='*60}")
    print(f"STAGE 2.5: TOKEN PROFILE & CLASSIFICATION")
    print(f"{'='*60}")
    
    # Build profile from signal data
    profile = build_token_profile(signal)
    
    print(f"  Token: {profile.symbol}")
    print(f"  Tier: {profile.asset_tier} → {TIER_MAP.get(profile.asset_tier, 'UNKNOWN')}")
    if profile.market_cap:
        print(f"  Market Cap: ${profile.market_cap:,.0f}")
    if profile.liquidity_usd:
        print(f"  Liquidity: ${profile.liquidity_usd:,.0f}")
    if profile.age_days:
        print(f"  Age: {profile.age_days} days")
    if profile.holder_top10_pct:
        print(f"  Top 10 Holders: {profile.holder_top10_pct:.1f}%")
    if profile.rugcheck_score:
        print(f"  RugCheck: {profile.rugcheck_score}/100")
    
    # Check for SKIP tier (stablecoins)
    simple_tier = TIER_MAP.get(profile.asset_tier, "TIER_3")
    if simple_tier == "SKIP":
        return None, f"Asset tier {profile.asset_tier} → SKIP (stablecoins not traded)"
    
    # TIER_3 safety gate — hard blocks before LLM
    if simple_tier == "TIER_3":
        passed, block_reason = meme_safety_gate(profile)
        if not passed:
            print(f"  ⛔ TIER_3 SAFETY GATE BLOCK: {block_reason}")
            return None, f"TIER_3 safety gate: {block_reason}"
        else:
            print(f"  ✓ TIER_3 safety gate passed")
    
    return profile, None


# ─────────────────────────────────────────────
# STAGE 3: STRATEGY MATCH
# ─────────────────────────────────────────────

def stage_3_strategy_match(signal, sanad_result, profile=None):
    """
    Select the best strategy for this signal.
    Currently: meme-momentum only. More strategies added in Phase 4.
    Uses Thompson Sampling once multiple strategies exist.
    """
    print(f"\n{'='*60}")
    print(f"STAGE 3: STRATEGY MATCH")
    print(f"{'='*60}")

    # Load available strategies
    strategies_dir = BASE_DIR / "strategies"
    available = list(strategies_dir.glob("*.md"))
    if not available:
        return None, "No strategies available"

    # ── Get current market regime ──
    regime_tag = "UNKNOWN"
    regime_data = {}
    try:
        from regime_classifier import get_current_regime
        regime_data = get_current_regime()
        regime_tag = regime_data.get("regime_tag", "UNKNOWN")
        print(f"  Market Regime: {regime_tag} (confidence: {regime_data.get('confidence', 0):.0%})")
        implications = regime_data.get("implications", {})
        if implications.get("notes"):
            for note in implications["notes"][:2]:
                print(f"    → {note}")
    except Exception as e:
        print(f"  Regime classifier error ({e}) — using UNKNOWN")

    # ── Get tier for strategy filtering (v3.0) ──
    simple_tier = TIER_MAP.get(profile.asset_tier, "TIER_3") if profile else "TIER_3"
    eligible_by_tier = get_eligible_strategies(profile, regime_tag) if profile else []
    
    if eligible_by_tier:
        print(f"  Eligible strategies by tier: {eligible_by_tier}")
    else:
        print(f"  WARNING: No strategies eligible for tier={simple_tier}, regime={regime_tag}")
    
    # ── Thompson Sampling: select best strategy for this signal + regime + tier ──
    strategy_name = "meme-momentum"  # fallback default
    matched_exit_rules = {}
    thompson_result = {}
    try:
        from thompson_sampler import select_strategy as thompson_select
        from strategy_registry import get_active_strategies

        thompson_result = thompson_select(
            signal=signal, 
            current_regime=regime_tag,
            eligible_strategies=eligible_by_tier  # v3.0: pass tier-filtered list
        )
        thompson_pick = thompson_result.get("selected")

        if thompson_pick:
            strategy_name = thompson_pick
            # Get exit rules from strategy registry
            active = get_active_strategies()
            if strategy_name in active:
                matched_exit_rules = active[strategy_name].get("exit_conditions", {})
            print(f"  Thompson selected: {strategy_name} (mode={thompson_result.get('mode', '?')}, score={thompson_result.get('scores', {}).get(strategy_name, 0):.4f})")
            if thompson_result.get("excluded"):
                for name, reason in thompson_result["excluded"].items():
                    print(f"    EXCLUDED {name}: {reason}")
        else:
            print(f"  Thompson: no eligible strategies — falling back to registry match")
            # Fallback to registry priority matching
            from strategy_registry import match_signal_to_strategies
            matches = match_signal_to_strategies(signal)
            if matches:
                best_match = matches[0]
                strategy_name = best_match["strategy"]
                matched_exit_rules = best_match.get("exit_rules", {})
                print(f"  Registry fallback: {strategy_name}")
    except Exception as e:
        print(f"  Thompson error ({e}) — falling back to registry match")
        try:
            from strategy_registry import match_signal_to_strategies
            matches = match_signal_to_strategies(signal)
            if matches:
                best_match = matches[0]
                strategy_name = best_match["strategy"]
                matched_exit_rules = best_match.get("exit_rules", {})
                print(f"  Registry fallback: {strategy_name}")
        except Exception as e2:
            print(f"  Registry also failed ({e2}) — using default {strategy_name}")

    strategy_path = strategies_dir / f"{strategy_name}.md"
    if not strategy_path.exists():
        # Fallback to meme-momentum if matched strategy file missing
        strategy_name = "meme-momentum"
        strategy_path = strategies_dir / f"{strategy_name}.md"

    if not strategy_path.exists():
        return None, f"Strategy file not found: {strategy_name}"

    with open(strategy_path, "r") as f:
        strategy_content = f.read()

    # Calculate position size using default sizing
    portfolio = _load_state("portfolio.json")
    balance = portfolio.get("current_balance_usd", 10000)

    # Kelly Criterion position sizing
    trade_history = _load_state("trade_history.json")
    all_trades = trade_history.get("trades", [])
    trade_count = len(all_trades)
    min_kelly_trades = THRESHOLDS["sizing"]["kelly_min_trades"]

    if trade_count >= min_kelly_trades:
        # Calculate Kelly fraction from actual trade data
        sells = [t for t in all_trades if t.get("side") == "SELL" and t.get("pnl_pct") is not None]
        if len(sells) >= min_kelly_trades:
            wins = [t for t in sells if t["pnl_pct"] > 0]
            losses = [t for t in sells if t["pnl_pct"] <= 0]
            win_rate = len(wins) / len(sells)
            avg_win = sum(abs(t["pnl_pct"]) for t in wins) / len(wins) if wins else 0
            avg_loss = sum(abs(t["pnl_pct"]) for t in losses) / len(losses) if losses else 1
            
            # Kelly formula: f* = (p * b - q) / b
            # p = win probability, q = loss probability, b = win/loss ratio
            if avg_loss > 0:
                b = avg_win / avg_loss
                kelly_full = (win_rate * b - (1 - win_rate)) / b
            else:
                kelly_full = win_rate  # No losses yet

            # Fractional Kelly (half Kelly for safety)
            kelly_fraction = THRESHOLDS["sizing"]["kelly_fraction"]
            position_pct = max(0.005, kelly_full * kelly_fraction)  # Floor at 0.5%
            print(f"  Kelly: WR={win_rate:.0%} AvgW={avg_win:.2%} AvgL={avg_loss:.2%} Full={kelly_full:.2%} → {position_pct:.2%}")
        else:
            position_pct = THRESHOLDS["sizing"]["kelly_default_pct"]
    else:
        position_pct = THRESHOLDS["sizing"]["kelly_default_pct"]  # 2% cold start
        print(f"  Kelly: cold start ({trade_count}/{min_kelly_trades} trades) → {position_pct:.1%}")

    # Paper mode: use smaller sizes for more concurrent learning trades
    try:
        with open(STATE_DIR / "portfolio.json") as _sf:
            _sizing_paper = json.load(_sf).get("mode", "paper") == "paper"
    except Exception:
        _sizing_paper = True

    if _sizing_paper:
        position_pct = min(position_pct, THRESHOLDS["sizing"].get("paper_default_pct", 0.02))
        max_pct = THRESHOLDS["sizing"].get("paper_max_position_pct", 0.05)
    else:
        max_pct = THRESHOLDS["sizing"].get("live_max_position_pct", THRESHOLDS["sizing"]["max_position_pct"])
    position_pct = min(position_pct, max_pct)

    # ── Regime-adjusted position sizing ──
    regime_size_modifier = regime_data.get("implications", {}).get("position_size_modifier", 1.0)
    # Paper mode: floor the regime modifier so sizing stays meaningful
    regime_floor = THRESHOLDS.get("sizing", {}).get("paper_regime_floor", 0.3)
    if THRESHOLDS.get("mode", "paper") == "paper":
        regime_size_modifier = max(regime_size_modifier, regime_floor)
    if regime_size_modifier < 1.0 and regime_tag != "UNKNOWN":
        adjusted_pct = position_pct * regime_size_modifier
        print(f"  Regime sizing: {position_pct*100:.1f}% × {regime_size_modifier:.1f} = {adjusted_pct*100:.1f}%")
        position_pct = adjusted_pct

    # Cap at max
    position_pct = min(position_pct, max_pct)
    position_usd = balance * position_pct

    strategy_result = {
        "strategy_name": strategy_name,
        "position_pct": position_pct,
        "position_usd": round(position_usd, 2),
        "balance_usd": balance,
        "sizing_mode": "cold_start" if trade_count < min_kelly_trades else "fractional_kelly",
        "trade_count": trade_count,
        "exit_rules": matched_exit_rules,
        "regime_tag": regime_tag,
        "regime_size_modifier": regime_size_modifier,
        "thompson_mode": thompson_result.get("mode", "fallback"),
        # v3.0 telemetry
        "eligible_strategies": eligible_by_tier,
        "meme_safety_gate": "N/A",  # set in stage 2.5 if applicable
    }

    print(f"  Strategy: {strategy_name}")
    print(f"  Position Size: {position_pct*100:.1f}% = ${position_usd:.2f}")
    print(f"  Sizing Mode: {strategy_result['sizing_mode']}")

    return strategy_result, None


# ─────────────────────────────────────────────
# STAGE 4: BULL / BEAR DEBATE
# ─────────────────────────────────────────────

def stage_4_debate(signal, sanad_result, strategy_result, profile=None):
    """
    Run Bull (Al-Baqarah) and Bear (Al-Dahhak) debate.
    v3.0: Uses tier-specific prompts based on asset classification.
    Critical rule: NEVER skip the Bear.
    """
    print(f"\n{'='*60}")
    print(f"STAGE 4: BULL / BEAR DEBATE")
    print(f"{'='*60}")
    
    # Determine tier for prompt selection (v3.0)
    simple_tier = TIER_MAP.get(profile.asset_tier, "TIER_3") if profile else "TIER_3"
    print(f"  Using {simple_tier} prompts")

    context = f"""TOKEN: {signal['token']}
THESIS: {signal['thesis']}
SOURCE: {signal['source']}
SANAD TRUST SCORE: {sanad_result.get('trust_score', 'N/A')}/100
SANAD GRADE: {sanad_result.get('grade', 'N/A')}
STRATEGY: {strategy_result.get('strategy_name', 'N/A')}
POSITION SIZE: ${strategy_result.get('position_usd', 'N/A')}
REAL-TIME INTELLIGENCE: {sanad_result.get('perplexity_intel', 'N/A')}
EXCHANGE DATA: {sanad_result.get('price_context', 'N/A')}"""

    # ── BULL (Al-Baqarah) — tier-specific prompt (v3.0) ──
    print(f"  [4a] Bull Al-Baqarah arguing FOR ({simple_tier})...")
    
    # Get tier-specific Bull prompt
    tier_bull_system = get_bull_prompt(simple_tier)
    
    # v3.0: Get RAG context (expert knowledge)
    rag_context = ""
    try:
        from vector_db import get_rag_context
        rag_context = get_rag_context(
            token=signal['token'],
            tier=simple_tier,
            strategy=strategy_result.get("strategy_name", ""),
            regime=strategy_result.get("regime_tag", "UNKNOWN"),
            n_results=2,
        )
        if rag_context:
            print(f"  RAG: Retrieved {len(rag_context.split(chr(10)))} lines of expert knowledge")
    except Exception as e:
        print(f"  RAG: Error ({e})")
    
    # Lint the context to ensure no tier-inappropriate language
    lint_ok, violations = lint_prompt(context, simple_tier, strategy_result.get("strategy_name", ""))
    strategy_result["lint_result"] = "PASS" if lint_ok else f"FAIL: {'; '.join(violations)}"
    if not lint_ok:
        print(f"  WARNING: Context contains tier-inappropriate language:")
        for v in violations:
            print(f"    - {v}")
    
    bull_message = f"""{context}

{rag_context}

Return valid JSON with these exact keys:
{{
  "conviction": <0-100>,
  "thesis": "<2-3 sentence core argument>",
  "entry_price": "<suggested entry price or 'market'>",
  "target_price": "<target price with reasoning>",
  "stop_loss": "<stop-loss price with reasoning>",
  "risk_reward_ratio": "<calculated R:R>",
  "timeframe": "<expected hold duration>",
  "supporting_evidence": [
    "<specific data point 1 with numbers>",
    "<specific data point 2 with numbers>",
    "<specific data point 3 with numbers>",
    "<specific data point 4 with numbers>",
    "<specific data point 5 with numbers>"
  ],
  "catalyst_timeline": "<what needs to happen and when>",
  "risk_acknowledgment": "<2-3 sentences on main risks>",
  "invalidation_point": "<what would make this thesis wrong>"
}}"""

    bull_response = call_claude(
        system_prompt=tier_bull_system,  # v3.0: tier-specific prompt
        user_message=bull_message,
        model="claude-haiku-4-5-20251001",  # Haiku for paper trading (30x cheaper than Opus)
        max_tokens=3000,
        stage="bull_debate",
        token_symbol=signal.get("token", ""),
    )
    bull_result = _parse_json_response(bull_response) if bull_response else None
    if not bull_result:
        print("  WARNING: Bull response parse failed, using defaults")
        bull_result = {"conviction": 50, "thesis": "Parse failed", "supporting_evidence": []}

    # v3.0: Validate evidence completeness
    evidence_list = bull_result.get('supporting_evidence', [])
    sufficient, evidence_count = validate_evidence(evidence_list, simple_tier)
    if not sufficient:
        print(f"  ⚠️ Bull evidence insufficient: {evidence_count} required fields (need 3+)")
        # Downgrade conviction by 20 points
        original_conviction = bull_result.get('conviction', 50)
        bull_result['conviction'] = max(0, original_conviction - 20)
        print(f"    Conviction downgraded: {original_conviction} → {bull_result['conviction']}")

    print(f"  Bull Conviction: {bull_result.get('conviction', 'N/A')}/100")
    print(f"  Bull Thesis: {bull_result.get('thesis', 'N/A')}")
    print(f"  Bull Entry: {bull_result.get('entry_price', 'N/A')}")
    print(f"  Bull Target: {bull_result.get('target_price', 'N/A')}")
    print(f"  Bull Stop-Loss: {bull_result.get('stop_loss', 'N/A')}")
    print(f"  Bull R:R: {bull_result.get('risk_reward_ratio', 'N/A')}")
    print(f"  Bull Timeframe: {bull_result.get('timeframe', 'N/A')}")
    print(f"  Bull Evidence: {json.dumps(bull_result.get('supporting_evidence', []), indent=4)}")
    print(f"  Bull Catalyst: {bull_result.get('catalyst_timeline', 'N/A')}")
    print(f"  Bull Invalidation: {bull_result.get('invalidation_point', 'N/A')}")
    print(f"  Bull Risk Ack: {bull_result.get('risk_acknowledgment', 'N/A')}")

    # ── BEAR (Al-Dahhak) — NEVER SKIP — tier-specific (v3.0) ──
    print(f"  [4b] Bear Al-Dahhak arguing AGAINST ({simple_tier})...")
    
    # Get tier-specific Bear prompt
    tier_bear_system = get_bear_prompt(simple_tier)
    
    # Bear uses same RAG context as Bull (for counter-arguments)
    
    bear_message = f"""{context}

{rag_context}

BULL'S ARGUMENT:
Conviction: {bull_result.get('conviction', 'N/A')}/100
Thesis: {bull_result.get('thesis', 'N/A')}
Entry: {bull_result.get('entry_price', 'N/A')}
Target: {bull_result.get('target_price', 'N/A')}
Stop-Loss: {bull_result.get('stop_loss', 'N/A')}
R:R Ratio: {bull_result.get('risk_reward_ratio', 'N/A')}
Evidence: {json.dumps(bull_result.get('supporting_evidence', []))}
Invalidation: {bull_result.get('invalidation_point', 'N/A')}

Apply your Muḥāsibī pre-reasoning discipline (Khawāṭir → Murāqaba → Mujāhada) first, then attack the Bull's thesis across all 8 vectors. Return valid JSON (you may include reasoning text before the JSON block):
{{
  "conviction": <0-100 where 100 = absolutely DO NOT trade>,
  "thesis": "<2-3 sentence core argument against>",
  "attack_points": [
    "<specific attack on evidence 1>",
    "<specific attack on evidence 2>",
    "<specific attack on evidence 3>",
    "<specific attack on evidence 4>",
    "<specific attack on evidence 5>"
  ],
  "worst_case_scenario": "<quantified worst case with specific numbers>",
  "hidden_risks": [
    "<risk the Bull ignores 1>",
    "<risk the Bull ignores 2>",
    "<risk the Bull ignores 3>"
  ],
  "historical_parallels": "<specific past failure — token, date, outcome>",
  "liquidity_assessment": "<can we actually exit? specific analysis>",
  "timing_assessment": "<early, on time, or late? evidence>",
  "what_must_be_true": "<assumptions that must ALL hold for Bull case>"
}}"""

    bear_response = call_claude(
        system_prompt=tier_bear_system,  # v3.0: tier-specific prompt
        user_message=bear_message,
        model="claude-haiku-4-5-20251001",  # Haiku for paper trading (30x cheaper than Opus)
        max_tokens=5000,
        stage="bear_debate",
        token_symbol=signal.get("token", ""),
    )
    bear_result = _parse_json_response(bear_response) if bear_response else None
    if not bear_result:
        # CRITICAL: If Bear fails, fail closed — cannot trade without opposition
        print("  FAIL-CLOSED: Bear response failed → cannot proceed without opposition")
        return None, None, "Bear agent failed — fail closed (never skip Bear)"

    print(f"  Bear Conviction (against): {bear_result.get('conviction', 'N/A')}/100")
    print(f"  Bear Thesis: {bear_result.get('thesis', 'N/A')}")
    print(f"  Bear Attack Points: {json.dumps(bear_result.get('attack_points', []), indent=4)}")
    print(f"  Bear Worst Case: {bear_result.get('worst_case_scenario', 'N/A')}")
    print(f"  Bear Hidden Risks: {json.dumps(bear_result.get('hidden_risks', []), indent=4)}")
    print(f"  Bear Historical Parallels: {bear_result.get('historical_parallels', 'N/A')}")
    print(f"  Bear Liquidity: {bear_result.get('liquidity_assessment', 'N/A')}")
    print(f"  Bear Timing: {bear_result.get('timing_assessment', 'N/A')}")
    print(f"  Bear Must Be True: {bear_result.get('what_must_be_true', 'N/A')}")

    return bull_result, bear_result, None


# ─────────────────────────────────────────────
# STAGE 5: AL-MUHASBI JUDGE
# ─────────────────────────────────────────────

def stage_5_judge(signal, sanad_result, strategy_result, bull_result, bear_result, profile=None):
    """
    Al-Muhasbi Judge — independent GPT-powered review.
    v3.0: Adds tier-specific veto rules.
    Verdict: APPROVE / REJECT / REVISE.
    Mandate: capital preservation, when in doubt REJECT.
    CRITICAL: Never override REJECT.
    """
    print(f"\n{'='*60}")
    print(f"STAGE 5: AL-MUHASBI JUDGE")
    print(f"{'='*60}")
    
    # Get tier for veto rules (v3.0)
    simple_tier = TIER_MAP.get(profile.asset_tier, "TIER_3") if profile else "TIER_3"
    circulating_pct = (profile.circulating_pct if profile else 100) or 100

    judge_message = f"""TRADE PROPOSAL FOR REVIEW:

TOKEN: {signal['token']}
THESIS: {signal['thesis']}
SOURCE: {signal['source']}

SANAD VERIFICATION:
- Trust Score: {sanad_result.get('trust_score', 'N/A')}/100
- Grade: {sanad_result.get('grade', 'N/A')}
- Source Grade: {sanad_result.get('source_grade', 'N/A')}
- Chain Integrity: {sanad_result.get('chain_integrity', 'N/A')}
- Corroboration: {sanad_result.get('corroboration_level', 'N/A')}
- Recommendation: {sanad_result.get('recommendation', 'N/A')}
- Key Findings: {json.dumps(sanad_result.get('key_findings', []))}
- Rugpull Flags: {json.dumps(sanad_result.get('rugpull_flags', []))}
- Sybil Risk: {sanad_result.get('sybil_risk', 'N/A')}

STRATEGY:
- Name: {strategy_result.get('strategy_name', 'N/A')}
- Position Size: ${strategy_result.get('position_usd', 'N/A')} ({(strategy_result.get('position_pct') or 0)*100:.1f}%)

BULL CASE (Al-Baqarah):
- Conviction: {bull_result.get('conviction', 'N/A')}/100
- Thesis: {bull_result.get('thesis', 'N/A')}
- Entry: {bull_result.get('entry_price', 'N/A')}
- Target: {bull_result.get('target_price', 'N/A')}
- Stop-Loss: {bull_result.get('stop_loss', 'N/A')}
- R:R Ratio: {bull_result.get('risk_reward_ratio', 'N/A')}
- Evidence: {json.dumps(bull_result.get('supporting_evidence', []))}
- Catalyst: {bull_result.get('catalyst_timeline', 'N/A')}
- Invalidation: {bull_result.get('invalidation_point', 'N/A')}

BEAR CASE (Al-Dahhak):
- Conviction Against: {bear_result.get('conviction', 'N/A')}/100
- Thesis: {bear_result.get('thesis', 'N/A')}
- Attack Points: {json.dumps(bear_result.get('attack_points', []))}
- Worst Case: {bear_result.get('worst_case_scenario', 'N/A')}
- Hidden Risks: {json.dumps(bear_result.get('hidden_risks', []))}
- Historical Parallels: {bear_result.get('historical_parallels', 'N/A')}
- Liquidity Assessment: {bear_result.get('liquidity_assessment', 'N/A')}
- Timing Assessment: {bear_result.get('timing_assessment', 'N/A')}
- Must Be True: {bear_result.get('what_must_be_true', 'N/A')}

TIER-SPECIFIC VETO RULES (v3.0 — ENFORCE STRICTLY):
- TIER_1 justified by "social media momentum" / "meme narrative" → VETO (wrong analytical framework)
- TIER_3 justified by "macro-economic factors" / "institutional flow" → VETO (wrong analytical framework)
- TIER_2 missing FDV analysis when circulating <30% → VETO (tokenomics blind spot)
- TIER_3 missing holder concentration / LP lock data → VETO (on-chain blind spot)
- Bull conviction >70 without 3+ evidence fields → VETO (overconfident without data)
- Empty or missing disconfirmation analysis → downgrade confidence 15 points

Current tier: {simple_tier}
Circulating supply: {(circulating_pct or 100):.1f}% (relevant for TIER_2 FDV analysis)

Execute your full 5-step Muḥāsibī discipline (Khawāṭir → Murāqaba → Mujāhada → 7-point checklist → Verdict). Return ONLY valid JSON:
{{
  "khawatir": [
    {{"impulse": "...", "classification": "nafs|waswas|genuine"}},
    {{"impulse": "...", "classification": "nafs|waswas|genuine"}},
    {{"impulse": "...", "classification": "nafs|waswas|genuine"}}
  ],
  "muraqaba_biases_caught": ["bias 1", "bias 2"],
  "mujahada_uncomfortable_truth": "...",
  "checklist": {{
    "cognitive_bias": {{"rating": "PASS|FLAG|FAIL", "conviction": <1-10>, "detail": "..."}},
    "statistical_review": {{"rating": "PASS|FLAG|FAIL", "conviction": <1-10>, "detail": "..."}},
    "risk_assessment": {{"rating": "PASS|FLAG|FAIL", "conviction": <1-10>, "detail": "..."}},
    "sanad_integrity": {{"rating": "PASS|FLAG|FAIL", "conviction": <1-10>, "detail": "..."}},
    "bear_case_strength": {{"rating": "PASS|FLAG|FAIL", "conviction": <1-10>, "detail": "..."}},
    "market_context": {{"rating": "PASS|FLAG|FAIL", "conviction": <1-10>, "detail": "..."}},
    "shariah_compliance": {{"rating": "PASS|FLAG|CONCERN", "conviction": <1-10>, "detail": "..."}}
  }},
  "verdict": "APPROVE|REJECT|REVISE",
  "confidence_score": <0-100>,
  "reasoning": "<5-7 sentences with specific evidence references>",
  "conditions": ["<if REVISE>"] or [],
  "position_size_recommendation": "FULL|REDUCE_TO_X%|REJECT",
  "key_concern": "<single most important concern>"
}}"""

    # Detect paper mode for prompt calibration
    try:
        with open(STATE_DIR / "portfolio.json") as _jf:
            _judge_paper = json.load(_jf).get("mode", "paper") == "paper"
    except Exception:
        _judge_paper = True

    judge_system = JUDGE_PROMPT
    if _judge_paper:
        judge_system += """

--- PAPER MODE CALIBRATION (OVERRIDES ALL PRIOR RULES) ---
MODE: PAPER TRADING (simulated, no real money at risk)

CRITICAL OVERRIDE: In paper mode, ALL live-mode hard rules about minimum trust scores are SUSPENDED.
Do NOT reject based on trust_score < 70. The trust threshold for paper mode is 15, not 70.
Do NOT reject based on strategy mismatch alone.
Do NOT reject based on "bear market conditions" alone.

PAPER MODE VERDICT RULES (these OVERRIDE the base rules above):
- If your confidence_score >= 60: your verdict MUST be APPROVE. Confidence >= 60 with REJECT is a logical contradiction in paper mode.
- If your confidence_score >= 40: verdict should be APPROVE unless rugpull flags are present.
- REJECT ONLY when: rugpull flags present, OR R:R < 1:1, OR bear_conviction > 90 with specific structural fraud/scam evidence.
- Default stance: APPROVE. We need executions to generate learning data.

The purpose of paper trading is to LEARN. Every rejected trade is a missed learning opportunity.
We need trade outcomes (wins AND losses) to calibrate Kelly sizing, UCB1 source grading,
Thompson strategy selection, and Genius Memory patterns.
A paper loss of $50 that teaches the system a pattern is worth more than a paper rejection that teaches nothing.
--- END PAPER MODE ---"""
        print("  [5a] Al-Muhasbi reviewing via GPT-5.2 (PAPER MODE — learning calibration)...")
    else:
        print("  [5a] Al-Muhasbi reviewing via GPT-5.2...")

    judge_response = call_openai_responses(
        system_prompt=judge_system,
        user_message=judge_message,
        model="gpt-5.2-pro",
        max_tokens=8000,
        stage="judge",
        token_symbol=signal.get("token", ""),
    )

    judge_result = _parse_json_response(judge_response) if judge_response else None
    if not judge_result:
        # Fail-closed: if judge can't render verdict, REJECT
        print("  FAIL-CLOSED: Al-Muhasbi returned no parseable verdict → REJECT")
        return {
            "verdict": "REJECT",
            "confidence_score": 0,
            "reasoning": "Al-Muhasbi API failure — fail closed, when in doubt REJECT",
        }, None

    verdict = judge_result.get("verdict", "REJECT")
    confidence = judge_result.get("confidence_score", 0) or 0

    # Infer confidence from verdict if missing/zero (Judge API failure fallback)
    if confidence <= 0 and verdict in ("APPROVE", "REVISE"):
        if verdict == "APPROVE":
            confidence = 65
            print(f"  ⚠️ Inferred confidence 65 from APPROVE verdict (model returned {judge_result.get('confidence_score', 0)})")
        elif verdict == "REVISE":
            confidence = 45
            print(f"  ⚠️ Inferred confidence 45 from REVISE verdict (model returned {judge_result.get('confidence_score', 0)})")
        judge_result["confidence_score"] = confidence
        judge_result["inferred_confidence"] = True

    # Paper mode deterministic override: confidence >= 60 = APPROVE (belt-and-suspenders)
    if _judge_paper and confidence >= 60 and verdict == "REJECT":
        rugpull_flags = sanad_result.get("rugpull_flags", [])
        if not rugpull_flags:
            print(f"  ⚡ PAPER OVERRIDE: confidence={confidence} >= 60 + no rugpulls → forcing APPROVE")
            verdict = "APPROVE"
            judge_result["verdict"] = "APPROVE"
            judge_result["paper_override"] = True

    # Track judge verdict in funnel
    if verdict == "APPROVE":
        _funnel("judge_approved")
    elif verdict == "REVISE":
        _funnel("judge_revised")
    else:
        _funnel("judge_rejected")

    # v3.0: Apply tier-specific veto rules (hard overrides)
    veto_triggered = False
    veto_reason = None
    
    bull_thesis_lower = (bull_result.get('thesis', '') + ' ' + ' '.join(bull_result.get('supporting_evidence', []))).lower()
    
    if simple_tier == "TIER_1":
        # TIER_1: veto if using meme/social language
        forbidden = ["social media momentum", "meme narrative", "viral", "community hype"]
        for kw in forbidden:
            if kw in bull_thesis_lower:
                veto_triggered = True
                veto_reason = f"TIER_1 veto: Bull uses inappropriate language '{kw}'"
                break
    
    elif simple_tier == "TIER_3":
        # TIER_3: veto if using macro language
        forbidden = ["macro-economic", "macroeconomic", "institutional flow", "etf inflow", "federal reserve"]
        for kw in forbidden:
            if kw in bull_thesis_lower:
                veto_triggered = True
                veto_reason = f"TIER_3 veto: Bull uses inappropriate language '{kw}'"
                break
        
        # TIER_3: veto if missing holder/LP data
        evidence_text = ' '.join(bull_result.get('supporting_evidence', [])).lower()
        if "holder concentration" not in evidence_text and "top 10" not in evidence_text:
            veto_triggered = True
            veto_reason = "TIER_3 veto: Missing holder concentration analysis"
        elif "lp lock" not in evidence_text and "liquidity lock" not in evidence_text:
            veto_triggered = True
            veto_reason = "TIER_3 veto: Missing LP lock analysis"
    
    elif simple_tier == "TIER_2":
        # TIER_2: veto if circulating <30% and no FDV analysis
        if circulating_pct < 30:
            evidence_text = ' '.join(bull_result.get('supporting_evidence', [])).lower()
            if "fdv" not in evidence_text and "fully diluted" not in evidence_text:
                veto_triggered = True
                veto_reason = f"TIER_2 veto: Circulating {(circulating_pct or 100):.1f}% but no FDV analysis"
    
    # Universal veto: high conviction without evidence
    if bull_result.get('conviction', 0) > 70:
        evidence_count = len(bull_result.get('supporting_evidence', []))
        if evidence_count < 3:
            veto_triggered = True
            veto_reason = f"Universal veto: Conviction {bull_result['conviction']} >70 with only {evidence_count} evidence fields"
    
    if veto_triggered:
        print(f"\n  ⛔ TIER VETO TRIGGERED: {veto_reason}")
        verdict = "REJECT"
        judge_result["verdict"] = "REJECT"
        judge_result["tier_veto"] = veto_reason
        confidence = 0

    print(f"  Verdict: {verdict}")
    print(f"  Confidence: {confidence}/100")
    print(f"  Reasoning: {judge_result.get('reasoning', 'N/A')[:100]}...")

    # Print Muḥāsibī framework details if present
    if judge_result.get("khawatir"):
        print(f"\n  ── Muḥāsibī Reasoning ──")
        for k in judge_result["khawatir"]:
            print(f"    Khawāṭir: [{k.get('classification','?')}] {k.get('impulse','')[:80]}")
    if judge_result.get("muraqaba_biases_caught"):
        for b in judge_result["muraqaba_biases_caught"]:
            print(f"    Bias caught: {b[:80]}")
    if judge_result.get("mujahada_uncomfortable_truth"):
        print(f"    Uncomfortable truth: {judge_result['mujahada_uncomfortable_truth'][:120]}")
    if judge_result.get("checklist"):
        print(f"    Checklist:")
        for check, data in judge_result["checklist"].items():
            if isinstance(data, dict):
                print(f"      {check}: {data.get('rating','?')} (conviction {data.get('conviction','?')}/10)")

    return judge_result, None


# ─────────────────────────────────────────────
# STAGE 6: POLICY ENGINE
# ─────────────────────────────────────────────

def stage_6_policy_engine(signal, sanad_result, strategy_result, bull_result, bear_result, judge_result):
    """
    Run the 15-gate deterministic Policy Engine.
    Builds a decision packet from all prior stages and feeds it to policy_engine.py.
    """
    print(f"\n{'='*60}")
    print(f"STAGE 6: POLICY ENGINE (15 Gates)")
    print(f"{'='*60}")

    # Build decision packet for Policy Engine
    symbol = signal.get("symbol", signal["token"] + "USDT")
    current_price = binance_client.get_price(symbol)

    # Get real slippage and spread
    slippage = binance_client.estimate_slippage_bps(symbol, "BUY", strategy_result.get("position_usd", 200))
    spread = binance_client.get_spread_bps(symbol)

    decision_packet = {
        "token": {
            "symbol": signal["token"],
            "deployment_timestamp": (datetime.now(timezone.utc) - timedelta(hours=signal.get("token_age_hours", 999))).isoformat(),
        },
        "symbol": symbol,
        "venue": "DEX" if signal.get("chain") else "CEX",
        "exchange": signal.get("exchange", "binance"),
        "strategy_name": strategy_result.get("strategy_name", ""),
        "data_timestamps": {
            "price_timestamp": datetime.now(timezone.utc).isoformat(),
            "onchain_timestamp": signal.get("onchain_timestamp", datetime.now(timezone.utc).isoformat()),
        },
        "current_price": current_price,
        "sanad_verification": {
            "sanad_trust_score": sanad_result.get("trust_score", 0),
            "grade": sanad_result.get("grade", "FAILED"),
            "recommendation": sanad_result.get("recommendation", "BLOCK"),
            "rugpull_flags": sanad_result.get("rugpull_flags", []),
        },
        "market_data": {
            "estimated_slippage_bps": slippage if slippage is not None else 0,
            "spread_bps": spread if spread is not None else 0,
            "depth_sufficient": True if (slippage is not None and slippage < 99999) else False,
            "price_change_pct_window": signal.get("volatility_30min_pct", 0),
        },
        "trade_intent": {
            "position_size_pct": strategy_result.get("position_pct", 0),
            "position_usd": strategy_result.get("position_usd", 0),
        },
        "trade_confidence_score": 0 if judge_result.get("verdict") == "REJECT" else judge_result.get("confidence_score", 0),
        "almuhasbi_verdict": judge_result.get("verdict", "REJECT"),
        "almuhasbi_confidence": judge_result.get("confidence_score", 0),
        "dex_sim_result": signal.get("dex_sim_result", None),
        "volatility_30min_pct": signal.get("volatility_30min_pct", 0),
        "has_verified_catalyst": signal.get("verified_catalyst", False),
    }

    # Write decision packet to temp file for Policy Engine
    packet_path = STATE_DIR / "current_decision_packet.json"
    with open(packet_path, "w") as f:
        json.dump(decision_packet, f, indent=2)

    # Run Policy Engine
    import subprocess
    result = subprocess.run(
        ["python3", str(SCRIPTS_DIR / "policy_engine.py"), str(packet_path)],
        capture_output=True,
        text=True,
    )

    policy_output = result.stdout.strip()
    policy_exit = result.returncode

    # Parse Policy Engine result
    policy_result = {
        "exit_code": policy_exit,
        "result": "PASS" if policy_exit == 0 else "BLOCK",
        "output": policy_output,
        "decision_packet": decision_packet,
    }

    if policy_exit == 0:
        print(f"  RESULT: PASS — All 15 gates cleared")
    else:
        print(f"  RESULT: BLOCK — {policy_output}")

    return policy_result, None


# ─────────────────────────────────────────────
# STAGE 7: EXECUTE / LOG
# ─────────────────────────────────────────────

def stage_7_execute(signal, sanad_result, strategy_result, bull_result, bear_result, judge_result, policy_result, profile=None):
    """
    Execute trade (paper mode) or log rejection.
    All decisions logged to Supabase with full decision packet.
    """
    print(f"\n{'='*60}")
    print(f"STAGE 7: EXECUTE / LOG")
    print(f"{'='*60}")

    correlation_id = signal.get("correlation_id", "unknown")
    final_action = "EXECUTE" if policy_result["result"] == "PASS" else "REJECT"
    if final_action == "EXECUTE":
        _funnel("executed")
    elif policy_result["result"] != "PASS":
        _funnel("policy_blocked", policy_result.get("gate_failed", "unknown"))
    rejection_reason = policy_result.get("output", "") if final_action == "REJECT" else None

    # Build full decision record (v3.0: include token profile)
    profile_dict = profile.to_dict() if profile else {}
    
    decision_record = {
        "correlation_id": correlation_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signal": {
            "token": signal["token"],
            "source": signal["source"],
            "thesis": signal["thesis"],
        },
        "token_profile": profile_dict,  # v3.0
        "asset_tier": profile.asset_tier if profile else "UNKNOWN",  # v3.0
        "regime_tag": strategy_result.get("regime_tag", "UNKNOWN"),  # v3.0 telemetry
        "eligible_strategies": strategy_result.get("eligible_strategies", []),  # v3.0
        "selected_strategy": strategy_result.get("strategy_name", "NONE"),  # v3.0
        "meme_safety_gate": strategy_result.get("meme_safety_gate", "N/A"),  # v3.0
        "lint_result": strategy_result.get("lint_result", "N/A"),  # v3.0
        "sanad": {
            "trust_score": sanad_result.get("trust_score", 0),
            "grade": sanad_result.get("grade", "FAILED"),
            "recommendation": sanad_result.get("recommendation", "BLOCK"),
            "rugpull_flags": sanad_result.get("rugpull_flags", []),
        },
        "corroboration": {
            "cross_source_count": signal.get("cross_source_count", 1),
            "cross_sources": signal.get("cross_sources", []),
            "corroboration_level": signal.get("corroboration_level", "AHAD"),
            "corroboration_quality": signal.get("corroboration_quality", "STRONG"),
            "corroboration_override": sanad_result.get("corroboration_override"),
        },
        "strategy": strategy_result,
        "bull": {
            "conviction": bull_result.get("conviction", 0) if bull_result else 0,
            "thesis": bull_result.get("thesis", "") if bull_result else "",
        },
        "bear": {
            "conviction": bear_result.get("conviction", 0) if bear_result else 0,
            "attack_points": bear_result.get("attack_points", []) if bear_result else [],
        },
        "judge": {
            "verdict": judge_result.get("verdict", "REJECT"),
            "confidence_score": judge_result.get("confidence_score", 0),
            "reasoning": judge_result.get("reasoning", ""),
        },
        "policy_engine": {
            "result": policy_result["result"],
            "exit_code": policy_result["exit_code"],
        },
        "final_action": final_action,
        "rejection_reason": rejection_reason,
    }

    if final_action == "EXECUTE":
        # Paper trade execution
        token = signal.get("token")
        if not token:
            print(f"  CRITICAL: signal missing 'token' field - cannot execute")
            decision_record["final_action"] = "REJECT"
            decision_record["rejection_reason"] = "Missing token field"
            return decision_record
        
        symbol = signal.get("symbol", token + "USDT")
        current_price = policy_result.get("decision_packet", {}).get("current_price", 0) or signal.get("price", 0) or 1
        if current_price <= 0:
            print(f"  CRITICAL: invalid price {current_price} - cannot calculate quantity")
            decision_record["final_action"] = "REJECT"
            decision_record["rejection_reason"] = f"Invalid price: {current_price}"
            return decision_record
        
        # Check if this is a REVISE probe (paper mode learning trade)
        verdict = judge_result.get("verdict", "REJECT")
        is_revise_probe = (verdict == "REVISE" and _load_json(STATE_DIR / "portfolio.json", {}).get("mode", "paper") == "paper")
        
        # Apply micro-sizing for REVISE probes
        base_position_usd = strategy_result.get("position_usd", 200)
        if is_revise_probe:
            # Micro-probe: cap at $25 for REVISE trades (learning data, not conviction)
            PAPER_REVISE_PROBE_USD = 25
            position_usd = min(base_position_usd, PAPER_REVISE_PROBE_USD)
            decision_record["execution_mode"] = "paper_probe_revise"
            print(f"  REVISE PROBE: Micro-sizing {base_position_usd} → ${position_usd} (learning mode)")
        else:
            position_usd = base_position_usd
            decision_record["execution_mode"] = "paper_standard"
        
        quantity = position_usd / current_price

        # Partial fill simulation
        try:
            from partial_fill_sim import simulate_fill
            fill_result = simulate_fill(
                order_size_usd=position_usd,  # Use adjusted position_usd (may be micro-sized for REVISE)
                liquidity_usd=signal.get("volume_24h", 1000000) / 24,
            )
            if fill_result.get("fill_pct", 1.0) < 0.5:
                # Less than 50% fill expected — skip trade
                print(f"  Partial fill sim: only {fill_result['fill_pct']*100:.0f}% fill expected — SKIPPING")
                # Return consistent dict format
                decision_record["final_action"] = "REJECT"
                decision_record["rejection_reason"] = "Partial fill too low"
                return decision_record
        except Exception as e:
            print(f"  Partial fill sim error ({e}) — proceeding")

        # Use exchange-agnostic paper execution (no live API calls)
        try:
            import paper_execution
            
            # Get execution parameters from decision record
            exec_params = paper_execution.get_execution_parameters(decision_record)
            
            # Get price from decision record (already validated)
            current_price = exec_params.get("price")
            if not current_price:
                # Fallback to current_price_checked from earlier
                current_price = decision_record.get("strategy", {}).get("current_price")
            
            if not current_price or current_price <= 0:
                raise ValueError(f"No valid price available: {current_price}")
            
            venue = exec_params.get("venue", "CEX")
            exchange = exec_params.get("exchange", "binance")
            liquidity_usd = exec_params.get("liquidity_usd")
            
            print(f"  EXECUTING PAPER TRADE: BUY {quantity:.6f} {symbol}")
            print(f"  Venue: {venue}, Exchange: {exchange}, Price: ${current_price:,.4f}")
            
            order_result = paper_execution.execute_paper_trade(
                token=signal["token"],
                symbol=symbol,
                side="BUY",
                quantity=quantity,
                decision_price=current_price,
                venue=venue,
                exchange=exchange,
                liquidity_usd=liquidity_usd
            )
            
            if order_result.get("success"):
                decision_record["execution"] = {
                    "order_id": order_result["orderId"],
                    "fill_price": order_result["price"],
                    "quantity": order_result["quantity"],
                    "fee_usd": order_result["fee_usd"],
                    "venue": order_result["venue"],
                    "exchange": order_result["exchange"],
                    "slippage_pct": order_result.get("slippage_pct", 0),
                }
                order = {
                    "orderId": order_result["orderId"],
                    "price": order_result["price"],
                    "quantity": order_result["quantity"],
                    "fee_usd": order_result["fee_usd"],
                }
                print(f"  Paper trade filled: {order['orderId']} @ ${order['price']:,.4f}")
            else:
                error_detail = order_result.get("detail", order_result.get("error", "Unknown"))
                decision_record["execution"] = {
                    "error": "Paper order failed",
                    "detail": error_detail,
                    "venue": venue,
                    "exchange": exchange,
                }
                print(f"  WARNING: Paper order execution failed: {error_detail}")
                order = None
                
        except Exception as e:
            import traceback
            error_detail = f"{type(e).__name__}: {str(e)}"
            decision_record["execution"] = {
                "error": "Paper order failed",
                "detail": error_detail,
                "traceback": traceback.format_exc()[-500:],  # Last 500 chars
            }
            print(f"  ERROR: Paper execution exception: {error_detail}")
            order = None

        if order:
            # Update positions state (pass execution_mode for tracking)
            execution_mode = decision_record.get("execution_mode", "paper_standard")
            _add_position(signal, strategy_result, order, sanad_result, bull_result, execution_mode=execution_mode)

            # ── TELEGRAM NOTIFICATION ──
            if HAS_NOTIFIER:
                try:
                    size_usd = order['quantity'] * order['price']
                    strat = strategy_result.get('strategy_name', '?') if strategy_result else '?'
                    sanad_sc = sanad_result.get('trust_score', '?') if sanad_result else '?'
                    notifier.send(
                        f"BUY {signal['token']}/USDT\n\n"
                        f"Entry: {order['price']:,.4f}\n"
                        f"Size: {size_usd:,.0f} ({order['quantity']:,.2f} units)\n\n"
                        f"Strategy: {strat}\n"
                        f"Sanad Score: {sanad_sc}\n"
                        f"Fee: {order['fee_usd']:,.2f}\n\n"
                        f"All 15 policy gates passed",
                        level="L2",
                        title=f"BUY {signal['token']}"
                    )
                except Exception as e:
                    print(f"  Telegram notification error: {e}")
    else:
        rejection_reason_short = str(rejection_reason)[:200] if rejection_reason else "Unknown"
        print(f"  REJECTED: {rejection_reason_short}")

        # Notify rejections too (L1 = low priority)
        if HAS_NOTIFIER:
            try:
                notifier.send(
                    f"🔴 *Signal Rejected*\n\n"
                    f"Token: {signal.get('token', '?')}\n"
                    f"Reason: {rejection_reason_short}",
                    level="L1",
                    title="Signal Rejected"
                )
            except Exception:
                pass

    # Log to execution-logs
    _log_decision(decision_record)

    # Log to Supabase
    _sync_to_supabase(decision_record)

    # Print pipeline timing
    try:
        start = datetime.fromisoformat(signal["pipeline_start"])
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        print(f"\n  Pipeline completed in {elapsed:.1f}s")
    except Exception:
        pass

    return decision_record


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _parse_json_response(text):
    """Extract JSON from LLM response (handles markdown fences, preamble)."""
    if not text:
        return None

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code fence
    import re
    json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding first { ... } block
    brace_start = text.find('{')
    brace_end = text.rfind('}')
    if brace_start >= 0 and brace_end > brace_start:
        try:
            return json.loads(text[brace_start:brace_end + 1])
        except json.JSONDecodeError:
            pass

    return None


def _load_state(filename):
    """Load a state JSON file."""
    try:
        with open(STATE_DIR / filename, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _calc_stop_pct_with_strategy(entry_price, bull_result, strategy_result):
    """Calculate stop-loss using strategy exit rules, with Bull override if valid."""
    exit_rules = strategy_result.get("exit_rules", {})
    strategy_sl = exit_rules.get("stop_loss_pct")

    # Try Bull's specific price first
    bull_sl = _calc_stop_pct(entry_price, bull_result) if bull_result else None

    if strategy_sl:
        strategy_sl_dec = strategy_sl / 100  # Convert from 5 → 0.05
        if bull_sl and 0.03 <= bull_sl <= 0.25:
            # Use Bull's if reasonable, but floor at strategy minimum
            return max(bull_sl, strategy_sl_dec * 0.5)  # Don't go tighter than half strategy default
        return strategy_sl_dec
    elif bull_sl and 0.03 <= bull_sl <= 0.25:
        return bull_sl
    return THRESHOLDS["risk"]["stop_loss_default_pct"]


def _calc_tp_pct_with_strategy(entry_price, bull_result, strategy_result):
    """Calculate take-profit using strategy exit rules, with Bull override if valid."""
    exit_rules = strategy_result.get("exit_rules", {})
    strategy_tp = exit_rules.get("take_profit_pct")

    bull_tp = _calc_tp_pct(entry_price, bull_result) if bull_result else None

    if strategy_tp:
        strategy_tp_dec = strategy_tp / 100  # Convert from 20 → 0.20
        if bull_tp and 0.10 <= bull_tp <= 5.0:
            # Use Bull's if reasonable, capped at 2x strategy default
            return min(bull_tp, strategy_tp_dec * 2)
        return strategy_tp_dec
    elif bull_tp and 0.10 <= bull_tp <= 5.0:
        return bull_tp
    return THRESHOLDS["risk"]["take_profit_default_pct"]


def _calc_stop_pct(entry_price, bull_result):
    """Calculate stop-loss percentage from Bull's specific price, with safety bounds."""
    try:
        stop_str = str(bull_result.get("stop_loss", ""))
        # Extract numeric value from string like "$0.00000410"
        stop_price = float(stop_str.replace("$", "").replace(",", "").split()[0])
        if stop_price <= 0 or stop_price >= entry_price:
            return THRESHOLDS["risk"]["stop_loss_default_pct"]
        pct = (entry_price - stop_price) / entry_price
        # Safety bounds: minimum 3%, maximum 25%
        return max(0.03, min(0.25, pct))
    except (ValueError, TypeError, IndexError):
        return THRESHOLDS["risk"]["stop_loss_default_pct"]


def _calc_tp_pct(entry_price, bull_result):
    """Calculate take-profit percentage from Bull's specific target, with safety bounds."""
    try:
        target_str = str(bull_result.get("target_price", ""))
        target_price = float(target_str.replace("$", "").replace(",", "").split()[0])
        if target_price <= entry_price:
            return THRESHOLDS["risk"]["take_profit_default_pct"]
        pct = (target_price - entry_price) / entry_price
        # Safety bounds: minimum 10%, maximum 500%
        return max(0.10, min(5.0, pct))
    except (ValueError, TypeError, IndexError):
        return THRESHOLDS["risk"]["take_profit_default_pct"]


def _add_position(signal, strategy_result, order, sanad_result, bull_result=None, execution_mode="paper_standard"):
    """Add position to positions.json state file."""
    try:
        positions = _load_state("positions.json")
        pos_list = positions.get("positions", [])

        new_position = {
            "id": order["orderId"],
            "token": signal["token"],
            "symbol": signal.get("symbol", signal["token"] + "USDT"),
            "exchange": signal.get("exchange", "binance"),
            "side": signal.get("direction", "LONG").upper(),  # Support SHORT direction
            "entry_price": order["price"],
            "current_price": order["price"],
            "quantity": order["quantity"],
            "position_usd": strategy_result.get("position_usd", 0),
            "stop_loss_pct": _calc_stop_pct_with_strategy(order["price"], bull_result, strategy_result),
            "take_profit_pct": _calc_tp_pct_with_strategy(order["price"], bull_result, strategy_result),
            "bull_stop_loss": bull_result.get("stop_loss", "N/A") if bull_result else "N/A",
            "bull_target_price": bull_result.get("target_price", "N/A") if bull_result else "N/A",
            "bull_entry_price": bull_result.get("entry_price", "N/A") if bull_result else "N/A",
            "risk_reward_ratio": bull_result.get("risk_reward_ratio", "N/A") if bull_result else "N/A",
            "bull_invalidation": bull_result.get("invalidation_point", "N/A") if bull_result else "N/A",
            "bull_timeframe": bull_result.get("timeframe", "N/A") if bull_result else "N/A",
            "strategy_name": strategy_result.get("strategy_name", ""),
            "signal_source": signal.get("source", "unknown"),
            "signal_source_canonical": _canonicalize_source(signal.get("source", "unknown")),
            "sanad_score": sanad_result.get("trust_score", 0),
            "regime_tag": strategy_result.get("regime_tag", "UNKNOWN"),
            "regime_size_modifier": strategy_result.get("regime_size_modifier", 1.0),
            "thompson_mode": strategy_result.get("thompson_mode", "fallback"),
            "execution_mode": execution_mode,  # Track if this is a REVISE probe
            "status": "OPEN",
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        pos_list.append(new_position)
        positions["positions"] = pos_list

        # Update portfolio
        portfolio = _load_state("portfolio.json")
        portfolio["open_position_count"] = len([p for p in pos_list if p["status"] == "OPEN"])

        with open(STATE_DIR / "positions.json", "w") as f:
            json.dump(positions, f, indent=2)
        with open(STATE_DIR / "portfolio.json", "w") as f:
            json.dump(portfolio, f, indent=2)
    except Exception as e:
        print(f"[PIPELINE] Error updating positions: {e}")


def _check_fast_track(signal):
    """Paper mode fast-track for high-confidence corroborated signals. TODO: implement."""
    return None


def _check_fast_track(signal):
    """
    Paper-mode fast-track for high-confidence corroborated Tier 1/2 signals.
    Skips Sanad LLM, Bull/Bear debate, and Judge — saves ~$0.30 and ~4 min.
    Returns decision record if fast-tracked, None otherwise.
    """
    # Only in paper mode
    try:
        with open(STATE_DIR / "portfolio.json") as f:
            portfolio = json.load(f)
        if portfolio.get("mode") != "paper":
            return None
    except Exception:
        return None

    # All conditions must be true
    cross_count = signal.get("cross_source_count", 1)
    router_score = signal.get("router_score", 0)
    volume = signal.get("volume_24h", 0) or 0

    # Classify token
    try:
        from token_profile import TokenProfile, classify_asset
        profile_data = dict(signal)
        if 'symbol' not in profile_data and 'token' in profile_data:
            profile_data['symbol'] = profile_data['token']
        profile = TokenProfile.from_dict(profile_data)
        tier = classify_asset(profile)
    except Exception:
        return None

    # Fast-track conditions
    if tier not in ("TIER_1_MACRO", "TIER_2_ALT_LARGE", "TIER_2_ALT_MID"):
        return None
    if cross_count < 2:
        return None
    if volume < 1_000_000:
        return None
    if router_score < 60:
        return None

    # Check no rugpull flags
    onchain = signal.get("onchain_evidence", {})
    if onchain.get("rugpull_scan", {}).get("verdict") in ("RUG", "BLACKLISTED"):
        return None

    print(f"\n⚡ FAST-TRACK: {signal.get('token')} — {tier}, {cross_count} sources, vol ${volume:,.0f}, score {router_score}")

    # Build deterministic trust score from corroboration
    quality = signal.get("corroboration_quality", "WEAK")
    CORR_STRONG = {"AHAD": 10, "MASHHUR": 18, "TAWATUR": 25, "TAWATUR_QAWIY": 30}
    CORR_WEAK = {"AHAD": 10, "MASHHUR": 14, "TAWATUR": 18, "TAWATUR_QAWIY": 22}
    corr_level = signal.get("corroboration_level", "AHAD")
    corr_pts = (CORR_WEAK if quality == "WEAK" else CORR_STRONG).get(corr_level, 10)
    # Base trust: 60 for Tier 1/2 + corroboration bonus
    trust_score = min(100, 60 + corr_pts)

    # Strategy match (still deterministic)
    strategy_result, error = stage_3_strategy_match(signal, {
        "trust_score": trust_score, "grade": "Mashhur" if cross_count >= 2 else "Ahad",
        "recommendation": "PROCEED", "rugpull_flags": [],
    }, profile)
    if error:
        print(f"  Fast-track blocked at strategy match: {error}")
        return None

    # Policy engine (still runs all 15 gates)
    judge_result = {"verdict": "APPROVE", "confidence_score": 75, "reasoning": "Paper fast-track: Tier 1/2 corroborated signal"}
    policy_result = stage_6_policy_engine(signal, {
        "trust_score": trust_score, "recommendation": "PROCEED",
        "rugpull_flags": [], "grade": "Mashhur",
    }, strategy_result, {"conviction": 60, "thesis": "Fast-track"}, {"conviction": 40, "attack_points": []}, judge_result)

    if isinstance(policy_result, tuple):
        policy_result = policy_result[0] if policy_result[0] else {"result": "FAIL", "output": str(policy_result[1])}

    if policy_result.get("result") != "PASS":
        print(f"  Fast-track blocked by policy engine: {policy_result.get('output', 'unknown gate')}")
        return None

    # Execute
    decision_record = stage_7_execute(signal, {
        "trust_score": trust_score, "recommendation": "PROCEED",
        "rugpull_flags": [], "grade": "Mashhur",
    }, strategy_result, {"conviction": 60, "thesis": "Fast-track"}, {"conviction": 40, "attack_points": []}, judge_result, policy_result)

    decision_record["fast_track"] = True
    if decision_record.get("final_action") == "EXECUTE":
        _funnel("fast_tracked")
        _funnel("executed")
    print(f"\n⚡ FAST-TRACK COMPLETE — {decision_record.get('final_action', 'UNKNOWN')}")
    return decision_record


def _pre_sanad_reject(signal):
    """
    Deterministic pre-Sanad reject for obvious garbage.
    Returns rejection reason string, or None if signal should proceed.
    No LLM calls — pure field validation.
    
    v3.1 Additions:
    - RugCheck score filter
    - Rejection cooldown tracker
    - DexScreener boost-only filter
    """
    token = signal.get("token", "")
    source = signal.get("source", "")

    # ── NEW FILTER 1: RugCheck score < 30 for non-premium tiers ──
    rugcheck_score = signal.get("rugcheck_score", signal.get("onchain_evidence", {}).get("rugcheck_score"))
    if rugcheck_score is not None and rugcheck_score < 30:
        # Classify token to check if it's TIER_1 or TIER_2
        try:
            from token_profile import TokenProfile, classify_asset
            profile_data = dict(signal)
            if 'symbol' not in profile_data:
                profile_data['symbol'] = token
            profile = TokenProfile.from_dict(profile_data)
            tier = classify_asset(profile)
            
            # Reject if NOT TIER_1 or TIER_2
            if not tier.startswith("TIER_1") and not tier.startswith("TIER_2"):
                return f"RugCheck score {rugcheck_score} too low (< 30)"
        except Exception:
            # If classification fails, apply filter conservatively
            return f"RugCheck score {rugcheck_score} too low (< 30)"
    
    # ── NEW FILTER 2: Rejection cooldown (30 minutes) ──
    cooldown_reason = _check_rejection_cooldown(token)
    if cooldown_reason:
        return cooldown_reason
    
    # ── NEW FILTER 3: DexScreener boost-only (no corroboration) ──
    cross_count = signal.get("cross_source_count", 1)
    if "dexscreener" in source.lower() and "boost" in source.lower():
        # Check if there's corroboration from other sources
        cross_sources = signal.get("cross_sources", [])
        
        # If only 1 source OR all sources are dexscreener variants
        has_non_dex = any(src != "dexscreener" for src in cross_sources)
        
        if cross_count == 1 or not has_non_dex:
            return "DexScreener boost-only (advertising)"
    
    # ── EXISTING FILTERS ──
    
    # Missing required fields
    required = ["token", "source", "thesis"]
    missing = [f for f in required if not signal.get(f)]
    if missing:
        return f"Missing required fields: {missing}"

    # Paid DexScreener boosts are advertising, not signal (general catch)
    if "dexscreener boost" in source.lower() and cross_count <= 1:
        return f"Paid DexScreener boost is advertising, not organic signal"

    # Token age < 30 minutes with LP unlocked (from signal metadata if available)
    onchain = signal.get("onchain_evidence", {})
    age_minutes = signal.get("age_minutes", onchain.get("age_minutes"))
    lp_locked = onchain.get("lp_locked", signal.get("lp_locked"))
    if age_minutes is not None and age_minutes < 30 and lp_locked is False:
        return f"Token age {age_minutes}min < 30min with LP unlocked"

    # Zero market cap with no exchange listing
    mc = signal.get("market_cap_usd", signal.get("market_cap"))
    if mc is not None and mc < 1000 and not signal.get("cex_listed"):
        return f"Market cap ${mc} < $1000 — not tradeable"

    return None


def _check_rejection_cooldown(token: str) -> str:
    """
    Check if token was recently rejected (within 30 minutes).
    Returns rejection reason if in cooldown, None otherwise.
    Also updates the cooldown tracker.
    """
    cooldown_file = STATE_DIR / "rejection_cooldowns.json"
    now = datetime.now(timezone.utc)
    
    # Load existing cooldowns
    cooldowns = {}
    if cooldown_file.exists():
        try:
            with open(cooldown_file, "r") as f:
                cooldowns = json.load(f)
        except Exception:
            cooldowns = {}
    
    # Clean old entries (> 24 hours)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cooldowns = {
        tok: ts for tok, ts in cooldowns.items()
        if ts > cutoff_24h
    }
    
    # Check if token is in cooldown
    if token in cooldowns:
        last_reject = datetime.fromisoformat(cooldowns[token].replace("Z", "+00:00"))
        minutes_ago = (now - last_reject).total_seconds() / 60
        
        if minutes_ago < 30:
            return f"Cooldown: {token} rejected {int(minutes_ago)}m ago"
    
    # No cooldown issue - save cleaned cooldowns
    try:
        with open(cooldown_file, "w") as f:
            json.dump(cooldowns, f, indent=2)
    except Exception:
        pass  # Non-critical
    
    return None


def _record_rejection_cooldown(token: str):
    """
    Record a rejection timestamp for cooldown tracking.
    Called after a pre-sanad rejection.
    """
    cooldown_file = STATE_DIR / "rejection_cooldowns.json"
    now = datetime.now(timezone.utc).isoformat()
    
    # Load existing cooldowns
    cooldowns = {}
    if cooldown_file.exists():
        try:
            with open(cooldown_file, "r") as f:
                cooldowns = json.load(f)
        except Exception:
            cooldowns = {}
    
    # Update timestamp
    cooldowns[token] = now
    
    # Save
    try:
        with open(cooldown_file, "w") as f:
            json.dump(cooldowns, f, indent=2)
    except Exception:
        pass  # Non-critical


def _log_decision_short_circuit(signal, sanad_result, stage="sanad"):
    """
    Log a short-circuited BLOCK decision with full telemetry (no LLM calls).
    
    Args:
        signal: Signal dict
        sanad_result: Sanad verification result (or pseudo-result for pre-sanad)
        stage: Stage name - "pre_sanad" or "sanad" (default)
    """
    from datetime import datetime, timezone

    # Run lightweight classification for telemetry (no LLM cost)
    asset_tier = "UNKNOWN"
    eligible_strategies = []
    regime_tag = "UNKNOWN"
    try:
        from token_profile import TokenProfile, classify_asset, get_eligible_strategies
        # Ensure symbol is set (signals use 'token' key)
        profile_data = dict(signal)
        if 'symbol' not in profile_data and 'token' in profile_data:
            profile_data['symbol'] = profile_data['token']
        profile = TokenProfile.from_dict(profile_data)
        asset_tier = classify_asset(profile)
        regime_state = _load_state("regime.json") or {}
        regime_tag = regime_state.get("regime_tag", "UNKNOWN")
        eligible_strategies = get_eligible_strategies(profile, regime_tag)
    except Exception:
        pass  # Best-effort — don't fail the log

    import uuid
    trust = sanad_result.get("trust_score", 0)
    
    # Customize rejection reason based on stage
    if stage == "pre_sanad":
        rejection_reason = f"Pre-Sanad reject: {sanad_result.get('rugpull_flags', ['deterministic_filter'])[0]}"
        stage_num = "pre_sanad"
    else:
        rejection_reason = f"Sanad BLOCK (trust={trust}, grade={sanad_result.get('grade','?')}, rugpull_flags={sanad_result.get('rugpull_flags',[])})"
        stage_num = 2

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "correlation_id": signal.get("correlation_id") or str(uuid.uuid4()),
        "signal": {
            "token": signal.get("token", "?"),
            "source": signal.get("source", "?"),
            "thesis": signal.get("thesis", ""),
        },
        "stage": stage_num,
        "asset_tier": asset_tier,
        "regime_tag": regime_tag,
        "eligible_strategies": eligible_strategies,
        "selected_strategy": "NONE",
        "meme_safety_gate": "N/A",
        "lint_result": "N/A",
        "sanad": {
            "trust_score": trust,
            "grade": sanad_result.get("grade", "?"),
            "recommendation": "BLOCK",
            "rugpull_flags": sanad_result.get("rugpull_flags", []),
        },
        "bull": {"conviction": 0, "thesis": ""},
        "bear": {"conviction": 0, "attack_points": []},
        "judge": {"verdict": "REJECT", "confidence_score": 0, "reasoning": f"Short-circuited: {stage} BLOCK before LLM debate"},
        "trade_confidence_score": 0,
        "short_circuit": True,
        "rejection_reason": rejection_reason,
        "final_action": "REJECT",
    }
    _log_decision(record)


def _log_decision(record):
    """Log decision to execution-logs/decisions.jsonl"""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOGS_DIR / "decisions.jsonl"
        with open(log_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        print(f"[PIPELINE] Error logging decision: {e}")


def _sync_to_supabase(record):
    """Sync decision to Supabase decision_packets table."""
    try:
        import supabase_client
        supabase_client.log_event(
            event_type="DECISION",
            payload=record,
            correlation_id=record.get("correlation_id", ""),
        )
        print("  Decision synced to Supabase")
    except Exception as e:
        print(f"  WARNING: Supabase sync failed: {e}")


# ─────────────────────────────────────────────
# MAIN PIPELINE ORCHESTRATOR
# ─────────────────────────────────────────────

def _funnel(field, gate_name=None):
    """Increment rejection funnel counter (best-effort)."""
    try:
        from rejection_funnel import increment
        increment(field, gate_name)
    except Exception:
        pass


def run_pipeline(signal):
    """
    Run the complete 7-stage pipeline on a signal.
    Fail-closed at every stage.
    Returns the full decision record.
    """
    _funnel("signals_ingested")
    print("\n" + "=" * 60)
    print("SANAD TRADER v3.0 — INTELLIGENCE PIPELINE")
    print("=" * 60)

    # Stage 1: Signal Intake
    signal, error = stage_1_signal_intake(signal)
    if error:
        print(f"\nPIPELINE BLOCKED at Stage 1: {error}")
        return {"final_action": "REJECT", "stage": 1, "reason": error}

    # Stage 1.5a: Paper mode fast-track for high-confidence corroborated signals
    fast_track_result = _check_fast_track(signal)
    if fast_track_result:
        return fast_track_result

    # Stage 1.5b: Pre-Sanad deterministic reject (saves Sanad LLM call)
    pre_reject = _pre_sanad_reject(signal)
    if pre_reject:
        _funnel("pre_sanad_rejected")
        print(f"\n⛔ PRE-SANAD REJECT: {pre_reject}")
        
        # Record rejection cooldown
        _record_rejection_cooldown(signal.get("token", ""))
        
        _log_decision_short_circuit(signal, {
            "trust_score": 0, "grade": "N/A", "recommendation": "BLOCK",
            "rugpull_flags": [pre_reject],
        }, stage="pre_sanad")
        return {"final_action": "REJECT", "stage": 1.5, "reason": pre_reject}

    # Stage 2: Sanad Verification
    sanad_result, error = stage_2_sanad_verification(signal)
    if error:
        print(f"\nPIPELINE BLOCKED at Stage 2: {error}")
        return {"final_action": "REJECT", "stage": 2, "reason": error}

    # SHORT-CIRCUIT: If Sanad says BLOCK, stop before burning LLM credits
    # But in paper mode, only short-circuit on rugpull flags or very low trust
    try:
        with open(STATE_DIR / "portfolio.json") as _scf:
            _sc_paper = json.load(_scf).get("mode", "paper") == "paper"
    except Exception:
        _sc_paper = True

    sc_rec = sanad_result.get("recommendation", "BLOCK")
    sc_trust = sanad_result.get("trust_score", 0)
    sc_rugpull = sanad_result.get("rugpull_flags", [])

    if _sc_paper:
        # Paper mode: only short-circuit on rugpull flags or trust below paper threshold
        sc_threshold = THRESHOLDS["sanad"]["minimum_trade_score"]  # 15
        should_short_circuit = bool(sc_rugpull) or sc_trust < sc_threshold
    else:
        # Live mode: short-circuit whenever Sanad says BLOCK
        should_short_circuit = sc_rec == "BLOCK"

    if should_short_circuit and sc_rec == "BLOCK":
        _funnel("short_circuited")
        reason = f"rugpull_flags={sc_rugpull}" if sc_rugpull else f"trust={sc_trust}"
        print(f"\n⛔ SHORT-CIRCUIT: Sanad BLOCK ({reason}) — skipping LLM debate")
        _log_decision_short_circuit(signal, sanad_result)
        return {"final_action": "REJECT", "stage": 2, "reason": f"Sanad BLOCK ({reason})"}

    # Stage 2.5: Token Profile & Classification (v3.0)
    profile, error = stage_2_5_token_profile(signal, sanad_result)
    if error:
        print(f"\nPIPELINE BLOCKED at Stage 2.5: {error}")
        return {"final_action": "REJECT", "stage": 2.5, "reason": error}

    # Stage 3: Strategy Match
    strategy_result, error = stage_3_strategy_match(signal, sanad_result, profile)
    if error:
        print(f"\nPIPELINE BLOCKED at Stage 3: {error}")
        return {"final_action": "REJECT", "stage": 3, "reason": error}

    # Stage 4: Bull/Bear Debate
    bull_result, bear_result, error = stage_4_debate(signal, sanad_result, strategy_result, profile)
    if error:
        print(f"\nPIPELINE BLOCKED at Stage 4: {error}")
        return {"final_action": "REJECT", "stage": 4, "reason": error}

    # Stage 5: Al-Muhasbi Judge
    judge_result, error = stage_5_judge(signal, sanad_result, strategy_result, bull_result, bear_result, profile)
    if error:
        print(f"\nPIPELINE BLOCKED at Stage 5: {error}")
        return {"final_action": "REJECT", "stage": 5, "reason": error}

    # Stage 6: Policy Engine
    policy_result, error = stage_6_policy_engine(signal, sanad_result, strategy_result, bull_result, bear_result, judge_result)
    if error:
        print(f"\nPIPELINE BLOCKED at Stage 6: {error}")
        return {"final_action": "REJECT", "stage": 6, "reason": error}

    # Stage 7: Execute / Log
    decision_record = stage_7_execute(signal, sanad_result, strategy_result, bull_result, bear_result, judge_result, policy_result, profile)
    
    # Hotfix: tolerate tuple return (legacy/alternate code path)
    if isinstance(decision_record, tuple):
        decision_record = decision_record[0]

    print("\n" + "=" * 60)
    print(f"PIPELINE COMPLETE — Final Action: {decision_record.get('final_action', 'UNKNOWN')}")
    print("=" * 60)

    return decision_record


# ─────────────────────────────────────────────
# CLI — Test with sample signal
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Load signal from JSON file
        signal_path = sys.argv[1]
        with open(signal_path, "r") as f:
            signal = json.load(f)
    else:
        # Default test signal — PEPE meme coin
        signal = {
            "token": "PEPE",
            "symbol": "PEPEUSDT",
            "source": "Twitter/X crypto influencer @CryptoWhale + Binance volume spike",
            "thesis": "PEPE showing unusual volume spike (3x avg) with multiple whale accumulation events detected on-chain. Social momentum building on Twitter with 50k+ mentions in 24h.",
            "exchange": "binance",
            "chain": "",
            "token_age_hours": 720,  # PEPE is old enough
            "volatility_30min_pct": 0.08,
            "verified_catalyst": False,
        }

    result = run_pipeline(signal)

    # Print summary
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(json.dumps({
        "final_action": result.get("final_action"),
        "stage_blocked": result.get("stage"),
        "reason": result.get("reason") or result.get("rejection_reason"),
    }, indent=2, default=str))
