#!/usr/bin/env python3
"""
Sanad Trader v3.1 — SQLite State Store (FINAL)
Single source of truth. Atomic transactions. Idempotent operations.
Fast-fail connections (250ms timeout).
"""

import os
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

# Canonical DB path: SANAD_DB_PATH env > SANAD_HOME/state/sanad_trader.db > __file__-relative
_SCRIPT_DIR = Path(__file__).resolve().parent
_BASE_DIR = Path(os.environ.get("SANAD_HOME", str(_SCRIPT_DIR.parent)))
DB_PATH = Path(os.environ["SANAD_DB_PATH"]) if os.environ.get("SANAD_DB_PATH") else _BASE_DIR / "state" / "sanad_trader.db"


# ═══════════════════════════════════════════════════════════
# RUNTIME SSOT GUARD — prevents split-brain at syscall boundary
# ═══════════════════════════════════════════════════════════

import builtins
import inspect

_ORIG_OPEN = builtins.open
_ORIG_REPLACE = os.replace
_ORIG_RENAME = os.rename
_SSOT_GUARD_INSTALLED = False


def _forbidden_paths():
    """Canonical JSON cache paths that only sync_json_cache may write."""
    sd = DB_PATH.parent
    return {
        (sd / "portfolio.json").resolve(),
        (sd / "positions.json").resolve(),
    }


def _is_forbidden(p):
    try:
        return Path(p).resolve() in _forbidden_paths()
    except Exception:
        return False


def _called_from_sync_json_cache():
    """Check if caller is sync_json_cache in state_store.py."""
    for frame in inspect.stack()[2:12]:
        if frame.function == "sync_json_cache" and frame.filename.endswith("state_store.py"):
            return True
    return False


def install_ssot_guard(strict_reads=False):
    """Install runtime guard blocking direct writes to JSON state files.
    
    Args:
        strict_reads: If True, also block reads (for decision-critical scripts).
    
    Once installed, only sync_json_cache() can write portfolio.json/positions.json.
    Any other code attempting to write raises PermissionError.
    """
    global _SSOT_GUARD_INSTALLED
    if _SSOT_GUARD_INSTALLED:
        return
    _SSOT_GUARD_INSTALLED = True

    def guarded_open(file, mode="r", *args, **kwargs):
        if _is_forbidden(file):
            write_mode = any(m in str(mode) for m in ("w", "a", "+", "x"))
            if write_mode and not _called_from_sync_json_cache():
                raise PermissionError(f"SSOT guard: direct write forbidden: {file} (mode={mode})")
            if strict_reads and "r" in str(mode) and not _called_from_sync_json_cache():
                raise PermissionError(f"SSOT guard: direct read forbidden: {file}")
        return _ORIG_OPEN(file, mode, *args, **kwargs)

    def guarded_replace(src, dst, *args, **kwargs):
        if _is_forbidden(dst) and not _called_from_sync_json_cache():
            raise PermissionError(f"SSOT guard: replace forbidden: {dst}")
        return _ORIG_REPLACE(src, dst, *args, **kwargs)

    def guarded_rename(src, dst, *args, **kwargs):
        if _is_forbidden(dst) and not _called_from_sync_json_cache():
            raise PermissionError(f"SSOT guard: rename forbidden: {dst}")
        return _ORIG_RENAME(src, dst, *args, **kwargs)

    builtins.open = guarded_open
    os.replace = guarded_replace
    os.rename = guarded_rename


class DBBusyError(Exception):
    """Raised when database is locked beyond acceptable timeout."""
    pass


def _add_column_if_missing(conn, table, column, coltype):
    """Add column if it doesn't exist. Idempotent."""
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def ensure_tables(db_path=None):
    """Ensure all tables exist. Alias for init_db for clarity."""
    init_db(db_path or DB_PATH)


def init_db(db_path=DB_PATH):
    """Initialize SQLite DB with schema. Idempotent."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    
    # decisions table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            decision_id TEXT PRIMARY KEY,
            signal_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            result TEXT NOT NULL CHECK (result IN ('EXECUTE','SKIP','BLOCK')),
            stage TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            token_address TEXT NOT NULL,
            chain TEXT NOT NULL,
            source_primary TEXT,
            signal_type TEXT,
            score_total REAL,
            score_breakdown_json TEXT,
            strategy_id TEXT,
            position_usd REAL,
            gate_failed INTEGER,
            evidence_json TEXT,
            timings_json TEXT NOT NULL,
            decision_packet_json TEXT NOT NULL
        )
    """)
    
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_signal_id ON decisions(signal_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_token ON decisions(token_address)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_created_at ON decisions(created_at)")
    
    # positions table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            position_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL UNIQUE,
            signal_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('OPEN','CLOSED')),
            token_address TEXT NOT NULL,
            chain TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            entry_price REAL NOT NULL,
            size_usd REAL NOT NULL,
            size_token REAL,
            entry_txid TEXT,
            exit_price REAL,
            exit_reason TEXT,
            closed_at TEXT,
            pnl_usd REAL,
            pnl_pct REAL,
            risk_flag TEXT,
            async_analysis_complete INTEGER NOT NULL DEFAULT 0,
            async_analysis_json TEXT,
            regime_tag TEXT,
            source_primary TEXT,
            features_json TEXT
        )
    """)
    
    conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_token ON positions(token_address)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_created_at ON positions(created_at)")
    
    # Learning loop columns (Ticket 5 migration — idempotent)
    _add_column_if_missing(conn, "positions", "learning_status", "TEXT DEFAULT 'PENDING'")
    _add_column_if_missing(conn, "positions", "learning_updated_at", "TEXT")
    _add_column_if_missing(conn, "positions", "learning_error", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_learning ON positions(status, learning_status)")
    
    # Position close metadata columns (Gate 02 + Close Metadata Fix)
    _add_column_if_missing(conn, "positions", "close_reason", "TEXT")
    _add_column_if_missing(conn, "positions", "close_price", "REAL")
    _add_column_if_missing(conn, "positions", "analysis_json", "TEXT")
    
    # Backfill legacy CLOSED rows where learning_status is NULL
    conn.execute("""
        UPDATE positions SET learning_status = 'PENDING'
        WHERE learning_status IS NULL AND status = 'CLOSED' AND pnl_pct IS NOT NULL
    """)
    
    # async_tasks table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS async_tasks (
            task_id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','DONE','FAILED')),
            attempts INTEGER NOT NULL DEFAULT 0,
            next_run_at TEXT NOT NULL,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_next ON async_tasks(status, next_run_at)")
    
    # bandit_strategy_stats table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bandit_strategy_stats (
            strategy_id TEXT NOT NULL,
            regime_tag TEXT NOT NULL,
            alpha REAL NOT NULL,
            beta REAL NOT NULL,
            n INTEGER NOT NULL,
            last_updated TEXT NOT NULL,
            PRIMARY KEY(strategy_id, regime_tag)
        )
    """)
    
    # source_ucb_stats table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_ucb_stats (
            source_id TEXT PRIMARY KEY,
            n INTEGER NOT NULL,
            reward_sum REAL NOT NULL,
            last_updated TEXT NOT NULL
        )
    """)
    
    # portfolio table (Ticket 12 — unified state)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY,
            current_balance_usd REAL NOT NULL,
            mode TEXT NOT NULL,
            open_position_count INTEGER NOT NULL DEFAULT 0,
            daily_pnl_usd REAL NOT NULL DEFAULT 0,
            max_drawdown_pct REAL NOT NULL DEFAULT 0,
            daily_trades INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)
    
    # Portfolio migrations (idempotent)
    _add_column_if_missing(conn, "portfolio", "starting_balance_usd", "REAL")
    _add_column_if_missing(conn, "portfolio", "daily_pnl_pct", "REAL")
    _add_column_if_missing(conn, "portfolio", "current_drawdown_pct", "REAL")
    _add_column_if_missing(conn, "portfolio", "daily_reset_at", "TEXT")
    
    # Migration: seed portfolio from JSON if table is empty
    count = conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0]
    if count == 0:
        # Resolve portfolio.json path relative to DB location
        portfolio_json_path = db_path.parent / "portfolio.json"
        migrated = False
        
        if portfolio_json_path.exists():
            try:
                import json as _json
                portfolio_data = _json.loads(portfolio_json_path.read_text())
                conn.execute("""
                    INSERT INTO portfolio (
                        id, current_balance_usd, mode, open_position_count,
                        daily_pnl_usd, max_drawdown_pct, daily_trades, updated_at
                    ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    portfolio_data.get("current_balance_usd", 10000.0),
                    portfolio_data.get("mode", "paper"),
                    portfolio_data.get("open_position_count", 0),
                    portfolio_data.get("daily_pnl_usd", 0.0),
                    portfolio_data.get("max_drawdown_pct", 0.0),
                    portfolio_data.get("daily_trades", 0),
                    portfolio_data.get("updated_at", datetime.now(timezone.utc).isoformat())
                ))
                migrated = True
            except Exception:
                pass
        
        # If no JSON or migration failed, insert default row
        if not migrated:
            conn.execute("""
                INSERT INTO portfolio (
                    id, current_balance_usd, mode, open_position_count,
                    daily_pnl_usd, max_drawdown_pct, daily_trades, updated_at
                ) VALUES (1, 10000.0, 'paper', 0, 0.0, 0.0, 0, ?)
            """, (datetime.now(timezone.utc).isoformat(),))
    
    # === V4: fills table ===
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fills (
            fill_id         TEXT PRIMARY KEY,
            position_id     TEXT NOT NULL,
            side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
            venue           TEXT NOT NULL,
            expected_price  REAL,
            exec_price      REAL NOT NULL,
            qty_base        REAL NOT NULL,
            notional_usd    REAL NOT NULL,
            fee_usd         REAL NOT NULL DEFAULT 0,
            fee_bps         REAL NOT NULL DEFAULT 0,
            slippage_bps    REAL NOT NULL DEFAULT 0,
            tx_hash         TEXT,
            created_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fills_position_id ON fills(position_id);
        CREATE INDEX IF NOT EXISTS idx_fills_created_at ON fills(created_at);
    """)

    # === V4: positions cost/reward/attribution columns (idempotent) ===
    _add_column_if_missing(conn, "positions", "entry_fill_id", "TEXT")
    _add_column_if_missing(conn, "positions", "exit_fill_id", "TEXT")
    _add_column_if_missing(conn, "positions", "entry_expected_price", "REAL")
    _add_column_if_missing(conn, "positions", "entry_slippage_bps", "REAL")
    _add_column_if_missing(conn, "positions", "entry_fee_usd", "REAL")
    _add_column_if_missing(conn, "positions", "entry_fee_bps", "REAL")
    _add_column_if_missing(conn, "positions", "exit_expected_price", "REAL")
    _add_column_if_missing(conn, "positions", "exit_slippage_bps", "REAL")
    _add_column_if_missing(conn, "positions", "exit_fee_usd", "REAL")
    _add_column_if_missing(conn, "positions", "exit_fee_bps", "REAL")
    _add_column_if_missing(conn, "positions", "fees_usd_total", "REAL")
    _add_column_if_missing(conn, "positions", "pnl_gross_usd", "REAL")
    _add_column_if_missing(conn, "positions", "pnl_gross_pct", "REAL")
    _add_column_if_missing(conn, "positions", "reward_bin", "INTEGER")
    _add_column_if_missing(conn, "positions", "reward_real", "REAL")
    _add_column_if_missing(conn, "positions", "reward_version", "TEXT")
    _add_column_if_missing(conn, "positions", "policy_version", "TEXT")
    _add_column_if_missing(conn, "positions", "decision_id", "TEXT")

    # === V4: performance indexes ===
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_positions_status_closed_at ON positions(status, closed_at);
        CREATE INDEX IF NOT EXISTS idx_positions_policy_closed_at ON positions(policy_version, closed_at);
        CREATE INDEX IF NOT EXISTS idx_positions_strategy_closed_at ON positions(strategy_id, closed_at);
    """)

    # === V4: eval + promotion tables ===
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS policy_configs (
            policy_version TEXT PRIMARY KEY,
            config_json    TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            notes          TEXT
        );

        CREATE TABLE IF NOT EXISTS meta (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS eval_walkforward_runs (
            eval_id                  TEXT PRIMARY KEY,
            created_at               TEXT NOT NULL,
            horizon_days             INTEGER NOT NULL,
            train_days               INTEGER NOT NULL,
            test_days                INTEGER NOT NULL,
            step_days                INTEGER NOT NULL,
            candidate_key            TEXT NOT NULL,
            results_json             TEXT NOT NULL,
            promotion_decision       TEXT NOT NULL CHECK (promotion_decision IN ('PROMOTE','HOLD','ROLLBACK')),
            promoted_policy_version  TEXT,
            promotion_reason         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_eval_walkforward_runs_created ON eval_walkforward_runs(created_at);
    """)

    # === V4 Fix 6: Seed meta + policy_configs for fresh DB ===
    _seed_row = conn.execute("SELECT value FROM meta WHERE key='active_policy_version'").fetchone()
    if _seed_row is None:
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value, updated_at) VALUES ('active_policy_version', 'main', ?)",
            (datetime.now(timezone.utc).isoformat(),)
        )
    _seed_policy = conn.execute("SELECT policy_version FROM policy_configs WHERE policy_version='main'").fetchone()
    if _seed_policy is None:
        conn.execute(
            "INSERT OR IGNORE INTO policy_configs(policy_version, config_json, created_at, notes) VALUES ('main', '{}', ?, 'auto-seeded at init')",
            (datetime.now(timezone.utc).isoformat(),)
        )

    conn.commit()
    conn.close()


@contextmanager
def get_connection(db_path=DB_PATH, timeout_s=0.25, busy_timeout_ms=250):
    """
    Context manager for DB connections with fast-fail semantics.
    
    Args:
        timeout_s: Connection timeout (default 250ms)
        busy_timeout_ms: SQLite busy timeout (default 250ms)
    
    Raises:
        DBBusyError: If database is locked beyond timeout
    """
    conn = sqlite3.connect(db_path, timeout=timeout_s)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    
    try:
        yield conn
        conn.commit()
    except sqlite3.OperationalError as e:
        conn.rollback()
        if "database is locked" in str(e).lower():
            raise DBBusyError(f"Database locked beyond {timeout_s}s timeout") from e
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ===== INTERNAL HELPERS =====

def _insert_decision_internal(conn, decision: dict):
    """Insert decision using existing connection. Idempotent."""
    conn.execute("""
        INSERT OR IGNORE INTO decisions (
            decision_id, signal_id, created_at, policy_version, result,
            stage, reason_code, token_address, chain, source_primary,
            signal_type, score_total, score_breakdown_json, strategy_id,
            position_usd, gate_failed, evidence_json, timings_json,
            decision_packet_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        decision["decision_id"],
        decision["signal_id"],
        decision["created_at"],
        decision["policy_version"],
        decision["result"],
        decision["stage"],
        decision["reason_code"],
        decision["token_address"],
        decision["chain"],
        decision.get("source_primary"),
        decision.get("signal_type"),
        decision.get("score_total"),
        decision.get("score_breakdown_json"),
        decision.get("strategy_id"),
        decision.get("position_usd"),
        decision.get("gate_failed"),
        decision.get("evidence_json"),
        decision.get("timings_json"),
        decision.get("decision_packet_json")
    ))


def _insert_position_internal(conn, position: dict):
    """Insert position using existing connection. Returns rowcount."""
    cursor = conn.execute("""
        INSERT OR IGNORE INTO positions (
            position_id, decision_id, signal_id, created_at, updated_at,
            status, token_address, chain, strategy_id, entry_price,
            size_usd, size_token, regime_tag, source_primary, features_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        position["position_id"],
        position["decision_id"],
        position["signal_id"],
        position["created_at"],
        position["updated_at"],
        "OPEN",
        position["token_address"],
        position["chain"],
        position["strategy_id"],
        position["entry_price"],
        position["size_usd"],
        position.get("size_token"),
        position.get("regime_tag"),
        position.get("source_primary"),
        json.dumps(position.get("features"))
    ))
    return cursor.rowcount


def _enqueue_task_internal(conn, task: dict):
    """Enqueue task using existing connection. Idempotent."""
    conn.execute("""
        INSERT OR IGNORE INTO async_tasks (
            task_id, task_type, entity_id, status, attempts,
            next_run_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        task["task_id"],
        task["task_type"],
        task["entity_id"],
        task["status"],
        task["attempts"],
        task["next_run_at"],
        task["created_at"],
        task["updated_at"]
    ))


# ===== PUBLIC API =====

def insert_decision(decision: dict):
    """
    Insert decision record (EXECUTE/SKIP/BLOCK) to database.
    Public API for router to log all decisions.
    Idempotent via decision_id PRIMARY KEY.
    
    Raises:
        DBBusyError: If database locked beyond timeout
    """
    try:
        with get_connection() as conn:
            _insert_decision_internal(conn, decision)
    except DBBusyError:
        raise


def try_open_position_atomic(decision: dict, price: float, position_payload: dict) -> tuple[dict, dict]:
    """
    Atomic: insert decision + position + task in single transaction.
    Idempotent: returns existing position if already executed.
    
    Returns: (position_dict, metadata_dict)
    metadata_dict contains: {"already_existed": bool, "error": str or None}
    
    Raises:
        DBBusyError: If database locked beyond timeout
    """
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            position_id = position_payload["position_id"]
            
            # Insert decision (idempotent)
            _insert_decision_internal(conn, decision)
            
            # Build position record
            position = {
                "position_id": position_id,
                "decision_id": decision["decision_id"],
                "signal_id": decision["signal_id"],
                "created_at": now_iso,
                "updated_at": now_iso,
                "token_address": decision["token_address"],
                "chain": decision["chain"],
                "strategy_id": decision["strategy_id"],
                "entry_price": price,
                "size_usd": decision["position_usd"],
                "size_token": position_payload.get("size_token"),
                "regime_tag": position_payload.get("regime_tag"),
                "source_primary": decision.get("source_primary"),
                "features": position_payload.get("features")
            }
            
            # Try insert (idempotent via decision_id UNIQUE constraint)
            rowcount = _insert_position_internal(conn, position)
            
            if rowcount == 0:
                # Already exists, fetch existing
                existing = conn.execute(
                    "SELECT * FROM positions WHERE decision_id = ?",
                    (decision["decision_id"],)
                ).fetchone()
                
                return dict(existing), {"already_existed": True, "error": None}
            
            # New position created, enqueue task
            task = {
                "task_id": f"analyze_{position_id}",
                "task_type": "ANALYZE_EXECUTED",
                "entity_id": position_id,
                "status": "PENDING",
                "attempts": 0,
                "next_run_at": now_iso,
                "created_at": now_iso,
                "updated_at": now_iso
            }
            _enqueue_task_internal(conn, task)
            
            # Sync JSON cache after mutation
            result = (position, {"already_existed": False, "error": None})
        
        # Call sync outside transaction context
        sync_json_cache()
        return result
    
    except DBBusyError:
        raise
    except Exception as e:
        return None, {"already_existed": False, "error": str(e)}


def update_position_close(position_id: str, exit_payload: dict, db_path=None):
    """Update position to CLOSED with exit data. Auto-syncs JSON cache.
    
    Args:
        position_id: Position ID to close
        exit_payload: dict with required keys:
            - close_price: exit price (float)
            - close_reason: reason string
            Optional keys (computed if missing):
            - pnl_pct: P&L percentage (computed from entry_price if missing)
            - pnl_usd: P&L USD (computed from pnl_pct and size_usd if missing)
    """
    _db = db_path or DB_PATH
    try:
        with get_connection(_db) as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            
            # Require close_price and close_reason
            if "close_price" not in exit_payload or "close_reason" not in exit_payload:
                raise ValueError("close_price and close_reason are required")
            
            close_price = exit_payload["close_price"]
            close_reason = exit_payload["close_reason"]
            
            # Fetch position for computation if needed
            pos = conn.execute(
                "SELECT entry_price, size_usd FROM positions WHERE position_id = ?",
                (position_id,)
            ).fetchone()
            
            if not pos:
                raise ValueError(f"Position {position_id} not found")
            
            entry_price = pos["entry_price"]
            size_usd = pos["size_usd"]
            
            # Compute pnl_pct if not provided
            if "pnl_pct" in exit_payload:
                pnl_pct = exit_payload["pnl_pct"]
            else:
                if entry_price and entry_price > 0:
                    pnl_pct = ((close_price - entry_price) / entry_price) * 100
                else:
                    pnl_pct = 0.0
            
            # Compute pnl_usd if not provided
            if "pnl_usd" in exit_payload:
                pnl_usd = exit_payload["pnl_usd"]
            else:
                pnl_usd = (pnl_pct / 100) * size_usd if size_usd else 0.0
            
            conn.execute("""
                UPDATE positions SET
                    status = 'CLOSED',
                    updated_at = ?,
                    exit_price = ?,
                    close_price = ?,
                    exit_reason = ?,
                    close_reason = ?,
                    closed_at = ?,
                    pnl_usd = ?,
                    pnl_pct = ?,
                    learning_status = 'PENDING',
                    learning_updated_at = ?,
                    learning_error = NULL
                WHERE position_id = ?
            """, (
                now_iso,
                close_price,  # Keep exit_price for backward compat
                close_price,
                close_reason,  # Keep exit_reason for backward compat
                close_reason,
                now_iso,
                pnl_usd,
                pnl_pct,
                now_iso,
                position_id
            ))
        
        # Sync JSON cache after mutation
        sync_json_cache(db_path=_db)
    except DBBusyError:
        raise


def ensure_and_close_position(position_dict: dict, exit_payload: dict, db_path=None):
    """
    Ensure a position exists in SQLite and close it.
    
    For v3.0→v3.1 bridge: positions opened via file-based pipeline may not
    exist in SQLite yet. This upserts the position then closes it, setting
    learning_status='PENDING' for the learning loop.
    
    Args:
        position_dict: Position data from positions.json (must have 'id' or 'position_id')
        exit_payload: dict with exit_price, exit_reason, pnl_usd, pnl_pct
    
    Returns: position_id
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    position_id = position_dict.get("position_id") or position_dict.get("id", "")
    if not position_id:
        raise ValueError("Position has no id/position_id")
    
    _db = db_path or DB_PATH
    try:
        with get_connection(_db) as conn:
            # Check if position exists
            existing = conn.execute(
                "SELECT position_id FROM positions WHERE position_id = ?",
                (position_id,)
            ).fetchone()
            
            if not existing:
                # Insert from file-based data (v3.0 bridge)
                conn.execute("""
                    INSERT OR IGNORE INTO positions (
                        position_id, decision_id, signal_id, created_at, updated_at,
                        status, token_address, chain, strategy_id, entry_price,
                        size_usd, regime_tag, source_primary
                    ) VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?)
                """, (
                    position_id,
                    position_dict.get("decision_id", f"legacy_decision_{position_id}"),
                    position_dict.get("signal_id", f"legacy_signal_{position_id}"),
                    position_dict.get("opened_at", now_iso),
                    now_iso,
                    position_dict.get("token_address", position_dict.get("token", "UNKNOWN")),
                    position_dict.get("chain", "unknown"),
                    position_dict.get("strategy_name", position_dict.get("strategy_id", "unknown")),
                    position_dict.get("entry_price", 0),
                    position_dict.get("position_usd", position_dict.get("size_usd", 0)),
                    position_dict.get("regime_tag", "unknown"),
                    position_dict.get("source_primary", position_dict.get("signal_source_canonical", "unknown")),
                ))
                
                # Verify row exists (fail-closed: INSERT OR IGNORE may silently skip)
                verify = conn.execute(
                    "SELECT position_id FROM positions WHERE position_id = ?",
                    (position_id,)
                ).fetchone()
                if not verify:
                    raise RuntimeError(
                        f"ensure_and_close_position: insert ignored and position still missing "
                        f"(possible decision_id collision) for {position_id}"
                    )
            
            # Compute required fields
            close_price = exit_payload.get("close_price") or exit_payload.get("exit_price", 0)
            close_reason = exit_payload.get("close_reason") or exit_payload.get("exit_reason", "UNKNOWN")
            
            # Compute P&L if not provided
            entry_price = position_dict.get("entry_price", 0)
            size_usd = position_dict.get("position_usd", position_dict.get("size_usd", 0))
            
            if "pnl_pct" in exit_payload:
                pnl_pct = exit_payload["pnl_pct"]
            elif entry_price and entry_price > 0 and close_price:
                pnl_pct = ((close_price - entry_price) / entry_price) * 100
            else:
                pnl_pct = 0.0
            
            if "pnl_usd" in exit_payload:
                pnl_usd = exit_payload["pnl_usd"]
            else:
                pnl_usd = (pnl_pct / 100) * size_usd if size_usd else 0.0
            
            # Close it — check rowcount
            cur = conn.execute("""
                UPDATE positions SET
                    status = 'CLOSED',
                    updated_at = ?,
                    exit_price = ?,
                    close_price = ?,
                    exit_reason = ?,
                    close_reason = ?,
                    closed_at = ?,
                    pnl_usd = ?,
                    pnl_pct = ?,
                    learning_status = 'PENDING',
                    learning_updated_at = ?,
                    learning_error = NULL
                WHERE position_id = ?
            """, (
                now_iso,
                close_price,  # Keep both for backward compat
                close_price,
                close_reason,
                close_reason,
                now_iso,
                pnl_usd,
                pnl_pct,
                now_iso,
                position_id
            ))
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"ensure_and_close_position: close UPDATE affected {cur.rowcount} rows "
                    f"(position missing) for {position_id}"
                )
        
        # Sync JSON cache after mutation
        sync_json_cache(db_path=_db)
        return position_id
    except DBBusyError:
        raise


def get_open_positions(db_path=None, db_conn=None) -> list[dict]:
    """Get all OPEN positions from SQLite.
    
    Args:
        db_path: Path to database (default: DB_PATH)
        db_conn: Optional existing connection (for use within transactions)
    
    Returns: list of position dicts with status='OPEN'
    """
    if db_conn:
        rows = db_conn.execute("SELECT * FROM positions WHERE status = 'OPEN'").fetchall()
        return [dict(row) for row in rows]
    else:
        _db = db_path or DB_PATH
        with get_connection(_db) as conn:
            rows = conn.execute("SELECT * FROM positions WHERE status = 'OPEN'").fetchall()
            return [dict(row) for row in rows]


def get_batch_size(db_conn=None):
    """Calculate batch size based on executed positions count."""
    if db_conn:
        count = db_conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    else:
        with get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    
    return 10 if count >= 50 else 5


# ============================================================================
# READ-ONLY STAT LOADERS (Ticket 10 — DB-backed hot path stats)
# ============================================================================

def get_source_ucb_stats(db_path=None):
    """Load all source UCB1 stats from SQLite.
    
    Returns: dict[source_id] = {"n": int, "reward_sum": float}
    Empty dict if table is empty. DBBusyError propagates.
    """
    with get_connection(db_path or DB_PATH) as conn:
        rows = conn.execute(
            "SELECT source_id, n, reward_sum FROM source_ucb_stats"
        ).fetchall()
    return {
        row["source_id"]: {"n": row["n"], "reward_sum": row["reward_sum"]}
        for row in rows
    }


def get_bandit_stats(db_path=None):
    """Load all bandit (Thompson) strategy stats from SQLite.
    
    Returns: dict[(strategy_id, regime_tag)] = {"alpha": float, "beta": float, "n": int}
    Empty dict if table is empty. DBBusyError propagates.
    """
    with get_connection(db_path or DB_PATH) as conn:
        rows = conn.execute(
            "SELECT strategy_id, regime_tag, alpha, beta, n FROM bandit_strategy_stats"
        ).fetchall()
    return {
        (row["strategy_id"], row["regime_tag"]): {
            "alpha": row["alpha"], "beta": row["beta"], "n": row["n"]
        }
        for row in rows
    }


# ============================================================================
# UNIFIED STATE API (Ticket 12 — SQLite as single source of truth)
# ============================================================================

def get_portfolio(db_path=None) -> dict:
    """Get portfolio state from SQLite (single row, id=1).
    
    Returns: dict with keys: current_balance_usd, mode, open_position_count,
             daily_pnl_usd, max_drawdown_pct, daily_trades, updated_at,
             daily_pnl_pct (computed), current_drawdown_pct (computed)
    Raises: RuntimeError if portfolio row doesn't exist (DB not initialized)
    """
    _db = db_path or DB_PATH
    with get_connection(_db) as conn:
        row = conn.execute("SELECT * FROM portfolio WHERE id = 1").fetchone()
        if not row:
            raise RuntimeError("Portfolio row missing — run init_db() or ensure_tables()")
        
        portfolio = dict(row)
        
        # Compute daily_pnl_pct if not already present or if starting_balance_usd exists
        starting_balance = portfolio.get("starting_balance_usd", 0) or 10000.0
        daily_pnl_usd = portfolio.get("daily_pnl_usd", 0) or 0.0
        
        if starting_balance > 0:
            portfolio["daily_pnl_pct"] = (daily_pnl_usd / starting_balance) * 100
        else:
            portfolio["daily_pnl_pct"] = 0.0
        
        # current_drawdown_pct = max_drawdown_pct (alias for Gate 02)
        portfolio["current_drawdown_pct"] = portfolio.get("max_drawdown_pct", 0.0)
        
        # DERIVED: open_position_count always computed from positions table (never trust stored value)
        actual_count = conn.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
        portfolio["open_position_count"] = actual_count
        
        return portfolio


def update_portfolio(updates: dict, db_path=None):
    """Atomic UPDATE of portfolio (id=1).
    
    Args:
        updates: dict with any subset of columns: current_balance_usd, mode,
                 daily_pnl_usd, max_drawdown_pct, daily_trades,
                 starting_balance_usd
    
    NOTE: open_position_count is DERIVED from positions table. Do not set it.
          Any attempt to set it is silently ignored.
    
    Auto-sets updated_at to current UTC timestamp.
    """
    _db = db_path or DB_PATH
    if not updates:
        return
    
    # Build SET clause dynamically
    # open_position_count is DERIVED — never accept it as an update
    allowed = {
        "current_balance_usd", "mode",
        "daily_pnl_usd", "max_drawdown_pct", "daily_trades",
        "starting_balance_usd", "daily_pnl_pct", "current_drawdown_pct",
        "daily_reset_at"
    }
    updates_filtered = {k: v for k, v in updates.items() if k in allowed}
    if not updates_filtered:
        return
    
    updates_filtered["updated_at"] = datetime.now(timezone.utc).isoformat()
    
    set_clause = ", ".join(f"{k} = ?" for k in updates_filtered.keys())
    values = list(updates_filtered.values())
    
    with get_connection(_db) as conn:
        conn.execute(f"UPDATE portfolio SET {set_clause} WHERE id = 1", values)
    
    # Auto-sync to JSON cache
    sync_json_cache(db_path=_db)


def get_all_positions(db_path=None) -> list[dict]:
    """Get all positions (OPEN and CLOSED) from SQLite.
    
    Returns: list of position dicts, ordered by created_at DESC
    """
    _db = db_path or DB_PATH
    with get_connection(_db) as conn:
        rows = conn.execute(
            "SELECT * FROM positions ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def update_position_price(position_id: str, current_price: float, db_path=None):
    """Update current_price for a position (used by position monitor).
    
    NOTE: This is a lightweight update for monitoring. Does NOT recalculate PnL
    or trigger JSON sync. Use update_position_close() for closing positions.
    """
    _db = db_path or DB_PATH
    now_iso = datetime.now(timezone.utc).isoformat()
    
    # Add current_price column if missing (dynamic schema evolution)
    with get_connection(_db) as conn:
        _add_column_if_missing(conn, "positions", "current_price", "REAL")
        conn.execute("""
            UPDATE positions
            SET current_price = ?, updated_at = ?
            WHERE position_id = ?
        """, (current_price, now_iso, position_id))


def update_position_analysis(position_id: str, analysis_dict: dict, db_path=None):
    """Update position with cold-path analysis results.
    
    Args:
        position_id: Position ID
        analysis_dict: Analysis results (sanad, bull, bear, judge)
        db_path: Optional DB path
    """
    _db = db_path or DB_PATH
    now_iso = datetime.now(timezone.utc).isoformat()
    
    try:
        with get_connection(_db) as conn:
            conn.execute("""
                UPDATE positions
                SET analysis_json = ?, updated_at = ?
                WHERE position_id = ?
            """, (json.dumps(analysis_dict), now_iso, position_id))
        
        # Auto-sync to JSON cache
        sync_json_cache(db_path=_db)
    except DBBusyError:
        raise


# ============================================================
# V4: Fee/Slippage/Reward helpers + record_fill + meta functions
# ============================================================

def _fee_usd(notional_usd: float, fee_bps: float) -> float:
    """Compute fee in USD from notional and basis points."""
    return float(notional_usd) * (float(fee_bps) / 10000.0)


def _clamp(x: float, lo: float, hi: float) -> float:
    """Clamp x to [lo, hi]."""
    return max(lo, min(hi, x))


def compute_reward(net_pnl_usd: float, net_pnl_pct: float, version: str = "v1"):
    """
    Compute reward from net PnL.

    Units contract:
    - net_pnl_pct is a DECIMAL FRACTION (e.g. -0.3092 == -30.92%)
    - reward_bin: binary (1=win, 0=loss) for Thompson/UCB1
    - reward_real: clamped fraction [-1.0, +1.0] for future continuous learning

    Returns: (reward_bin, reward_real, version)
    """
    reward_bin = 1 if (net_pnl_usd or 0.0) > 0 else 0
    reward_real = _clamp(float(net_pnl_pct or 0.0), -1.0, 1.0)
    return reward_bin, reward_real, version


def record_fill(
    position_id: str,
    side: str,
    venue: str,
    expected_price: float = None,
    exec_price: float = 0.0,
    qty_base: float = 0.0,
    fee_bps: float = 0.0,
    fee_usd: float = None,
    slippage_bps: float = 0.0,
    tx_hash: str = None,
    created_at: str = None,
    db_path=None,
    db_conn=None,
) -> str:
    """
    Insert a fill row. Returns fill_id (uuid4).
    notional_usd = qty_base * exec_price.
    fee_usd computed from notional * fee_bps if None.
    
    If db_conn is provided, uses it (for transactional grouping with position ops).
    Otherwise opens own connection.
    """
    import uuid
    fill_id = str(uuid.uuid4())
    notional_usd = float(qty_base) * float(exec_price)
    if fee_usd is None:
        fee_usd = _fee_usd(notional_usd, fee_bps)
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()

    sql = """
        INSERT INTO fills (fill_id, position_id, side, venue, expected_price,
                           exec_price, qty_base, notional_usd, fee_usd, fee_bps,
                           slippage_bps, tx_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (fill_id, position_id, side, venue, expected_price,
              exec_price, qty_base, notional_usd, fee_usd, fee_bps,
              slippage_bps, tx_hash, created_at)

    if db_conn is not None:
        db_conn.execute(sql, params)
    else:
        _db = db_path or DB_PATH
        with get_connection(_db) as conn:
            conn.execute(sql, params)
    return fill_id


def get_meta(key: str, default: str = None, db_path=None) -> str:
    """Read a value from the meta key/value store."""
    _db = db_path or DB_PATH
    with get_connection(_db) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_meta(key: str, value: str, db_path=None):
    """Write a value to the meta key/value store."""
    _db = db_path or DB_PATH
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(_db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now)
        )


def get_active_policy_version(db_path=None) -> str:
    """Get the currently active policy version. Defaults to 'main'."""
    return get_meta("active_policy_version", default="main", db_path=db_path) or "main"


def set_active_policy_version(new_version: str, reason: str = "", eval_id: str = None, db_path=None):
    """Set the active policy version in meta store."""
    set_meta("active_policy_version", new_version, db_path=db_path)


def get_policy_config(policy_version: str = None, db_path=None) -> dict:
    """
    Load config_json from policy_configs for the given version.
    If policy_version is None, uses active policy version.
    Raises KeyError if missing (fail closed).
    """
    _db = db_path or DB_PATH
    if policy_version is None:
        policy_version = get_active_policy_version(db_path=_db)
    with get_connection(_db) as conn:
        row = conn.execute(
            "SELECT config_json FROM policy_configs WHERE policy_version=?",
            (policy_version,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Policy config '{policy_version}' not found — fail closed")
        return json.loads(row["config_json"])


def sync_json_cache(db_path=None):
    """Write current SQLite state to positions.json and portfolio.json.
    
    This is WRITE-ONLY: scripts should NEVER read these JSON files.
    They exist only for backward compat and debugging.
    
    Called automatically after mutations (open_position, close_position, update_portfolio).
    """
    _db = Path(db_path or DB_PATH)
    
    with get_connection(_db) as conn:
        # Fetch all positions
        positions_rows = conn.execute(
            "SELECT * FROM positions ORDER BY created_at DESC"
        ).fetchall()
        positions_list = [dict(row) for row in positions_rows]
        
        # Fetch portfolio
        portfolio_row = conn.execute("SELECT * FROM portfolio WHERE id = 1").fetchone()
        if not portfolio_row:
            portfolio_dict = {
                "mode": "paper",
                "current_balance_usd": 10000.0,
                "open_position_count": 0,
                "daily_pnl_usd": 0.0,
                "max_drawdown_pct": 0.0,
                "daily_trades": 0,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
        else:
            portfolio_dict = dict(portfolio_row)
            # Remove 'id' from JSON output (internal SQLite key)
            portfolio_dict.pop("id", None)
        
        # DERIVED: open_position_count always from positions table
        actual_open = conn.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
        portfolio_dict["open_position_count"] = actual_open
    
    # Write to JSON atomically — use DB location's parent (state/) directory
    state_dir = _db.parent
    positions_path = state_dir / "positions.json"
    portfolio_path = state_dir / "portfolio.json"
    
    state_dir.mkdir(parents=True, exist_ok=True)
    
    # Atomic write via temp file
    import tempfile
    
    # positions.json
    positions_json = {"positions": positions_list}
    with tempfile.NamedTemporaryFile(mode="w", dir=state_dir, delete=False) as tmp:
        json.dump(positions_json, tmp, indent=2, default=str)
        tmp_path_pos = Path(tmp.name)
    tmp_path_pos.replace(positions_path)
    
    # portfolio.json
    with tempfile.NamedTemporaryFile(mode="w", dir=state_dir, delete=False) as tmp:
        json.dump(portfolio_dict, tmp, indent=2, default=str)
        tmp_path_port = Path(tmp.name)
    tmp_path_port.replace(portfolio_path)
