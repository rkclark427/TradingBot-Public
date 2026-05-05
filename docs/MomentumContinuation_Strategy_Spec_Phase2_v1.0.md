# MomentumContinuation Strategy Specification

**TradingBot Phase 2 — Strategy Design Document**
**Version 1.0 — May 2026**

---

## 1. Purpose and scope

This document specifies the MomentumContinuation strategy for Phase 2 of the TradingBot platform. It is intended as the authoritative reference for implementation by Claude Code and subsequent review by the user. Implementation that diverges from this spec without explicit decision should be flagged as a deviation, not silently accepted.

The strategy is a long-only, multi-day, breakout-based momentum continuation strategy operating on a fixed universe of 16 ETFs. It is designed to:

- Generate trades regularly enough to provide meaningful platform activity (target velocity: several trades per week in normal markets).
- Match the current macro environment, characterized by a persistent equity uptrend punctuated by sharp volatility events ("TACO" pattern).
- Exploit the user's automation edge — systematic scanning of a multi-asset universe — without competing on speed against professional algorithmic traders.
- Bound per-trade risk through mechanical stop-losses and position sizing rules, with a strategy-level drawdown circuit breaker.
- Run as a paper sleeve initially, with potential graduation to a live sleeve after demonstrated paper performance.

Out of scope for this spec:

- Backtest harness implementation (covered by separate BacktestHarness specification).
- Environment hardening (separate Phase 2 task).
- Options-based variants of momentum strategies (deferred to a later phase).
- Short selling (explicitly excluded by user preference).

---

## 2. Strategy overview

### 2.1. Edge being exploited

The strategy exploits the well-documented short-to-medium-term momentum effect: assets that have recently outperformed tend to continue outperforming over the next several days to weeks. The mechanical implementation uses a 20-day high breakout signal as the entry trigger, which is a classic Donchian-style formulation.

**Honest assessment:** The empirical edge of simple breakout strategies on liquid US ETFs is modest. Over long historical periods, such strategies have produced low-to-mid single-digit annual returns with significant drawdowns. The strategy is unlikely to produce dramatic outperformance vs. a buy-and-hold benchmark; it is expected to produce a different return stream with periods of outperformance and underperformance. Its primary value is as a learning vehicle and as a non-correlated return stream alongside passive equity exposure.

### 2.2. Return profile expectation

This strategy has a momentum-style return profile, which is deliberately accepted by the user despite an initial preference for higher win rates:

- Win rate: typically 35-45%. Most trades will lose money or break even.
- Average winner significantly larger than average loser. The strategy makes money by letting winners run and cutting losers quickly.
- Returns concentrated in a few large winning trades; most trades contribute marginally to overall P&L.
- Strategy will whipsaw in choppy markets and thrive in trending markets.
- Drawdown periods (consecutive months of negative returns) are expected and not necessarily indicative of strategy failure.

### 2.3. How the strategy fits the platform

The strategy is implemented as a Strategy class conforming to the Phase 1 Strategy ABC. It produces target weights for instruments in its universe; the platform's portfolio sizer translates these into share counts, the execution router submits orders, and the sleeve manager attributes fills back to the strategy's positions.

The strategy is stateless across cycles in the sense that it does not maintain its own internal database — all positions, NAV, and order state are tracked by the sleeve infrastructure. The strategy's responsibility on each cycle is to inspect current market data and current positions and produce a target weights vector.

---

## 3. Universe

The strategy trades a fixed universe of 16 ETFs:

### 3.1. Sector ETFs (11)

| Symbol | Name                                          | Sector                 |
|--------|-----------------------------------------------|------------------------|
| XLE    | Energy Select Sector SPDR                     | Energy                 |
| XLK    | Technology Select Sector SPDR                 | Technology             |
| XLF    | Financial Select Sector SPDR                  | Financials             |
| XLV    | Health Care Select Sector SPDR                | Health Care            |
| XLI    | Industrial Select Sector SPDR                 | Industrials            |
| XLY    | Consumer Discretionary Select Sector SPDR     | Consumer Discretionary |
| XLP    | Consumer Staples Select Sector SPDR           | Consumer Staples       |
| XLU    | Utilities Select Sector SPDR                  | Utilities              |
| XLB    | Materials Select Sector SPDR                  | Materials              |
| XLRE   | Real Estate Select Sector SPDR                | Real Estate            |
| XLC    | Communication Services Select Sector SPDR     | Communications         |

### 3.2. Broad market ETFs (3)

| Symbol | Name                          | Coverage      |
|--------|-------------------------------|---------------|
| SPY    | SPDR S&P 500 ETF Trust        | Large-cap US  |
| QQQ    | Invesco QQQ Trust             | NASDAQ-100    |
| IWM    | iShares Russell 2000 ETF      | Small-cap US  |

### 3.3. International ETFs (2)

| Symbol | Name                                  | Coverage                  |
|--------|---------------------------------------|---------------------------|
| EFA    | iShares MSCI EAFE ETF                 | Developed markets ex-US   |
| EEM    | iShares MSCI Emerging Markets ETF     | Emerging markets          |

### 3.4. Universe edge cases

- Note that XLRE was created in 2015 and XLC in 2018; backtests prior to those dates must handle missing data appropriately. See BacktestHarness spec for details.
- All 16 ETFs are highly liquid (typical penny-wide spreads on the major names) and support fractional shares on Alpaca.
- If any of these ETFs is delisted, restructured, or otherwise becomes untradeable, the strategy should log a warning and skip that symbol for the cycle. The risk layer's tradability check (Phase 1) handles this.
- The universe is fixed in code, not in config. Adding or removing ETFs from the universe is a code change with a migration path, not a runtime parameter. This is deliberate to prevent silent universe drift.

---

## 4. Signal definition

### 4.1. Entry signal

The entry signal is a 20-day high breakout on closing prices. Specifically, an entry signal triggers for a symbol on day T if:

```
close[T] > max(close[T-20], close[T-19], ..., close[T-1])
```

In words: today's close is strictly greater than the highest closing price over the previous 20 trading days. The current day's close is excluded from the lookback window — we are testing whether today closed at a new high relative to the prior 20-day window.

### 4.2. Why closing prices, not intraday highs

Two reasons:

- Closing prices are cleaner and more reliable signals than intraday highs. Intraday highs are vulnerable to brief spikes and aberrant prints that don't reflect sustained interest.
- The strategy's cycle runs at end-of-day, executing at next open. Intraday-based signals would require intraday execution, which adds operational complexity without corresponding edge.

### 4.3. Why 20 days specifically

20 trading days is approximately one calendar month and is a standard formation window in the trend-following literature. Shorter windows (5-10 days) produce more signals but more whipsaws; longer windows (60+ days) produce stronger signals but fewer trades and slower response. 20 days is a reasonable balance for the user's stated velocity preference.

This parameter SHOULD be exposed as a strategy configuration value (default: 20) so it can be varied during backtesting and parameter sensitivity analysis.

### 4.4. Exit signals

The strategy has three exit conditions. The first one to trigger results in a sell order at next available execution:

#### 4.4.1. Hard stop loss

If `close[T] < entry_price * (1 - 0.04)`, exit at next open. The hard stop is fixed at 4% below the entry price and does not trail.

**Rationale:** A breakout that fails by 4% has demonstrated that the breakout was false. The 4% threshold is wide enough to absorb normal post-breakout volatility on liquid ETFs (which typically have daily volatilities of 1-2%) but tight enough to cap downside per trade.

#### 4.4.2. Trailing stop

Once the position has shown a profit, a trailing stop activates. Specifically:

```
trailing_stop_level = max(close[T-1], ..., close[entry_day]) * (1 - 0.05)
```

If `close[T] < trailing_stop_level`, exit at next open.

The trailing stop ratchets up as the highest close since entry rises, but never moves down. Once the trailing stop level rises above the entry price, it effectively replaces the hard stop loss; the system exits on whichever stop is hit first.

**Implementation note:** track `highest_close_since_entry` as a per-position state variable, updated each cycle. The trailing stop level is derived from this value.

#### 4.4.3. Time stop

If a position has been held for 10 trading days without exiting via stop, force exit at next open on day 11.

**Rationale:** A momentum trade that has not produced sufficient profit to lift the trailing stop above the entry price within 10 trading days is unlikely to do so subsequently. Forcing the exit frees capital for new setups and prevents the strategy from holding stale positions indefinitely.

### 4.5. Signal precedence and edge cases

- If both an entry signal and an existing position exist for the same symbol on the same cycle, no action is taken (the position is already long; we do not pyramid).
- If multiple exit conditions trigger on the same cycle, the order does not matter — the result is the same exit order.
- If an entry signal fires for a symbol on the same day a position in that symbol was just exited, the new entry is allowed (no "cooling off" period). This is intentional; the strategy has no view on whether re-entry is sensible beyond the signal itself.
- If the closing-price data is missing or stale (last close older than 1 trading day), the strategy SHOULD skip that symbol with a warning log entry and not generate signals based on stale data.

---

## 5. Position sizing

### 5.1. Per-trade risk

Each entry sizes the position so that a stop-out at the hard stop level results in a loss of approximately 1% of sleeve NAV at the time of entry.

```
risk_per_trade_dollars = nav_at_entry * 0.01
position_notional = risk_per_trade_dollars / 0.04 = nav_at_entry * 0.25
```

In words: each position is approximately 25% of NAV by notional value. With a 4% hard stop, a stop-out loses approximately 1% of NAV (4% loss × 25% position size = 1% NAV impact, ignoring slippage).

### 5.2. Position concentration cap

Hard limit: no single position may exceed 25% of NAV at entry. This matches the natural sizing produced by the formula above; it serves as a safety check in case sizing logic produces a larger position due to a bug.

### 5.3. Maximum concurrent positions

The strategy will hold at most 5 positions simultaneously. If a 6th entry signal fires while 5 positions are already held, the signal is ignored for that cycle. The signal is not queued; if the breakout is still valid the next day, the entry can fire then.

**Implementation note:** 5 × 25% = 125% of NAV in notional, which would exceed available capital. The total deployment cap (Section 5.4) enforces the actual constraint; the 5-position max is a soft limit used in conjunction with it.

### 5.4. Total capital deployment cap

Total notional value of all open positions must not exceed 100% of NAV. If a new entry would push deployed capital over 100% of NAV, the entry is refused for that cycle.

In practice this means the strategy will typically hold 3-4 concurrent positions at full sizing rather than 5, since reaching 5 would require either smaller position sizes or partially-deployed capital. This is acceptable and intentional.

### 5.5. Order rounding

Position sizes computed as fractional shares should be passed through to the platform's portfolio sizer (Phase 1) unchanged. The portfolio sizer applies its existing ROUND_DOWN logic and minimum-notional checks. The strategy itself does not perform rounding.

---

## 6. Drawdown circuit breaker

In addition to per-trade stops, the strategy implements a sleeve-level drawdown circuit breaker that halts all new entries if cumulative losses become severe.

### 6.1. Trigger condition

The circuit breaker triggers when:

```
current_nav < 0.85 * trailing_high_nav
```

Where `trailing_high_nav` is the maximum NAV value reached by this sleeve since inception. The reference is the equity high water mark, not the initial capital — a sleeve that has gained money may experience a 15% drawdown from its peak before triggering.

### 6.2. Behavior when triggered

- No new entry orders are generated. The strategy will continue to evaluate exit signals on existing positions and submit exit orders normally.
- The sleeve enters a HALTED state. This is recorded in the sleeve manager.
- A WARN-level log entry is generated identifying the sleeve, the trigger NAV, the trailing high NAV, and the drawdown percentage.
- Manual intervention via CLI is required to resume the sleeve. Specifically, a new CLI command (e.g., `bot-ctl sleeve resume <sleeve_id>`) is required to clear the HALTED state.

### 6.3. Why 85% (not 80%)

The trigger threshold is configurable but defaults to 85%. The default is deliberately conservative for a paper strategy where we want to be alerted to extended losing streaks early. For a live sleeve with more capital, the user may choose to widen the threshold (e.g., to 75%) to reduce the frequency of false triggers. This is a per-sleeve configuration.

---

## 7. Cycle and execution

### 7.1. Cycle frequency

The strategy runs once per trading day, after market close. It uses end-of-day data (closing prices for the just-completed session) and produces orders for execution at the next session's open.

### 7.2. Cycle workflow

1. Trigger: orchestrator runs the strategy's cycle method after market close (e.g., 4:30 PM ET, configurable).
2. Fetch closing prices for all 16 ETFs in the universe. If any are missing or stale, log a warning and skip those symbols for this cycle.
3. Compute the 20-day rolling high (excluding today) for each symbol.
4. Identify entry signals: symbols where today's close > 20-day prior high AND no current position in that symbol.
5. Identify exit signals on existing positions: hard stop, trailing stop, or time stop triggered.
6. Check drawdown circuit breaker. If triggered, suppress all entry signals; allow exit signals to proceed.
7. Apply position sizing rules: compute target notional for each entry, check concentration cap, check max concurrent positions, check total deployment cap. Drop any entries that fail these checks.
8. Produce target weights vector for the platform's portfolio sizer.
9. Platform's portfolio sizer, execution router, and risk layer take over from here (Phase 1 plumbing).
10. Orders execute at next market open.

### 7.3. Execution timing

Orders are submitted as market-on-open orders (MOO) where supported, or as limit orders at the previous close ± 10 bps where MOO is not available, consistent with Phase 1 behavior. The 10 bps offset is a per-strategy parameter inherited from the platform default.

### 7.4. Position state tracking

The strategy must track the following per-position state, retrievable across cycles:

- Symbol
- Entry date and entry price (close on the day the entry signal fired)
- Highest close since entry (used to compute the trailing stop level)
- Days held (for time stop)

This state can be derived from the platform's existing positions and fills tables (Phase 1 schema) or stored in a strategy-specific table. Implementation should prefer derivation from existing tables where feasible to avoid duplicate state. If a strategy-specific table is required, document the schema in a migration.

---

## 8. Strategy parameters

The following parameters are exposed in the strategy configuration (YAML, Pydantic-validated). Defaults are shown; values may be overridden per sleeve.

| Parameter                       | Default | Range       | Notes                                                  |
|---------------------------------|---------|-------------|--------------------------------------------------------|
| `lookback_days`                 | 20      | 5-60        | Window for high-water mark; entry trigger              |
| `hard_stop_pct`                 | 0.04    | 0.02-0.10   | Hard stop loss below entry                             |
| `trailing_stop_pct`             | 0.05    | 0.03-0.10   | Trailing stop below highest-close-since-entry          |
| `time_stop_days`                | 10      | 5-30        | Force exit after N trading days                        |
| `risk_per_trade_pct`            | 0.01    | 0.005-0.02  | Per-trade risk as fraction of NAV                      |
| `max_position_pct`              | 0.25    | 0.10-0.40   | Hard cap on single-position size                       |
| `max_concurrent_positions`      | 5       | 2-10        | Max simultaneous open positions                        |
| `max_total_deployment_pct`      | 1.00    | 0.50-1.00   | Cap on total deployed capital                          |
| `drawdown_circuit_breaker_pct`  | 0.15    | 0.10-0.30   | Drawdown from trailing high to halt entries            |

**Implementation note:** `drawdown_circuit_breaker_pct` is expressed as the size of the drawdown that triggers the halt (e.g., 0.15 means halt at 85% of trailing high). This is the inverse of the threshold value used in code; document this inversion clearly to avoid confusion.

---

## 9. Capability requirements

The strategy declares the following capabilities (`StrategyCapabilities` dataclass from Phase 1):

- **Long-only:** TRUE. The strategy never produces short signals.
- **Fractional shares:** REQUIRED. Position sizing produces fractional share counts; the strategy will not function correctly without fractional share support.
- **Pattern Day Trader (PDT) tolerance:** NOT REQUIRED. The strategy holds positions for several days; PDT rules do not apply.
- **Margin:** NOT REQUIRED. The strategy enforces a 100% deployment cap and does not use leverage.
- **Options:** NOT REQUIRED.

If any required capability is unavailable on the configured account, sleeve creation MUST fail with a clear error message. This is a Phase 1 capability discovery responsibility; the strategy declares its requirements via the `StrategyCapabilities` dataclass.

---

## 10. Edge cases and failure modes

### 10.1. Market closure and holidays

The strategy uses trading days, not calendar days, throughout. The platform's existing market calendar (Phase 1) provides the authoritative trading day sequence. Strategy logic must reference this calendar, not raw timestamps.

### 10.2. Insufficient data

If a symbol has fewer than 21 trading days of closing-price history (e.g., a recently-created ETF), it is skipped for entry signals until sufficient history accumulates. Existing positions in such a symbol continue to be evaluated for exits normally.

### 10.3. Gaps and limit moves

If a symbol gaps below its hard stop level overnight, the exit order will fill at the open price, not at the stop level. The realized loss may exceed the nominal 4% stop. This is an accepted strategy characteristic, not a bug.

If gap behavior produces frequent realized losses meaningfully larger than 4% during backtesting, consider revisiting the stop methodology (e.g., using ATR-based stops). For initial implementation, fixed-percentage stops are sufficient.

### 10.4. Concurrent fills and partial fills

The platform's execution router (Phase 1) handles partial fills. The strategy does not need special logic for partial fills — it inspects current position size each cycle and sizes new orders against remaining available capital.

### 10.5. Stale data on cycle start

If the strategy cycle runs but market data has not yet updated (e.g., the cycle ran 2 minutes after close before EOD data was available), the strategy SHOULD detect this and abort the cycle with a warning rather than producing signals from stale data. Specifically: if the latest closing-price timestamp for any universe symbol is older than today's close, abort the cycle.

### 10.6. Halt during open positions

If the drawdown circuit breaker triggers while positions are open, those positions continue to be managed normally — exits proceed, but no new entries are taken. The sleeve can effectively wind down to zero positions before requiring manual intervention to resume.

### 10.7. Symbol delisting or trading halt

If an ETF in the universe is delisted, halted, or otherwise becomes untradeable, the strategy should:

- Skip the symbol for new entry signals.
- If a position is currently held in that symbol, log an ERROR-level message and notify the orchestrator. Manual intervention is required.

---

## 11. Acceptance criteria

Implementation is considered complete when ALL of the following are demonstrated:

### 11.1. Unit tests

- 20-day high computation produces correct values on synthetic data including edge cases (insufficient history, missing days, ties).
- Entry signal generation correctly identifies new highs and correctly excludes symbols with existing positions.
- Hard stop, trailing stop, and time stop each trigger correctly on synthetic price paths designed to test each case.
- Position sizing produces correct notional values given various NAV and stop configurations.
- Concentration cap, max concurrent positions, and deployment cap each correctly reject signals that would violate the constraint.
- Drawdown circuit breaker correctly tracks trailing high and triggers at the configured threshold.
- Drawdown circuit breaker correctly suppresses entries while allowing exits.

### 11.2. Integration tests

- End-to-end test: strategy + sleeve manager + portfolio sizer + execution router + risk layer produces correct orders on a synthetic market scenario.
- Two MomentumContinuation sleeves running in parallel with different parameters produce independent positions, fills, and NAVs without interference (regression test for the `client_order_id` collision bug from Phase 1).
- Strategy correctly handles the universe edge case of XLRE/XLC missing in historical periods (relevant for backtest harness integration).

### 11.3. Backtest validation

The strategy must run successfully under the BacktestHarness over a multi-year historical period and produce:

- A complete equity curve.
- Trade log with entry date, exit date, entry price, exit price, exit reason, P&L.
- Summary statistics: total return, annualized return, max drawdown, Sharpe ratio, win rate, average winner, average loser, number of trades.
- Comparison against the BuyAndHoldSPY benchmark over the same period.

Specific quantitative criteria for backtest acceptance are deliberately omitted. The strategy is being implemented as a candidate for paper trading, not as an alpha-validated production strategy. Backtest results inform whether to promote to paper, but no specific threshold gates this decision.

### 11.4. Paper validation

- Strategy runs as a paper sleeve for at least 4 weeks with no operational errors (crashes, exceptions, bot halts).
- Strategy generates trades during the paper period (i.e., the universe is producing breakouts in real-time, not just historically).
- Stops, time-stops, and circuit breakers are observed to function correctly on real (paper) trades, not just unit tests.

---

## 12. Open questions and deferred decisions

The following items are deliberately deferred to later iterations or to backtest results:

- **ATR-based stops vs. fixed-percentage stops.** The current spec uses fixed 4% hard stops and fixed 5% trailing stops. ATR-based (volatility-adjusted) stops would adapt to each ETF's volatility but add implementation complexity. Defer until backtest results indicate whether fixed stops produce systematic bias in stop-out frequency across the universe.

- **Profit target.** The current spec has no fixed profit target — profits are realized via the trailing stop. A fixed profit target (e.g., exit at 8% above entry) would lock in winners but cap upside. Defer until backtest results indicate whether the trailing-only approach loses too much to subsequent reversals.

- **Entry timing.** The current spec enters at next-day open after a closing-price breakout. An alternative is to enter intraday on a same-day breakout. The intraday approach is faster but introduces operational complexity. Defer until paper trading reveals whether the next-open entry is missing significant moves.

- **Universe expansion.** If 16 ETFs produce too few signals to be interesting, expand to thematic ETFs (XBI, IBB, SMH, KRE, etc.) or country-specific ETFs (EWJ, EWG, FXI). Defer until paper trading provides actual velocity data.

- **Volatility regime filter.** Some momentum strategies turn off in high-VIX environments to avoid whipsaws. The current spec has no such filter. Defer until backtest results indicate whether volatility filtering improves or hurts performance.

- **Correlation limits.** All 11 sector ETFs are sub-components of SPY. If breakouts cluster in sectors during a broad market rally, the strategy may end up with 5 highly-correlated positions. Adding a correlation cap (e.g., "no more than 3 positions with pairwise correlation > 0.7") would diversify but reduce signal usage. Defer until backtest results show whether this is a real problem.

---

## 13. Implementation notes for Claude Code

- Implement as a class conforming to the Phase 1 Strategy ABC.
- Strategy parameters loaded via Pydantic model from YAML. Re-use the existing Phase 1 config infrastructure.
- The strategy class should be pure: given inputs (current positions, recent prices, NAV), it produces outputs (target weights). It should not have side effects. Persistence and order submission are handled by the platform.
- Position state (highest-close-since-entry, days held, entry price) should be derivable from existing tables (positions, fills) where possible. If derivation is impractical, add a `strategy_state` table with a clear schema and Alembic migration.
- Use `Decimal` for monetary calculations and `float` for ratios, consistent with Phase 1 conventions.
- All logging at INFO level for normal operations; WARN for skipped symbols; ERROR for failures requiring intervention.
- Include type hints on all public methods. Pass mypy strict mode.
- Unit test coverage target: 90%+ for the strategy module, with explicit tests for each acceptance criterion in section 11.1.

---

**END OF SPECIFICATION**
