#!/usr/bin/env python3
"""
Honeypot Detector — Sprint 7.1.7
Deterministic Python. No LLMs.

Simulates buy + sell transactions before execution.
If buy succeeds but sell fails/has excessive tax → honeypot.

Uses:
  - Helius simulateTransaction (pre-flight)
  - Jupiter Quote API (swap simulation)
  - Transfer fee config check
  - Sell tax estimation

Called by: sanad_pipeline.py stage_2 (Sanad verification)
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[HONEYPOT] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _get_helius_key() -> str:
    import env_loader
    return env_loader.get_key("HELIUS_API_KEY") or ""


# ─────────────────────────────────────────────────────────
# Jupiter Quote API — Simulate swaps
# ─────────────────────────────────────────────────────────

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Test amount: 0.1 SOL in lamports
TEST_AMOUNT_LAMPORTS = 100_000_000  # 0.1 SOL


def _get_jupiter_quote(input_mint: str, output_mint: str,
                       amount: int, slippage_bps: int = 500) -> dict | None:
    """Get a swap quote from Jupiter with Helius fallback."""
    try:
        resp = requests.get(
            JUPITER_QUOTE_URL,
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": str(slippage_bps),
                "onlyDirectRoutes": "false",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        _log(f"Jupiter quote error: {e}")
    
    # Fallback: Use Helius simulation heuristic
    _log("Jupiter unavailable - using Helius simulation fallback")
    return _helius_simulation_fallback(input_mint, output_mint, amount)


def _helius_simulation_fallback(input_mint: str, output_mint: str, amount: int) -> dict | None:
    """
    Fallback honeypot check using Helius RPC simulation.
    Returns a Jupiter-compatible dict structure for compatibility.
    """
    try:
        from helius_client import simulate_swap
        
        # Simulate the swap using Helius
        sim_result = simulate_swap(
            input_mint=input_mint,
            output_mint=output_mint,
            amount_lamports=amount
        )
        
        if sim_result and sim_result.get("success"):
            # Convert to Jupiter-style response
            out_amount = sim_result.get("output_amount", 0)
            price_impact = sim_result.get("price_impact_pct", 0)
            
            return {
                "outAmount": str(out_amount),
                "priceImpactPct": price_impact,
                "routePlan": [{"swapInfo": {"label": "helius_simulation"}}],
                "fallback_source": "helius"
            }
    except Exception as e:
        _log(f"Helius simulation fallback failed: {e}")
    
    return None


# ─────────────────────────────────────────────────────────
# Honeypot Detection
# ─────────────────────────────────────────────────────────

def check_honeypot(token_mint: str, token_name: str = "") -> dict:
    """
    Full honeypot detection for a Solana SPL token.

    Strategy:
      1. Try Jupiter quote: SOL → token (buy simulation)
      2. Try Jupiter quote: token → SOL (sell simulation)
      3. Compare buy/sell to detect tax asymmetry
      4. Check transfer fee config via Helius

    Returns:
      dict with is_honeypot, buy_possible, sell_possible,
      buy_tax_pct, sell_tax_pct, verdict
    """
    _log(f"Checking honeypot for {token_name or token_mint[:12]}...")

    result = {
        "mint": token_mint,
        "token": token_name,
        "is_honeypot": False,
        "buy_possible": False,
        "sell_possible": False,
        "buy_quote": None,
        "sell_quote": None,
        "buy_tax_pct": 0,
        "sell_tax_pct": 0,
        "round_trip_loss_pct": 0,
        "verdict": "UNKNOWN",
        "checks": [],
        "checked_at": _now().isoformat(),
    }

    # ── Check 1: Transfer fee config (Helius) ──
    transfer_fee = _check_transfer_fee(token_mint)
    if transfer_fee is not None:
        result["checks"].append(f"transfer_fee_bps={transfer_fee}")
        if transfer_fee > 5000:  # >50%
            result["is_honeypot"] = True
            result["verdict"] = "HONEYPOT"
            result["checks"].append("CRITICAL: transfer fee >50%")
            _log(f"  HONEYPOT: Transfer fee {transfer_fee/100:.1f}%")
            return result
        elif transfer_fee > 1000:  # >10%
            result["sell_tax_pct"] = transfer_fee / 100
            result["checks"].append(f"HIGH tax: {transfer_fee/100:.1f}%")

    # ── Check 2: Buy simulation (SOL → token) ──
    buy_quote = _get_jupiter_quote(SOL_MINT, token_mint, TEST_AMOUNT_LAMPORTS)
    if buy_quote:
        result["buy_possible"] = True
        out_amount = int(buy_quote.get("outAmount", 0))
        result["buy_quote"] = {
            "in_amount": TEST_AMOUNT_LAMPORTS,
            "out_amount": out_amount,
            "price_impact_pct": float(buy_quote.get("priceImpactPct", 0)),
            "routes": len(buy_quote.get("routePlan", [])),
        }

        # Estimate buy tax from price impact
        price_impact = abs(float(buy_quote.get("priceImpactPct", 0)))
        if price_impact > 15:
            result["buy_tax_pct"] = price_impact
            result["checks"].append(f"High buy impact: {price_impact:.1f}%")

        # ── Check 3: Sell simulation (token → SOL) ──
        if out_amount > 0:
            sell_quote = _get_jupiter_quote(token_mint, SOL_MINT, out_amount)
            if sell_quote:
                result["sell_possible"] = True
                sell_out = int(sell_quote.get("outAmount", 0))
                result["sell_quote"] = {
                    "in_amount": out_amount,
                    "out_amount": sell_out,
                    "price_impact_pct": float(sell_quote.get("priceImpactPct", 0)),
                }

                # Round-trip loss
                if TEST_AMOUNT_LAMPORTS > 0:
                    round_trip_loss = (1 - sell_out / TEST_AMOUNT_LAMPORTS) * 100
                    result["round_trip_loss_pct"] = round(round_trip_loss, 2)

                    if round_trip_loss > 50:
                        result["is_honeypot"] = True
                        result["checks"].append(
                            f"CRITICAL: {round_trip_loss:.1f}% round-trip loss")
                    elif round_trip_loss > 20:
                        result["checks"].append(
                            f"HIGH: {round_trip_loss:.1f}% round-trip loss")

                # Sell price impact
                sell_impact = abs(float(sell_quote.get("priceImpactPct", 0)))
                if sell_impact > 15:
                    result["sell_tax_pct"] = max(result["sell_tax_pct"], sell_impact)
                    result["checks"].append(f"High sell impact: {sell_impact:.1f}%")
            else:
                # Sell quote failed — possible honeypot
                result["sell_possible"] = False
                result["is_honeypot"] = True
                result["checks"].append("CRITICAL: Sell quote failed — cannot sell")
    else:
        result["checks"].append("No Jupiter route — token may be too new or illiquid")

    # ── Final verdict ──
    if result["is_honeypot"]:
        result["verdict"] = "HONEYPOT"
    elif not result["sell_possible"] and result["buy_possible"]:
        result["verdict"] = "HONEYPOT"
        result["is_honeypot"] = True
    elif result["round_trip_loss_pct"] > 30:
        result["verdict"] = "DANGER"
    elif result["round_trip_loss_pct"] > 15 or result["sell_tax_pct"] > 10:
        result["verdict"] = "CAUTION"
    elif result["buy_possible"] and result["sell_possible"]:
        result["verdict"] = "SAFE"
    else:
        result["verdict"] = "UNKNOWN"

    _log(f"  Verdict: {result['verdict']} "
         f"(buy={result['buy_possible']}, sell={result['sell_possible']}, "
         f"round-trip loss={result['round_trip_loss_pct']:.1f}%)")

    return result


def _check_transfer_fee(token_mint: str) -> int | None:
    """Check if token has a transfer fee configured."""
    api_key = _get_helius_key()
    if not api_key:
        return None
    try:
        resp = requests.post(
            f"https://mainnet.helius-rpc.com/?api-key={api_key}",
            json={
                "jsonrpc": "2.0",
                "id": "transfer-fee",
                "method": "getAsset",
                "params": {"id": token_mint},
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            result = data.get("result", {})
            extensions = result.get("mint_extensions", {})
            tf = extensions.get("transfer_fee_config", {})
            if tf:
                newer = tf.get("newer_transfer_fee", {})
                return int(newer.get("transfer_fee_basis_points", 0))
            return 0
        return None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    _log("=== HONEYPOT DETECTOR TEST ===")

    import env_loader
    env_loader.load_env()

    # Test with BONK (should be SAFE)
    BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    result = check_honeypot(BONK_MINT, "BONK")
    print(f"  Token: BONK")
    print(f"  Verdict: {result['verdict']}")
    print(f"  Buy possible: {result['buy_possible']}")
    print(f"  Sell possible: {result['sell_possible']}")
    print(f"  Round-trip loss: {result['round_trip_loss_pct']:.1f}%")
    print(f"  Checks: {result['checks']}")

    # Test with fake address (should be UNKNOWN)
    result2 = check_honeypot("FakeMint111111111111111111111111111111111111", "FAKE")
    print(f"  Token: FAKE")
    print(f"  Verdict: {result2['verdict']}")

    _log("=== TEST COMPLETE ===")
