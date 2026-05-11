# ShortTermMeanReversion Strategy — Implementation Spec
**Version:** 1.0  
**Phase:** 2 (addition to existing backtest harness)  
**For:** Claude Code implementation  
**Date:** 2026-05-08

---

## 1. Context and Codebase

This strategy is added to an existing multi-strategy trading platform. Before writing any code, read these files to understand conventions, interfaces, and patterns:

- `src/strategies/base.py` — Strategy ABC, Target, MarketDataView, StrategyCapabilities
- `src/strategies/momentum_continuation.py` — reference implementation (most complex existing strategy)
- `src/strategies/buy_and_hold.py` — minimal strategy reference
- `src/backtest/config.py` — BacktestConfig, StrategyConfig, _STRATEGY_REGISTRY, UNIVERSE_MAP
- `config/strategies/momentum_continuation.yaml` — parameter file pattern to replicate
- `tests/test_momentum.py` — test pattern to replicate

Follow all conventions established in those files exactly. Do not introduce new patterns unless the spec explicitly requires them.

---

## 2. Strategy Logic

### 2.1 Name and class

```python
# src/strategies/short_term_mean_reversion.py
class ShortTermMeanReversion(Strategy):
    name = "ShortTermMeanReversion"
```

### 2.2 Entry signal

A stock is eligible to buy when **all three** conditions are true on the current `as_of` date:

1. **RSI(2) < entry_rsi_threshold** (default: 10) — deeply oversold on a 2-period RSI
2. **Close > 200-day simple moving average** — stock is in a long-term uptrend; do not buy downtrending stocks
3. **Not already held** — no existing position in this symbol

RSI calculation: standard Wilder RSI using 2 periods. Use closing prices. `lookback_days` must be sufficient to compute both the 200-day MA and the RSI(2) — request `max(200, lookback_days) + 10` days of bars to ensure enough history. Raise `ValueError` at strategy init if `lookback_days < 210`.

### 2.3 Exit signal

Exit a held position when **either** condition is true:

1. **RSI(2) > exit_rsi_threshold** (default: 70) — mean reversion complete
2. **days_held >= time_stop_days** (default: 15) — position has stagnated; cut and move on

Exit takes priority over entry. If a held symbol triggers an exit, generate a sell target (weight 0.0) before evaluating new buys.

### 2.4 Hard stop

If a held position's current price is below `entry_price × (1 - hard_stop_pct)`, generate a sell target with `rationale={"reason": "hard_stop"}`. Hard stop check runs before RSI exit check. Hard stop overrides all other logic for that symbol.

Default `hard_stop_pct`: 0.08 (8%). This is intentionally wide — mean reversion positions that drop further are more oversold, not failed trades. The hard stop is for genuine blow-ups only.

### 2.5 Position sizing

Identical to MomentumContinuation: `risk_per_trade_pct / hard_stop_pct` of NAV per position, capped at `max_position_pct`.

```
position_size = min(risk_per_trade_pct / hard_stop_pct, max_position_pct)
```

With defaults (risk_per_trade_pct=0.01, hard_stop_pct=0.08): `0.01 / 0.08 = 0.125` → 12.5% of NAV per position.

### 2.6 Capacity and constraints

- Maximum concurrent positions: `max_concurrent_positions` (default: 8)
- Maximum total NAV deployed: `max_total_deployment_pct` (default: 1.00)
- Long-only. No short selling, no options, no fractional shares required.
- If more symbols are eligible for entry than open slots allow, rank by RSI(2) ascending (most oversold first) and take the top N that fit within capacity.

### 2.7 generate_targets return contract

Return the **full desired portfolio** as a list of `Target` objects — not just changes. This matches the existing strategy interface contract.

- Held positions that should continue: `Target(symbol, weight=current_weight)` — recalculate weight from current NAV
- Positions to exit: `Target(symbol, weight=0.0, rationale={"reason": exit_reason})`
- New entries: `Target(symbol, weight=position_size)`
- Symbols not mentioned: engine assumes 0 (sell everything not listed)

Exit reasons must be one of: `"hard_stop"`, `"rsi_exit"`, `"time_stop"`. These flow through to the simulation engine's TradeRecord.exit_reason. The engine maps anything not in `{hard_stop, trailing_stop, time_stop}` to `"strategy"` — so `"rsi_exit"` will appear as `"strategy"` in the trade log. That is correct and expected behavior; do not work around it.

### 2.8 position_state usage

The orchestrator passes `position_state: dict[str, dict[str, Any]]` with keys per held symbol:
- `entry_price` — float
- `days_held` — int (incremented once per session by orchestrator)
- `highest_close_since_entry` — float (not used by this strategy)
- `entry_date` — date

The strategy reads `entry_price` for hard stop calculation and `days_held` for time stop. It does not write to position_state — that is the orchestrator's responsibility.

In the backtest engine, position_state is maintained in-memory by the simulation loop using the same keys. The strategy code is identical in both contexts.

---

## 3. Parameters

All parameters live in `config/strategies/short_term_mean_reversion.yaml`. Per-sleeve overrides follow the existing pattern.

| Parameter | Default | Notes |
|-----------|---------|-------|
| `lookback_days` | 210 | Must be >= 210 to support 200-day MA + buffer |
| `entry_rsi_threshold` | 10 | RSI(2) must be below this to enter |
| `exit_rsi_threshold` | 70 | RSI(2) must be above this to exit |
| `hard_stop_pct` | 0.08 | Wide by design — see §2.4 |
| `time_stop_days` | 15 | Calendar trading days held |
| `risk_per_trade_pct` | 0.01 | 1% NAV at risk per trade |
| `max_position_pct` | 0.20 | Cap per position regardless of sizing formula |
| `max_concurrent_positions` | 8 | Max open positions at once |
| `max_total_deployment_pct` | 1.00 | Max fraction of NAV deployed |
| `drawdown_circuit_breaker_pct` | 0.15 | Sleeve-level halt threshold |

---

## 4. Universe

### 4.1 Rationale

The universe is a fixed list of S&P 100 constituents that were large-cap, liquid, and continuously traded throughout the 2018–2026 backtest window. Today's S&P 100 composition is not used directly — it introduces survivorship bias (stocks added recently may have been added *because* they performed well). This list excludes stocks that joined the S&P 100 after 2018 or were removed during the window due to poor performance.

### 4.2 Universe constant

```python
# In src/strategies/short_term_mean_reversion.py
UNIVERSE: list[str] = [
    # Technology
    "AAPL", "MSFT", "GOOGL", "GOOG", "META", "NVDA", "AVGO", "TXN", "QCOM", "IBM",
    "ORCL", "ACN", "CSCO", "INTC", "AMD",
    # Consumer Discretionary
    "AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "TGT", "LOW", "BKNG", "F",
    # Consumer Staples
    "WMT", "PG", "KO", "PEP", "COST", "CL", "MO", "PM", "EL",
    # Healthcare
    "JNJ", "UNH", "PFE", "MRK", "ABBV", "TMO", "ABT", "MDT", "BMY", "AMGN",
    "GILD", "CVS",
    # Financials
    "BRK-B", "JPM", "BAC", "WFC", "GS", "MS", "BLK", "AXP", "USB", "C",
    "MMC", "CB",
    # Industrials
    "HON", "UPS", "BA", "CAT", "DE", "MMM", "GE", "LMT", "RTX", "FDX",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG",
    # Materials / Utilities / Real Estate
    "LIN", "APD", "NEE", "DUK", "SO", "AMT", "PLD",
    # Communication Services
    "VZ", "T", "DIS", "CMCSA", "NFLX",
]
```

**Notes on inclusions/exclusions:**
- GOOGL and GOOG both included — they trade independently and both have full history
- BRK-B used (not BRK-A — BRK-A price makes position sizing meaningless at small NAV)
- TSLA included despite high volatility — it was large-cap and liquid throughout the window; the 200-day MA filter will naturally reduce exposure during its worst drawdown periods
- INTC included despite poor recent performance — it was S&P 100 throughout the window; survivorship bias cut goes both ways
- Excluded: stocks that went public or joined S&P 100 after 2020 (e.g., newer additions), stocks with data gaps in yfinance for this period

Total: 88 symbols.

### 4.3 Registry entries

Add to `_STRATEGY_REGISTRY` in `src/backtest/config.py`:
```python
"ShortTermMeanReversion": ("strategies.short_term_mean_reversion", "ShortTermMeanReversion"),
```

Add to `UNIVERSE_MAP` in `src/backtest/config.py`:
```python
"ShortTermMeanReversion": STMR_UNIVERSE,  # import from strategy module or duplicate inline
```

---

## 5. StrategyCapabilities Declaration

```python
capabilities = StrategyCapabilities(
    asset_classes={"us_equity"},
    requires_fractional=False,
    requires_shorting=False,
    requires_options=False,
)
```

---

## 6. Backtest Configuration

Create `backtest_configs/stmr_2018_baseline.yaml`:

```yaml
strategy:
  class: ShortTermMeanReversion
  params:
    lookback_days: 210
    entry_rsi_threshold: 10
    exit_rsi_threshold: 70
    hard_stop_pct: 0.08
    time_stop_days: 15
    risk_per_trade_pct: 0.01
    max_position_pct: 0.20
    max_concurrent_positions: 8
    max_total_deployment_pct: 1.00
    drawdown_circuit_breaker_pct: 0.15

simulation:
  start_date: "2018-01-01"
  end_date: null          # resolves to latest cache date at runtime
  starting_capital: 10000
  slippage_bps: 5
  cash_annualized_rate: 0.04

output:
  output_dir: "backtest_results/stmr_2018_baseline"
  benchmarks:
    - symbol: SPY
      label: "S&P 500 (SPY)"
    - symbol: QQQ
      label: "Nasdaq 100 (QQQ)"
```

Note: QQQ added as a second benchmark since the universe is tech-heavy. This gives a more honest comparison than SPY alone.

---

## 7. RSI Implementation Note

Do not use `pandas_ta` or `ta-lib` if they are not already project dependencies. Check `pyproject.toml` or `requirements.txt` first. If neither is present, implement RSI(2) directly — it is simple enough:

```python
def _compute_rsi(closes: pd.Series, period: int = 2) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float('inf'))
    return 100 - (100 / (1 + rs))
```

This uses Wilder's smoothing (EWM with alpha=1/period). For period=2 this is standard. Return NaN for rows with insufficient history — the strategy should treat NaN RSI as ineligible for entry.

---

## 8. Test Specification

Create `tests/test_short_term_mean_reversion.py`. Target: 25–30 tests. Follow the mock/fixture pattern in `tests/test_momentum.py` exactly.

### 8.1 RSI computation (5 tests)
1. RSI(2) returns correct values for a known price series (verify against hand-calculated values)
2. RSI(2) returns NaN for first row (insufficient history)
3. RSI below 10 correctly identified as oversold
4. RSI above 70 correctly identified as overbought
5. RSI computation is not affected by the number of rows beyond the minimum (stability test)

### 8.2 Entry signal (5 tests)
6. Symbol above 200-day MA with RSI(2) < 10 → generates buy target
7. Symbol below 200-day MA with RSI(2) < 10 → no buy (trend filter blocks entry)
8. Symbol above 200-day MA with RSI(2) = 15 → no buy (RSI not oversold enough)
9. Symbol already held → no new buy target generated (not double-counted)
10. When more signals than open slots, most oversold (lowest RSI) selected first

### 8.3 Exit signal (5 tests)
11. Held symbol with RSI(2) > 70 → generates sell target with rationale rsi_exit
12. Held symbol with days_held >= time_stop_days → generates sell target with rationale time_stop
13. Held symbol with price below hard stop threshold → generates sell target with rationale hard_stop
14. Hard stop fires even when RSI(2) < 70 (hard stop overrides rsi exit check)
15. Held symbol with RSI(2) = 50, days_held = 5, price above hard stop → continue target (no exit)

### 8.4 Position sizing (4 tests)
16. Default params: position size = 0.01 / 0.08 = 0.125 of NAV
17. Position size capped at max_position_pct when formula exceeds cap
18. Multiple simultaneous entries respect max_total_deployment_pct
19. max_concurrent_positions cap respected when 10+ signals fire simultaneously

### 8.5 Full generate_targets integration (6 tests)
20. Empty portfolio, 3 eligible symbols → 3 buy targets returned
21. Full portfolio (8 positions), 2 new signals → no new buys (at capacity)
22. Portfolio with 1 RSI exit, 1 time stop, 1 continuing position → correct target list
23. Mix of exits and new entries in same cycle: exits processed, freed slots filled with new entries
24. No eligible symbols → empty target list (not an error)
25. All held positions trigger hard stop simultaneously → all sell targets generated

### 8.6 Edge cases (3 tests)
26. Insufficient price history (< 210 days) → symbol skipped gracefully, no crash
27. NaN RSI due to missing price data → symbol treated as ineligible, no crash
28. lookback_days < 210 at strategy init → raises ValueError

---

## 9. What NOT to Build

Do not build:
- Short selling logic (long-only in v1)
- Dynamic universe loading from a database or API
- Any new database tables or migrations (position_state wiring already exists and handles this strategy identically to MomentumContinuation)
- New CLI commands (existing `bot-ctl backtest run` and `refresh-data` handle this strategy)
- Modifications to the simulation engine, reporting layer, or orchestrator

The strategy plugs into the existing architecture via the Strategy ABC. No engine changes required.

---

## 10. Acceptance Criteria

Implementation is complete when:

1. `bot-ctl backtest refresh-data --all` successfully caches all 88 universe symbols (some may already be cached if they overlap with MomentumContinuation universe; `--force` not required)
2. `bot-ctl backtest run backtest_configs/stmr_2018_baseline.yaml` produces a `report.html` without errors
3. All existing 270 tests continue to pass
4. All new STMR tests pass (target: 25+ tests)
5. The strategy can be instantiated and added as a sleeve via `bot-ctl sleeves add` without errors

Do not tune parameters before the baseline backtest has been run and reviewed.