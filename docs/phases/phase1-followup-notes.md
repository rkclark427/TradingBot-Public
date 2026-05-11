# Phase 1 Follow-Up Notes

**Author:** Claude Code
**Date:** 2026-05-04
**Corresponding doc:** `docs/phases/tradingbot-phase1-followups-for-claude-code.md`

---

## Item 1: `client_order_id` idempotency on retry

### What I found

The original fix (using `net_row.id` instead of a sequence counter) solved the within-cycle collision between two sleeves. But it did NOT address the cross-cycle case: if Alpaca received an order but we got a network timeout, the next cycle would create a new `net_row` with a new ID, generating a different `client_order_id` and potentially submitting a second order for the same symbol.

### What I changed

- **`NetOrderRepo.find_error_for_retry(date_str, symbol, mode)`** — queries for the oldest `error`-status net_order for a given (date, symbol, mode). On retry (next cycle, same day), `submit_intended_orders` calls this before creating a new row.
- **Reuse logic in `submit_intended_orders`** — if an error row is found, we reset it to `pending`, update qty/side/limit_price to the current cycle's values, and delete its old `intended_to_net` mappings (to prevent double-attribution on fill). The client_order_id is then `f"{date_str}-{symbol}-{net_row.id}"` — same as the first attempt.
- **"client_order_id must be unique" handling** — if Alpaca raises this error, we look up the order via `client.get_order_by_client_order_id(client_order_id)` and record the alpaca_id. This handles the case where Alpaca received the order but we got a timeout on the response.
- **Mid-cycle kill switch re-check** — added a kill switch check inside `submit_intended_orders` after the net_row is created but before `client.submit_order()` is called. If the switch activates in that window, the net_row is marked `cancelled_pre_submit` and submission is skipped.
- **New client methods** — added `get_order_by_client_order_id`, `cancel_order`, and `cancel_all_orders` to `_BaseClient`.

### Why error-only (not pending)

Only `error`-status rows are candidates for retry. `pending` could mean either "created this cycle, not yet submitted" or "created in a crashed cycle." Treating `pending` as a retry target risks reusing another sleeve's net_row from the same cycle. Using `error` is unambiguous: it means we attempted submission and got an exception. The oldest-first ordering on `id` ensures consistent assignment in the multi-sleeve edge case.

### Tests added

- `test_retry_reuses_existing_error_net_row` — simulates transient failure on first call, verifies second call reuses the same net_row and uses the same `client_order_id`.
- `test_two_sleeves_same_symbol_get_distinct_client_order_ids` — two sleeves both buying SPY in the same cycle get distinct `client_order_id`s (net_row.id=1 vs net_row.id=2).

---

## Item 2: Risk threshold moved to config

### What I changed

- Added `max_order_nav_pct: float = 1.01` to `RiskConfig` in `src/config/models.py`. Configurable via YAML or the `RISK__MAX_ORDER_NAV_PCT` environment variable.
- `run_pre_trade_checks` and `check_and_filter` now accept `max_nav_pct: Decimal = Decimal("1.01")` as a parameter with the default matching the config default.
- The orchestrator reads `self._config.risk.max_order_nav_pct` and passes it as `max_nav_pct` to `check_and_filter`.
- The rejection message uses the configured value: `f"order exceeds {pct}% of sleeve NAV"`.
- The relationship between `execution.limit_offset_bps` and `risk.max_order_nav_pct` is documented in a comment at both the config model and the check function.

### Judgment call: explicit defaults, not derived

I kept both values as independent config fields rather than deriving the threshold from the offset (`1.0 + bps/10000 + buffer`). Derived values are clever but fragile: if either field changes independently (e.g., you tune the offset on one strategy but not the risk threshold), the derived formula breaks silently. Explicit defaults make the relationship visible through documentation only, which is enough at Phase 1 scale.

### Tests added

- `test_tighter_threshold_rejects_order_default_would_pass` — `max_nav_pct=1.0` rejects an order that 1.01 passes.
- `test_looser_threshold_passes_order_default_would_reject` — `max_nav_pct=1.05` passes an order that 1.01 rejects.

---

## Item 3: Alembic vs `Base.metadata.create_all`

### What I changed

- **CLI bootstrap** — replaced `Base.metadata.create_all(engine)` in `_app_context` with a programmatic `alembic upgrade head` call. Any pending migrations run automatically on every CLI invocation. On a fresh DB this runs all migrations; on an existing DB at head it's a fast no-op.
- **`bot-ctl db-status`** — new command that shows current revision, head revision, and whether they match. Exits 1 if behind head (useful for scripted pre-flight checks).
- **Tests** — test fixtures still use `Base.metadata.create_all(engine)` directly. This is intentional: Alembic requires `alembic.ini` which may not be present in arbitrary test environments, and tests don't care about migration history — they just need a schema. The production CLI path exclusively uses Alembic.

### Why this matters

`create_all` and Alembic can silently diverge if a migration is written incorrectly or a model is edited without a migration. By forcing the production path through Alembic, we catch that divergence at startup rather than at query time. The test split documents the intention: tests own their schema, production owns its history.

### Note

Schema changes from this point forward require an Alembic migration (`alembic revision --autogenerate -m "description"` + review + commit). Editing models alone is not sufficient.

---

## Item 4a: Multi-sleeve, same-symbol collision test

### Coverage

The `test_two_sleeves_same_symbol_get_distinct_client_order_ids` test in `tests/test_execution.py` covers this directly:

- Creates two sleeves (sleeve_a with `_SLEEVE_ID`, sleeve_b with a different UUID)
- Calls `submit_intended_orders` twice — once per sleeve — for SPY buys in the same `as_of` day
- Asserts both calls succeed (each gets an alpaca_id)
- Asserts the `client_order_id`s used in `client.submit_order(...)` are distinct

Since sleeve A's net_row has `status="submitted"` by the time sleeve B runs, `find_error_for_retry` returns `None` for sleeve B, so a new net_row is created. The two net_row.ids are different, giving distinct client_order_ids.

---

## Item 4b: Continuous-run validation

**This is a manual observation step — no code changes required.**

To-do for the smoke test session:

- [ ] Start `bot-ctl run` (no `--once`) during market hours with one active sleeve
- [ ] Observe at least 3 full cycles (3 × `cycle_interval_seconds`)
- [ ] Confirm heartbeat timestamp advances in DB after each cycle
- [ ] Confirm market-hours gate: after 4:00 PM ET, cycles should log "Market closed; skipping cycle"
- [ ] Confirm no unbounded growth in memory or log file size over a 30-minute run
- [ ] Document: time of first submitted order, time of fill, time of NAV update

Record observations below when complete:

```
Run window: ___________
First order submitted: ___________
Fill received: ___________
NAV updated: ___________
Any anomalies: ___________
```

---

## Item 4c: Kill switch graceful halt

### What I changed

**Kill switch modes** — the flag file now contains the mode string ("graceful", "cancel_open", "force"). `get_kill_switch_mode()` reads it. `activate(mode)` writes it. Unrecognised file content defaults to "graceful" for safety.

**Orchestrator `run()` loop** — checks `get_kill_switch_mode()` at the top of every loop iteration (before `run_once()`). On graceful/cancel_open: calls `_drain_fills(cancel_open=...)` and returns. On force: calls `_handle_force_exit()` which logs a warning, writes a final heartbeat, and raises `SystemExit(1)`.

**`_drain_fills(cancel_open)`** — if `cancel_open=True`, calls `client.cancel_all_orders()` first. Then polls fills every 30 seconds until no net_orders remain in "submitted" status, or `drain_timeout_seconds` (default 600) elapses. Writes a heartbeat on each poll. Logs a warning if the timeout is hit with orders still open (this can happen if Alpaca is down or very slow).

**`poll_fills` extended** — after processing filled orders, also scans "expired" and "cancelled" closed orders and marks matching "submitted" net_rows as expired/cancelled. This allows the drain loop to terminate even when DAY orders expire at 4:00 PM ET without filling.

**`bot-ctl kill`** — updated with `--cancel-open` and `--force` flags. `bot-ctl resume` is unchanged.

**`bot-ctl run`** — simplified to delegate to `orch.run(force=force)` instead of reimplementing the loop. Kill switch handling is now entirely inside the orchestrator.

### On DAY orders and drain timing

BuyAndHoldSPY uses DAY orders (TIF=DAY). The worst-case drain without `--cancel-open` is: order submitted at 9:31 AM, market close at 4:00 PM — up to 6.5 hours. Use `bot-ctl kill --cancel-open` if you need the bot out of the market immediately. The default "graceful" mode is appropriate for after-hours kills or situations where you're comfortable waiting for the day's orders to resolve.

### Live validation required

The kill switch graceful halt has not yet been validated against a running process with open orders. Schedule this during the Phase 1 smoke test session:

- [ ] Start `bot-ctl run` with one sleeve
- [ ] Confirm at least one order is open at Alpaca
- [ ] Run `bot-ctl kill` (graceful)
- [ ] Confirm bot stops generating new cycles immediately
- [ ] Confirm bot continues polling until the order fills or expires
- [ ] Confirm clean exit (exit code 0, final heartbeat written)
- [ ] Run `bot-ctl kill --cancel-open` with a fresh sleeve + open order
- [ ] Confirm open order cancelled at Alpaca before exit
- [ ] Run `bot-ctl resume` and confirm bot restarts cleanly
- [ ] Document timing below

```
Kill flag created at: ___________
Last fill reconciled at: ___________
Process exited at: ___________
Exit code: ___________
```

---

## Summary

All items are implemented and passing in the test suite. The only remaining steps are manual live validations (4b and 4c) which require market hours and a running process. Those should be completed during the Phase 1 smoke test session before declaring Phase 1 done and opening Phase 2.
