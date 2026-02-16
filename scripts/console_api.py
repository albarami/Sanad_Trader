#!/usr/bin/env python3
"""
Console API Server — Sprint 8.2.1 + 8.3.1-8.3.6
FastAPI backend that serves all state data to the React console.
Handles control actions (kill switch, pause, force close, mode switch).

Run: uvicorn console_api:app --host 0.0.0.0 --port 8100
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"
CONFIG_DIR = BASE_DIR / "config"
REPORTS_DIR = BASE_DIR / "reports"
SIGNALS_DIR = BASE_DIR / "signals"
GENIUS_DIR = BASE_DIR / "genius-memory"
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
except ImportError:
    print("FastAPI not installed. Run: pip install fastapi uvicorn --break-system-packages")
    sys.exit(1)

app = FastAPI(
    title="Sanad Trader Console API",
    version="3.0",
    description="Backend API for Sanad Trader v3.0 Console",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Lock down in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────
# 8.2.4 — Auth: API Key (single user)
# ─────────────────────────────────────────────────────────

import env_loader
from fastapi import Request, Depends, Security
from fastapi.security import APIKeyHeader

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

CONSOLE_API_KEY = env_loader.get_key("CONSOLE_API_KEY") or ""

# Public endpoints (no auth needed)
PUBLIC_PATHS = {"/api/ping", "/", "/docs", "/openapi.json"}

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Skip auth for public paths and static files
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)

    # Skip auth if no key configured (dev mode)
    if not CONSOLE_API_KEY:
        return await call_next(request)

    # Check API key in header or query param
    key = request.headers.get(API_KEY_NAME) or request.query_params.get("api_key")
    if key != CONSOLE_API_KEY:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})

    return await call_next(request)



# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

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


def _now():
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────
# 8.1.1 — System Status
# ─────────────────────────────────────────────────────────

@app.get("/api/status")
def system_status():
    """System Status: heartbeat, policy engine, circuit breakers, mode."""
    heartbeat = _load_json(STATE_DIR / "heartbeat_state.json")
    policy = _load_json(STATE_DIR / "policy_engine_state.json")
    portfolio = _load_json(STATE_DIR / "portfolio.json")
    config = _load_json(CONFIG_DIR / "thresholds.yaml_cache.json",
                        _load_json(STATE_DIR / "config_cache.json"))

    # Circuit breaker states
    cb_files = list(STATE_DIR.glob("*circuit*"))
    circuits = {}
    for f in cb_files:
        data = _load_json(f)
        circuits[f.stem] = data.get("state", "UNKNOWN")

    # Uptime from heartbeat
    hb_ts = heartbeat.get("last_heartbeat", heartbeat.get("timestamp"))
    stale = False
    if hb_ts:
        try:
            last = datetime.fromisoformat(hb_ts.replace("Z", "+00:00"))
            stale = (_now() - last).total_seconds() > 600
        except Exception:
            stale = True

    return {
        "status": "STALE" if stale else "HEALTHY",
        "mode": policy.get("mode", portfolio.get("mode", "paper")),
        "heartbeat": heartbeat,
        "policy_gates_passed": policy.get("gates_passed", 0),
        "policy_gates_total": policy.get("gates_total", 15),
        "circuit_breakers": circuits,
        "portfolio_balance": portfolio.get("balance", portfolio.get("total_equity", 0)),
        "open_positions": portfolio.get("open_positions", 0),
        "kill_switch_active": policy.get("kill_switch", False),
        "server_time": _now().isoformat(),
    }


# ─────────────────────────────────────────────────────────
# 8.1.2 — Live Positions
# ─────────────────────────────────────────────────────────

@app.get("/api/positions")
def live_positions():
    """All open positions with current P&L."""
    positions = _load_json(STATE_DIR / "positions.json", {})
    if isinstance(positions, list):
        pos_list = positions
    elif isinstance(positions, dict):
        pos_list = []
        for k, v in positions.items():
            if isinstance(v, dict):
                v["symbol"] = v.get("symbol", k)
                pos_list.append(v)
    else:
        pos_list = []

    open_pos = [p for p in pos_list if p.get("status") == "open"]
    return {"positions": open_pos, "count": len(open_pos)}


# ─────────────────────────────────────────────────────────
# 8.1.3 — Decision Trace
# ─────────────────────────────────────────────────────────

@app.get("/api/decisions")
def decision_trace(limit: int = 20):
    """Recent pipeline decisions with full agent trace."""
    logs_dir = BASE_DIR / "execution-logs"
    decisions = []
    if logs_dir.exists():
        files = sorted(logs_dir.glob("*.json"), reverse=True)[:limit]
        for f in files:
            data = _load_json(f)
            if data:
                decisions.append({
                    "id": f.stem,
                    "token": data.get("token", "?"),
                    "timestamp": data.get("timestamp"),
                    "sanad_score": data.get("sanad_score", data.get("trust_score")),
                    "recommendation": data.get("recommendation"),
                    "judge_verdict": data.get("judge_verdict"),
                    "stages": data.get("stages", {}),
                })
    return {"decisions": decisions, "count": len(decisions)}


# ─────────────────────────────────────────────────────────
# 8.1.4 — Trade History
# ─────────────────────────────────────────────────────────

@app.get("/api/trades")
def trade_history(limit: int = 50):
    """Completed trades with P&L."""
    history = _load_json(STATE_DIR / "trade_history.json", [])
    trades = history if isinstance(history, list) else history.get("trades", [])

    # Sort by close time descending
    trades = sorted(trades,
                    key=lambda t: t.get("closed_at", t.get("exit_time", "")),
                    reverse=True)

    # Stats
    if trades:
        wins = sum(1 for t in trades if t.get("pnl_pct", 0) > 0)
        total_pnl = sum(t.get("pnl_pct", 0) for t in trades)
        return {
            "trades": trades[:limit],
            "total": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "win_rate": round(wins / len(trades), 4) if trades else 0,
            "total_pnl_pct": round(total_pnl, 2),
        }
    return {"trades": [], "total": 0, "wins": 0, "losses": 0, "win_rate": 0}


# ─────────────────────────────────────────────────────────
# 8.1.5 — Signal Feed
# ─────────────────────────────────────────────────────────

@app.get("/api/signals")
def signal_feed(limit: int = 30):
    """Recent signals from all sources."""
    signals = []
    if SIGNALS_DIR.exists():
        for subdir in SIGNALS_DIR.iterdir():
            if subdir.is_dir():
                for f in sorted(subdir.glob("*.json"), reverse=True)[:limit]:
                    data = _load_json(f)
                    if data:
                        data["_source_dir"] = subdir.name
                        data["_file"] = f.name
                        signals.append(data)

    # Sort by timestamp
    signals.sort(key=lambda s: s.get("timestamp", s.get("_file", "")), reverse=True)
    return {"signals": signals[:limit], "count": len(signals)}


# ─────────────────────────────────────────────────────────
# 8.1.6 — Strategy Dashboard
# ─────────────────────────────────────────────────────────

@app.get("/api/strategies")
def strategy_dashboard():
    """Per-strategy performance stats."""
    stats = _load_json(STATE_DIR / "strategy_stats.json", {})
    changes = _load_json(STATE_DIR / "strategy_changes.json", {})

    strategies = []
    strat_dir = BASE_DIR / "strategies"
    if strat_dir.exists():
        for f in strat_dir.glob("*.md"):
            name = f.stem
            s = stats.get(name, {})
            c = changes.get(name, {})
            strategies.append({
                "name": name,
                "trades": s.get("total_trades", 0),
                "win_rate": s.get("win_rate", 0),
                "avg_pnl": s.get("avg_pnl_pct", 0),
                "active": s.get("active", True),
                "last_change": c.get("last_change_at"),
                "changes_this_week": c.get("changes_this_week", 0),
            })
    return {"strategies": strategies}


# ─────────────────────────────────────────────────────────
# 8.1.7 — Genius Memory Insights
# ─────────────────────────────────────────────────────────

@app.get("/api/genius")
def genius_memory():
    """Pattern extraction + statistical review + counterfactuals."""
    # Latest patterns
    patterns_dir = GENIUS_DIR / "patterns"
    latest_pattern = None
    if patterns_dir.exists():
        files = sorted(patterns_dir.glob("*.json"), reverse=True)
        if files:
            latest_pattern = _load_json(files[0])

    # Latest statistical review
    stats_dir = GENIUS_DIR / "statistical-reviews"
    latest_stats = None
    if stats_dir.exists():
        files = sorted(stats_dir.glob("*.json"), reverse=True)
        if files:
            latest_stats = _load_json(files[0])

    # Counterfactuals
    cf = _load_json(GENIUS_DIR / "counterfactuals.json")

    # Regime
    regime = _load_json(GENIUS_DIR / "regime-data" / "latest.json")

    return {
        "patterns": latest_pattern,
        "statistical_review": latest_stats,
        "counterfactuals": cf,
        "regime": regime,
    }


# ─────────────────────────────────────────────────────────
# 8.1.8 — Execution Quality
# ─────────────────────────────────────────────────────────

@app.get("/api/execution-quality")
def execution_quality():
    """Slippage, fill rates, latency metrics."""
    eq = _load_json(STATE_DIR / "execution_quality_state.json", {})
    return eq


# ─────────────────────────────────────────────────────────
# 8.1.9 — Budget & Cost
# ─────────────────────────────────────────────────────────

@app.get("/api/budget")
def budget_cost():
    """API costs, trade fees, daily budget usage."""
    budget = _load_json(STATE_DIR / "budget_state.json", {})
    portfolio = _load_json(STATE_DIR / "portfolio.json", {})

    return {
        "daily_budget": budget.get("daily_budget_usd", 0),
        "spent_today": budget.get("spent_today_usd", 0),
        "api_calls_today": budget.get("api_calls_today", 0),
        "total_fees_usd": budget.get("total_fees_usd", 0),
        "portfolio_balance": portfolio.get("balance", 0),
    }


# ─────────────────────────────────────────────────────────
# 8.1.10 — Data & Circuit Health
# ─────────────────────────────────────────────────────────

@app.get("/api/health")
def data_health():
    """All data feeds, circuit breakers, cron jobs health."""
    # Price feeds
    price_cache = _load_json(STATE_DIR / "price_cache.json", {})
    ws_state = _load_json(STATE_DIR / "ws_manager_state.json", {})

    # Cron health
    cron_states = {}
    cron_patterns = [
        "heartbeat_state",
        "reconciliation_state",
        "price_snapshot_state",
        "onchain_analytics_state",
        "social_sentiment_state",
    ]
    for name in cron_patterns:
        data = _load_json(STATE_DIR / f"{name}.json")
        if data:
            cron_states[name] = {
                "last_run": data.get("last_run", data.get("timestamp")),
                "status": data.get("status", "unknown"),
            }

    # Circuit breakers
    circuits = {}
    for f in STATE_DIR.glob("*circuit*"):
        circuits[f.stem] = _load_json(f)

    return {
        "price_feeds": len(price_cache),
        "websockets": ws_state,
        "cron_jobs": cron_states,
        "circuit_breakers": circuits,
    }


# ─────────────────────────────────────────────────────────
# 8.1.11 — Red Team Log
# ─────────────────────────────────────────────────────────

@app.get("/api/red-team")
def red_team_log(limit: int = 20):
    """Al-Jassas red team challenges and results."""
    rt_dir = BASE_DIR / "red-team"
    logs = []
    if rt_dir.exists():
        for f in sorted(rt_dir.glob("*.json"), reverse=True)[:limit]:
            data = _load_json(f)
            if data:
                logs.append(data)
    return {"red_team_logs": logs, "count": len(logs)}


# ─────────────────────────────────────────────────────────
# 8.1.12 — Settings & Control
# ─────────────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    """Current thresholds and system settings."""
    # Try YAML first
    try:
        import yaml
        with open(CONFIG_DIR / "thresholds.yaml") as f:
            thresholds = yaml.safe_load(f)
    except Exception:
        thresholds = _load_json(CONFIG_DIR / "thresholds.json", {})

    portfolio = _load_json(STATE_DIR / "portfolio.json", {})

    return {
        "thresholds": thresholds,
        "mode": portfolio.get("mode", "paper"),
        "exchanges": ["binance", "mexc"],
    }


# ─────────────────────────────────────────────────────────
# 8.3.1-8.3.5 — Control Actions
# ─────────────────────────────────────────────────────────

class ControlAction(BaseModel):
    action: str  # kill_switch, pause_strategy, force_close, mode_switch, budget_override
    params: dict = {}
    confirmed: bool = False


COMMANDS_PATH = STATE_DIR / "pending_commands.json"


@app.post("/api/control")
def control_action(action: ControlAction):
    """Execute a control action on the trading system."""
    if not action.confirmed:
        raise HTTPException(status_code=400, detail="Action must be confirmed (confirmed=true)")

    commands = _load_json(COMMANDS_PATH, {"commands": []})

    command = {
        "action": action.action,
        "params": action.params,
        "requested_at": _now().isoformat(),
        "status": "PENDING",
    }

    # 8.3.1 — Kill Switch
    if action.action == "kill_switch":
        policy = _load_json(STATE_DIR / "policy_engine_state.json", {})
        policy["kill_switch"] = True
        policy["kill_switch_reason"] = action.params.get("reason", "Manual activation")
        policy["kill_switch_at"] = _now().isoformat()
        _save_json(STATE_DIR / "policy_engine_state.json", policy)
        command["status"] = "EXECUTED"
        command["result"] = "Kill switch ACTIVATED"
        try:
            import notifier
            notifier.notify_kill_switch(action.params.get("reason", "Manual console activation"))
        except Exception:
            pass

    # 8.3.2 — Pause Strategy
    elif action.action == "pause_strategy":
        strategy = action.params.get("strategy")
        if not strategy:
            raise HTTPException(status_code=400, detail="Missing 'strategy' param")
        stats = _load_json(STATE_DIR / "strategy_stats.json", {})
        if strategy not in stats:
            stats[strategy] = {}
        stats[strategy]["active"] = False
        stats[strategy]["paused_at"] = _now().isoformat()
        stats[strategy]["paused_reason"] = action.params.get("reason", "Manual pause")
        _save_json(STATE_DIR / "strategy_stats.json", stats)
        command["status"] = "EXECUTED"
        command["result"] = f"Strategy {strategy} PAUSED"

    # 8.3.3 — Force Close Position
    elif action.action == "force_close":
        symbol = action.params.get("symbol")
        if not symbol:
            raise HTTPException(status_code=400, detail="Missing 'symbol' param")
        # Write to pending commands for heartbeat to pick up
        command["status"] = "QUEUED"
        command["result"] = f"Force close {symbol} queued for heartbeat"

    # 8.3.4 — Mode Switch
    elif action.action == "mode_switch":
        new_mode = action.params.get("mode")
        if new_mode not in ("paper", "shadow", "live"):
            raise HTTPException(status_code=400, detail="Mode must be paper/shadow/live")
        portfolio = _load_json(STATE_DIR / "portfolio.json", {})
        old_mode = portfolio.get("mode", "paper")
        portfolio["mode"] = new_mode
        portfolio["mode_changed_at"] = _now().isoformat()
        _save_json(STATE_DIR / "portfolio.json", portfolio)
        command["status"] = "EXECUTED"
        command["result"] = f"Mode: {old_mode} → {new_mode}"

    # 8.3.5 — Budget Override
    elif action.action == "budget_override":
        new_budget = action.params.get("daily_budget_usd")
        if not new_budget or float(new_budget) <= 0:
            raise HTTPException(status_code=400, detail="Invalid budget amount")
        budget = _load_json(STATE_DIR / "budget_state.json", {})
        budget["daily_budget_usd"] = float(new_budget)
        budget["overridden_at"] = _now().isoformat()
        _save_json(STATE_DIR / "budget_state.json", budget)
        command["status"] = "EXECUTED"
        command["result"] = f"Budget: ${float(new_budget):.2f}/day"

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action.action}")

    # Log command
    commands["commands"].append(command)
    commands["commands"] = commands["commands"][-100:]
    _save_json(COMMANDS_PATH, commands)

    return command


# ─────────────────────────────────────────────────────────
# 8.3.6 — Command polling (for heartbeat)
# ─────────────────────────────────────────────────────────

@app.get("/api/commands/pending")
def pending_commands():
    """Get pending commands for heartbeat to execute."""
    commands = _load_json(COMMANDS_PATH, {"commands": []})
    pending = [c for c in commands["commands"] if c.get("status") == "QUEUED"]
    return {"pending": pending, "count": len(pending)}


@app.post("/api/commands/ack/{index}")
def ack_command(index: int):
    """Acknowledge a command as executed."""
    commands = _load_json(COMMANDS_PATH, {"commands": []})
    queued = [i for i, c in enumerate(commands["commands"]) if c.get("status") == "QUEUED"]
    if index >= len(queued):
        raise HTTPException(status_code=404, detail="Command not found")
    actual_idx = queued[index]
    commands["commands"][actual_idx]["status"] = "EXECUTED"
    commands["commands"][actual_idx]["executed_at"] = _now().isoformat()
    _save_json(COMMANDS_PATH, commands)
    return {"acknowledged": True}


# ─────────────────────────────────────────────────────────
# 8.4 — Observability Summary
# ─────────────────────────────────────────────────────────

@app.get("/api/observability")
def observability():
    """Full observability snapshot for dashboards."""
    return {
        "status": system_status(),
        "positions": live_positions(),
        "trades_summary": trade_history(limit=5),
        "health": data_health(),
        "budget": budget_cost(),
        "timestamp": _now().isoformat(),
    }


# ─────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────

@app.get("/api/ping")
def ping():
    return {"pong": True, "time": _now().isoformat(), "version": "3.0"}


if __name__ == "__main__":
    import uvicorn
    print("Starting Sanad Trader Console API on port 8100...")
    uvicorn.run(app, host="0.0.0.0", port=8100)


# ─────────────────────────────────────────────────────────
# Serve Console Frontend
# ─────────────────────────────────────────────────────────

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

CONSOLE_DIR = BASE_DIR / "console"

if CONSOLE_DIR.exists():
    @app.get("/")
    def serve_console():
        return FileResponse(CONSOLE_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(CONSOLE_DIR)), name="static")
