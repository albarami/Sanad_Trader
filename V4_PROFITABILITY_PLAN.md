# V4 Profitability Plan — Reward + Fees/Slippage + Walk-Forward Evaluation

**Status**: AWAITING APPROVAL  
**Branch**: `main`  
**Base commit**: `c2a9c32`  
**Author**: Sanad Trader v3.1  
**Date**: 2026-02-23  

---

## Overview

Two ROI items that transform the system from "learning exists" to "learning is measurably improving":

| ROI | What | Why |
|-----|------|-----|
| **#1** | Reward + Fees/Slippage Storage | Paper trades must punish bad fills; learning must use stored reward, not recompute |
| **#2** | Walk-Forward Evaluation + Promotion | Prove improvement before deploying; automated policy promotion |

---

## Implementation Order

| Ticket | Description | Depends On | Tests | Commit Strategy |
|--------|-------------|------------|-------|-----------------|
| **T1** | Schema changes (fills table + positions columns + eval tables + meta table) | None | 1 test (schema existence) | Single commit |
| **T2** | state_store API (record_fill, compute_reward, helpers, meta functions) | T1 | 2 tests (fill compute, reward clamp) | Single commit |
| **T3** | Extend open_position() with entry costs + fill | T2 | 1 test (entry fill + cost fields) | Single commit |
| **T4** | Extend close_position() with gross/net/fees/reward + exit fill | T3 | 1 test (close computes gross/net/fees/reward) | Single commit |
| **T5** | Wire entry costs into fast_decision_engine (BUY side) | T4 | Regression: ticket 8 tests still pass | Single commit |
| **T6** | Wire exit costs into position_monitor (SELL side) | T4 | Regression: gate02 + close metadata tests still pass | Single commit |
| **T7** | Learning loop uses stored reward_bin | T4 | 1 test (contract: inconsistent pnl vs reward_bin) | Single commit |
| **T8** | Trading summary displays net + fees | T4 | Manual verification | Single commit |
| **T9** | eval_walkforward.py (full script) | T1, T2 | 4 tests + 1 micro-test (net vs gross) | Single commit |
| **T10** | Wire eval cron + policy_engine loads active policy | T9 | Regression: all existing tests pass | Single commit |

**Total new tests**: 11 (6 in test_reward_fees_and_fills.py, 5 in test_eval_walkforward.py)  
**Total test suite after**: 51+ tests across 9 test files  

---

## Ticket T1: Schema Changes

### What
Add all new tables and columns to `state_store.py:init_db()`.

### Schema: fills table
```sql
CREATE TABLE IF NOT EXISTS fills (
    fill_id         TEXT PRIMARY KEY,
    position_id     TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    venue           TEXT NOT NULL,           -- 'paper', 'binance', 'dex'
    expected_price  REAL,                    -- mid/mark price at decision time
    exec_price      REAL NOT NULL,           -- actually applied price (after slippage)
    qty_base        REAL NOT NULL,           -- size_usd / exec_entry_price
    notional_usd    REAL NOT NULL,           -- qty_base * exec_price
    fee_usd         REAL NOT NULL DEFAULT 0,
    fee_bps         REAL NOT NULL DEFAULT 0,
    slippage_bps    REAL NOT NULL DEFAULT 0,
    tx_hash         TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_position_id ON fills(position_id);
CREATE INDEX IF NOT EXISTS idx_fills_created_at ON fills(created_at);
```

### Schema: positions table extensions (idempotent via _add_column_if_missing)
```python
# Entry cost fields
_add_column_if_missing(conn, "positions", "entry_fill_id", "TEXT")
_add_column_if_missing(conn, "positions", "exit_fill_id", "TEXT")
_add_column_if_missing(conn, "positions", "entry_expected_price", "REAL")
_add_column_if_missing(conn, "positions", "entry_slippage_bps", "REAL")
_add_column_if_missing(conn, "positions", "entry_fee_usd", "REAL")
_add_column_if_missing(conn, "positions", "entry_fee_bps", "REAL")

# Exit cost fields
_add_column_if_missing(conn, "positions", "exit_expected_price", "REAL")
_add_column_if_missing(conn, "positions", "exit_slippage_bps", "REAL")
_add_column_if_missing(conn, "positions", "exit_fee_usd", "REAL")
_add_column_if_missing(conn, "positions", "exit_fee_bps", "REAL")

# Aggregated costs + gross PnL
_add_column_if_missing(conn, "positions", "fees_usd_total", "REAL")
_add_column_if_missing(conn, "positions", "pnl_gross_usd", "REAL")
_add_column_if_missing(conn, "positions", "pnl_gross_pct", "REAL")

# Reward fields
_add_column_if_missing(conn, "positions", "reward_bin", "INTEGER")
_add_column_if_missing(conn, "positions", "reward_real", "REAL")
_add_column_if_missing(conn, "positions", "reward_version", "TEXT")

# Eval attribution
_add_column_if_missing(conn, "positions", "policy_version", "TEXT")
_add_column_if_missing(conn, "positions", "decision_id", "TEXT")
```

**Contract**: Existing `pnl_usd` / `pnl_pct` become NET going forward. Gross stored separately in `pnl_gross_*`.

### Schema: performance indexes
```sql
CREATE INDEX IF NOT EXISTS idx_positions_status_closed_at ON positions(status, closed_at);
CREATE INDEX IF NOT EXISTS idx_positions_policy_closed_at ON positions(policy_version, closed_at);
CREATE INDEX IF NOT EXISTS idx_positions_strategy_closed_at ON positions(strategy_id, closed_at);
```

### Schema: eval tables
```sql
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
```

### Test
`test_schema_has_fills_and_columns()` — verifies fills table exists, all new positions columns exist, meta/policy_configs/eval_walkforward_runs tables exist.

### Commit
```
T1: Schema — fills table, positions cost/reward columns, eval tables

fills table: per-leg execution data (BUY/SELL)
positions: entry/exit costs, gross/net PnL, reward_bin/reward_real
policy_configs + meta + eval_walkforward_runs for walk-forward eval
All migrations idempotent via _add_column_if_missing + IF NOT EXISTS
```

---

## Ticket T2: state_store API — Helpers + record_fill + compute_reward + meta

### What
Add to `state_store.py`:

#### Fee/slippage helpers
```python
def _fee_usd(notional_usd: float, fee_bps: float) -> float:
    return float(notional_usd) * (float(fee_bps) / 10000.0)

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
```

#### compute_reward()
```python
def compute_reward(net_pnl_usd: float, net_pnl_pct: float, version: str = "v1"):
    """
    v1: reward_bin = 1 if net_pnl_usd > 0 else 0
        reward_real = clamp(net_pnl_pct, -1.0, +1.0)
    Returns: (reward_bin, reward_real, version)
    """
    reward_bin = 1 if (net_pnl_usd or 0.0) > 0 else 0
    reward_real = _clamp(float(net_pnl_pct or 0.0), -1.0, 1.0)
    return reward_bin, reward_real, version
```

#### record_fill()
```python
def record_fill(
    position_id: str, side: str, venue: str,
    expected_price: float | None, exec_price: float, qty_base: float,
    fee_bps: float = 0.0, fee_usd: float | None = None,
    slippage_bps: float = 0.0, tx_hash: str | None = None,
    created_at: str | None = None, db_path=None
) -> str:
    """
    Inserts fills row. Returns fill_id.
    notional_usd = qty_base * exec_price
    fee_usd = computed from notional * fee_bps if None
    fill_id = deterministic hash (sha256 of position_id + side + created_at)
    """
```

#### Meta functions
```python
def get_meta(key: str, default: str | None = None, db_path=None) -> str | None: ...
def set_meta(key: str, value: str, db_path=None): ...
def get_active_policy_version(db_path=None) -> str: ...
def set_active_policy_version(new_version: str, reason: str, eval_id: str | None = None, db_path=None): ...
def get_policy_config(policy_version: str | None = None, db_path=None) -> dict: ...
```

### Tests
- `test_record_fill_computes_notional_and_fee()` — verifies notional_usd = qty * price, fee_usd computed from bps when None
- `test_reward_real_is_clamped()` — verifies +400% pnl clamps reward_real to +1.0

### Commit
```
T2: state_store API — record_fill, compute_reward, meta functions

record_fill(): inserts fills row with computed notional + fee
compute_reward(): v1 binary + clamped real reward
get/set_meta(), get/set_active_policy_version(), get_policy_config()
_fee_usd() and _clamp() helpers
```

---

## Ticket T3: Extend open_position() with Entry Costs + Fill

### What
Add optional parameters to existing `open_position()`:
- `decision_id`, `policy_version`
- `entry_expected_price`, `entry_slippage_bps`, `entry_fee_bps`, `entry_fee_usd`
- `venue`, `tx_hash`

#### Computation on open:
```
qty_base = size_usd / entry_price  (guard entry_price > 0)
notional_usd = qty_base * entry_price  (≈ size_usd)
entry_fee_usd = provided or _fee_usd(notional_usd, entry_fee_bps)
fill_id = record_fill(side='BUY', ...)
```

Write to positions: `entry_fill_id`, `entry_expected_price`, `entry_slippage_bps`, `entry_fee_usd`, `entry_fee_bps`, `decision_id`, `policy_version`.

**Backward compatible**: all new args optional with defaults (0.0 for bps, None for IDs).

### Test
`test_open_position_writes_entry_fill_and_cost_fields()` — verifies entry_fill_id set, entry_fee_usd computed (200 * 10bps = 0.2), BUY fill row created with correct qty_base.

### Commit
```
T3: open_position() — entry costs, fill linkage, policy attribution

New optional args: entry_expected_price, entry_slippage_bps, entry_fee_bps,
entry_fee_usd, venue, tx_hash, decision_id, policy_version
Creates BUY fill row and links via entry_fill_id
Backward compatible: all new args optional
```

---

## Ticket T4: Extend close_position() with Gross/Net/Fees/Reward + Exit Fill

### What
Add optional parameters to existing close_position():
- `exit_expected_price`, `exit_slippage_bps`, `exit_fee_bps`, `exit_fee_usd`
- `venue`, `tx_hash`

#### Computation on close (exact contract):
```
# Load from existing position row:
entry_price, size_usd, entry_fee_usd

qty_base = size_usd / entry_price
gross_exit_notional = qty_base * close_price
pnl_gross_usd = gross_exit_notional - size_usd
pnl_gross_pct = pnl_gross_usd / size_usd

exit_fee_usd = provided or _fee_usd(gross_exit_notional, exit_fee_bps)
fees_usd_total = (entry_fee_usd or 0) + (exit_fee_usd or 0)

pnl_net_usd = pnl_gross_usd - fees_usd_total
pnl_net_pct = pnl_net_usd / size_usd

(reward_bin, reward_real, reward_version) = compute_reward(pnl_net_usd, pnl_net_pct, "v1")

exit_fill_id = record_fill(side='SELL', ...)
```

**Atomic single UPDATE** sets: `status='CLOSED'`, `closed_at`, `close_reason`, `close_price`, `exit_fill_id`, `exit_expected_price`, `exit_slippage_bps`, `exit_fee_usd`, `exit_fee_bps`, `fees_usd_total`, `pnl_gross_usd`, `pnl_gross_pct`, `pnl_usd` (NET), `pnl_pct` (NET), `reward_bin`, `reward_real`, `reward_version`.

**Backward compatible**: all new args optional.

### Test
`test_close_position_computes_gross_net_fees_reward_and_exit_fill()`:
- Open at 2.00, size $200, entry_fee_bps=10
- Close at 2.20, exit_fee_bps=10
- Asserts:
  - pnl_gross_usd = 20.0
  - pnl_gross_pct = 0.10
  - entry_fee = 0.20, exit_fee = 0.22, fees_total = 0.42
  - pnl_usd (net) = 19.58
  - pnl_pct (net) = 19.58/200 = 0.0979
  - reward_bin = 1 (net positive)
  - reward_real = 0.0979
  - exit fill row exists with SELL side, notional=220, fee=0.22

### Commit
```
T4: close_position() — gross/net PnL, total fees, reward, exit fill

Computes: gross PnL, exit fees, total fees, net PnL, reward (v1)
Creates SELL fill row and links via exit_fill_id
Atomic single UPDATE — all fields in one statement
pnl_usd/pnl_pct are now NET (fees deducted)
pnl_gross_usd/pnl_gross_pct stored separately
Backward compatible: all new args optional
```

---

## Ticket T5: Wire Entry Costs into fast_decision_engine (BUY Side)

### What
In `fast_decision_engine.py`, where `state_store.open_position()` is called on EXECUTE:

1. Read cost config from `thresholds.yaml`:
   ```yaml
   execution_costs:
     paper_fee_bps: 10        # 0.10% per side
     paper_slippage_bps: 5    # 0.05% adverse
     binance_taker_fee_bps: 10
   ```

2. Pass to open_position():
   ```python
   state_store.open_position(
       ...,
       entry_expected_price=mid_price,       # signal["price"]
       entry_slippage_bps=paper_slippage_bps,
       entry_fee_bps=paper_fee_bps,
       venue="paper",
       decision_id=decision_id,
       policy_version=active_policy_version,
   )
   ```

3. The `entry_price` (exec price) should be: `mid_price * (1 + slippage_bps/10000)` for BUY (adverse = pay more).

### Test
Regression: all 7 tests in `test_ticket8_e2e_router_flow.py` must still pass.

### Commit
```
T5: Wire entry costs into hot path (BUY side)

fast_decision_engine passes entry_expected_price, entry_slippage_bps,
entry_fee_bps, venue, decision_id, policy_version to open_position()
Config: execution_costs in thresholds.yaml
Paper BUY exec_price = mid * (1 + slippage_bps/10000)
```

---

## Ticket T6: Wire Exit Costs into position_monitor (SELL Side)

### What
In `position_monitor.py`, at each close trigger (stop-loss, take-profit, max-hold, judge reject, emergency):

1. Read same cost config from `thresholds.yaml`.
2. Compute exec exit price: `mid_exit_price * (1 - slippage_bps/10000)` for SELL (adverse = receive less).
3. Call:
   ```python
   state_store.close_position(
       position_id=pid,
       close_reason=reason,
       close_price=exec_exit_price,
       exit_expected_price=mid_exit_price,
       exit_slippage_bps=paper_slippage_bps,
       exit_fee_bps=paper_fee_bps,
       venue="paper",
   )
   ```

4. Remove any manual PnL computation that exists in position_monitor — `close_position()` now handles all math.

### Test
Regression: all 7 tests in `test_gate02_and_close_metadata.py` must still pass.

### Commit
```
T6: Wire exit costs into position_monitor (SELL side)

position_monitor passes exit_expected_price, exit_slippage_bps,
exit_fee_bps, venue to close_position()
Paper SELL exec_price = mid * (1 - slippage_bps/10000)
Removed manual PnL computation — close_position() is single source
```

---

## Ticket T7: Learning Loop Uses Stored reward_bin

### What
In `learning_loop.py`, change outcome extraction:

**Before**: `outcome = 1 if pnl_usd > 0 else 0`  
**After**: `outcome = reward_bin if reward_bin is not None else (1 if pnl_usd > 0 else 0)`

Query must include: `reward_bin, reward_real, reward_version, pnl_usd, fees_usd_total` when selecting closed positions.

### Test
`test_learning_loop_uses_reward_bin_not_pnl_sign()` — CONTRACT test:
- Insert position with `pnl_usd = -1.0` but `reward_bin = 1` (intentional inconsistency)
- Run learning_loop as subprocess
- Assert: alpha incremented (win recorded), beta unchanged
- Proves learning_loop uses reward_bin, not pnl_usd sign

### Commit
```
T7: Learning loop uses stored reward_bin (not pnl recompute)

Reads reward_bin from positions (fallback: pnl_usd > 0 if NULL)
CONTRACT: reward computation is close_position()'s job, not learning_loop's
```

---

## Ticket T8: Trading Summary Displays Net + Fees

### What
In `trading_summary.py`, closed trades list shows:
- `pnl_usd` (net, after fees)
- `fees_usd_total` (if non-zero)
- `pnl_gross_usd` (for comparison)

Format: `🔴 TESTTOKEN: -16.3% net ($-32.72, fees $0.42)`

### Test
Manual verification via Telegram.

### Commit
```
T8: Trading summary shows net PnL + fees breakdown
```

---

## Ticket T9: eval_walkforward.py

### What
New script: `scripts/eval_walkforward.py`

#### CLI contract
```
--horizon-days     default 30
--train-days       default 14
--test-days        default 2
--step-days        default 2
--min-test-trades  default 20
--max-dd           default 0.15
--min-median-improve-usd  default 10.0
--promote          flag (default false)
--notify           flag (default true)
--now-iso          ISO timestamp (for deterministic tests)
```

#### Data query
```sql
SELECT position_id, policy_version, strategy_id, source_primary,
       created_at, closed_at, size_usd,
       pnl_usd, pnl_pct, pnl_gross_usd, pnl_gross_pct,
       fees_usd_total, close_reason
FROM positions
WHERE status='CLOSED' AND closed_at IS NOT NULL AND closed_at >= ?
ORDER BY closed_at ASC;
```

**CRITICAL**: Uses `pnl_usd` (NET, after fees). NOT gross.

#### Walk-forward split
```
start = now - timedelta(days=horizon_days)
for anchor t from (start + train_days), step by step_days:
    train = [t - train_days, t)
    test  = [t, t + test_days)
    stop when t + test_days > now
```

#### Metrics function (exact)
```python
def compute_metrics(trades: list[dict]) -> dict:
    # n, net_pnl_usd, gross_pnl_usd, fees_usd
    # win_rate, profit_factor
    # max_drawdown_pct (from equity curve using per-trade returns)
    # sharpe_trade (annualized-ish: avg_r / std * sqrt(n))
    # avg_return
```

#### Candidate evaluation
- Group test trades by `policy_version`
- Per fold: compute metrics per candidate
- Aggregate across folds: median of each metric

#### Promotion decision (only if --promote)
```
active = get_active_policy_version()
winner = candidate with highest median_net_pnl_usd
CONSTRAINTS:
  - total_test_trades >= min_test_trades
  - median_max_dd <= max_dd
DECISION:
  - winner == active → HOLD
  - winner beats active by >= min_median_improve_usd → PROMOTE
  - else → HOLD
PERSIST:
  - Insert eval_walkforward_runs row (always)
  - If PROMOTE: set_active_policy_version(winner)
```

#### Output
- Print concise table to stdout
- If --notify: send Telegram summary via notifier

### Tests (5 total in test_eval_walkforward.py)

1. **`test_eval_inserts_run_row_and_no_promote_by_default`**
   - Create trades for pvA and pvB, run without --promote
   - Assert: eval_walkforward_runs row exists, active policy unchanged
   - Assert: results_json has keys: params, folds, aggregate, winner, active_policy

2. **`test_promotes_when_candidate_beats_active_and_constraints_met`**
   - pvA: 8 trades at pnl=0, pvB: 8 trades at pnl=+15
   - Run with --promote, min_test_trades=6, min_median_improve=10
   - Assert: active_policy_version changed to pvB, decision=PROMOTE

3. **`test_hold_when_insufficient_test_trades`**
   - Only 2 trades per candidate, min_test_trades=6
   - Assert: active stays pvA, decision=HOLD

4. **`test_hold_when_candidate_maxdd_exceeds_threshold`**
   - pvB has volatile pattern (+50/-40 repeated), max_dd=0.15
   - Assert: active stays pvA, decision=HOLD

5. **`test_eval_uses_net_pnl_not_gross`** (micro-test)
   - Insert trades where gross is positive but net is negative (high fees)
   - Assert: candidate with positive gross but negative net does NOT get promoted

### Commit
```
T9: eval_walkforward.py — walk-forward evaluation + automated promotion

CLI: --horizon/train/test/step-days, --min-test-trades, --max-dd,
     --min-median-improve-usd, --promote, --notify, --now-iso
Metrics: net PnL, win rate, profit factor, max drawdown, Sharpe
Walk-forward: rolling train/test splits, per-candidate aggregation
Promotion: PROMOTE only if constraints met + meaningful improvement
Audit: every run persisted to eval_walkforward_runs
5 deterministic tests
```

---

## Ticket T10: Wire Eval Cron + Policy Engine Loads Active Policy

### What

1. **Cron**: Run eval_walkforward.py every 6 hours
   ```
   0 */6 * * * cd /trading && python3 scripts/eval_walkforward.py --promote --notify
   ```

2. **policy_engine.py**: At config load:
   ```python
   active = state_store.get_active_policy_version()
   config = state_store.get_policy_config(active)
   # Use config for gates
   # If config missing → fail closed (BLOCK)
   ```

3. **fast_decision_engine.py**: Same pattern — load active policy version, pass as `policy_version` to open_position().

### Test
Regression: all existing tests pass (40+ across 7 files).

### Commit
```
T10: Wire eval cron (6h) + policy_engine loads active policy from SQLite

policy_engine: loads config via get_active_policy_version() → get_policy_config()
fast_decision_engine: stamps policy_version on every position
Cron: eval_walkforward --promote --notify every 6 hours
Fail closed: missing policy config → BLOCK
```

---

## Config Addition (thresholds.yaml)

```yaml
execution_costs:
  paper_fee_bps: 10           # 0.10% per side (realistic taker fee)
  paper_slippage_bps: 5       # 0.05% adverse slippage
  binance_taker_fee_bps: 10   # for live mode later
  dex_fee_bps: 30             # DEX swap fee estimate
  dex_slippage_bps: 50        # DEX slippage estimate
```

---

## Test Files Summary

### New: `scripts/test_reward_fees_and_fills.py` (6 tests)
1. `test_schema_has_fills_and_columns` — schema existence
2. `test_record_fill_computes_notional_and_fee` — fill math
3. `test_open_position_writes_entry_fill_and_cost_fields` — entry side
4. `test_close_position_computes_gross_net_fees_reward_and_exit_fill` — close side (the big one)
5. `test_reward_real_is_clamped` — +400% clamps to +1.0
6. `test_learning_loop_uses_reward_bin_not_pnl_sign` — CONTRACT test

### New: `scripts/test_eval_walkforward.py` (5 tests)
1. `test_eval_inserts_run_row_and_no_promote_by_default` — audit trail
2. `test_promotes_when_candidate_beats_active_and_constraints_met` — happy path
3. `test_hold_when_insufficient_test_trades` — guard
4. `test_hold_when_candidate_maxdd_exceeds_threshold` — guard
5. `test_eval_uses_net_pnl_not_gross` — micro-test proving NET not GROSS

### Existing (must all still pass)
- `test_gate02_and_close_metadata` (7)
- `test_ticket8_e2e_router_flow` (7)
- `test_ticket10_sqlite_stats_hotpath` (5)
- `test_ticket12_unified_state` (7)
- `test_learning_loop` (9)
- `test_ticket6_learning_wiring` (5)
- `test_no_direct_json_writes` (126 scripts)

**Total after implementation: 51+ tests across 9 files**

---

## CI Guard Updates

- `test_no_direct_json_writes.py` DB_ALLOWLIST: add `eval_walkforward.py` ONLY if it uses `state_store.get_connection()` (it should, no direct sqlite3.connect needed).
- If eval_walkforward.py uses only state_store functions, no allowlist change needed.

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Breaking existing close_position() callers | All new args optional with defaults |
| Old positions missing cost data | reward_bin/fees_usd_total default NULL; learning loop falls back to pnl_usd > 0 |
| eval_walkforward promotes bad policy | --min-test-trades=20 + --max-dd=0.15 + --min-median-improve=10 |
| Policy config missing → trading halted | fail closed is correct behavior; insert "main" policy config at init |

---

## Approval Checklist

- [ ] Schema design approved
- [ ] Computation contracts approved (gross/net/reward formulas)
- [ ] Test cases approved (11 new tests)
- [ ] Config values approved (paper_fee_bps=10, paper_slippage_bps=5)
- [ ] Commit strategy approved (10 tickets, sequential)
- [ ] Cron schedule approved (eval every 6h)

**Ready to implement on your approval, Salim.**
