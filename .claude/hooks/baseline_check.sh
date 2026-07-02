#!/usr/bin/env bash
# Stop hook: if this session touched research/, amd_engine.py, backtest_combine.py,
# or other backtest code, verify BOTH frozen baselines still reproduce before the
# session ends. Exit 2 = block the stop and tell Claude to investigate.
#
# Baseline A — 15m harness alone:        755 trades / $183,632.75
# Baseline B — combined production seed: 886 trades / $383,441.27
#   (15m + 5m trend overlay, cross-strategy gating, per-strategy adaptive streaks)
# Update the constants below ONLY when a baseline is deliberately re-frozen.

B15_TRADES=755
B15_EQUITY="183632.75"
BC_TRADES=886
BC_EQUITY="383441.27"

cd "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || exit 0

# Only run the (slow) check when relevant files changed in the working tree.
if ! git status --porcelain | grep -Eq 'research/|amd_engine\.py|backtest'; then
  exit 0
fi

# Prefer the project venv (system python lacks deps / `python` may not exist).
PYBIN="python3"
[[ -x "venv/bin/python" ]] && PYBIN="venv/bin/python"

result=$("$PYBIN" - <<'PY' 2>/dev/null
import asyncio
from research.harness import Harness
from research.overlay import CFG5M
import backtest_combine

async def main():
    # Baseline A: 15m harness alone
    h15 = await Harness.create(interval="15m")
    s15 = h15.run_all()
    st15 = h15.equity(s15)

    # Baseline B: combined production seed (mirrors research/gen_trade_log.py)
    h5 = await Harness.create(interval="5m")
    s5 = h5.run_all(CFG5M)
    for s in s15:
        s["_strategy"] = "amd_15m"
    for s in s5:
        s["_strategy"] = "trend_5m"
    res = backtest_combine.simulate(s15 + s5, with_trades=True)

    # Defensive extraction — trade count from the trades list, final equity from
    # common stat keys with a last-trade fallback.
    trades_list = res.get("trades")
    n_combined = len(trades_list) if isinstance(trades_list, list) else int(trades_list or 0)
    final = res.get("finalCapital") or res.get("final") or res.get("finalEquity") or res.get("final_equity")
    if final is None and isinstance(trades_list, list) and trades_list:
        final = trades_list[-1].get("equity_after") or trades_list[-1].get("equityAfter")

    print(f"{st15['trades']} {st15['final']:.2f} {n_combined} {float(final):.2f}")

asyncio.run(main())
PY
)

t15=$(echo "$result" | awk '{print $1}')
e15=$(echo "$result" | awk '{print $2}')
tc=$(echo "$result" | awk '{print $3}')
ec=$(echo "$result" | awk '{print $4}')

fail=""
if [[ "$t15" != "$B15_TRADES" || "$e15" != "$B15_EQUITY" ]]; then
  fail="15m harness: ${t15:-?} trades / \$${e15:-?} (expected ${B15_TRADES} / \$${B15_EQUITY}). "
fi
if [[ "$tc" != "$BC_TRADES" || "$ec" != "$BC_EQUITY" ]]; then
  fail="${fail}Combined seed: ${tc:-?} trades / \$${ec:-?} (expected ${BC_TRADES} / \$${BC_EQUITY})."
fi

if [[ -n "$fail" ]]; then
  echo "BASELINE BROKEN: ${fail} The research code no longer reproduces the frozen baseline(s) — investigate before ending the session. Do NOT commit." >&2
  exit 2
fi

exit 0
