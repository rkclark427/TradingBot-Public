# Phase 1 Smoke Test

Manual end-to-end procedure for the Phase 1 gate. Run this against Alpaca paper after all unit tests pass. A clean pass confirms the full pipeline — sleeve creation → order submission → fill → NAV update — works end-to-end.

## Prerequisites

- `.env` is populated with `ALPACA_PAPER_API_KEY` and `ALPACA_PAPER_API_SECRET`
- You are in `~/trading-bot` with the venv active (`source .venv/bin/activate` or use `uv run`)
- Alpaca paper account is available and not rate-limited

---

## Step 1: Verify the environment

```bash
uv run python -c "
from src.config.models import Secrets
s = Secrets()
print('paper key present:', bool(s.alpaca_paper_api_key))
print('db path:', s.db_path)
"
```

Expected: `paper key present: True`

---

## Step 2: Seed account capabilities and asset universe

These calls hit Alpaca once and cache to SQLite.

```bash
uv run bot-ctl refresh-capabilities
uv run bot-ctl refresh-universe
```

Expected output (refresh-universe):
```
Fetching asset universe from Alpaca…
Done. XXXX assets upserted.
```

Spot-check that SPY is present and tradable:

```bash
uv run python -c "
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from src.config.models import Secrets
from src.tracking.repos.market import AssetUniverseRepo
engine = create_engine('sqlite:///' + Secrets().db_path)
with Session(engine) as s:
    row = s.get(__import__('src.tracking.models', fromlist=['AssetUniverseRow']).AssetUniverseRow, 'SPY')
    print('SPY tradable:', row.tradable if row else 'NOT FOUND')
"
```

Expected: `SPY tradable: True`

---

## Step 3: Create the BuyAndHoldSPY paper sleeve

```bash
uv run bot-ctl sleeves create --strategy buy_and_hold --mode paper --capital 100
```

Expected output:
```
Created sleeve <UUID>
  strategy=buy_and_hold  mode=paper  status=running  nav=$100.00
```

Note the sleeve UUID for use in later steps. Call it `SLEEVE_ID`.

---

## Step 4: Verify initial state

```bash
uv run bot-ctl status
uv run bot-ctl sleeves show <SLEEVE_ID>
```

Expected from `status`:
- Kill switch: `inactive`
- Sleeves: `1 running`

Expected from `show`:
- Status: `running`
- Current NAV: `$100.00`
- Positions: `none`
- Capital events: one `deposit` of `$100`

---

## Step 5: Run one orchestrator cycle

```bash
uv run bot-ctl run
```

The orchestrator will:
1. Check the kill switch (inactive)
2. Call `get_clock()` — if market is **open**, it proceeds; if closed, it logs "Market closed; skipping cycle" and sleeps

**If market is open (9:30 AM – 4:00 PM ET):**

Within one cycle (≤60 s), you should see log output like:
```
INFO  src.orchestrator: Submitted 1 order(s) for sleeve <SLEEVE_ID>
```

Press Ctrl-C after the first cycle completes.

**If market is closed:** The orchestrator will log "Market closed; skipping cycle" and keep sleeping. Either wait for market open or proceed to the manual verification steps using the Alpaca paper dashboard.

---

## Step 6: Verify order in Alpaca paper

Log in to [https://app.alpaca.markets](https://app.alpaca.markets) and switch to the **Paper** environment.

Check **Orders** for a limit buy order on SPY:
- Side: `buy`
- Type: `limit`
- Qty: approximately `0.2` shares (100 ÷ SPY price)
- `client_order_id` in the format `YYYYMMDD-SPY-1`

---

## Step 7: Wait for fill and check NAV

Alpaca paper fills limit orders quickly when SPY is trading. After the fill arrives, run another cycle (or wait for the orchestrator's poll cycle):

```bash
uv run bot-ctl sleeves show <SLEEVE_ID>
```

Expected after fill:
- **Positions**: `SPY qty=~0.2  avg_cost=~<fill price>`
- **Current NAV**: close to `$100.00` (slightly different due to fill price vs. limit price)

The NAV should reflect: `cash_remaining + qty × fill_price ≈ $100`

---

## Step 8: Run a second cycle (noop)

```bash
uv run bot-ctl run
# Ctrl-C after one cycle
```

Expected: no new orders submitted (`orders_submitted=0`). BuyAndHoldSPY already has SPY in positions and will return `target_weight=1.0` → sizer computes delta ≈ 0 → no orders.

---

## Gate checklist

| Gate criterion | Verified |
|----------------|----------|
| Sleeve created via CLI | ☐ |
| Sleeve persists across bot restarts (`bot-ctl sleeves list` after restart) | ☐ |
| BuyAndHoldSPY produces `Target(SPY, 1.0)` on cycle | ☐ |
| Portfolio layer generates a fractional SPY buy order (~$100) | ☐ |
| Risk layer passes the order | ☐ |
| Limit order appears in Alpaca paper with correct `client_order_id` | ☐ |
| Fill attributed: position row created, NAV updated | ☐ |
| `bot-ctl sleeves show` displays accurate NAV and position | ☐ |
| `bot-ctl status` shows account capabilities | ☐ |
| All unit tests pass (`uv run pytest`) | ☐ |

When all boxes are checked, Phase 1 is complete.

---

## Troubleshooting

**"Asset universe is empty"** — Run `bot-ctl refresh-universe` first.

**"order exceeds 101% of sleeve NAV"** — The sizer computed a quantity too large for the sleeve. Check that `current_nav` is correct in `bot-ctl sleeves show`.

**"Market closed; skipping cycle"** — Run during NYSE trading hours (9:30 AM – 4:00 PM ET, Mon–Fri) or wait.

**Fill never arrives** — Verify the limit price is reasonable (within a few cents of SPY's current price). If SPY has moved significantly since the order, the limit price may be stale. Cancel the order in the Alpaca paper UI, then run another bot cycle.

**DB file location** — `trading_bot.db` in the current directory (configurable via `DB_PATH` in `.env`).
