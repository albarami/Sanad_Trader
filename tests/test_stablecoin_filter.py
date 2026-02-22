#!/usr/bin/env python3
"""
Test: Stablecoin Filter — Universal blocking across all signal sources
"""
import sys
from pathlib import Path

# Add scripts to path
BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR / "scripts"))

from stablecoin_filter import is_stablecoin, filter_signals

def test_symbol_detection():
    """Test stablecoin detection by symbol"""
    assert is_stablecoin(token="USDT") == True
    assert is_stablecoin(token="USDC") == True
    assert is_stablecoin(token="DAI") == True
    assert is_stablecoin(symbol="BUSD") == True
    assert is_stablecoin(symbol="USDC.e") == True  # Bridged variant
    assert is_stablecoin(token="BTC") == False
    assert is_stablecoin(token="SOL") == False
    assert is_stablecoin(token="PEPE") == False
    print("✓ Symbol detection tests passed")

def test_address_detection():
    """Test stablecoin detection by Solana address"""
    assert is_stablecoin(address="Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB") == True  # USDT
    assert is_stablecoin(address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v") == True  # USDC
    assert is_stablecoin(address="NOTASTABLECOINADDRESS1111111111111111111111") == False
    print("✓ Address detection tests passed")

def test_filter_signals():
    """Test signal list filtering"""
    signals = [
        {"token": "BTC", "symbol": "BTC", "volume_24h": 1000000},
        {"token": "USDT", "symbol": "USDT", "volume_24h": 1000000},
        {"token": "PEPE", "symbol": "PEPE", "volume_24h": 500000},
        {"token": "TEST", "token_address": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"},  # USDT address
    ]
    
    valid, blocked = filter_signals(signals)
    
    assert len(valid) == 2  # BTC, PEPE
    assert len(blocked) == 2  # USDT (symbol), TEST (USDT address)
    assert valid[0]["token"] == "BTC"
    assert valid[1]["token"] == "PEPE"
    assert blocked[0]["token"] == "USDT"
    assert blocked[1]["token"] == "TEST"
    assert all(b["block_reason"] == "STABLECOIN_FILTER" for b in blocked)
    
    print("✓ Filter signals tests passed")

def test_whale_tracker_malformed_symbols():
    """Test that malformed symbols like 'AQZMdy53USDT' are NOT blocked"""
    # These are NOT real USDT, just garbage symbols containing "USDT"
    assert is_stablecoin(token="AQZMdy53USDT") == False  # Random prefix + USDT
    assert is_stablecoin(token="Cp3G6HCEUSDT") == False  # Random prefix + USDT
    
    # But actual USDT variants ARE blocked
    assert is_stablecoin(token="USDT") == True
    assert is_stablecoin(token="USDT-8") == True  # Binance variant
    
    print("✓ Malformed symbol tests passed")

if __name__ == "__main__":
    test_symbol_detection()
    test_address_detection()
    test_filter_signals()
    test_whale_tracker_malformed_symbols()
    print("\n✅ ALL STABLECOIN FILTER TESTS PASSED")
