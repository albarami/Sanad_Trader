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


class DBBusyError(Exception):
    """Raised when database is locked beyond acceptable timeout."""
    pass


def _add_column_if_missing(conn, table, column, coltype):
    """Add column if it doesn't exist. Idempotent."""
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


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
    
    # Learning loop columns (Ticket 5 migration — idempotent)
    _add_column_if_missing(conn, "positions", "learning_status", "TEXT DEFAULT 'PENDING'")
    _add_column_if_missing(conn, "positions", "learning_updated_at", "TEXT")
    _add_column_if_missing(conn, "positions", "learning_error", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_learning ON positions(status, learning_status)")
    
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
                    pnl_pct = ?,
                    learning_status = 'PENDING',
                    learning_updated_at = ?,
                    learning_error = NULL
                WHERE position_id = ?
            """, (
                now_iso,
                exit_payload["exit_price"],
                exit_payload["exit_reason"],
                now_iso,
                exit_payload["pnl_usd"],
                exit_payload["pnl_pct"],
                now_iso,
                position_id
            ))
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
            
            # Close it — check rowcount
            cur = conn.execute("""
                UPDATE positions SET
                    status = 'CLOSED',
                    updated_at = ?,
                    exit_price = ?,
                    exit_reason = ?,
                    closed_at = ?,
                    pnl_usd = ?,
                    pnl_pct = ?,
                    learning_status = 'PENDING',
                    learning_updated_at = ?,
                    learning_error = NULL
                WHERE position_id = ?
            """, (
                now_iso,
                exit_payload["exit_price"],
                exit_payload["exit_reason"],
                now_iso,
                exit_payload["pnl_usd"],
                exit_payload["pnl_pct"],
                now_iso,
                position_id
            ))
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"ensure_and_close_position: close UPDATE affected {cur.rowcount} rows "
                    f"(position missing) for {position_id}"
                )
        
        return position_id
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
