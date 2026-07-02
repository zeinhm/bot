# ZENITH Claude Code Pipeline

Drop the `.claude/` directory into the repo root (`bot/`'s parent, wherever CLAUDE.md lives). Commit it — project-scoped agents are meant to be versioned.

## Install

```bash
cp -r .claude /path/to/zenith-repo/
chmod +x /path/to/zenith-repo/.claude/hooks/*.sh
```

No extra dependencies — hooks use python3 only.

Restart any running Claude Code session — agent files are loaded at session start.

## Usage

```
/feature add a monthly PDF report download to the analytics page
```

Pipeline: researcher → planner → **human gate (you approve the plan)** → spec-writer (spec saved to docs/specs/) → implementer → tester → code-reviewer → summary. You commit and push manually; the pipeline never touches git history or deploys.

Any agent can also be invoked standalone:

```
Use the code-reviewer subagent on the current diff
Use the researcher subagent: how is the paper trading balance reset wired?
```

## The stages

| Agent | Model | Write access | Halts pipeline when |
|---|---|---|---|
| researcher | sonnet | none | request touches frozen zone |
| planner | opus | none | product/regulatory decision needed |
| spec-writer | opus | none | — |
| implementer | opus | full (except frozen) | spec defect, new dependency |
| tester | opus | tests/ only | criteria fail (2 fix cycles max) |
| code-reviewer | inherit | none | CRITICAL/HIGH findings (2 cycles max) |

**Launch the main session on Opus** — `claude --model opus` — so the orchestrator and the
code-reviewer (model: inherit) also run on the top model. Best-result routing costs
noticeably more per pipeline run; if you ever want a cheaper mode, set
`CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-4-6` in the environment — it overrides every
subagent's model in one place without editing the files.

## Hooks

- **guard_frozen.sh** (PreToolUse on Edit/Write/Bash): hard-blocks any write to `strategy.py`, including bash redirection/sed tricks. Exit 2 → tool call denied, reason fed back to Claude.
- **baseline_check.sh** (Stop): if the session touched `research/`, `amd_engine.py`, `backtest_combine.py`, or other backtest code, re-runs BOTH frozen baselines and blocks session end on any mismatch: (A) 15m harness alone = 755 trades / $183,632.75, (B) combined production seed (15m + 5m overlay, cross-strategy gating, per-strategy adaptive streaks) = 886 trades / $383,441.27. Checking both isolates the failure: A broken = harness/engine problem, B broken with A intact = gating/overlay problem. Update the constants only when a baseline is deliberately re-frozen. Requires DB access from the environment where Claude Code runs (skips silently if not in the repo). NOTE: the combined-stats key extraction in the script is defensive (`final`/`finalEquity`/last-trade fallback) — verify it against `backtest_combine.simulate`'s actual return shape on first run.

## Tuning notes

- Auto-delegation to custom agents is unreliable; the `/feature` command invokes each agent explicitly by name, which is the reliable trigger.
- Expect a full pipeline run to cost several times a normal single-session task in tokens — more so on the Opus routing. Skip the pipeline for trivial changes ("fix this typo"); it's for features, not one-liners.
- Agent file edits require a session restart to take effect; edits via `/agents` apply immediately.
- If you later enable a Postgres MCP server, add it to the researcher's tools so it can answer "how does this data actually look in prod" questions during research.
