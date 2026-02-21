#!/usr/bin/env python3
"""
Sanad Trader v3.1 — SQLite State Store (FINAL)
Single source of truth. Atomic transactions. Idempotent operations.
Fast-fail connections (250ms timeout).
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

DB_PATH = Path("state/sanad_trader.db")


class DBBusyError(Exception):
    """Raised when database is locked beyond acceptable timeout."""
    pass


def init_db(db_path=DB_PATH):
    """Initialize SQLite DB with schema. Idempotent."""
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
            
            return position, {"already_existed": False, "error": None}
    
    except DBBusyError:
        raise
    except Exception as e:
        return None, {"already_existed": False, "error": str(e)}


def update_position_close(position_id: str, exit_payload: dict):
    """Update position to CLOSED with exit data."""
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute("""
                UPDATE positions SET
                    status = 'CLOSED',
                    updated_at = ?,
                    exit_price = ?,
                    exit_reason = ?,
                    closed_at = ?,
                    pnl_usd = ?,
                    pnl_pct = ?
                WHERE position_id = ?
            """, (
                now_iso,
                exit_payload["exit_price"],
                exit_payload["exit_reason"],
                now_iso,
                exit_payload["pnl_usd"],
                exit_payload["pnl_pct"],
                position_id
            ))
    except DBBusyError:
        raise


def get_open_positions(db_conn=None):
    """Get all open positions."""
    if db_conn:
        rows = db_conn.execute("SELECT * FROM positions WHERE status = 'OPEN'").fetchall()
        return [dict(row) for row in rows]
    else:
        with get_connection() as conn:
            return get_open_positions(conn)


def get_batch_size(db_conn=None):
    """Calculate batch size based on executed positions count."""
    if db_conn:
        count = db_conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    else:
        with get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    
    return 10 if count >= 50 else 5
