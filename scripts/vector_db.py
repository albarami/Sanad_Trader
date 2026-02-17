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
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
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

def _flatten_trade(trade: dict) -> dict:
    """Flatten nested analysis dicts for uniform access."""
    flat = dict(trade)
    # Post-trade analyzer nests under metrics/trade_details/regime
    if "metrics" in trade and isinstance(trade["metrics"], dict):
        for k, v in trade["metrics"].items():
            flat.setdefault(k, v)
    if "trade_details" in trade and isinstance(trade["trade_details"], dict):
        for k, v in trade["trade_details"].items():
            flat.setdefault(k, v)
    if "regime" in trade and isinstance(trade["regime"], dict):
        flat.setdefault("regime_tag", trade["regime"].get("tag", "unknown"))
        flat["regime"] = trade["regime"].get("tag", str(trade["regime"]))
    if "exit" in trade and isinstance(trade["exit"], dict):
        flat.setdefault("exit_reason", trade["exit"].get("reason", "unknown"))
    return flat


def _trade_to_document(trade: dict) -> str:
    """Convert a trade record to a text document for embedding."""
    trade = _flatten_trade(trade)
    parts = []

    token = trade.get("token", trade.get("symbol", "unknown"))
    strategy = trade.get("strategy", trade.get("strategy_name", "unknown"))
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
    trade = _flatten_trade(trade)
    pnl = trade.get("pnl_pct", 0)
    if isinstance(pnl, str):
        try:
            pnl = float(pnl.replace("%", ""))
        except ValueError:
            pnl = 0

    return {
        "token": str(trade.get("token", trade.get("symbol", "unknown"))),
        "strategy": str(trade.get("strategy", trade.get("strategy_name", "unknown"))),
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
# v3.0 — Expert Knowledge Base
# ─────────────────────────────────────────────────────────

EXPERT_KNOWLEDGE = [
    {
        "id": "EXPERT_GCR_BEAR",
        "content": """GCR Contrarian Bear Setup:
When everyone is bullish and leverage is maxed out, the setup is actually bearish.
Look for: funding rates >0.1% (overleveraged longs), social sentiment >80 (euphoria),
exchange inflows spiking (whales distributing to retail), realized cap flat while price pumps (distribution).
The "obvious pump" is often the top. Fade the crowd when positioning is extreme.""",
        "metadata": {"type": "expert", "source": "GCR", "regime": "BULL", "strategy": "sentiment-divergence", "tier": "TIER_1"},
    },
    {
        "id": "EXPERT_WHALE_ACCUM",
        "content": """Whale Accumulation Pattern (Wintermute/Jump):
Smart money accumulates quietly over DAYS, not hours. Look for: consistent CEX to cold wallet flows,
multiple known smart money addresses buying at similar prices, low social chatter despite buying.
RED FLAG: Single large buy + immediate social hype = likely dump setup. 
Real accumulation = boring + consistent + low key.""",
        "metadata": {"type": "expert", "source": "Wintermute", "regime": "ANY", "strategy": "whale-following", "tier": "TIER_1"},
    },
    {
        "id": "EXPERT_COBIE_FDV",
        "content": """Cobie FDV Trap:
If circulating supply <30% and FDV is 10x+ current market cap, VCs are waiting to dump on you.
Check unlock schedule: are major unlocks in next 3-6 months? Price often tops BEFORE unlock, not during.
The "fundamental thesis" doesn't matter if supply inflation crushes price. Always calc: new supply per month / current circ supply.
If >5% monthly inflation incoming, avoid.""",
        "metadata": {"type": "expert", "source": "Cobie", "regime": "ANY", "strategy": "cex-listing-play", "tier": "TIER_2"},
    },
    {
        "id": "EXPERT_HAYES_MACRO",
        "content": """Arthur Hayes Macro Liquidity:
Bitcoin doesn't care about fundamentals, it cares about global liquidity. Track: Fed balance sheet,
reverse repo facility, Treasury general account, China credit impulse. When liquidity is expanding (Fed QE, RRP draining),
risk assets pump. When liquidity contracts (QT, RRP filling), everything dumps.
Trade the liquidity cycle, not the narrative.""",
        "metadata": {"type": "expert", "source": "Arthur Hayes", "regime": "ANY", "strategy": "sentiment-divergence", "tier": "TIER_1"},
    },
    {
        "id": "EXPERT_MEME_LIFECYCLE",
        "content": """Meme Coin Lifecycle (Murad):
Phase 1 (0-24h): Launch + early hype. Most fail here. High risk.
Phase 2 (24-72h): Cult formation or death. Look for: holder count growing, top 10 holders DECREASING (distribution to believers).
Phase 3 (3-7 days): Make or break. If still alive + growing holders + stable LP, potential runner.
Phase 4 (1-4 weeks): CEX listing speculation. This is the top for most memes.
Don't marry your bags. Memes are trades, not investments.""",
        "metadata": {"type": "expert", "source": "Murad", "regime": "BULL", "strategy": "meme-momentum", "tier": "TIER_3"},
    },
    {
        "id": "EXPERT_MURAD_CULT",
        "content": """Murad Cult Conviction Test:
A real cult holds through -50% dips. Fake cult panic-sells at -20%.
How to test: Check holder retention during dips. Are same wallets still holding after pullback?
Check Telegram/Twitter: are they buying the dip or crying?
BEST signal: organic memes appearing (art, videos) without dev asking. If community creates, they believe.
If dev has to post "wen marketing?", it's dead.""",
        "metadata": {"type": "expert", "source": "Murad", "regime": "ANY", "strategy": "meme-momentum", "tier": "TIER_3"},
    },
    {
        "id": "EXPERT_ZACHXBT_RUG",
        "content": """ZachXBT Rug Patterns:
1. Dev wallet holds >15% = instant red flag
2. LP unlocked or <60 day lock = can rug anytime
3. Top 10 holders >70% = coordinated dump incoming
4. New token but "team" has no GitHub/Twitter history = anonymous scammers
5. Promises of "utilities" but no actual product = vaporware
6. Celebrity endorsement out of nowhere = they got paid, will dump on you
Trust on-chain data, not marketing.""",
        "metadata": {"type": "expert", "source": "ZachXBT", "regime": "ANY", "strategy": "meme-momentum", "tier": "TIER_3"},
    },
    {
        "id": "EXPERT_CEX_LISTING",
        "content": """CEX Listing Play (Cobie/Hsaka):
Listings are buy-the-rumor, sell-the-news events. Optimal entry: 1-3 weeks BEFORE announcement when smart money accumulates.
RED FLAGS: already pumped 300% = listing priced in. Social hype at ATH = you're exit liquidity.
BEST SETUP: consolidating after initial pump, volume drying up, then listing announcement.
Exit: list price +20% to +50%, don't wait for "Binance main" dreams.""",
        "metadata": {"type": "expert", "source": "Cobie", "regime": "SIDEWAYS", "strategy": "cex-listing-play", "tier": "TIER_2"},
    },
    {
        "id": "EXPERT_SENTIMENT_DIV",
        "content": """Sentiment Divergence Setup:
When social sentiment is extremely bearish but price is holding steady/up slightly = accumulation.
When social sentiment is extremely bullish but price is chopping/down slightly = distribution.
BEST entries: Fear & Greed Index <20 + price higher lows + whale accumulation.
WORST entries: Fear & Greed Index >80 + price lower highs + whale distribution.
Sentiment is a lagging indicator. Smart money front-runs it.""",
        "metadata": {"type": "expert", "source": "GCR", "regime": "BEAR", "strategy": "sentiment-divergence", "tier": "TIER_1"},
    },
    {
        "id": "EXPERT_BEAR_RULES",
        "content": """Bear Market Rules:
1. Most pumps are bull traps. Default to skeptical.
2. "Good news" doesn't matter in bear market. Price still dumps.
3. Bounces are for exiting, not entering (unless you're day trading).
4. Whale accumulation in bear market is real (they buy dips). But verify: is it accumulation or dead cat bounce?
5. Best bear trades: short squeezes, but exit fast. No holding long positions overnight.
6. Cash is a position. Don't force trades just to be "in the game".""",
        "metadata": {"type": "expert", "source": "Multiple", "regime": "BEAR", "strategy": "sentiment-divergence", "tier": "ALL"},
    },
    {
        "id": "EXPERT_ANTI_PATTERNS",
        "content": """Anti-Patterns (Autopsy of Failed Trades):
1. Buying "cheap" = buying dying projects. Focus on momentum, not cheapness.
2. Averaging down = throwing good money at bad trade. If thesis broke, exit.
3. "Everyone is talking about it" = you're late. Best entries are boring.
4. "Fundamentals are good" = doesn't matter if market structure is bad (overleveraged, low liquidity).
5. "It can't go lower" = famous last words. No price is too low.
6. Ignoring on-chain data because "narrative is strong" = hopium. Chain doesn't lie.""",
        "metadata": {"type": "expert", "source": "Post-Trade Analysis", "regime": "ANY", "strategy": "ALL", "tier": "ALL"},
    },
    {
        "id": "EXPERT_ONCHAIN_HIERARCHY",
        "content": """On-Chain Data Hierarchy (by reliability):
1. TIER 1 (Deterministic, never lies): LP lock status, mint authority, freeze authority, holder concentration
2. TIER 2 (High signal): Exchange flows, whale accumulation/distribution, realized cap
3. TIER 3 (Moderate signal): Social metrics, GitHub activity, dev wallet activity
4. TIER 4 (Low signal, high noise): Twitter followers, Telegram members (mostly bots)
When TIER 1 data contradicts narrative, trust TIER 1. When social hype contradicts on-chain reality, fade social.""",
        "metadata": {"type": "expert", "source": "On-Chain Analysis", "regime": "ANY", "strategy": "ALL", "tier": "ALL"},
    },
]


def load_expert_knowledge() -> int:
    """Index all expert knowledge entries into ChromaDB."""
    collection = get_collection()
    if not collection:
        _log("ERROR: ChromaDB not available")
        return 0
    
    count = 0
    for entry in EXPERT_KNOWLEDGE:
        try:
            collection.upsert(
                ids=[entry["id"]],
                documents=[entry["content"]],
                metadatas=[entry["metadata"]],
            )
            count += 1
        except Exception as e:
            _log(f"Error indexing {entry['id']}: {e}")
    
    _log(f"Loaded {count}/{len(EXPERT_KNOWLEDGE)} expert knowledge entries")
    return count


def get_rag_context(token: str, tier: str, strategy: str, regime: str, n_results: int = 3) -> str:
    """
    Get RAG context filtered by tier, strategy, and regime.
    Returns formatted string for inclusion in agent prompts.
    """
    collection = get_collection()
    if not collection:
        return ""
    
    query_text = f"{token} {strategy} {regime}"
    
    # Build filter for tier + strategy + regime
    where_filter = {"type": "expert"}
    # Note: ChromaDB where filters are AND-based, so we query more broadly and filter in post-processing
    
    try:
        results = collection.query(
            query_texts=[query_text],
            n_results=min(n_results * 3, 10),  # Get more then filter
            where=where_filter,
        )
        
        if not results or not results["documents"]:
            return ""
        
        # Post-filter for tier/strategy/regime match
        relevant = []
        for i, doc_id in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i]
            doc = results["documents"][0][i]
            
            # Check if expert knowledge matches our context
            expert_tier = meta.get("tier", "ALL")
            expert_strat = meta.get("strategy", "ALL")
            expert_regime = meta.get("regime", "ANY")
            
            tier_match = expert_tier == "ALL" or expert_tier == tier
            strat_match = expert_strat == "ALL" or expert_strat == strategy
            regime_match = expert_regime == "ANY" or expert_regime == regime
            
            if tier_match and (strat_match or expert_strat == "ALL"):
                relevant.append({
                    "source": meta.get("source", "Unknown"),
                    "content": doc,
                    "strategy": expert_strat,
                    "regime": expert_regime,
                })
        
        if not relevant:
            return ""
        
        # Format for prompt inclusion
        context_lines = ["EXPERT KNOWLEDGE (from past trades & market analysis):"]
        for i, item in enumerate(relevant[:n_results], 1):
            context_lines.append(f"\n{i}. {item['source']} ({item['strategy']}, {item['regime']}):")
            context_lines.append(f"   {item['content']}")
        
        return "\n".join(context_lines)
        
    except Exception as e:
        _log(f"RAG context error: {e}")
        return ""


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
    
    # v3.0: Load expert knowledge
    expert_count = load_expert_knowledge()
    
    total = collection.count()
    _log(f"Total documents in DB: {total} (trades={trade_count}, postmortems={pm_count}, experts={expert_count})")

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
