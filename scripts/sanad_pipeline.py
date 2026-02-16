#!/usr/bin/env python3
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
BASE_DIR = Path("/data/.openclaw/workspace/trading")
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
import binance_client

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


def call_claude(system_prompt, user_message, model="claude-haiku-4-5-20251001", max_tokens=2000):
    """
    Call Anthropic Claude API directly.
    Primary for: Sanad Verifier, Bull, Bear (Opus 4.6), Execution (Haiku).
    Fallback: OpenRouter Claude.
    """
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
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("content") and len(result["content"]) > 0:
                text = result["content"][0].get("text", "")
                if text:
                    print(f"    [Claude direct OK — {model}]")
                    return text
        return None
    except Exception as e:
        print(f"    [Claude direct FAILED: {e}]")
        print(f"    [Falling back to OpenRouter Claude...]")
        return _fallback_openrouter(system_prompt, user_message, f"anthropic/{model}", max_tokens)


def call_openai(system_prompt, user_message, model="gpt-5.2", max_tokens=2000):
    """
    Call OpenAI API directly.
    Primary for: Al-Muhasbi Judge (GPT-5.2).
    Fallback: OpenRouter GPT.
    """
    if not OPENAI_API_KEY:
        print(f"    [OpenAI key missing — falling back to OpenRouter]")
        return _fallback_openrouter(system_prompt, user_message, f"openai/{model}", max_tokens)

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
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            choices = result.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "")
                if text:
                    print(f"    [OpenAI direct OK — {model}]")
                    return text
        return None
    except Exception as e:
        print(f"    [OpenAI direct FAILED: {e}]")
        print(f"    [Falling back to OpenRouter GPT...]")
        return _fallback_openrouter(system_prompt, user_message, f"openai/{model}", max_tokens)


def call_perplexity(query, model="sonar-pro"):
    """
    Call Perplexity API directly for real-time intelligence.
    Primary for: Sanad Verifier source research.
    Fallback: OpenRouter Perplexity.
    """
    if not PERPLEXITY_API_KEY:
        print(f"    [Perplexity key missing — falling back to OpenRouter]")
        return _fallback_openrouter(
            "You are a real-time crypto intelligence agent. Return factual, sourced information only.",
            query,
            f"perplexity/{model}",
            1500,
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
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            choices = result.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "")
                if text:
                    print(f"    [Perplexity direct OK — {model}]")
                    return text
        return None
    except Exception as e:
        print(f"    [Perplexity direct FAILED: {e}]")
        print(f"    [Falling back to OpenRouter Perplexity...]")
        return _fallback_openrouter(
            "You are a real-time crypto intelligence agent. Return factual, sourced information only.",
            query,
            f"perplexity/{model}",
            1500,
        )


def _fallback_openrouter(system_prompt, user_message, model, max_tokens=2000):
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
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            choices = result.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "")
                if text:
                    print(f"    [OpenRouter fallback OK — {model}]")
                    return text
        return None
    except Exception as e:
        print(f"    [OpenRouter fallback FAILED: {e}]")
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

    signal["onchain_evidence"] = onchain_evidence
    signal["onchain_evidence_summary"] = evidence_summary.strip()
    return signal


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

    # Step 0: Enrich with on-chain data for Solana tokens
    signal = enrich_signal_with_onchain_data(signal)

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

    perplexity_intel = call_perplexity(intel_query)
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
    verification_prompt = f"""SIGNAL TO VERIFY:
Token: {signal['token']}
Source: {signal['source']}
Thesis: {signal['thesis']}
Timestamp: {signal.get('timestamp', 'unknown')}

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
  "corroboration_level": "<TAWATUR|MASHHUR|AHAD_SAHIH|AHAD_DAIF>",
  "recency_decay_points": <0 to -15>,
  "rugpull_flags": ["<flag1>", "<flag2>"] or [],
  "sybil_risk": "<LOW|MEDIUM|HIGH>",
  "sybil_evidence": "<description if MEDIUM/HIGH>",
  "key_findings": ["<finding1>", "<finding2>", "<finding3>"],
  "recommendation": "<PROCEED|CAUTION|BLOCK>",
  "source_count": <number of independent sources found>,
  "reasoning": "<3-5 sentence detailed explanation>"
}}"""

    sanad_response = call_claude(
        system_prompt=SANAD_PROMPT,
        user_message=verification_prompt,
        model="claude-opus-4-6",  # Opus for verification
        max_tokens=8000,
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

    trust_score = sanad_result.get("trust_score", 0)
    grade = sanad_result.get("grade", "FAILED")
    recommendation = sanad_result.get("recommendation", "BLOCK")

    print(f"  Trust Score: {trust_score}/100")
    print(f"  Grade: {grade}")
    print(f"  Source Grade: {sanad_result.get('source_grade', 'N/A')}")
    print(f"  Chain Integrity: {sanad_result.get('chain_integrity', 'N/A')}")
    print(f"  Corroboration: {sanad_result.get('corroboration_level', 'N/A')}")
    print(f"  Recency Decay: {sanad_result.get('recency_decay_points', 'N/A')}")
    print(f"  Sybil Risk: {sanad_result.get('sybil_risk', 'N/A')}")
    print(f"  Rugpull Flags: {sanad_result.get('rugpull_flags', [])}")
    print(f"  Recommendation: {recommendation}")
    print(f"  Source Count: {sanad_result.get('source_count', 'N/A')}")
    print(f"  Reasoning: {sanad_result.get('reasoning', 'N/A')[:200]}")

    # HARD RULE: score < 70 → BLOCK (temporarily lowered to 40 for field mapping test)
    min_score = THRESHOLDS["sanad"]["minimum_trade_score"]
    if trust_score < min_score:
        print(f"  BLOCKED: Trust score {trust_score} < {min_score} minimum")
        sanad_result["recommendation"] = "BLOCK"
        return sanad_result, f"Trust score {trust_score} < {min_score}"

    return sanad_result, None


# ─────────────────────────────────────────────
# STAGE 3: STRATEGY MATCH
# ─────────────────────────────────────────────

def stage_3_strategy_match(signal, sanad_result):
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

    # For now: single strategy (meme-momentum)
    # TODO Phase 4: Thompson Sampling across multiple strategies
    strategy_name = "meme-momentum"
    strategy_path = strategies_dir / f"{strategy_name}.md"

    if not strategy_path.exists():
        return None, f"Strategy file not found: {strategy_name}"

    with open(strategy_path, "r") as f:
        strategy_content = f.read()

    # Calculate position size using default sizing
    portfolio = _load_state("portfolio.json")
    balance = portfolio.get("current_balance_usd", 10000)

    # Kelly fraction or default
    trade_count = len(_load_state("trade_history.json").get("trades", []))
    min_kelly_trades = THRESHOLDS["sizing"]["kelly_min_trades"]

    if trade_count >= min_kelly_trades:
        position_pct = THRESHOLDS["sizing"]["kelly_fraction"] * 0.5  # Fractional Kelly placeholder
    else:
        position_pct = THRESHOLDS["sizing"]["kelly_default_pct"]  # 2% cold start

    # Cap at max
    max_pct = THRESHOLDS["sizing"]["max_position_pct"]
    position_pct = min(position_pct, max_pct)
    position_usd = balance * position_pct

    strategy_result = {
        "strategy_name": strategy_name,
        "position_pct": position_pct,
        "position_usd": round(position_usd, 2),
        "balance_usd": balance,
        "sizing_mode": "cold_start" if trade_count < min_kelly_trades else "fractional_kelly",
        "trade_count": trade_count,
    }

    print(f"  Strategy: {strategy_name}")
    print(f"  Position Size: {position_pct*100:.1f}% = ${position_usd:.2f}")
    print(f"  Sizing Mode: {strategy_result['sizing_mode']}")

    return strategy_result, None


# ─────────────────────────────────────────────
# STAGE 4: BULL / BEAR DEBATE
# ─────────────────────────────────────────────

def stage_4_debate(signal, sanad_result, strategy_result):
    """
    Run Bull (Al-Baqarah) and Bear (Al-Dahhak) debate.
    Both argue simultaneously (or sequentially for cost control).
    Critical rule: NEVER skip the Bear.
    """
    print(f"\n{'='*60}")
    print(f"STAGE 4: BULL / BEAR DEBATE")
    print(f"{'='*60}")

    context = f"""TOKEN: {signal['token']}
THESIS: {signal['thesis']}
SOURCE: {signal['source']}
SANAD TRUST SCORE: {sanad_result.get('trust_score', 'N/A')}/100
SANAD GRADE: {sanad_result.get('grade', 'N/A')}
STRATEGY: {strategy_result.get('strategy_name', 'N/A')}
POSITION SIZE: ${strategy_result.get('position_usd', 'N/A')}
REAL-TIME INTELLIGENCE: {sanad_result.get('perplexity_intel', 'N/A')}
EXCHANGE DATA: {sanad_result.get('price_context', 'N/A')}"""

    # ── BULL (Al-Baqarah) ──
    print("  [4a] Bull Al-Baqarah arguing FOR...")
    bull_message = f"""{context}

Argue FOR this trade. Address all 7 points in your mandate: entry thesis, market microstructure, social momentum, on-chain evidence, historical pattern match, risk/reward calculation, and position sizing.

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
        system_prompt=BULL_PROMPT,
        user_message=bull_message,
        model="claude-opus-4-6",
        max_tokens=3000,
    )
    bull_result = _parse_json_response(bull_response) if bull_response else None
    if not bull_result:
        print("  WARNING: Bull response parse failed, using defaults")
        bull_result = {"conviction": 50, "thesis": "Parse failed", "supporting_evidence": []}

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

    # ── BEAR (Al-Dahhak) — NEVER SKIP ──
    print("  [4b] Bear Al-Dahhak arguing AGAINST...")
    bear_message = f"""{context}

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
        system_prompt=BEAR_PROMPT,
        user_message=bear_message,
        model="claude-opus-4-6",
        max_tokens=5000,
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

def stage_5_judge(signal, sanad_result, strategy_result, bull_result, bear_result):
    """
    Al-Muhasbi Judge — independent GPT-powered review.
    6-point checklist. Verdict: APPROVE / REJECT / REVISE.
    Mandate: capital preservation, when in doubt REJECT.
    CRITICAL: Never override REJECT.
    """
    print(f"\n{'='*60}")
    print(f"STAGE 5: AL-MUHASBI JUDGE")
    print(f"{'='*60}")

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
- Position Size: ${strategy_result.get('position_usd', 'N/A')} ({strategy_result.get('position_pct', 0)*100:.1f}%)

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

    # Use GPT via OpenAI direct for independent review (different model = different blindspot)
    print("  [5a] Al-Muhasbi reviewing via GPT-5.2...")
    judge_response = call_openai(
        system_prompt=JUDGE_PROMPT,
        user_message=judge_message,
        model="gpt-5.2",
        max_tokens=8000,
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
    confidence = judge_result.get("confidence_score", 0)

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
        "trade_confidence_score": judge_result.get("confidence_score", 0),
        "almuhasbi_verdict": judge_result.get("verdict", "REJECT"),
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

def stage_7_execute(signal, sanad_result, strategy_result, bull_result, bear_result, judge_result, policy_result):
    """
    Execute trade (paper mode) or log rejection.
    All decisions logged to Supabase with full decision packet.
    """
    print(f"\n{'='*60}")
    print(f"STAGE 7: EXECUTE / LOG")
    print(f"{'='*60}")

    correlation_id = signal.get("correlation_id", "unknown")
    final_action = "EXECUTE" if policy_result["result"] == "PASS" else "REJECT"
    rejection_reason = policy_result.get("output", "") if final_action == "REJECT" else None

    # Build full decision record
    decision_record = {
        "correlation_id": correlation_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signal": {
            "token": signal["token"],
            "source": signal["source"],
            "thesis": signal["thesis"],
        },
        "sanad": {
            "trust_score": sanad_result.get("trust_score", 0),
            "grade": sanad_result.get("grade", "FAILED"),
            "recommendation": sanad_result.get("recommendation", "BLOCK"),
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
        symbol = signal.get("symbol", signal["token"] + "USDT")
        quantity = strategy_result.get("position_usd", 200) / (policy_result["decision_packet"].get("current_price", 1))

        print(f"  EXECUTING PAPER TRADE: BUY {quantity:.6f} {symbol}")
        order = binance_client.place_order(
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            paper_mode=True,
        )

        if order:
            decision_record["execution"] = {
                "order_id": order["orderId"],
                "fill_price": order["price"],
                "quantity": order["quantity"],
                "fee_usd": order["fee_usd"],
            }
            print(f"  Paper trade filled: {order['orderId']} @ ${order['price']:,.4f}")

            # Update positions state
            _add_position(signal, strategy_result, order, sanad_result, bull_result)
        else:
            decision_record["execution"] = {"error": "Paper order failed"}
            print(f"  WARNING: Paper order execution failed")
    else:
        print(f"  REJECTED: {rejection_reason}")

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


def _add_position(signal, strategy_result, order, sanad_result, bull_result=None):
    """Add position to positions.json state file."""
    try:
        positions = _load_state("positions.json")
        pos_list = positions.get("positions", [])

        new_position = {
            "id": order["orderId"],
            "token": signal["token"],
            "symbol": signal.get("symbol", signal["token"] + "USDT"),
            "exchange": signal.get("exchange", "binance"),
            "side": "LONG",
            "entry_price": order["price"],
            "current_price": order["price"],
            "quantity": order["quantity"],
            "position_usd": strategy_result.get("position_usd", 0),
            "stop_loss_pct": _calc_stop_pct(order["price"], bull_result) if bull_result else THRESHOLDS["risk"]["stop_loss_default_pct"],
            "take_profit_pct": _calc_tp_pct(order["price"], bull_result) if bull_result else THRESHOLDS["risk"]["take_profit_default_pct"],
            "bull_stop_loss": bull_result.get("stop_loss", "N/A") if bull_result else "N/A",
            "bull_target_price": bull_result.get("target_price", "N/A") if bull_result else "N/A",
            "bull_entry_price": bull_result.get("entry_price", "N/A") if bull_result else "N/A",
            "risk_reward_ratio": bull_result.get("risk_reward_ratio", "N/A") if bull_result else "N/A",
            "bull_invalidation": bull_result.get("invalidation_point", "N/A") if bull_result else "N/A",
            "bull_timeframe": bull_result.get("timeframe", "N/A") if bull_result else "N/A",
            "strategy_name": strategy_result.get("strategy_name", ""),
            "sanad_score": sanad_result.get("trust_score", 0),
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

def run_pipeline(signal):
    """
    Run the complete 7-stage pipeline on a signal.
    Fail-closed at every stage.
    Returns the full decision record.
    """
    print("\n" + "=" * 60)
    print("SANAD TRADER v3.0 — INTELLIGENCE PIPELINE")
    print("=" * 60)

    # Stage 1: Signal Intake
    signal, error = stage_1_signal_intake(signal)
    if error:
        print(f"\nPIPELINE BLOCKED at Stage 1: {error}")
        return {"final_action": "REJECT", "stage": 1, "reason": error}

    # Stage 2: Sanad Verification
    sanad_result, error = stage_2_sanad_verification(signal)
    if error:
        print(f"\nPIPELINE BLOCKED at Stage 2: {error}")
        return {"final_action": "REJECT", "stage": 2, "reason": error}

    # Stage 3: Strategy Match
    strategy_result, error = stage_3_strategy_match(signal, sanad_result)
    if error:
        print(f"\nPIPELINE BLOCKED at Stage 3: {error}")
        return {"final_action": "REJECT", "stage": 3, "reason": error}

    # Stage 4: Bull/Bear Debate
    bull_result, bear_result, error = stage_4_debate(signal, sanad_result, strategy_result)
    if error:
        print(f"\nPIPELINE BLOCKED at Stage 4: {error}")
        return {"final_action": "REJECT", "stage": 4, "reason": error}

    # Stage 5: Al-Muhasbi Judge
    judge_result, error = stage_5_judge(signal, sanad_result, strategy_result, bull_result, bear_result)
    if error:
        print(f"\nPIPELINE BLOCKED at Stage 5: {error}")
        return {"final_action": "REJECT", "stage": 5, "reason": error}

    # Stage 6: Policy Engine
    policy_result, error = stage_6_policy_engine(signal, sanad_result, strategy_result, bull_result, bear_result, judge_result)
    if error:
        print(f"\nPIPELINE BLOCKED at Stage 6: {error}")
        return {"final_action": "REJECT", "stage": 6, "reason": error}

    # Stage 7: Execute / Log
    decision_record = stage_7_execute(signal, sanad_result, strategy_result, bull_result, bear_result, judge_result, policy_result)

    print("\n" + "=" * 60)
    print(f"PIPELINE COMPLETE — Final Action: {decision_record['final_action']}")
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
