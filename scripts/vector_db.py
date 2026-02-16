#!/usr/bin/env python3
"""
Vector Database (RAG Architecture) — Sprint 5.8.1 through 5.8.5

Uses ChromaDB for semantic search over trade history.

5.8.1 — ChromaDB install + setup
5.8.2 — Trade log embeddings
5.8.3 — Semantic query system
5.8.4 — Regime-weighted retrieval
5.8.5 — DuckDB/Parquet for quantitative data (stub — activates with volume)

Reads: genius-memory/wins/, losses/, state/trade_history.json
Writes: state/chromadb/ (persistent storage)

Used by: Strategy Layer to find similar past setups.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"
CHROMA_PATH = STATE_DIR / "chromadb"
WINS_DIR = BASE_DIR / "genius-memory" / "wins"
LOSSES_DIR = BASE_DIR / "genius-memory" / "losses"
TRADE_HISTORY = BASE_DIR / "state" / "trade_history.json"
REGIME_LATEST = BASE_DIR / "genius-memory" / "regime-data" / "latest.json"

COLLECTION_NAME = "trade_logs"

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[VECDB] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


# ─────────────────────────────────────────────────────────
# 5.8.1 — ChromaDB Setup
# ─────────────────────────────────────────────────────────

_client = None
_collection = None


def get_collection():
    """Get or create the ChromaDB collection."""
    global _client, _collection

    if _collection is not None:
        return _collection

    try:
        import chromadb
        from chromadb.config import Settings

        CHROMA_PATH.mkdir(parents=True, exist_ok=True)

        _client = chromadb.PersistentClient(
            path=str(CHROMA_PATH),
            settings=Settings(anonymized_telemetry=False),
        )

        _collection = _client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"description": "Sanad Trader trade logs for RAG retrieval"},
        )

        _log(f"ChromaDB ready: {_collection.count()} documents in collection")
        return _collection

    except ImportError:
        _log("ChromaDB not installed. Run: pip install chromadb --break-system-packages")
        return None
    except Exception as e:
        _log(f"ChromaDB init failed: {e}")
        return None


# ─────────────────────────────────────────────────────────
# 5.8.2 — Trade Log Embeddings
# ─────────────────────────────────────────────────────────

def _trade_to_document(trade: dict) -> str:
    """Convert a trade record to a text document for embedding."""
    parts = []

    token = trade.get("token", trade.get("symbol", "unknown"))
    strategy = trade.get("strategy", "unknown")
    result = "WIN" if trade.get("pnl_pct", 0) > 0 else "LOSS"
    pnl = trade.get("pnl_pct", 0)
    regime = trade.get("regime", trade.get("regime_at_entry", "unknown"))
    source = trade.get("source", trade.get("signal_source", "unknown"))

    parts.append(f"Token: {token}")
    parts.append(f"Strategy: {strategy}")
    parts.append(f"Result: {result} ({pnl}%)")
    parts.append(f"Regime: {regime}")
    parts.append(f"Source: {source}")

    if trade.get("entry_price"):
        parts.append(f"Entry: ${trade['entry_price']}")
    if trade.get("exit_price"):
        parts.append(f"Exit: ${trade['exit_price']}")
    if trade.get("hold_duration_hours") or trade.get("hold_hours"):
        hours = trade.get("hold_duration_hours", trade.get("hold_hours", 0))
        parts.append(f"Hold: {hours}h")
    if trade.get("exit_reason"):
        parts.append(f"Exit reason: {trade['exit_reason']}")
    if trade.get("sanad_score"):
        parts.append(f"Sanad score: {trade['sanad_score']}")
    if trade.get("notes"):
        parts.append(f"Notes: {trade['notes']}")

    return " | ".join(parts)


def _trade_metadata(trade: dict) -> dict:
    """Extract metadata for filtering."""
    pnl = trade.get("pnl_pct", 0)
    if isinstance(pnl, str):
        try:
            pnl = float(pnl.replace("%", ""))
        except ValueError:
            pnl = 0

    return {
        "token": str(trade.get("token", trade.get("symbol", "unknown"))),
        "strategy": str(trade.get("strategy", "unknown")),
        "result": "WIN" if pnl > 0 else "LOSS",
        "pnl_pct": float(pnl),
        "regime": str(trade.get("regime", trade.get("regime_at_entry", "unknown"))),
        "source": str(trade.get("source", trade.get("signal_source", "unknown"))),
    }


def index_trade(trade: dict, trade_id: str = None) -> bool:
    """Index a single trade into ChromaDB."""
    collection = get_collection()
    if not collection:
        return False

    if not trade_id:
        trade_id = trade.get("trade_id", trade.get("id",
            f"trade_{hash(json.dumps(trade, default=str)) % 10**8}"))

    doc = _trade_to_document(trade)
    meta = _trade_metadata(trade)

    try:
        collection.upsert(
            ids=[str(trade_id)],
            documents=[doc],
            metadatas=[meta],
        )
        return True
    except Exception as e:
        _log(f"Index error for {trade_id}: {e}")
        return False


def index_all_trades() -> int:
    """Index all trades from history."""
    collection = get_collection()
    if not collection:
        return 0

    history = _load_json(TRADE_HISTORY, [])
    trades = history if isinstance(history, list) else history.get("trades", [])

    count = 0
    for i, trade in enumerate(trades):
        trade_id = trade.get("trade_id", trade.get("id", f"trade_{i}"))
        if index_trade(trade, trade_id):
            count += 1

    _log(f"Indexed {count}/{len(trades)} trades")
    return count


def index_post_mortems() -> int:
    """Index win/loss post-mortem files."""
    collection = get_collection()
    if not collection:
        return 0

    count = 0
    for folder, result in [(WINS_DIR, "WIN"), (LOSSES_DIR, "LOSS")]:
        if not folder.exists():
            continue
        for f in folder.iterdir():
            if f.suffix in (".json", ".md", ".txt"):
                try:
                    content = f.read_text()[:2000]
                    doc_id = f"postmortem_{f.stem}"
                    collection.upsert(
                        ids=[doc_id],
                        documents=[content],
                        metadatas=[{"type": "post_mortem", "result": result, "filename": f.name}],
                    )
                    count += 1
                except Exception as e:
                    _log(f"Error indexing {f.name}: {e}")

    _log(f"Indexed {count} post-mortems")
    return count


# ─────────────────────────────────────────────────────────
# 5.8.3 — Semantic Query System
# ─────────────────────────────────────────────────────────

def query_similar(query_text: str, n_results: int = 10, where: dict = None) -> list:
    """Find similar past trades/documents.

    query_text: description of current setup
    n_results: max results
    where: ChromaDB filter dict, e.g. {"strategy": "meme-momentum"}
    """
    collection = get_collection()
    if not collection:
        return []

    try:
        kwargs = {
            "query_texts": [query_text],
            "n_results": min(n_results, collection.count() or 1),
        }
        if where:
            kwargs["where"] = where

        results = collection.query(**kwargs)

        output = []
        if results and results["ids"]:
            for i, doc_id in enumerate(results["ids"][0]):
                output.append({
                    "id": doc_id,
                    "document": results["documents"][0][i] if results["documents"] else "",
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "distance": results["distances"][0][i] if results["distances"] else 0,
                })
        return output

    except Exception as e:
        _log(f"Query error: {e}")
        return []


# ─────────────────────────────────────────────────────────
# 5.8.4 — Regime-Weighted Retrieval
# ─────────────────────────────────────────────────────────

def query_regime_weighted(query_text: str, n_results: int = 10) -> list:
    """Query with regime weighting.

    1. First: search ONLY same-regime trades
    2. If insufficient: fall back to cross-regime with penalty
    """
    regime_data = _load_json(REGIME_LATEST, {})
    current_regime = regime_data.get("combined_tag", "UNKNOWN")

    # Same-regime first
    same_regime = query_similar(
        query_text,
        n_results=n_results,
        where={"regime": current_regime} if current_regime != "UNKNOWN" else None,
    )

    if len(same_regime) >= n_results // 2:
        # Enough same-regime results
        for r in same_regime:
            r["regime_match"] = True
            r["regime_penalty"] = 0
        return same_regime

    # Not enough — add cross-regime with penalty
    all_results = query_similar(query_text, n_results=n_results * 2)

    output = []
    for r in all_results:
        r_regime = r.get("metadata", {}).get("regime", "UNKNOWN")
        if r_regime == current_regime:
            r["regime_match"] = True
            r["regime_penalty"] = 0
        else:
            r["regime_match"] = False
            r["regime_penalty"] = 0.3  # 30% relevance penalty
            r["distance"] = r.get("distance", 0) * 1.3  # Increase distance
        output.append(r)

    # Sort by adjusted distance
    output.sort(key=lambda x: x.get("distance", float("inf")))
    return output[:n_results]


# ─────────────────────────────────────────────────────────
# 5.8.5 — Quantitative Data (DuckDB/Parquet stub)
# ─────────────────────────────────────────────────────────

def init_parquet_store():
    """Initialize Parquet store for quantitative data.

    Activates when trade volume justifies it (100+ trades).
    """
    parquet_dir = BASE_DIR / "data" / "parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)

    # Check if we have enough data
    history = _load_json(TRADE_HISTORY, [])
    trades = history if isinstance(history, list) else history.get("trades", [])

    if len(trades) < 100:
        _log(f"Parquet store: {len(trades)} trades (need 100+ for activation)")
        return False

    try:
        import pandas as pd
        df = pd.DataFrame(trades)
        output = parquet_dir / "trade_history.parquet"
        df.to_parquet(output, index=False)
        _log(f"Parquet store: wrote {len(df)} trades to {output}")
        return True
    except ImportError:
        _log("Parquet store: pandas not available (install for quantitative analysis)")
        return False
    except Exception as e:
        _log(f"Parquet store error: {e}")
        return False


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def run():
    _log("=== VECTOR DB SETUP ===")

    # 5.8.1: Init ChromaDB
    collection = get_collection()
    if not collection:
        _log("FAILED: ChromaDB not available")
        return

    # 5.8.2: Index trades
    trade_count = index_all_trades()
    pm_count = index_post_mortems()
    total = collection.count()
    _log(f"Total documents in DB: {total}")

    # 5.8.3: Test query
    print(f"\n  === Query Test ===")
    results = query_similar("meme coin pump high volume social momentum", n_results=3)
    print(f"    Query: 'meme coin pump high volume social momentum'")
    print(f"    Results: {len(results)}")
    for r in results:
        print(f"      [{r['distance']:.3f}] {r['document'][:80]}...")

    # 5.8.4: Regime-weighted query
    print(f"\n  === Regime-Weighted Query Test ===")
    results = query_regime_weighted("bearish sentiment reversal whale selling", n_results=3)
    print(f"    Query: 'bearish sentiment reversal whale selling'")
    print(f"    Results: {len(results)}")
    for r in results:
        match = "SAME" if r.get("regime_match") else "CROSS"
        print(f"      [{r['distance']:.3f}] ({match}) {r.get('document', '')[:80]}...")

    # 5.8.5: Parquet stub
    init_parquet_store()

    _log("=== SETUP COMPLETE ===")


if __name__ == "__main__":
    run()
