# TradingBot Phase 1 — Follow-Up Tasks Before Phase 2

**For:** Claude Code
**From:** Kent (via Claude Desktop coordinator review)
**Date:** 2026-05-04
**Context:** Phase 1 code is complete and 161 tests pass. Smoke test will run tomorrow during market hours. Before declaring Phase 1 done and starting Phase 2, please address the items below. Several are concerns about the three bug fixes from yesterday's after-hours smoke testing — I want them resolved with **maximum power and flexibility** (config-driven where possible, no hardcoded magic numbers, idempotent where it matters).

I am not in a position to referee architectural tradeoffs. Where I've raised a question, please make a judgment call, implement it, and document the reasoning in the code and in the followup notes at the bottom of this doc so I can review.

---

## 1. `client_order_id` idempotency on retry

**Background of the fix:** When two sleeves both bought SPY in the same cycle, the per-call sequence counter generated colliding `client_order_id`s like `20260504-SPY-1` twice. The fix was to create the `net_orders` DB row first, get its auto-incremented `id`, and use `f"{date_str}-{symbol}-{net_{row.id}}"` as the `client_order_id`.

**The concern:** What happens if the Alpaca submission fails *after* the DB row is created but before we know whether Alpaca accepted it?

There are two possible scenarios on a retry:
- Alpaca actually accepted the first attempt and our client just didn't see the response (network blip, timeout, etc.). In this case, retrying with the **same** `client_order_id` lets Alpaca dedupe and is safe.
- Alpaca rejected the first attempt (or never received it). In this case, either reusing or creating a new row is fine, but reusing keeps things simple.

**True idempotency requires reusing the existing `net_orders` row's ID on retry, not creating a new row.** Creating a new row on retry risks double-submission if the first attempt actually succeeded.

**What I need:**
1. Audit the order submission retry path. Confirm whether retries reuse the original `net_orders` row or create a new one.
2. If retries currently create a new row, change it so they reuse the original row's ID. The `net_orders` row should be created once per *intent to trade*, not once per *submission attempt*.
3. Add a state field on `net_orders` if one doesn't exist (e.g., `submission_state`: `pending` → `submitted` → `acknowledged` / `rejected`) so retries can find and reuse rows that were created but never confirmed.
4. Add a unit test that simulates a transient failure on first submission and verifies the retry uses the same `client_order_id`.
5. If you find this is already correctly handled, just confirm it in the followup notes and add a test if one doesn't exist.

---

## 2. Risk threshold: 101% NAV check should be config-driven

**Background of the fix:** The risk layer rejected fully-allocated BuyAndHoldSPY orders because the 10bps limit price offset pushed the order notional to ~100.1% of NAV. The fix was to raise the threshold to 101%.

**The concern:** 101% is a magic number that's tied to a specific limit price offset (10bps). If someone tunes the offset later — say to 5bps or 20bps — the risk threshold won't automatically follow, and the relationship between the two values will be invisible in the code.

**What I need:**
1. Move the threshold to config. Suggested location: `risk.max_order_nav_pct` (default `1.01`). It should be loadable via the same Pydantic config system as everything else, with env override capability.
2. Move the limit price offset to config too if it isn't already. Suggested location: `execution.limit_price_offset_bps` (default `10`).
3. At the risk check site, add a comment that explains *why* the default is 1.01 and references the limit price offset as the driver. Something like:
   ```python
   # Default 1.01 allows for the limit_price_offset_bps (default 10bps = 0.1%)
   # plus a small buffer. If you tune the limit offset, also tune this threshold.
   ```
4. Bonus if it's clean: derive the default threshold from the limit offset rather than hardcoding both. E.g., `max_order_nav_pct = 1.0 + (limit_price_offset_bps / 10000) + 0.0005` (5bps buffer). Only do this if it doesn't make the config awkward — explicit defaults are also fine, just document the relationship.
5. Update the risk layer's unit tests to exercise the config-driven threshold.

---

## 3. Alembic vs `Base.metadata.create_all`

**Background:** Phase 1 has migrations 0001 and 0002 written, but the CLI uses `Base.metadata.create_all` to bootstrap the DB. This was a Phase 1 convenience.

**The concern:** Schema drift between `create_all` and migrations is a classic source of "works in dev, breaks when we cut over to live." Since the migrations already exist, switching to migration-driven schema management is cheap now and expensive later.

**What I need:**
1. Replace `Base.metadata.create_all` in the CLI bootstrap with `alembic upgrade head` (or the programmatic equivalent using `alembic.config.Config` + `command.upgrade`).
2. On a fresh DB, the bootstrap should run all migrations from scratch and end up at head.
3. On an existing DB, it should be a no-op if already at head.
4. Add a CLI command `bot-ctl db status` that shows current migration version and whether the DB is up to date — useful for debugging and pre-flight checks.
5. Document in the README that schema changes from this point forward must go through Alembic, not model edits alone.
6. If there's a reason you think `create_all` should stay (e.g., test fixtures), keep it for tests only and use Alembic for the production CLI path. Document the split.

---

## 4. Pre-Phase-2 integration gates

**Background:** The smoke test (Task 15) is `bot-ctl run --once` with a single sleeve during market hours. That validates the happy path but doesn't exercise some scenarios that Phase 1 specifically claims to handle.

**What I need (in addition to the standard smoke test tomorrow):**

### 4a. Multi-sleeve, same-symbol collision test

The `client_order_id` collision bug from yesterday only manifests when two sleeves trade the same symbol in the same cycle. A single-sleeve `--once` run will not exercise the fix.

- Add an integration test (or a documented manual smoke test step) that creates two sleeves both running BuyAndHoldSPY and runs `--once`. Verify both orders submit successfully with distinct `client_order_id`s and both fills attribute correctly to the right sleeve.
- If this is already covered in the 161 tests, point me to the test name in the followup notes.

### 4b. Continuous-run validation

`--once` exits after one cycle. The orchestrator's main loop, heartbeat, and market-hours gating only matter during a continuous run.

- Run `bot-ctl run` (no `--once`) for at least one full session — or a meaningful portion of one — and verify:
  - Heartbeat updates as expected
  - Market-hours check correctly gates trading at open and close
  - Per-sleeve cycle timing behaves as configured
  - No memory leaks or unbounded growth in any in-memory structure
- This can be done tomorrow alongside the standard smoke test if practical, or scheduled for later in the week. Document the run window and observations in the followup notes.

### 4c. Kill switch behavior and live test

The kill switch is documented as file-based (`kill_switch.flag`) and is supposed to halt a running process. Both the behavior and the live validation need work.

**Required behavior — graceful halt by default:**

When the kill switch trips (either via `kill_switch.flag` file or `bot-ctl kill` command), the bot should perform a **graceful halt**:

1. Stop initiating new cycles. No new strategy evaluations, no new order generation, no new `net_orders` rows created.
2. Let in-flight orders complete their lifecycle. Orders already submitted to Alpaca should run to fill, expiry, or Alpaca-side cancellation (DAY orders auto-cancel at 4:00 PM ET). `poll_fills` should keep running until all submitted orders are reconciled.
3. Handle the unsubmitted-but-generated edge case. If a `net_orders` row was created but `submit_order` hasn't been called yet when the kill switch trips, mark the row as `cancelled_pre_submit` (or equivalent) and abandon it — do not submit.
4. Persist final state. Reconcile NAV, fills, and positions to the DB before exit.
5. Exit cleanly. Log the halt reason, write a final heartbeat marking shutdown, exit zero.

**Required CLI flags:**

- `bot-ctl kill` → graceful halt (default behavior described above)
- `bot-ctl kill --cancel-open` → graceful halt **plus** cancel all open orders at Alpaca before letting the lifecycle complete. For cases where the operator wants the bot fully out of the market before halting.
- `bot-ctl kill --force` → immediate process exit, no cleanup. Last resort for when graceful halt is wedged. Should log a clear warning that state may be inconsistent.

The `kill_switch.flag` file should trigger the graceful halt path (equivalent to `bot-ctl kill` with no flags).

**Live validation:**

- Start `bot-ctl run` in continuous mode during market hours with at least one active sleeve.
- While it's running and ideally with at least one open order at Alpaca, create the `kill_switch.flag` file.
- Verify the bot:
  - Stops generating new cycles immediately
  - Continues polling the open order until it fills or expires
  - Reconciles state to DB
  - Exits cleanly with a zero exit code
- Separately, test `bot-ctl kill --cancel-open` against a running process with open orders. Verify open orders are cancelled at Alpaca before exit.
- Test `bot-ctl resume` to confirm the bot can restart cleanly after a graceful halt (clears the flag file, picks up where it left off).
- Document observed timing in the followup notes — e.g., "kill flag created at T+0, last fill reconciled at T+47s, process exited at T+48s."

**Note on DAY orders:** Since BuyAndHoldSPY uses DAY limit orders, the worst-case "let in-flight orders complete" duration is bounded by market close. This makes graceful halt safer than it would be with GTC orders. If Phase 2 strategies introduce GTC orders, revisit this design — graceful halt with an unbounded order lifetime may need a timeout or be paired with `--cancel-open` as the new default.

---

## 5. Followup notes section

At the bottom of this doc (or in a sibling file `docs/phase1-followup-notes.md`), please add a short writeup for each of items 1–4 covering:
- What you found / what you changed
- Any judgment calls you made and why
- Anything that surprised you or that I should be aware of before Phase 2

I'll review these before we open Phase 2.

---

## Out of scope for this doc

- Phase 2 strategy work (MeanReversion, SectorRotation, backtesting). Do not start until Phase 1 gate is passed, which now means: standard smoke test + items 1–4 above.
- Live trading cutover. Phase 1 is paper-only.

---

## Summary checklist

- [ ] Item 1: `client_order_id` retry idempotency audited and corrected if needed; test added
- [ ] Item 2: Risk threshold and limit offset moved to config; risk layer tests updated
- [ ] Item 3: CLI bootstrap uses Alembic; `bot-ctl db status` added; README updated
- [ ] Item 4a: Multi-sleeve same-symbol scenario tested
- [ ] Item 4b: Continuous run validated for at least a meaningful portion of a session
- [ ] Item 4c: Kill switch validated against a running process
- [ ] Followup notes written for each item
- [ ] Standard smoke test (Task 15) still passes after these changes
