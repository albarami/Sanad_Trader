#!/usr/bin/env python3
"""
Token Profile & Tier Classification — Sanad v3.0

Implements dynamic asset classification and tier-based strategy routing.
Every signal gets a TokenProfile with classification that determines:
- Which strategies are eligible
- Which prompts the Bull/Bear agents use
- Which evidence is required from agents
- What Judge veto rules apply

Tier System:
- SKIP: Stablecoins (no trade)
- TIER_1: Macro bluechips (>$20B MC) — institutional analysis
- TIER_2: Alts/Mid-caps ($100M-$20B) — tokenomics/narrative focus
- TIER_3: Meme/Microcaps (<$100M) — on-chain trench warfare
- WHALE: Special tier for pure whale-following signals

Safety Gates:
- meme_safety_gate(): Hard blocks for TIER_3 before LLM processing
- Saves API credits by rejecting obvious scams deterministically
"""

import re
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────
# Token Profile Dataclass
# ─────────────────────────────────────────────────────────

@dataclass
class TokenProfile:
    """Complete token profile with all classification data."""
    
    # Basic Info
    symbol: str
    name: Optional[str] = None
    chain: Optional[str] = None
    address: Optional[str] = None
    
    # Market Data
    market_cap: Optional[float] = None
    fdv: Optional[float] = None  # Fully Diluted Valuation
    circulating_pct: Optional[float] = None  # % of max supply in circulation
    age_days: Optional[int] = None
    liquidity_usd: Optional[float] = None
    volume_24h: Optional[float] = None
    
    # Exchange Listings
    cex_listed: bool = False
    cex_names: List[str] = field(default_factory=list)
    dex_only: bool = True
    
    # Security & Safety
    rugcheck_score: Optional[int] = None  # 0-100
    security_flags: List[str] = field(default_factory=list)
    holder_top10_pct: Optional[float] = None
    lp_locked_pct: Optional[float] = None
    honeypot_verdict: Optional[str] = None
    rugpull_verdict: Optional[str] = None
    
    # Metadata
    coingecko_categories: List[str] = field(default_factory=list)
    
    # Derived Classification
    asset_tier: Optional[str] = None  # TIER_1, TIER_2, TIER_3, SKIP, WHALE
    mc_to_liquidity_ratio: Optional[float] = None
    
    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return asdict(self)


# ─────────────────────────────────────────────────────────
# Tier Classification
# ─────────────────────────────────────────────────────────

def classify_asset(profile: TokenProfile) -> str:
    """
    Classify token into tiers based on profile data.
    
    Returns: TIER_1_MACRO, TIER_2_ALT_LARGE, TIER_2_ALT_MID, TIER_2_ALT_SMALL,
             TIER_3_MEME_CEX, TIER_3_MEME_MID, TIER_3_MEME_MICRO, TIER_3_MICRO,
             STABLE, WHALE, or UNKNOWN
    """
    mc = profile.market_cap or 0
    liq = profile.liquidity_usd or 0
    age_days = profile.age_days or 0
    
    # 1. STABLE — skip entirely
    stable_keywords = ["usd", "usdt", "usdc", "dai", "busd", "tusd", "frax"]
    if any(kw in profile.symbol.lower() for kw in stable_keywords):
        return "STABLE"
    
    # 2. TIER_1 — Macro bluechips (>$20B)
    if mc > 20_000_000_000:
        return "TIER_1_MACRO"
    
    # 3. TIER_3_MEME — Meme detection BEFORE alt classification (first-match-wins)
    # A $3B meme is still a meme. Category and symbol pattern take priority over MC.
    import re
    is_meme_category = any(cat.lower() in ("meme", "meme token", "memecoin", "community")
                          for cat in (profile.coingecko_categories or []))
    is_meme_pattern = bool(re.search(
        r"(inu|pepe|doge|dog|cat|wif|bonk|meme|trump|elon|moon|rocket|cum|safe|baby|floki)",
        profile.symbol.lower()
    ))
    
    if is_meme_category or is_meme_pattern:
        if profile.cex_listed and mc >= 100_000_000:
            return "TIER_3_MEME_CEX"     # Established meme, CEX-listed
        elif mc >= 10_000_000:
            return "TIER_3_MEME_MID"     # Mid meme, may have DEX liquidity
        else:
            return "TIER_3_MEME_MICRO"   # Trench warfare territory
    
    # 4. TIER_3_MICRO — Any micro-cap DEX-only token (non-meme)
    if mc < 50_000_000 and profile.dex_only and liq < 2_000_000:
        return "TIER_3_MICRO"
    
    # 5. TIER_2 — Everything else ($50M-$20B, utility/infra/DeFi)
    if mc >= 5_000_000_000:
        return "TIER_2_ALT_LARGE"
    elif mc >= 200_000_000:
        return "TIER_2_ALT_MID"
    elif mc >= 50_000_000:
        return "TIER_2_ALT_SMALL"
    
    # 6. Small non-meme with CEX listing
    if profile.cex_listed:
        return "TIER_2_ALT_SMALL"
    
    # Fallback: micro-cap non-meme
    return "TIER_3_MICRO"


# Simplified tier mapping for strategy constraints
TIER_MAP = {
    "STABLE": "SKIP",
    "TIER_1_MACRO": "TIER_1",
    "TIER_2_ALT_LARGE": "TIER_2",
    "TIER_2_ALT_MID": "TIER_2",
    "TIER_2_ALT_SMALL": "TIER_2",
    "TIER_3_MEME_CEX": "TIER_3",
    "TIER_3_MEME_MID": "TIER_3",
    "TIER_3_MEME_MICRO": "TIER_3",
    "TIER_3_MICRO": "TIER_3",
    "WHALE": "WHALE",  # Special tier for whale signals
    "UNKNOWN": "TIER_3",  # Default to most conservative analysis
}


# ─────────────────────────────────────────────────────────
# Build Token Profile from Signal Data
# ─────────────────────────────────────────────────────────

def build_token_profile(signal_data: dict) -> TokenProfile:
    """
    Construct TokenProfile from signal data (CoinGecko, Birdeye, DexScreener).
    
    signal_data should contain fields from:
    - CoinGecko: market_cap, fdv, categories, circulating_supply
    - Birdeye: liquidity, volume_24h, holder data, security
    - DexScreener: DEX pool info
    - RugCheck: safety score
    - Honeypot detector: verdict
    """
    
    # Extract basic info
    profile = TokenProfile(
        symbol=signal_data.get("token", signal_data.get("symbol", "UNKNOWN")),
        name=signal_data.get("name"),
        chain=signal_data.get("chain"),
        address=signal_data.get("token_address", signal_data.get("address")),
    )
    
    # CoinGecko data
    cg_data = signal_data.get("coingecko", {})
    if cg_data:
        market_data = cg_data.get("market_data", {})
        profile.market_cap = market_data.get("market_cap", {}).get("usd")
        profile.fdv = market_data.get("fully_diluted_valuation", {}).get("usd")
        profile.volume_24h = market_data.get("total_volume", {}).get("usd")
        
        max_supply = market_data.get("max_supply")
        circ_supply = market_data.get("circulating_supply")
        if max_supply and circ_supply and max_supply > 0:
            profile.circulating_pct = (circ_supply / max_supply) * 100
        
        profile.coingecko_categories = cg_data.get("categories", [])
        
        # Age calculation
        genesis_date = cg_data.get("genesis_date")
        if genesis_date:
            try:
                genesis_dt = datetime.fromisoformat(genesis_date.replace("Z", "+00:00"))
                profile.age_days = (datetime.now(timezone.utc) - genesis_dt).days
            except (ValueError, TypeError):
                pass
    
    # Birdeye data
    onchain = signal_data.get("onchain_evidence", {})
    birdeye_overview = onchain.get("birdeye_overview", {})
    birdeye_security = onchain.get("birdeye_security", {})
    
    if birdeye_overview:
        profile.liquidity_usd = birdeye_overview.get("liquidity")
        profile.volume_24h = profile.volume_24h or birdeye_overview.get("volume_24h")
        profile.market_cap = profile.market_cap or birdeye_overview.get("market_cap")
    
    if birdeye_security:
        profile.holder_top10_pct = birdeye_security.get("top10_holder_pct")
    
    # Token creation data
    token_creation = onchain.get("token_creation", {})
    if token_creation and token_creation.get("age_hours"):
        profile.age_days = int(token_creation["age_hours"] / 24)
    
    # RugCheck data
    rugcheck = onchain.get("rugcheck", {})
    if rugcheck:
        profile.rugcheck_score = rugcheck.get("score")
        profile.lp_locked_pct = rugcheck.get("lp_locked_pct")
        profile.security_flags.extend(rugcheck.get("risks", []))
    
    # Honeypot data
    honeypot = onchain.get("honeypot", {})
    if honeypot:
        profile.honeypot_verdict = honeypot.get("verdict")
    
    # Rugpull scan
    rugpull_scan = onchain.get("rugpull_scan", {})
    if rugpull_scan:
        profile.rugpull_verdict = rugpull_scan.get("verdict")
        profile.security_flags.extend(rugpull_scan.get("flags", []))
    
    # Holder analysis
    holder_analysis = onchain.get("holder_analysis", {})
    if holder_analysis:
        profile.holder_top10_pct = profile.holder_top10_pct or holder_analysis.get("top_10_pct")
    
    # CEX listings (from signal or CoinGecko)
    if signal_data.get("exchange") not in [None, "dex", "pump.fun"]:
        profile.cex_listed = True
        profile.cex_names.append(signal_data["exchange"])
        profile.dex_only = False
    
    # Calculate derived metrics
    if profile.market_cap and profile.liquidity_usd and profile.liquidity_usd > 0:
        profile.mc_to_liquidity_ratio = profile.market_cap / profile.liquidity_usd
    
    # Classify
    detailed_tier = classify_asset(profile)
    profile.asset_tier = detailed_tier
    
    return profile


# ─────────────────────────────────────────────────────────
# Safety Gate for TIER_3 (Memes/Microcaps)
# ─────────────────────────────────────────────────────────

def meme_safety_gate(profile: TokenProfile) -> tuple[bool, Optional[str]]:
    """
    Pre-LLM safety gate for TIER_3 tokens.
    Returns: (passed: bool, block_reason: Optional[str])
    
    HARD BLOCKS:
    - Honeypot detected
    - Mint authority active
    - Freeze authority active
    - LP locked <50%
    - Top 10 holders >60%
    - RugCheck score <30
    - MC/Liquidity ratio >50x
    - High tax (>10% buy or sell)
    """
    simple_tier = TIER_MAP.get(profile.asset_tier, "TIER_3")
    if simple_tier != "TIER_3":
        return True, None  # Only applies to TIER_3
    
    # 1. Honeypot
    if profile.honeypot_verdict == "HONEYPOT":
        return False, "Honeypot detected"
    
    # 2. Rugpull blacklist
    if profile.rugpull_verdict in ["RUG", "BLACKLISTED"]:
        return False, f"Rugpull verdict: {profile.rugpull_verdict}"
    
    # 3. Security flags
    dangerous_flags = ["mint_active", "freeze_active", "honeypot"]
    for flag in dangerous_flags:
        if flag in profile.security_flags:
            return False, f"Security flag: {flag}"
    
    # 4. LP lock
    if profile.lp_locked_pct is not None and profile.lp_locked_pct < 50:
        return False, f"LP locked <50%: {profile.lp_locked_pct:.1f}%"
    
    # 5. Holder concentration
    if profile.holder_top10_pct is not None and profile.holder_top10_pct > 60:
        return False, f"Top 10 holders >{60}%: {profile.holder_top10_pct:.1f}%"
    
    # 6. RugCheck score
    if profile.rugcheck_score is not None and profile.rugcheck_score < 30:
        return False, f"RugCheck score <30: {profile.rugcheck_score}/100"
    
    # 7. MC/Liquidity ratio (if MC is known)
    if profile.mc_to_liquidity_ratio is not None and profile.mc_to_liquidity_ratio > 50:
        return False, f"MC/Liquidity ratio >50x: {profile.mc_to_liquidity_ratio:.1f}x"
    
    # 8. High tax detection (from security flags)
    if "high_tax" in profile.security_flags:
        return False, "High tax detected (>10% buy or sell)"
    
    return True, None


# ─────────────────────────────────────────────────────────
# Strategy Constraints by Tier
# ─────────────────────────────────────────────────────────

STRATEGY_CONSTRAINTS = {
    "meme-momentum": {
        "allowed_tiers": ["TIER_3"],
        "forbidden_tiers": ["TIER_1", "TIER_2", "SKIP"],
        "min_liquidity": 50_000,
        "max_age_days": 30,
        "min_social_score": 40,
    },
    "early-launch": {
        "allowed_tiers": ["TIER_3"],
        "forbidden_tiers": ["TIER_1", "TIER_2", "SKIP"],
        "min_liquidity": 10_000,
        "max_age_days": 1,  # Only for brand new tokens
        "requires_pumpfun": True,
    },
    "whale-following": {
        "allowed_tiers": ["TIER_1", "TIER_2", "TIER_3"],
        "forbidden_tiers": ["SKIP"],
        "min_whale_volume": 50_000,
        "min_liquidity": 100_000,
    },
    "sentiment-divergence": {
        "allowed_tiers": ["TIER_1", "TIER_2"],
        "forbidden_tiers": ["TIER_3", "SKIP"],
        "min_market_cap": 100_000_000,  # $100M+
        "min_social_score": 60,
    },
    "cex-listing-play": {
        "allowed_tiers": ["TIER_2"],
        "forbidden_tiers": ["TIER_1", "TIER_3", "SKIP"],
        "min_market_cap": 50_000_000,  # $50M+
        "max_market_cap": 5_000_000_000,  # <$5B (not already mega-cap)
        "min_holder_count": 10_000,
        "requires_no_cex": True,  # Must NOT be CEX-listed yet
    },
}


def get_eligible_strategies(profile: TokenProfile, regime: str) -> list[str]:
    """
    Filter strategies by tier constraints.
    
    Args:
        profile: TokenProfile with classification
        regime: Current market regime
    
    Returns:
        List of strategy names that are eligible for this token
    """
    simple_tier = TIER_MAP.get(profile.asset_tier, "TIER_3")
    
    if simple_tier == "SKIP":
        return []
    
    eligible = []
    
    for strategy_name, constraints in STRATEGY_CONSTRAINTS.items():
        # Check tier allowance
        allowed = constraints.get("allowed_tiers", [])
        forbidden = constraints.get("forbidden_tiers", [])
        
        if simple_tier in forbidden:
            continue
        if allowed and simple_tier not in allowed:
            continue
        
        # Check specific constraints
        if "min_liquidity" in constraints:
            if not profile.liquidity_usd or profile.liquidity_usd < constraints["min_liquidity"]:
                continue
        
        if "max_age_days" in constraints:
            if not profile.age_days or profile.age_days > constraints["max_age_days"]:
                continue
        
        if "min_market_cap" in constraints:
            if not profile.market_cap or profile.market_cap < constraints["min_market_cap"]:
                continue
        
        if "max_market_cap" in constraints:
            if profile.market_cap and profile.market_cap > constraints["max_market_cap"]:
                continue
        
        if "requires_no_cex" in constraints and constraints["requires_no_cex"]:
            if profile.cex_listed:
                continue
        
        if "requires_pumpfun" in constraints and constraints["requires_pumpfun"]:
            # Check if signal mentions pump.fun
            # (This would be passed in via signal_data in practice)
            pass
        
        eligible.append(strategy_name)
    
    return eligible


# ─────────────────────────────────────────────────────────
# Prompt Linting (Tier-Specific)
# ─────────────────────────────────────────────────────────

# Forbidden keywords by tier
TIER_1_FORBIDDEN = [
    "community hype", "meme narrative", "viral", "rug pull", "rugpull",
    "holder concentration", "LP locked", "cult following", "roadmap",
    "tokenomics unlock", "pump.fun", "degen", "moon", "100x",
]

TIER_3_FORBIDDEN = [
    "institutional flow", "ETF inflow", "macro correlation", "DXY",
    "Federal Reserve", "Fed", "fundamental utility", "protocol revenue",
    "TVL", "total value locked", "developer activity", "GitHub commits",
    "enterprise adoption", "regulatory clarity",
]

TIER_2_REQUIRED = [
    "FDV", "fully diluted", "circulating", "unlock", "vesting",
]


def lint_prompt(prompt: str, tier: str, strategy: str) -> tuple[bool, List[str]]:
    """
    Validate that prompt language matches the tier.
    
    Args:
        prompt: The constructed agent prompt
        tier: TIER_1, TIER_2, or TIER_3
        strategy: Strategy name
    
    Returns:
        (passed: bool, violations: List[str])
    """
    violations = []
    prompt_lower = prompt.lower()
    
    if tier == "TIER_1":
        # TIER_1: Check for forbidden meme/micro language
        for keyword in TIER_1_FORBIDDEN:
            if keyword.lower() in prompt_lower:
                violations.append(f"TIER_1 forbidden keyword: '{keyword}'")
    
    elif tier == "TIER_3":
        # TIER_3: Check for forbidden macro/institutional language
        for keyword in TIER_3_FORBIDDEN:
            if keyword.lower() in prompt_lower:
                violations.append(f"TIER_3 forbidden keyword: '{keyword}'")
    
    elif tier == "TIER_2":
        # TIER_2: Require FDV/tokenomics analysis
        has_required = False
        for keyword in TIER_2_REQUIRED:
            if keyword.lower() in prompt_lower:
                has_required = True
                break
        if not has_required:
            violations.append(f"TIER_2 missing required FDV/tokenomics analysis")
    
    passed = len(violations) == 0
    return passed, violations


# ─────────────────────────────────────────────────────────
# Required Evidence by Tier
# ─────────────────────────────────────────────────────────

REQUIRED_EVIDENCE = {
    "TIER_1": [
        "exchange_reserves",
        "whale_netflow",
        "derivatives_data",
        "macro_correlation",
        "institutional_flow",
    ],
    "TIER_2": [
        "fdv_analysis",
        "circulating_supply_pct",
        "unlock_schedule",
        "narrative_strength",
        "relative_strength",
    ],
    "TIER_3": [
        "holder_concentration",
        "lp_lock_status",
        "rugcheck_score",
        "smart_money_wallets",
        "cult_conviction_score",
    ],
    "WHALE": [
        "wallet_credibility",
        "transaction_direction",
        "transaction_size_significance",
        "wallet_clustering",
        "historical_performance",
    ],
}


def validate_evidence(evidence_list: List[str], tier: str) -> tuple[bool, int]:
    """
    Check if agent provided enough required evidence fields.
    
    Args:
        evidence_list: List of evidence strings from agent
        tier: TIER_1, TIER_2, TIER_3, or WHALE
    
    Returns:
        (sufficient: bool, count: int)
    """
    required = REQUIRED_EVIDENCE.get(tier, [])
    if not required:
        return True, len(evidence_list)
    
    # Check how many required fields are mentioned
    count = 0
    evidence_text = " ".join(evidence_list).lower()
    
    for req_field in required:
        # Flexible matching (e.g., "exchange_reserves" matches "exchange reserves")
        field_variants = [req_field, req_field.replace("_", " ")]
        if any(variant.lower() in evidence_text for variant in field_variants):
            count += 1
    
    # Require at least 3 of the required fields
    sufficient = count >= 3
    return sufficient, count


# ─────────────────────────────────────────────────────────
# Pre/Post Trade Muhasaba Fields
# ─────────────────────────────────────────────────────────

PRE_TRADE_MUHASABA = {
    "strategy_fit": None,  # Why this strategy for this asset?
    "disconfirmation": None,  # What would prove thesis wrong?
    "max_acceptable_loss": None,  # Quantified worst case
    "forbidden_assumptions": [],  # What must NOT be assumed
    "tier_verification": None,  # Confirm tier classification is correct
}

POST_TRADE_REASON_CODES = {
    "WIN_THESIS_CONFIRMED": "Thesis played out as expected",
    "WIN_LUCKY": "Thesis wrong but external catalyst saved it",
    "LOSS_THESIS_INVALIDATED": "Core assumption proved false",
    "LOSS_POOR_TIMING": "Thesis correct but entry/exit timing off",
    "LOSS_BLACK_SWAN": "Unpredictable external event",
    "LOSS_POOR_EXECUTION": "Slippage/liquidity issues",
}


if __name__ == "__main__":
    # Test cases
    print("=== TOKEN PROFILE TEST ===\n")
    
    # Test 1: TIER_1 — Bitcoin
    btc_signal = {
        "token": "BTC",
        "symbol": "BTC",
        "coingecko": {
            "market_data": {
                "market_cap": {"usd": 850_000_000_000},
                "fully_diluted_valuation": {"usd": 850_000_000_000},
                "circulating_supply": 19_500_000,
                "max_supply": 21_000_000,
            },
            "categories": ["Cryptocurrency"],
        }
    }
    btc_profile = build_token_profile(btc_signal)
    print(f"BTC: tier={btc_profile.asset_tier}, mc=${btc_profile.market_cap:,.0f}")
    print(f"  Simplified: {TIER_MAP.get(btc_profile.asset_tier)}")
    print(f"  Eligible strategies: {get_eligible_strategies(btc_profile, 'BULL')}\n")
    
    # Test 2: TIER_2 — Mid-cap alt
    link_signal = {
        "token": "LINK",
        "symbol": "LINK",
        "coingecko": {
            "market_data": {
                "market_cap": {"usd": 8_000_000_000},
                "circulating_supply": 500_000_000,
                "max_supply": 1_000_000_000,
            },
            "categories": ["Decentralized Finance (DeFi)"],
        }
    }
    link_profile = build_token_profile(link_signal)
    print(f"LINK: tier={link_profile.asset_tier}, mc=${link_profile.market_cap:,.0f}")
    print(f"  Circulating: {link_profile.circulating_pct:.1f}%")
    print(f"  Eligible strategies: {get_eligible_strategies(link_profile, 'BULL')}\n")
    
    # Test 3: TIER_3 — Meme with safety issues
    pepe_signal = {
        "token": "PEPE",
        "symbol": "PEPE",
        "chain": "solana",
        "address": "PEPE1234....",
        "coingecko": {
            "market_data": {
                "market_cap": {"usd": 50_000_000},
            },
            "categories": ["Meme"],
        },
        "onchain_evidence": {
            "rugcheck": {
                "score": 25,
                "lp_locked_pct": 30,
                "risks": ["high_tax"],
            },
            "birdeye_security": {
                "top10_holder_pct": 65,
            },
            "honeypot": {
                "verdict": "SAFE",
            },
        }
    }
    pepe_profile = build_token_profile(pepe_signal)
    print(f"PEPE: tier={pepe_profile.asset_tier}, mc=${pepe_profile.market_cap:,.0f}")
    print(f"  RugCheck: {pepe_profile.rugcheck_score}/100")
    print(f"  Top10 holders: {pepe_profile.holder_top10_pct:.1f}%")
    passed, reason = meme_safety_gate(pepe_profile)
    print(f"  Safety gate: {'PASS' if passed else 'BLOCKED'}")
    if not passed:
        print(f"  Block reason: {reason}")
    print(f"  Eligible strategies: {get_eligible_strategies(pepe_profile, 'BULL')}\n")
    
    # Test 4: Prompt linting
    print("=== PROMPT LINTING TEST ===\n")
    
    tier1_bad_prompt = "This macro bluechip is showing meme narrative momentum and cult following on crypto Twitter."
    passed, violations = lint_prompt(tier1_bad_prompt, "TIER_1", "whale-following")
    print(f"TIER_1 bad prompt: {'PASS' if passed else 'FAIL'}")
    if violations:
        for v in violations:
            print(f"  - {v}")
    
    tier3_bad_prompt = "This microcap shows strong institutional flow and Federal Reserve correlation."
    passed, violations = lint_prompt(tier3_bad_prompt, "TIER_3", "meme-momentum")
    print(f"\nTIER_3 bad prompt: {'PASS' if passed else 'FAIL'}")
    if violations:
        for v in violations:
            print(f"  - {v}")
    
    tier2_good_prompt = "FDV is 3x current MC due to 67% circulating supply. Major unlock in 6 months."
    passed, violations = lint_prompt(tier2_good_prompt, "TIER_2", "cex-listing-play")
    print(f"\nTIER_2 good prompt: {'PASS' if passed else 'FAIL'}")
    
    print("\n=== TEST COMPLETE ===")
