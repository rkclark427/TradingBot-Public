# System Setup

Everything needed to run backtests on a fresh Windows machine.

---

## 1. Install Git

Download and run the installer from [git-scm.com](https://git-scm.com/download/win).
Accept defaults. Verify:

```bash
git --version
```

---

## 2. Install uv

`uv` is the package manager for this project. It also manages the Python installation,
so you don't need to install Python separately.

Open PowerShell and run:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen your terminal, then verify:

```bash
uv --version
```

---

## 3. Install VS Code

Download from [code.visualstudio.com](https://code.visualstudio.com).

Recommended extensions (install from the Extensions panel):
- **Python** (Microsoft)
- **Pylance** (Microsoft)

---

## 4. Clone the repo

```bash
git clone https://github.com/rkclark427/TradingBot.git
cd TradingBot
```

No GitHub account required — the repo is public.

---

## 5. Create the Python environment

```bash
uv python install 3.11
uv venv
.venv\Scripts\activate
uv pip install -e .
```

To point VS Code at this environment: open the Command Palette (`Ctrl+Shift+P`),
select **Python: Select Interpreter**, and choose the `.venv` entry.

---

## 6. Seed historical data

```bash
bot-ctl backtest refresh-data --all
```

Downloads price history from Yahoo Finance into a local cache (`data/backtest_cache.db`).
Takes a few minutes. Only needed once.

---

## 7. Run a backtest

```bash
bot-ctl backtest run backtest_configs/momentum_2018_baseline.yaml
```

Results are written to `backtests/momentum_2018_baseline/` — open `equity_curve.html`
in a browser to view the performance chart.

---

## Next steps

See [backtest_guide.md](backtest_guide.md) for how to write your own strategy and run it.
