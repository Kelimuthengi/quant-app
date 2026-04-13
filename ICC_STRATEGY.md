# ICC Strategy — Indication, Correction, Continuation

## What is ICC?

A market-structure-based trading strategy that enters at the correction (pullback) after confirming trend direction through Higher Highs / Higher Lows (buy) or Lower Highs / Lower Lows (sell).

The core principle: **don't chase breakouts, don't guess reversals — enter at the pullback when structure confirms direction.**

## How It Works (Buy Example)

```
Price action:

     H1 (4475) ← INDICATION: price makes a High
    ╱          ╲
   ╱            ╲
  ╱              ╲
L1 (4351)        HL (4404) ← ENTRY ZONE: Higher Low (above L1)
                      ╲
                       → price continues up to 4554+ (CONTINUATION)

Entry: at the HL (4404)
SL: just below the HL
TP: at H1 (4475) or next resistance
```

### The 3 Phases

| Phase | What Happens | Do You Trade? |
|-------|-------------|---------------|
| **Indication** | Price breaks a previous high/low, showing direction | NO — too early |
| **Correction** | Price pulls back but does NOT break the previous low (buy) or high (sell) | PREPARE — this is where your limit order goes |
| **Continuation** | Price bounces from the correction and continues in trend direction | YES — you are already in from the correction |

### Key Rule
> "If price makes a Higher Low, it's going to make a Higher High."
> "If price makes a Lower High, it's going to make a Lower Low."

The Higher Low / Lower High is the confirmation that the trend is intact. That's where you enter.

## Buy Setup Rules

1. Price makes a **Low (L1)**
2. Price makes a **High (H1)** — this is the Indication
3. Price pulls back and makes a **Higher Low (HL)** — HL must be above L1
4. **Enter** at the HL zone
5. **SL** below the HL (if this breaks, the thesis is wrong)
6. **TP** at H1 (conservative) or project the move (aggressive)

## Sell Setup Rules

1. Price makes a **High (H1)**
2. Price makes a **Low (L1)** — this is the Indication
3. Price pulls back and makes a **Lower High (LH)** — LH must be below H1
4. **Enter** at the LH zone
5. **SL** above the LH
6. **TP** at L1 (conservative) or project the move (aggressive)

## Detection Logic (Algorithm)

### Step 1: Swing Detection
- A candle is a **swing high** if its high is greater than the highs of N candles on both sides (lookback)
- A candle is a **swing low** if its low is less than the lows of N candles on both sides
- Lookback = 3 for H1 Gold/USD (volatile instrument, swings form quickly)

### Step 2: Structure Classification
- Compare each swing high to the previous swing high: **HH** if higher, **LH** if lower
- Compare each swing low to the previous swing low: **HL** if higher, **LL** if lower

### Step 3: ICC Pattern Matching
- **BUY**: Find sequence → L1 (any swing low) → H1 (swing high, must be HH) → HL (swing low, must be classified HL and price > L1)
- **SELL**: Find sequence → H1 (any swing high) → L1 (swing low, must be LL) → LH (swing high, must be classified LH and price < H1)

### Step 4: SL/TP Calculation
- **SL**: Below the HL (buy) or above the LH (sell) with a buffer
- Minimum SL distance must account for spread + noise (instrument-specific)
- **TP**: At the indication level (H1 for buys, L1 for sells) — conservative target
- Filter: minimum R:R of 1.5:1 to take the trade

## Real Example — Gold/USD, March 26-31, 2026

The system detected a chain of 4 consecutive ICC buy setups:

```
Setup #110: L1=4351 (Mar 26) → H1=4475 (Mar 27) → HL=4404 (Mar 27) → TP 4475 ✅ WIN
Setup #111: L1=4404 (Mar 27) → H1=4554 (Mar 27) → HL=4420 (Mar 30) → TP 4554 ✅ WIN
Setup #112: L1=4420 (Mar 30) → H1=4580 (Mar 30) → HL=4483 (Mar 31) → TP 4580 ✅ WIN
Setup #113: L1=4483 (Mar 31) → H1=4619 (Mar 31) → HL=4530 (Mar 31) → TP 4619 ✅ WIN
```

Each HL became the next L1 — a staircase of Higher Lows confirming a strong uptrend. Price went from 4351 to 4619+ across this sequence.

## POC Results (v2 — 7 months of Gold/USD H1 data)

- **116 setups** detected (61 buy, 55 sell)
- **55.7% win rate** (64 wins, 51 losses)
- Pattern exists and is detectable algorithmically

### v2 Issues (fake SL — fixed in v3)
- SL buffer was 5% of the indication move → $1-3 on Gold → unrealistic
- R:R inflated to 1:20 on many trades
- 116 setups, 55.7% win rate, +728R — all fake due to tiny SL

### v3 Results (ATR-based SL — realistic)
- **ATR(14) on Gold H1: ~$23.62** — this is the average candle range
- SL = ATR * 1.0 below the HL (min $10)
- 23 setups, **30.4% win rate**, avg R:R 1:1.5, **-5.2R total, -0.23R expectancy**
- Strategy is slightly negative with conservative TP (targeting only H1)

### Why v3 loses
1. **TP too conservative**: Targeting H1 (the indication high) captures only a small portion of the move. In the March example, price went from 4404 → 4854 but the TP was at 4475.
2. **30% win rate + 1.5:1 R:R = negative edge**: Need either higher win rate or higher R:R.
3. **No trend filter on higher timeframe**: Many setups fire against the dominant trend.

### Next Steps to Explore
- **Extended TP**: Trail the stop or target 2x-3x the indication move instead of H1
- **Higher TF trend filter**: Only take BUYs when 4H/D1 structure is also bullish
- **Session filter**: Focus on London/NY open where Gold has most volume
- **Minimum structure size**: Skip setups where H1-L1 move is less than 1.5x ATR
- **Chain detection**: When ICC setups chain (each HL becomes next L1), the continuation trades are higher probability

## Backtest Results Summary

| Version | SL Method | TP Method | Setups | Win Rate | Avg R:R | Total P&L | Expectancy |
|---------|-----------|-----------|--------|----------|---------|-----------|------------|
| v2 unrealistic | 5% of move | H1/L1 | 116 | 55.7% | 1:12.2 | +728R | +6.34R (fake) |
| v3 conservative | ATR-based | H1/L1 | 23 | 30.4% | 1:1.5 | -5.2R | -0.23R |
| v3 extended | ATR-based | 1.5x move | 110 | 36.1% | 1:1.8 | +1.0R | +0.01R |

The extended TP version is breakeven. The edge likely comes from human judgment: session timing, higher TF trend alignment, level strength — filters the algorithm doesn't yet apply.

## Prediction History

### Apr 10 Prediction: SELL → CORRECT
- **Called**: Sell setup forming, LH pattern, target 4700-4703
- **Result**: Market gapped down to 4664 on Monday open. TP zone hit.
- **If traded**: Entry ~4790, SL 4810, TP 4703 → WIN at ~4:1 R:R

### Apr 13 Prediction: SELL (forming)

**Current price: ~4726 | ATR: $20.70 | Trend: RANGING (bearish lean)**

```
H1: 4795.10 (Apr 10)
L1: 4664.87 (Apr 13) ← Indication (gap down, $130 drop)
LH: FORMING — bounce to ~4730-4738 (57% retrace so far)
→ Waiting for bounce to stall and confirm LH
```

- **Ideal sell entry zone: $4714 - $4746** (38-62% retrace of the drop)
- **SL: above wherever the LH confirms (~$4770-4790)**
- **TP: $4665 (retest Monday low), then $4600**
- **Kill the sell if**: price closes H1 above 4795

### Key Levels (Apr 13)
- 4795: Last swing high — sell thesis dead above here
- 4746: 62% retrace zone — upper edge of sell entry
- 4730: 50% retrace — current area
- 4714: 38% retrace — lower edge of sell entry
- 4665: Monday gap low — sell TP target
- 4600: Next support below

## Configuration Parameters

| Parameter | v3 Value | Description |
|-----------|----------|-------------|
| `SWING_LOOKBACK` | 3 | Bars on each side to confirm a swing |
| `ATR_PERIOD` | 14 | Period for ATR calculation |
| `SL_ATR_MULT` | 1.0 | SL distance = ATR * multiplier |
| `MIN_SL_DOLLARS` | 10.0 | Absolute minimum SL for Gold |
| `MAX_RR_CAP` | 8.0 | Cap displayed R:R |
| `TP_TARGET` | H1/L1 | Conservative: target the indication level |

## Live Monitor

`icc_monitor.py` — self-learning real-time observer:

- Connects to Deriv WebSocket, analyzes every new H1 candle close
- Detects ICC setups in real-time with confidence scoring (0-100)
- Tracks active setups: monitors SL/TP hit in real-time
- Shows "Watching For" — what patterns are forming and entry zones
- Self-learning: adjusts parameters based on win/loss outcomes
- Persists state to `icc_state.json` (survives restarts)
- Logs all events to `icc_monitor_log.json`

### Confidence Scoring (0-100)
- **Trend alignment** (+20): setup direction matches H1 trend
- **R:R quality** (+15): higher R:R = higher score
- **Correction depth** (+10): 35-65% retrace is ideal
- **Move size** (+10): larger indication moves relative to ATR
- **Historical performance** (+10): similar past setups winning

### Self-Learning Mechanism
After every completed trade, the system analyzes the last 30 trades:
- If win rate < 35%: tightens `min_move_atr` and `correction_depth_max`
- If win rate > 55%: loosens `min_move_atr` to find more setups
- Adjusts `tp_move_mult` based on realized R:R vs target
- All adjustments logged with reasoning

### Running the Monitor
```bash
source .venv/bin/activate
python icc_monitor.py          # Foreground — live output
nohup python icc_monitor.py &  # Background — runs while terminal is closed
```

## Files

| File | Purpose |
|------|---------|
| `icc_poc.py` | v1 — level-break based detection (kept for reference) |
| `icc_poc_v2.py` | v2/v3 — structure-based backtest with ATR SL + extended TP |
| `icc_monitor.py` | Live monitor — real-time ICC detection + self-learning |
| `icc_state.json` | Persisted state: active setups, outcomes, learned params |
| `icc_monitor_log.json` | Event log: all detections, outcomes, learning adjustments |
| `analyze_example.py` | Helper to inspect candle data around specific dates |
| `ICC_STRATEGY.md` | This file — strategy logic, results, predictions |
| `.env` | Deriv API credentials and config |

## What We Learned

1. **v1 (level-break approach) was wrong.** Entering after price breaks above the reaction high is too late — poor R:R, tight stops get hit.
2. **v2 (enter at the correction) matches how a human trader sees ICC.** The HL/LH is the entry, not the breakout.
3. **The pattern exists in real data.** Detectable algorithmically on Gold/USD H1.
4. **SL sizing is critical.** Gold's ATR on H1 is ~$23. Any SL tighter than that gets hit by noise. Realistic SL sizing reduces the R:R dramatically.
5. **The edge is in trade management, not pattern detection.** The pattern detection works. The profitability depends on: (a) how you size the SL, (b) where you set the TP, (c) which setups you filter out.
6. **Conservative TP kills the strategy.** Targeting only H1 captures too little of the move. The real profit comes from extended moves (like 4404 → 4854 in March), which requires trailing stops or projected TPs.
7. **ICC setups chain together in trends.** In strong trends, each HL becomes the next L1. These chain trades are the highest-probability setups — a potential filter.

## Claude Observation Agent — Setup Guide

### What This Is

A scheduled remote Claude agent that runs every 4 hours in Anthropic's cloud. It reads the monitor's state, analyzes outcomes with judgment (not just mechanical parameter tweaks), and updates this strategy doc with learnings over days/weeks. The Python monitor (`icc_monitor.py`) is the **eyes** (data collection, setup detection, paper-trading). Claude is the **brain** (pattern recognition, filter proposals, strategy refinement).

### Prerequisites — Do These First

1. **Initialize git repo and push to GitHub:**
   ```bash
   cd /Users/jimmykeli/personalprojects/trading-exp
   git init
   git add ICC_STRATEGY.md icc_monitor.py icc_poc.py icc_poc_v2.py analyze_example.py app.py
   git add icc_state.json icc_monitor_log.json
   echo ".env" >> .gitignore
   echo "__pycache__/" >> .gitignore
   echo "*.pid" >> .gitignore
   echo "icc_output.log" >> .gitignore
   git add .gitignore
   git commit -m "Initial commit — ICC trading system"
   gh repo create trading-exp --private --source=. --push
   ```

2. **Set up auto-commit of state files** — the monitor updates `icc_state.json` and `icc_monitor_log.json` locally, but the remote agent reads from git. Add this cron to push state every hour:
   ```bash
   # Add to crontab (crontab -e):
   0 * * * * cd /Users/jimmykeli/personalprojects/trading-exp && git add icc_state.json icc_monitor_log.json && git diff --cached --quiet || git commit -m "auto: update monitor state" && git push
   ```
   This ensures the remote agent sees fresh data each run.

3. **Pull strategy updates locally** — when the remote agent updates ICC_STRATEGY.md, pull those changes:
   ```bash
   # Add to crontab, offset by 30 min from the push:
   30 * * * * cd /Users/jimmykeli/personalprojects/trading-exp && git pull --rebase
   ```

### Remote Agent Config

- **Schedule:** Every 4 hours (`0 */4 * * *` UTC)
- **Model:** claude-sonnet-4-6 (cost-efficient, good enough for analysis)
- **Token cost:** ~15-20K tokens/run, ~90-120K/day, ~2.7-3.6M/month
- **Repo:** `https://github.com/jimmykeli/trading-exp` (update with actual URL after creating)
- **Tools needed:** Bash, Read, Write, Edit, Glob, Grep

### Agent Prompt

Use this exact prompt when creating the scheduled agent:

```
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
- Was this a chain trade? (entry_swing price = previous setup's L1)
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
- Don't change parameters until you have 10+ completed trades. Before that, just observe and note.
- When you do propose changes, explain the evidence. Show the numbers.
- If nothing new happened since last run, just note "No new completions. X active setups being tracked." — keep it brief.
- Commit your changes: git add ICC_STRATEGY.md && git commit -m "observation: [brief summary]" && git push
```

### After Creating the Agent

The remote agent will start adding entries to the "Observation Log" section below. Pull changes locally to see them, or check the GitHub repo directly.

## Observation Log

*Claude observation agent entries will appear here. Each entry is timestamped and includes what was observed, patterns found, and filter proposals.*

---

### 2026-04-13 ~18:00 UTC — First observation run

**Status:** No completed trades yet. 1 active setup being tracked.

**Active Setup — SELL @ 4739.82**
- Detected: 2026-04-13 11:20 UTC (London session — good timing)
- H1: 4795.10 → L1: 4664.87 → LH: 4739.82
- Correction depth: 57.6% — within ideal 35-65% range ✓
- Move size: 130.23 (~5.4x ATR of 23.94) — large indication move ✓
- R:R: 1:3.2 — well above 1.5 minimum ✓
- Confidence: 100/100

**Key Flag — H4 Trend Conflict:**
H1 trend = DOWNTREND, H4 trend = UPTREND. The setup is selling into a higher-timeframe uptrend.
This is the exact scenario flagged in "Next Steps to Explore" — no higher-TF filter is active yet.
The Apr 10 prediction was correct (SELL to 4664 worked at ~4:1), but that was a pure gap-down move.
This continuation SELL is entering after the gap, with the H4 still bullish — risk is a reversal back toward 4795+.

**Price behavior since detection (11:20 → 18:00 UTC):**
Predictions show price oscillating 4708-4745 during the period. Setup has not been stopped out (SL 4800.10) and has not hit TP (4544.47). Current price ~4731 is below entry 4739.82 — trade is nominally in profit.

**No pattern conclusions yet** — 0 completed trades. Cannot derive win rates. Observing.

**To watch on resolution:**
- If SELL wins: note whether H4 conflict mattered (it didn't stop the move)
- If SELL loses (SL at 4800): this becomes data point #1 for "H1 vs H4 conflict → loss"
- Correction depth 57.6% is in the middle of the ideal range — neutral data point regardless of outcome
