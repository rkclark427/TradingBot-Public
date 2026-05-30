# Running a Strategy in Backtest

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (package manager)

No Alpaca account or `.env` file is needed for backtesting. Data comes from yfinance.

---

## 1. Install

```bash
git clone <repo-url>
cd TradingBot
uv venv

# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

uv pip install -e .
```

---

## 2. Seed historical data (one-time)

```bash
bot-ctl backtest refresh-data --all
```

Downloads OHLCV data from yfinance starting 2010-01-01 into a local SQLite cache at
`data/backtest_cache.db`. Takes a few minutes. Only needed once; subsequent runs use the cache.

To add specific symbols later:
```bash
bot-ctl backtest refresh-data --symbol AAPL --symbol MSFT
```

---

## 3. Write the strategy

Create `src/strategies/my_strategy.py`. The strategy must extend the `Strategy` ABC from
`src/strategies/base.py` and implement one method: `generate_targets`.

```python
from __future__ import annotations
from datetime import datetime
from typing import Any

from src.strategies.base import MarketDataView, Position, Strategy, StrategyCapabilities, Target


class MyStrategy(Strategy):
    name = "my_strategy"

    capabilities = StrategyCapabilities(
        asset_classes=frozenset({"us_equity"}),
        requires_shorting=False,
        requires_options=False,
        requires_fractional=True,
        min_cash_buffer_pct=0.0,
        rebalance_cadence="daily",
        typical_holding_period_days=5,
    )

    def generate_targets(
        self,
        params: dict[str, Any],
        nav: float,
        cash: float,
        positions: dict[str, Position],
        market_data: MarketDataView,
        universe: list[str],
        as_of: datetime,
        position_state: dict[str, dict[str, Any]] | None = None,
    ) -> list[Target]:
        # Return the FULL desired portfolio as a list of Target objects.
        # target_weight is a fraction of NAV (0.5 = 50% of portfolio).
        # Any symbol not in the returned list will be sold.
        return [
            Target(symbol="SPY", target_weight=0.5, rationale={"reason": "example"})
        ]
```

### What you get

| Argument | Type | What it is |
|---|---|---|
| `params` | `dict` | Your strategy's parameters from the YAML config |
| `nav` | `float` | Total portfolio value today |
| `cash` | `float` | Uninvested cash |
| `positions` | `dict[str, Position]` | Currently held symbols → `Position(symbol, qty, avg_cost)` |
| `market_data` | `MarketDataView` | Price history and latest quotes |
| `universe` | `list[str]` | Symbols this strategy is allowed to trade |
| `as_of` | `datetime` | Current simulation date (timezone-aware, US/Eastern) |
| `position_state` | `dict` | Per-position metadata: `entry_date`, `days_held`, `highest_close_since_entry` |

### Getting price data

```python
# DataFrame with columns: open, high, low, close, volume
bars = market_data.get_bars("SPY", lookback_days=20, as_of=as_of)

# Latest closing price as float
price = market_data.get_latest_price("SPY")
```

### Key rules

- Return the **full desired portfolio**, not just changes. If you're holding SPY and want to keep it,
  include it in the return list.
- `target_weight` is a fraction of NAV. `0.5` = 50% in that symbol, `1.0` = fully invested.
- The engine won't let you over-leverage — weights exceeding available cash are scaled down.
- Strategy must be **stateless** — all state between calls arrives through the arguments.

See `src/strategies/momentum_continuation.py` and `src/strategies/short_term_mean_reversion.py`
for complete working examples.

---

## 4. Register the strategy

Edit `src/backtest/config.py`. Add two entries:

**Strategy registry** — maps the class name the YAML uses to the Python module and class:

```python
_STRATEGY_REGISTRY: dict[str, tuple[str, str]] = {
    # existing entries ...
    "MyStrategy": (
        "src.strategies.my_strategy",
        "MyStrategy",
    ),
}
```

**Universe map** — symbols this strategy is allowed to trade:

```python
UNIVERSE_MAP: dict[str, list[str]] = {
    # existing entries ...
    "MyStrategy": ["SPY", "QQQ", "IWM"],
}
```

Any symbol in the universe map must be in the data cache. If you added new symbols, run
`bot-ctl backtest refresh-data --symbol <SYMBOL>` before backtesting.

---

## 5. Create a backtest config

Create `backtest_configs/my_strategy_baseline.yaml`:

```yaml
strategy:
  class: MyStrategy
  parameters:
    # Any key-value pairs here land in generate_targets as the `params` dict.
    lookback_days: 20
    stop_pct: 0.05

benchmarks:
  - BuyAndHoldSPY

simulation:
  start_date: 2018-01-01
  end_date: null              # null = use latest date in cache
  starting_capital: 10000
  cash_annualized_rate: 0.04
  slippage_bps: 5

output:
  directory: ./backtests/my_strategy_baseline/
  format: html

cache_db: data/backtest_cache.db
```

---

## 6. Run the backtest

```bash
bot-ctl backtest run backtest_configs/my_strategy_baseline.yaml
```

Results are printed to stdout and written to the output directory:

| File | Contents |
|---|---|
| `equity_curve.html` | Interactive performance chart vs. benchmark |
| `trade_log.csv` | All closed positions: entry/exit dates, P&L, exit reason |
| `metadata.json` | Run config, timestamp, summary statistics |

---

## 7. Iterate

**Change a parameter:** Edit the YAML and re-run — no code change needed.

**Try multiple parameter sets:** Create separate config files and run each:
```bash
bot-ctl backtest run backtest_configs/my_strategy_v1.yaml
bot-ctl backtest run backtest_configs/my_strategy_v2.yaml
```

**List past runs:**
```bash
bot-ctl backtest list
```

**Check cache freshness:**
```bash
bot-ctl backtest cache-status
```
