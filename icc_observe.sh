#!/bin/bash
# ICC Observation Agent — runs every 4 hours via cron
# Invokes Claude CLI with the observation prompt, reads local files, updates strategy doc

set -e

PROJECT_DIR="/Users/jimmykeli/personalprojects/trading-exp"
LOG="/Users/jimmykeli/personalprojects/trading-exp/icc_observe.log"
cd "$PROJECT_DIR"

echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] Starting observation run..." >> "$LOG"

claude --dangerously-skip-permissions -p '
You are the learning layer for an ICC (Indication, Correction, Continuation) trading system on Gold/USD.

Your job: read the current state, analyze what happened since your last run, learn from outcomes, and update the strategy.

## Every Run

1. Read `icc_state.json` — check active_setups, completed_setups, learning_log, and current params
2. Read `icc_monitor_log.json` — check for NEW_SETUP and OUTCOME events since your last observation
3. Read `ICC_STRATEGY.md` — check the "Observation Log" section for your previous notes

## If Trades Completed

For each completed trade, analyze WHY it won or lost. Consider:
- Did the setup direction match the H4 trend? (h1_trend vs h4_trend fields)
- What was the correction depth? (Winners tend to be 35-65%)
- What was the move size relative to ATR?
- What time of day was the setup detected? (London/NY session = more volume)
- Was this a chain trade? (entry_swing price = previous setup L1)
- What was the confidence score? Did high-confidence setups actually win more?

## Pattern Recognition

Look across ALL completed trades for patterns:
- Win rate by direction (BUY vs SELL)
- Win rate when H1 and H4 trends align vs conflict
- Win rate by correction depth ranges (20-35%, 35-50%, 50-65%, 65-75%)
- Win rate by move size (small vs large relative to ATR)
- Win rate by confidence score ranges
- Any streaks or clustering of wins/losses

## Update the Strategy

Add a timestamped entry to the "Observation Log" section of ICC_STRATEGY.md:
- What you observed
- Any patterns emerging
- Specific filter proposals with reasoning (e.g., "Skip setups where H1 and H4 trends conflict — 2/7 of these won vs 5/8 aligned setups")
- Current parameter assessment — should anything change?

## Rules
- Be specific. "Win rate is low" is useless. "SELL setups with correction depth >60% are 1/5 (20%)" is useful.
- Do not change parameters until you have 10+ completed trades. Before that, just observe and note.
- When you do propose changes, explain the evidence. Show the numbers.
- If nothing new happened since last run, just note "No new completions. X active setups being tracked." — keep it brief.
- Commit your changes: git add ICC_STRATEGY.md && git commit -m "observation: [brief summary]" && git push
' >> "$LOG" 2>&1

echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] Observation run complete." >> "$LOG"
