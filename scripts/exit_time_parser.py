#!/usr/bin/env python3
"""
Exit Time Parser — Extract max hold duration from Bull's trade plan

Bull writes intelligent timeframes:
- "3-7 days" → max_hold = 168h (7 days)
- "24-72 hours" → max_hold = 72h
- "2-4 weeks" → max_hold = 672h (28 days)

This parser extracts the maximum hold time and converts to hours.
"""

import re


def extract_max_hold_hours(bull_timeframe: str, asset_tier: str = "TIER_3_MICRO") -> int:
    """
    Parse Bull's timeframe string and return max hold hours.
    
    Args:
        bull_timeframe: String like "3-7 days", "24-72 hours", "2-4 weeks"
        asset_tier: Fallback if parsing fails
    
    Returns:
        Maximum hold hours
    """
    if not bull_timeframe or not isinstance(bull_timeframe, str):
        return _tier_default(asset_tier)
    
    text = bull_timeframe.lower().strip()
    
    # Pattern: "X-Y units" or "X units"
    # Examples: "3-7 days", "24-72 hours", "5 days", "2 weeks"
    
    # Try range pattern first: "3-7 days"
    range_match = re.search(r'(\d+)[-–to]+(\d+)\s*(hour|day|week|month)', text)
    if range_match:
        max_val = int(range_match.group(2))
        unit = range_match.group(3)
        return _convert_to_hours(max_val, unit)
    
    # Try single value: "5 days"
    single_match = re.search(r'(\d+)\s*(hour|day|week|month)', text)
    if single_match:
        val = int(single_match.group(1))
        unit = single_match.group(2)
        return _convert_to_hours(val, unit)
    
    # Fallback to tier default
    return _tier_default(asset_tier)


def _convert_to_hours(value: int, unit: str) -> int:
    """Convert time value to hours."""
    if "hour" in unit:
        return value
    elif "day" in unit:
        return value * 24
    elif "week" in unit:
        return value * 24 * 7
    elif "month" in unit:
        return value * 24 * 30
    return value


def _tier_default(asset_tier: str) -> int:
    """Default max hold based on asset tier."""
    defaults = {
        "TIER_1_MACRO": 168,        # 7 days (BTC, ETH)
        "TIER_2_ALT_LARGE": 120,    # 5 days (SOL, LINK)
        "TIER_3_MEME_CEX": 72,      # 3 days (memes on CEX)
        "TIER_3_MICRO": 24,         # 1 day (micro/new tokens)
    }
    return defaults.get(asset_tier, 24)


# Tests
if __name__ == "__main__":
    test_cases = [
        ("3-7 days", "TIER_1_MACRO", 168),
        ("24-72 hours", "TIER_3_MICRO", 72),
        ("2-4 weeks", "TIER_2_ALT_LARGE", 672),
        ("5 days", "TIER_2_ALT_LARGE", 120),
        ("14-30 days", "TIER_1_MACRO", 720),
        ("", "TIER_1_MACRO", 168),  # Fallback to tier
        ("some random text", "TIER_3_MICRO", 24),  # Fallback
    ]
    
    print("=== EXIT TIME PARSER TESTS ===\n")
    
    for timeframe, tier, expected in test_cases:
        result = extract_max_hold_hours(timeframe, tier)
        status = "✅" if result == expected else "❌"
        print(f"{status} '{timeframe}' ({tier}) → {result}h (expected {expected}h)")
