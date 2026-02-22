#!/usr/bin/env python3
"""
Sanad Trader v3.1 — Test Gate 02 + Position Close Metadata

Tests for two critical fixes:
1. Gate 02 portfolio field mismatch (daily_pnl_pct / current_drawdown_pct computation)
2. Position close metadata persistence (close_reason, close_price, closed_at, analysis_json)

All tests use isolated temp DBs. Never touch production.

Author: Sanad Trader v3.1 Test Suite
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent))

import state_store
import policy_engine


class TestGate02PortfolioFieldMismatch(unittest.TestCase):
    """Test Gate 02 capital preservation with various portfolio field states."""
    
    def setUp(self):
        """Create isolated temp DB for each test."""
        self.temp_dir = tempfile.mkdtemp(prefix="test_gate02_")
        self.db_path = Path(self.temp_dir) / "test.db"
        state_store.init_db(self.db_path)
        
        # Mock config
        self.config = {
            "risk": {
                "daily_loss_limit_pct": 5.0,
                "max_drawdown_pct": 15.0
            }
        }
        
        # Mock decision packet
        self.decision_packet = {
            "correlation_id": "test_gate02",
            "token": {"symbol": "TEST"},
            "position_size_usd": 100.0
        }
    
    def tearDown(self):
        """Clean up temp DB."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_gate02_with_pct_fields(self):
        """Test 1: Portfolio has daily_pnl_pct + current_drawdown_pct → gate passes."""
        # Manually set pct fields via ALTER TABLE and INSERT
        with state_store.get_connection(self.db_path) as conn:
            # Seed portfolio with pct fields
            conn.execute("DELETE FROM portfolio WHERE id = 1")
            conn.execute("""
                INSERT INTO portfolio (
                    id, current_balance_usd, mode, open_position_count,
                    daily_pnl_usd, max_drawdown_pct, daily_trades, updated_at,
                    starting_balance_usd, daily_pnl_pct, current_drawdown_pct
                ) VALUES (1, 10500.0, 'paper', 0, 100.0, 2.0, 5, ?, 10000.0, 1.0, 2.0)
            """, (datetime.now(timezone.utc).isoformat(),))
        
        portfolio = state_store.get_portfolio(self.db_path)
        state = {"portfolio": portfolio}
        
        passed, evidence = policy_engine.gate_02_capital_preservation(
            self.config, self.decision_packet, state
        )
        
        self.assertTrue(passed, f"Gate 02 should pass with pct fields. Evidence: {evidence}")
        self.assertIn("1.00%", evidence)  # daily_pnl_pct
        self.assertIn("2.00%", evidence)  # current_drawdown_pct
    
    def test_gate02_computes_from_usd(self):
        """Test 2: Portfolio has daily_pnl_usd + starting_balance_usd → gate computes pct and passes."""
        with state_store.get_connection(self.db_path) as conn:
            conn.execute("DELETE FROM portfolio WHERE id = 1")
            conn.execute("""
                INSERT INTO portfolio (
                    id, current_balance_usd, mode, open_position_count,
                    daily_pnl_usd, max_drawdown_pct, daily_trades, updated_at,
                    starting_balance_usd
                ) VALUES (1, 10200.0, 'paper', 0, 200.0, 3.0, 2, ?, 10000.0)
            """, (datetime.now(timezone.utc).isoformat(),))
        
        portfolio = state_store.get_portfolio(self.db_path)
        state = {"portfolio": portfolio}
        
        # get_portfolio should compute daily_pnl_pct = 200 / 10000 * 100 = 2.0
        self.assertAlmostEqual(portfolio["daily_pnl_pct"], 2.0, places=2)
        
        passed, evidence = policy_engine.gate_02_capital_preservation(
            self.config, self.decision_packet, state
        )
        
        self.assertTrue(passed, f"Gate 02 should pass with computed pct. Evidence: {evidence}")
        self.assertIn("2.00%", evidence)
    
    def test_gate02_missing_all_defaults_zero(self):
        """Test 3: Portfolio has neither pct nor USD baseline → defaults to 0, passes."""
        with state_store.get_connection(self.db_path) as conn:
            conn.execute("DELETE FROM portfolio WHERE id = 1")
            conn.execute("""
                INSERT INTO portfolio (
                    id, current_balance_usd, mode, open_position_count,
                    daily_pnl_usd, max_drawdown_pct, daily_trades, updated_at
                ) VALUES (1, 10000.0, 'paper', 0, 0.0, 0.0, 0, ?)
            """, (datetime.now(timezone.utc).isoformat(),))
        
        portfolio = state_store.get_portfolio(self.db_path)
        state = {"portfolio": portfolio}
        
        # Should default to 0% PnL (no loss = no block)
        self.assertEqual(portfolio["daily_pnl_pct"], 0.0)
        
        passed, evidence = policy_engine.gate_02_capital_preservation(
            self.config, self.decision_packet, state
        )
        
        self.assertTrue(passed, f"Gate 02 should pass with 0% loss. Evidence: {evidence}")
    
    def test_gate02_blocks_on_real_loss(self):
        """Test 4: daily_pnl_pct = -6% → gate correctly blocks (limit is -5%)."""
        with state_store.get_connection(self.db_path) as conn:
            conn.execute("DELETE FROM portfolio WHERE id = 1")
            conn.execute("""
                INSERT INTO portfolio (
                    id, current_balance_usd, mode, open_position_count,
                    daily_pnl_usd, max_drawdown_pct, daily_trades, updated_at,
                    starting_balance_usd, daily_pnl_pct, current_drawdown_pct
                ) VALUES (1, 9400.0, 'paper', 0, -600.0, 6.0, 3, ?, 10000.0, -6.0, 6.0)
            """, (datetime.now(timezone.utc).isoformat(),))
        
        portfolio = state_store.get_portfolio(self.db_path)
        state = {"portfolio": portfolio}
        
        passed, evidence = policy_engine.gate_02_capital_preservation(
            self.config, self.decision_packet, state
        )
        
        self.assertFalse(passed, "Gate 02 should BLOCK on -6% loss")
        self.assertIn("Daily loss limit hit", evidence)
        self.assertIn("-6.0", evidence)  # Allow either -6.00% or -6.0000%


class TestPositionCloseMetadata(unittest.TestCase):
    """Test position close metadata persistence."""
    
    def setUp(self):
        """Create isolated temp DB for each test."""
        self.temp_dir = tempfile.mkdtemp(prefix="test_close_meta_")
        self.db_path = Path(self.temp_dir) / "test.db"
        state_store.init_db(self.db_path)
    
    def tearDown(self):
        """Clean up temp DB."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_close_position_metadata(self):
        """Test 5: Open position, close with reason+price → all metadata persisted."""
        now_iso = datetime.now(timezone.utc).isoformat()
        
        # Create position
        with state_store.get_connection(self.db_path) as conn:
            conn.execute("""
                INSERT INTO positions (
                    position_id, decision_id, signal_id, created_at, updated_at,
                    status, token_address, chain, strategy_id, entry_price, size_usd
                ) VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
            """, (
                "pos_test5", "dec_test5", "sig_test5", now_iso, now_iso,
                "0x123", "solana", "test_strategy", 1.00, 200.0
            ))
        
        # Close with reason and price
        state_store.update_position_close("pos_test5", {
            "close_price": 1.50,
            "close_reason": "TAKE_PROFIT"
        }, db_path=self.db_path)
        
        # Verify all fields
        with state_store.get_connection(self.db_path) as conn:
            pos = dict(conn.execute(
                "SELECT * FROM positions WHERE position_id = ?",
                ("pos_test5",)
            ).fetchone())
        
        self.assertEqual(pos["status"], "CLOSED")
        self.assertEqual(pos["close_reason"], "TAKE_PROFIT")
        self.assertEqual(pos["close_price"], 1.50)
        self.assertIsNotNone(pos["closed_at"])
        self.assertAlmostEqual(pos["pnl_pct"], 50.0, places=2)  # (1.50-1.00)/1.00 * 100
        self.assertAlmostEqual(pos["pnl_usd"], 100.0, places=2)  # 50% * 200
    
    def test_close_position_pnl_computation(self):
        """Test 6: Entry=$1.00, Close=$1.50, Size=$200 → pnl_pct=50%, pnl_usd=$100."""
        now_iso = datetime.now(timezone.utc).isoformat()
        
        with state_store.get_connection(self.db_path) as conn:
            conn.execute("""
                INSERT INTO positions (
                    position_id, decision_id, signal_id, created_at, updated_at,
                    status, token_address, chain, strategy_id, entry_price, size_usd
                ) VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
            """, (
                "pos_test6", "dec_test6", "sig_test6", now_iso, now_iso,
                "0xABC", "ethereum", "momentum", 1.00, 200.0
            ))
        
        state_store.update_position_close("pos_test6", {
            "close_price": 1.50,
            "close_reason": "MANUAL_CLOSE"
        }, db_path=self.db_path)
        
        with state_store.get_connection(self.db_path) as conn:
            pos = dict(conn.execute(
                "SELECT pnl_pct, pnl_usd FROM positions WHERE position_id = ?",
                ("pos_test6",)
            ).fetchone())
        
        # Exact computation check
        self.assertAlmostEqual(pos["pnl_pct"], 50.0, places=4)
        self.assertAlmostEqual(pos["pnl_usd"], 100.0, places=4)
    
    def test_analysis_json_persistence(self):
        """Test 7: Write analysis to position, read it back, verify structure."""
        now_iso = datetime.now(timezone.utc).isoformat()
        
        with state_store.get_connection(self.db_path) as conn:
            conn.execute("""
                INSERT INTO positions (
                    position_id, decision_id, signal_id, created_at, updated_at,
                    status, token_address, chain, strategy_id, entry_price, size_usd
                ) VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
            """, (
                "pos_test7", "dec_test7", "sig_test7", now_iso, now_iso,
                "0xDEF", "base", "early_launch", 0.50, 100.0
            ))
        
        # Write analysis
        analysis = {
            "sanad": {"parsed": {"trust_score": 75}, "timestamp": now_iso},
            "bull": {"parsed": {"verdict": "BUY", "confidence": 80}, "timestamp": now_iso},
            "bear": {"parsed": {"verdict": "SKIP", "confidence": 60}, "timestamp": now_iso},
            "judge": {"parsed": {"verdict": "APPROVE", "confidence": 70}, "timestamp": now_iso}
        }
        
        state_store.update_position_analysis("pos_test7", analysis, db_path=self.db_path)
        
        # Read back
        with state_store.get_connection(self.db_path) as conn:
            pos = dict(conn.execute(
                "SELECT analysis_json FROM positions WHERE position_id = ?",
                ("pos_test7",)
            ).fetchone())
        
        self.assertIsNotNone(pos["analysis_json"])
        retrieved = json.loads(pos["analysis_json"])
        
        self.assertEqual(retrieved["sanad"]["parsed"]["trust_score"], 75)
        self.assertEqual(retrieved["judge"]["parsed"]["verdict"], "APPROVE")


def run_all_tests():
    """Run all tests and report results."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # Add all test classes
    suite.addTests(loader.loadTestsFromTestCase(TestGate02PortfolioFieldMismatch))
    suite.addTests(loader.loadTestsFromTestCase(TestPositionCloseMetadata))
    
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    print(f"Tests run: {result.testsRun}")
    print(f"Successes: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"Failures: {len(result.failures)}")
    print(f"Errors: {len(result.errors)}")
    
    if result.failures:
        print("\nFAILURES:")
        for test, traceback in result.failures:
            print(f"  - {test}: {traceback}")
    
    if result.errors:
        print("\nERRORS:")
        for test, traceback in result.errors:
            print(f"  - {test}: {traceback}")
    
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
