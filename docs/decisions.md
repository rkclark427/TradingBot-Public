# Decisions Log

Running record of design decisions with rationale. Add new entries at the top. Keep entries concise — link to longer discussions in chat or commits when needed.

## Format

Each entry: date, decision, rationale, status (active / superseded by date).

---

## 2026-05-07 — strategy_state last_session_date gate for once-per-day days_held increment

**Decision:** The `strategy_state` table has a `last_session_date` column. The orchestrator increments `days_held` only when `last_session_date < today`, then sets `last_session_date = today`. Multiple cycles within the same trading day don't double-count.
**Rationale:** The orchestrator runs every N seconds (many times per trading day). Without a gate, `days_held` would accumulate far faster than the backtest engine's once-per-day logic, causing time stops to fire incorrectly. `last_session_date` is the cheapest gate — no additional DB queries, no in-memory state.
**Status:** Active.

---

## 2026-05-07 — Pre-fetch held-position prices in orchestrator to avoid duplicate API calls

**Decision:** `_run_sleeve_cycle` fetches current prices for held positions (needed to update `highest_close_since_entry`) before calling `generate_targets`, then re-uses those prices for order sizing rather than fetching them again.
**Rationale:** `market_data.get_latest_price` is a live Alpaca REST call. For a sleeve with 5 held positions, fetching twice per cycle would double the API surface for no gain. Pre-fetching once and passing it through avoids the redundancy.
**Status:** Active.

---

## 2026-05-07 — strategy_state entry_price frozen at first buy; not updated on position additions

**Decision:** When `attribute_fill` processes a buy and a `strategy_state` entry already exists for that symbol, it leaves the existing entry unchanged. Only the first buy creates the entry.
**Rationale:** MomentumContinuation never adds to a position (it opens a full position at once). If it ever did, the original entry_price is the economically correct one for hard-stop calculation. Updating on additions would reset the stop-loss reference point and could allow a losing trade to avoid its stop.
**Status:** Active.

---

## 2026-05-07 — Separate SQLite for backtest cache; create_all not Alembic

**Decision:** Backtest price data lives in its own SQLite file (`data/backtest_cache.db`), initialized with `Base.metadata.create_all()` rather than Alembic migrations.
**Rationale:** The backtest cache is entirely derived data — it can be deleted and rebuilt from yfinance at any time. Alembic migrations are for state that would be lost if the DB were deleted (live trades, fills, NAV history). Using `create_all` for the cache avoids polluting the migration history with schema changes to ephemeral data.
**Status:** Active.

---

## 2026-05-07 — MomentumContinuation universe fixed in code, not config

**Decision:** The 16-symbol universe (11 sector ETFs + SPY, QQQ, IWM, EFA, EEM) is a constant `UNIVERSE` in `momentum_continuation.py`. Changing it requires a code edit, not a YAML change.
**Rationale:** The universe is load-bearing for strategy behavior — a wrong universe changes the signal characteristics entirely, not just a parameter. Making it config-editable without corresponding test coverage would create silent misuse risk. When the strategy warrants a different universe, that deserves a deliberate code change and review.
**Status:** Active.

---

## 2026-05-07 — yfinance confirmed as backtest data source; Alpaca not used for historical data

**Decision:** Historical price data for backtests comes from yfinance (`yfinance` 1.x). Alpaca is not used for backtest data.
**Rationale:** Alpaca's historical data API has tighter rate limits and costs more at scale. yfinance provides free adjusted OHLCV going back to the 1990s for the ETF universe we use, with `auto_adjust=True` giving correct split+dividend-adjusted prices. The tradeoff is that yfinance has no SLA and can break on API changes, but the local SQLite cache insulates backtests from that once populated.
**Status:** Active.

---

## 2026-05 — VM size constraint pending investigation

**Decision:** Use whatever Azure VM size is available; B2s preferred but D2 acceptable if subscription forces it.
**Rationale:** Kent's Azure subscription is forcing minimum size of D2. Likely a subscription type or region restriction. To investigate, but not blocking the build.
**Status:** Active.

---

## 2026-05 — Personal Alpaca account, not LLC

**Decision:** Open Alpaca account in Kent's personal name, not Windward Clark or other consulting LLC.
**Rationale:** Trading volume too low to qualify for Trader Tax Status, so LLC wrapper provides no tax benefit. Avoids muddying Windward Clark's charter-business activity coherence. Simpler operationally. Can form a dedicated trading LLC later if volume ever justifies it.
**Status:** Active. ID auth in progress for live; paper account already active.

---

## 2026-05 — OpenClaw integration as future state, Pushover for now

**Decision:** Pushover for critical alerts in v1. Build CLI commands as thin wrappers over Python query/command functions to make future OpenClaw conversational integration trivial.
**Rationale:** Pushover has tiered priorities (critical alerts can override DND), reliable, $5 one-time. OpenClaw is the conversational layer for "what's going on" queries — different use case than alerting; would coexist with Pushover even after deployed. Keeping the layered architecture means OpenClaw integration doesn't require refactoring.
**Status:** Active.

---

## 2026-05 — YAML sleeve registry uses hybrid model (Option C)

**Decision:** `sleeves.yaml` declares each sleeve with a `managed: true|false` flag. Managed sleeves are YAML-canonical (CLI changes are temporary, restored from YAML on restart). Unmanaged or CLI-created sleeves are DB-canonical.
**Rationale:** Production sleeves benefit from infrastructure-as-code discipline (repo reflects what's deployed). Experimental sleeves benefit from CLI flexibility (don't want to commit YAML for a sleeve you'll kill in two days). Hybrid serves both.
**Status:** Active.

---

## 2026-05 — Pushover for critical alerts, email for daily summary

**Decision:** Pushover for tiered critical alerts (account halt, drawdown halt, reconciliation failure, repeated rejections). Email for daily summary.
**Rationale:** Pushover has priority levels including "emergency" that overrides quiet hours. $5 one-time. Email for daily summary is fine because it's not time-sensitive.
**Status:** Active.

---

## 2026-05 — First live sleeve: BuyAndHoldSPY at $100, then MeanReversion at $500

**Decision:** When transitioning from paper to live (Phase 5), start with BuyAndHoldSPY at $100 to prove the live execution path with minimum risk. After 1-2 weeks of clean operation, add MeanReversion at $500.
**Rationale:** Conservative ramp-up validates the live path with the simplest possible strategy before adding the strategy that does most of the trading. Cost of caution: ~2 weeks delay on the showcase strategy.
**Status:** Active.

---

## 2026-05 — Six-phase build with explicit gates

**Decision:** Build in six phases — Foundation, Backtesting+Strategies, Execution+Multi-sleeve, Paper Validation, Live Deployment, Library Expansion. Each phase has a defined gate; don't proceed past a gate without meeting its criteria.
**Rationale:** Trading systems fail in subtle ways that compound. Phased delivery with gates catches issues at the level where they're cheap to fix. Particularly important for Phase 2 (backtest) and Phase 4 (paper validation) — these are the discipline gates that prevent deploying something that doesn't work.
**Status:** Active.

---

## 2026-05 — Three launch strategies

**Decision:** Launch the strategy library with MeanReversion (showcase), BuyAndHoldSPY (benchmark), and SectorRotation (interface stress test).
**Rationale:** Together they exercise the strategy interface across different cadences (daily / never / monthly) and signal types (mean reversion / none / momentum). If the engine handles all three, the interface is general enough.
**Status:** Active.

---

## 2026-05 — Strategy capability declaration as static metadata

**Decision:** Each strategy class has a static `capabilities: StrategyCapabilities` declaration. Validated against account capabilities at sleeve creation.
**Rationale:** Catches "strategy needs shorting but account doesn't support it" at creation time, not at first trade. Makes the engine fail fast and explicitly.
**Status:** Active.

---

## 2026-05 — Order netting within an environment

**Decision:** Within an environment (live or paper separately), intended orders from multiple sleeves on the same symbol are netted before submission to Alpaca. Fills allocated back proportionally.
**Rationale:** Slippage savings; cleaner accounting against the broker. Database split into intended_orders / net_orders / intended_to_net mapping table preserves full attribution.
**Status:** Active.

---

## 2026-05 — Sleeve NAV compounds (no capital cap)

**Decision:** Each sleeve has a starting bankroll that compounds with its own P&L. Position sizing within the sleeve is based on current NAV, not the starting amount. A sleeve can grow without bound or shrink (with sleeve-level drawdown halt to prevent grinding to dust).
**Rationale:** Matches how strategies actually work in the real world. Per-sleeve return becomes a meaningful metric (ending NAV / starting NAV). Capital movement is explicit (deposit/withdraw via CLI).
**Status:** Active.

---

## 2026-05 — Use Alpaca paper account for paper sleeves; no internal simulator in v1

**Decision:** Paper sleeves trade against Alpaca's paper environment. No internal simulator.
**Rationale:** Real fills, real market data, real edge cases; one fewer thing to build. Internal simulator could be added in v2 if there's a need for paper and live sleeves of the same strategy with identical conditions for direct comparison — but that need hasn't materialized.
**Status:** Active.

---

## 2026-05 — Multi-strategy sleeve-based platform (not single-strategy bot)

**Decision:** Build a platform that hosts a library of pluggable strategies, each running as an isolated sleeve. Replaces earlier framing as a single-strategy mean-reversion bot.
**Rationale:** Kent's actual use case is running multiple strategies with independent capital allocations and modes. Building this in from the start is ~30-40% more work than a single-strategy bot but the right architecture; retrofitting later would be much more.
**Status:** Active.

---

## 2026-05 — Mean reversion long-only in v1

**Decision:** First implementation of MeanReversion is long-only. Short selling deferred to v2.
**Rationale:** Avoids short borrow costs, locate availability issues, asymmetric risk profile. Long side is well-documented and sufficient to validate the engine. Short sleeve can be added later as a separate strategy.
**Status:** Active.

---

## 2026-05 — Limit orders only

**Decision:** All orders are limit orders. No market orders.
**Rationale:** Market orders expose to bad fills on illiquid moments. Limit price defaults to previous close ± 10 bps; cancel-and-retry-next-day if unfilled.
**Status:** Active.

---

## 2026-05 — SQLite + Alembic for state

**Decision:** SQLite database, Alembic migrations.
**Rationale:** Sufficient at this scale (single VM, single user, modest write volume). No DB server to run. Alembic handles schema evolution.
**Status:** Active.
