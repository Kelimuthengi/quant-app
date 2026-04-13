"""
ICC (Indication, Correction, Continuation) — Proof of Concept
Fetches Gold/USD candles from Deriv, detects ICC patterns, reports results.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import websockets
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from dotenv import load_dotenv

load_dotenv()

# --- Config ---
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1")
DERIV_TOKEN = os.getenv("DERIV_API_TOKEN", "")
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
SYMBOL = "frxXAUUSD"  # Gold/USD on Deriv
GRANULARITY = 3600     # 1H candles (seconds)
CANDLE_COUNT = 5000    # Max per request
SWING_LOOKBACK = 3     # Reduced from 5 — Gold is volatile, swings form faster
MERGE_THRESHOLD_RATIO = 0.003  # Widen from 0.2% to 0.3% — Gold has wider S/R zones
ICC_MAX_CORRECTION_BARS = 50   # More time for correction (from 30) — Gold retests can be slow
ICC_MAX_CONTINUATION_BARS = 30 # More time for continuation (from 20)
INVALIDATION_RATIO = 0.7       # More lenient (from 0.5) — allow deeper corrections (liquidity sweeps)
CORRECTION_TOLERANCE = 0.003   # 0.3% zone around level (was 0.1% hardcoded)
MIN_RR = 1.5                   # Minimum R:R to take a trade
TP_MULTIPLIER = 3.0            # If no clear next level, target 3:1 R:R (was 2:1)


# --- Data Structures ---
@dataclass
class Candle:
    epoch: int
    open: float
    high: float
    low: float
    close: float
    index: int = 0

    @property
    def time(self):
        return datetime.fromtimestamp(self.epoch, tz=timezone.utc)

    @property
    def body(self):
        return abs(self.close - self.open)

    @property
    def is_bullish(self):
        return self.close > self.open


@dataclass
class SwingPoint:
    index: int
    price: float
    swing_type: str  # "HIGH" or "LOW"
    classification: str = ""  # "HH", "HL", "LH", "LL"


@dataclass
class Level:
    price: float
    level_type: str  # "RESISTANCE" or "SUPPORT"
    strength: int = 1
    swing_indices: list = field(default_factory=list)


@dataclass
class ICCSetup:
    direction: str  # "BUY" or "SELL"
    level: Level
    indication_idx: int
    correction_idx: int
    continuation_idx: int
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    outcome: str = ""  # "WIN", "LOSS", "OPEN"
    pnl_rr: float = 0.0


# --- Step 1: Fetch candles from Deriv ---
async def fetch_candles():
    print(f"Connecting to Deriv API...")
    async with websockets.connect(WS_URL) as ws:
        # Fetch candles (public endpoint, no auth needed)
        req = {
            "ticks_history": SYMBOL,
            "adjust_start_time": 1,
            "count": CANDLE_COUNT,
            "end": "latest",
            "granularity": GRANULARITY,
            "style": "candles"
        }
        await ws.send(json.dumps(req))
        resp = json.loads(await ws.recv())

        if "error" in resp:
            print(f"API error: {resp['error']['message']}")
            return []

        raw_candles = resp.get("candles", [])
        print(f"Fetched {len(raw_candles)} candles for {SYMBOL} (H1)")

        candles = []
        for i, c in enumerate(raw_candles):
            candles.append(Candle(
                epoch=c["epoch"],
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                index=i
            ))
        return candles


# --- Step 2: Detect swing points ---
def detect_swings(candles, lookback=SWING_LOOKBACK):
    swings = []
    for i in range(lookback, len(candles) - lookback):
        high_i = candles[i].high
        low_i = candles[i].low

        is_swing_high = all(
            high_i > candles[j].high
            for j in range(i - lookback, i + lookback + 1)
            if j != i
        )
        is_swing_low = all(
            low_i < candles[j].low
            for j in range(i - lookback, i + lookback + 1)
            if j != i
        )

        if is_swing_high:
            swings.append(SwingPoint(index=i, price=high_i, swing_type="HIGH"))
        if is_swing_low:
            swings.append(SwingPoint(index=i, price=low_i, swing_type="LOW"))

    return sorted(swings, key=lambda s: s.index)


# --- Step 3: Classify market structure (HH/HL/LH/LL) ---
def classify_structure(swings):
    prev_high = None
    prev_low = None

    for s in swings:
        if s.swing_type == "HIGH":
            if prev_high is not None:
                s.classification = "HH" if s.price > prev_high.price else "LH"
            prev_high = s
        elif s.swing_type == "LOW":
            if prev_low is not None:
                s.classification = "HL" if s.price > prev_low.price else "LL"
            prev_low = s

    return swings


# --- Step 4: Identify support/resistance levels ---
def find_levels(swings, merge_threshold_ratio=MERGE_THRESHOLD_RATIO):
    raw_levels = []
    for s in swings:
        lt = "RESISTANCE" if s.swing_type == "HIGH" else "SUPPORT"
        raw_levels.append(Level(price=s.price, level_type=lt, swing_indices=[s.index]))

    # Merge nearby levels
    raw_levels.sort(key=lambda l: l.price)
    merged = []
    for level in raw_levels:
        if merged:
            threshold = merged[-1].price * merge_threshold_ratio
            if abs(level.price - merged[-1].price) < threshold:
                merged[-1].strength += 1
                merged[-1].swing_indices.extend(level.swing_indices)
                # Weighted average price
                merged[-1].price = (merged[-1].price + level.price) / 2
                continue
        merged.append(level)

    return merged


# --- Step 5: Detect ICC patterns (improved) ---
def detect_icc_setups(candles, swings, levels):
    """
    Improved ICC detection:
    - Wider correction tolerance zone (CORRECTION_TOLERANCE)
    - Finds ALL setups per level, not just the first
    - Better entry: enter at the close of the continuation candle
    - SL placed at the lowest point of the correction (not just below the level)
    - TP uses risk multiple if next level is too close
    - Filters by MIN_RR
    """
    setups = []

    # Pre-compute trend at each candle index using classified swings
    def get_trend_at(idx):
        recent = [s for s in swings if s.index < idx and s.classification]
        recent = recent[-6:]  # Last 6 classified swings
        hh = sum(1 for s in recent if s.classification == "HH")
        hl = sum(1 for s in recent if s.classification == "HL")
        lh = sum(1 for s in recent if s.classification == "LH")
        ll = sum(1 for s in recent if s.classification == "LL")
        if hh >= 1 and hl >= 1:
            return "UPTREND"
        elif lh >= 1 and ll >= 1:
            return "DOWNTREND"
        return "RANGING"

    for level in levels:
        level_price = level.price
        last_swing_idx = max(level.swing_indices) if level.swing_indices else 0

        # --- BUY SETUPS: break above resistance ---
        if level.level_type == "RESISTANCE":
            # Scan for ALL indications at this level (not just the first)
            i = last_swing_idx + 1
            while i < len(candles):
                # Find INDICATION: candle closes above resistance
                if candles[i].close <= level_price:
                    i += 1
                    continue

                # Verify it's a real break — prior candles were below
                prior_below = any(
                    candles[j].close < level_price
                    for j in range(max(0, i - 10), i)
                )
                if not prior_below:
                    i += 1
                    continue

                # Check trend alignment — prefer buying in uptrend or ranging
                trend = get_trend_at(i)
                if trend == "DOWNTREND":
                    i += 1
                    continue

                indication_idx = i
                indication_high = candles[i].high

                # Track the reaction high from indication onward
                reaction_high = indication_high

                # Find CORRECTION: price pulls back to or near the broken level
                correction_idx = None
                correction_low = float('inf')
                search_end = min(indication_idx + ICC_MAX_CORRECTION_BARS + 1, len(candles))

                for j in range(indication_idx + 1, search_end):
                    reaction_high = max(reaction_high, candles[j].high)

                    # Wider correction zone
                    tolerance = level_price * CORRECTION_TOLERANCE
                    if candles[j].low <= level_price + tolerance:
                        # Find the deepest point of the correction
                        correction_idx = j
                        # Keep scanning for the actual correction low
                        for jj in range(j, min(j + 10, search_end)):
                            if candles[jj].low < correction_low:
                                correction_low = candles[jj].low
                                correction_idx = jj
                            # If price starts moving back up strongly, correction is done
                            if candles[jj].close > level_price + tolerance * 2:
                                break
                        break

                if correction_idx is None:
                    i = indication_idx + 1
                    continue

                # Invalidation check
                indication_move = reaction_high - level_price
                depth = level_price - correction_low
                if indication_move > 0 and depth / indication_move > INVALIDATION_RATIO:
                    i = indication_idx + 1
                    continue

                # Re-compute reaction high up to correction
                reaction_high = max(candles[k].high for k in range(indication_idx, correction_idx + 1))

                # Find CONTINUATION: price bounces and closes above reaction high
                continuation_idx = None
                cont_end = min(correction_idx + ICC_MAX_CONTINUATION_BARS + 1, len(candles))
                for k in range(correction_idx + 1, cont_end):
                    if candles[k].close > reaction_high:
                        continuation_idx = k
                        break
                    # Invalidation: drops well below the level
                    if candles[k].close < correction_low:
                        break

                if continuation_idx is None:
                    i = indication_idx + 1
                    continue

                # --- Better entry/SL/TP calculation ---
                entry = candles[continuation_idx].close

                # SL at the correction low (where liquidity was swept) with small buffer
                sl_buffer = level_price * 0.0005  # 0.05% buffer below correction low
                sl = correction_low - sl_buffer

                risk = entry - sl

                # TP: find next resistance above, but ensure minimum R:R
                next_resistance = None
                for lv in sorted(levels, key=lambda l: l.price):
                    if lv.price > entry and (lv.price - entry) >= risk * MIN_RR:
                        next_resistance = lv.price
                        break

                # If no good level found, use risk multiple
                if next_resistance is None:
                    next_resistance = entry + risk * TP_MULTIPLIER

                reward = next_resistance - entry
                rr = reward / risk if risk > 0 else 0

                # Filter: skip if R:R too low
                if rr < MIN_RR:
                    i = continuation_idx + 1
                    continue

                setup = ICCSetup(
                    direction="BUY",
                    level=level,
                    indication_idx=indication_idx,
                    correction_idx=correction_idx,
                    continuation_idx=continuation_idx,
                    entry_price=entry,
                    stop_loss=sl,
                    take_profit=next_resistance,
                    risk_reward=rr,
                )
                setups.append(setup)

                # Skip past this setup to find the next one at this level
                i = continuation_idx + 1
                continue

                i += 1

        # --- SELL SETUPS: break below support ---
        elif level.level_type == "SUPPORT":
            i = last_swing_idx + 1
            while i < len(candles):
                if candles[i].close >= level_price:
                    i += 1
                    continue

                prior_above = any(
                    candles[j].close > level_price
                    for j in range(max(0, i - 10), i)
                )
                if not prior_above:
                    i += 1
                    continue

                trend = get_trend_at(i)
                if trend == "UPTREND":
                    i += 1
                    continue

                indication_idx = i
                reaction_low = candles[i].low

                # Find CORRECTION: price pulls back up to the broken support
                correction_idx = None
                correction_high = float('-inf')
                search_end = min(indication_idx + ICC_MAX_CORRECTION_BARS + 1, len(candles))

                for j in range(indication_idx + 1, search_end):
                    reaction_low = min(reaction_low, candles[j].low)
                    tolerance = level_price * CORRECTION_TOLERANCE
                    if candles[j].high >= level_price - tolerance:
                        correction_idx = j
                        for jj in range(j, min(j + 10, search_end)):
                            if candles[jj].high > correction_high:
                                correction_high = candles[jj].high
                                correction_idx = jj
                            if candles[jj].close < level_price - tolerance * 2:
                                break
                        break

                if correction_idx is None:
                    i = indication_idx + 1
                    continue

                # Invalidation check
                indication_move = level_price - reaction_low
                depth = correction_high - level_price
                if indication_move > 0 and depth / indication_move > INVALIDATION_RATIO:
                    i = indication_idx + 1
                    continue

                reaction_low = min(candles[k].low for k in range(indication_idx, correction_idx + 1))

                # Find CONTINUATION
                continuation_idx = None
                cont_end = min(correction_idx + ICC_MAX_CONTINUATION_BARS + 1, len(candles))
                for k in range(correction_idx + 1, cont_end):
                    if candles[k].close < reaction_low:
                        continuation_idx = k
                        break
                    if candles[k].close > correction_high:
                        break

                if continuation_idx is None:
                    i = indication_idx + 1
                    continue

                entry = candles[continuation_idx].close
                sl_buffer = level_price * 0.0005
                sl = correction_high + sl_buffer

                risk = sl - entry

                # TP: find next support below
                next_support = None
                for lv in sorted(levels, key=lambda l: l.price, reverse=True):
                    if lv.price < entry and (entry - lv.price) >= risk * MIN_RR:
                        next_support = lv.price
                        break
                if next_support is None:
                    next_support = entry - risk * TP_MULTIPLIER

                reward = entry - next_support
                rr = reward / risk if risk > 0 else 0

                if rr < MIN_RR:
                    i = continuation_idx + 1
                    continue

                setup = ICCSetup(
                    direction="SELL",
                    level=level,
                    indication_idx=indication_idx,
                    correction_idx=correction_idx,
                    continuation_idx=continuation_idx,
                    entry_price=entry,
                    stop_loss=sl,
                    take_profit=next_support,
                    risk_reward=rr,
                )
                setups.append(setup)

                i = continuation_idx + 1
                continue

                i += 1

    # Sort setups chronologically by continuation (entry) time
    setups.sort(key=lambda s: s.continuation_idx)
    return setups


# --- Step 6: Simulate outcomes ---
def simulate_outcomes(candles, setups):
    for setup in setups:
        entry = setup.entry_price
        sl = setup.stop_loss
        tp = setup.take_profit

        # Walk forward from continuation candle
        for i in range(setup.continuation_idx + 1, len(candles)):
            c = candles[i]
            if setup.direction == "BUY":
                if c.low <= sl:
                    setup.outcome = "LOSS"
                    setup.pnl_rr = -1.0
                    break
                if c.high >= tp:
                    setup.outcome = "WIN"
                    setup.pnl_rr = setup.risk_reward
                    break
            else:  # SELL
                if c.high >= sl:
                    setup.outcome = "LOSS"
                    setup.pnl_rr = -1.0
                    break
                if c.low <= tp:
                    setup.outcome = "WIN"
                    setup.pnl_rr = setup.risk_reward
                    break
        else:
            setup.outcome = "OPEN"
            setup.pnl_rr = 0.0

    return setups


# --- Step 7: Print results ---
def print_results(candles, swings, levels, setups):
    print("\n" + "=" * 70)
    print("ICC PATTERN DETECTION — PROOF OF CONCEPT")
    print(f"Symbol: Gold/USD | Timeframe: H1 | Candles: {len(candles)}")
    if candles:
        print(f"Period: {candles[0].time.strftime('%Y-%m-%d')} to {candles[-1].time.strftime('%Y-%m-%d')}")
    print("=" * 70)

    print(f"\n--- Market Structure ---")
    print(f"Swing points detected: {len(swings)}")
    hh = sum(1 for s in swings if s.classification == "HH")
    hl = sum(1 for s in swings if s.classification == "HL")
    lh = sum(1 for s in swings if s.classification == "LH")
    ll = sum(1 for s in swings if s.classification == "LL")
    print(f"  HH: {hh} | HL: {hl} | LH: {lh} | LL: {ll}")
    print(f"S/R levels (after merging): {len(levels)}")

    print(f"\n--- ICC Setups Found: {len(setups)} ---")
    if not setups:
        print("No ICC patterns detected in this data.")
        return

    buys = [s for s in setups if s.direction == "BUY"]
    sells = [s for s in setups if s.direction == "SELL"]
    print(f"  BUY setups: {buys and len(buys) or 0}")
    print(f"  SELL setups: {sells and len(sells) or 0}")

    print(f"\n--- Setup Details ---")
    for i, s in enumerate(setups):
        c_ind = candles[s.indication_idx]
        c_cor = candles[s.correction_idx]
        c_con = candles[s.continuation_idx]
        print(f"\n  Setup #{i+1} [{s.direction}] — {s.outcome}")
        print(f"    Level: {s.level.price:.2f} ({s.level.level_type}, strength {s.level.strength})")
        print(f"    Indication:   candle {s.indication_idx} ({c_ind.time.strftime('%Y-%m-%d %H:%M')})")
        print(f"    Correction:   candle {s.correction_idx} ({c_cor.time.strftime('%Y-%m-%d %H:%M')})")
        print(f"    Continuation: candle {s.continuation_idx} ({c_con.time.strftime('%Y-%m-%d %H:%M')})")
        print(f"    Entry: {s.entry_price:.2f} | SL: {s.stop_loss:.2f} | TP: {s.take_profit:.2f}")
        print(f"    R:R = 1:{s.risk_reward:.1f} | P&L = {s.pnl_rr:+.1f}R")

    # Summary stats
    closed = [s for s in setups if s.outcome in ("WIN", "LOSS")]
    if closed:
        wins = sum(1 for s in closed if s.outcome == "WIN")
        losses = sum(1 for s in closed if s.outcome == "LOSS")
        win_rate = wins / len(closed) * 100
        total_rr = sum(s.pnl_rr for s in closed)
        avg_rr_win = sum(s.risk_reward for s in closed if s.outcome == "WIN") / wins if wins else 0
        print(f"\n--- Performance Summary ---")
        print(f"  Closed trades: {len(closed)}")
        print(f"  Wins: {wins} | Losses: {losses}")
        print(f"  Win rate: {win_rate:.1f}%")
        print(f"  Avg R:R on wins: 1:{avg_rr_win:.1f}")
        print(f"  Total P&L: {total_rr:+.1f}R")
        print(f"  Expectancy per trade: {total_rr/len(closed):+.2f}R")
    else:
        print(f"\n  No closed trades to evaluate.")

    still_open = [s for s in setups if s.outcome == "OPEN"]
    if still_open:
        print(f"  Still open: {len(still_open)}")


# --- Step 8: Generate chart ---
def generate_chart(candles, swings, setups, filename="icc_results.png"):
    if not candles:
        return

    fig, ax = plt.subplots(figsize=(24, 10))

    # Plot candlesticks manually
    times = list(range(len(candles)))
    for i, c in enumerate(candles):
        color = '#26a69a' if c.is_bullish else '#ef5350'
        # Wick
        ax.plot([i, i], [c.low, c.high], color=color, linewidth=0.5)
        # Body
        body_bottom = min(c.open, c.close)
        body_height = max(c.body, 0.01)
        ax.bar(i, body_height, bottom=body_bottom, width=0.6, color=color, edgecolor=color)

    # Mark swing points
    for s in swings:
        if s.swing_type == "HIGH":
            ax.annotate(s.classification or "H", (s.index, s.price),
                       textcoords="offset points", xytext=(0, 10),
                       fontsize=6, ha='center', color='blue', alpha=0.6)
        else:
            ax.annotate(s.classification or "L", (s.index, s.price),
                       textcoords="offset points", xytext=(0, -12),
                       fontsize=6, ha='center', color='orange', alpha=0.6)

    # Mark ICC setups
    for i, setup in enumerate(setups):
        color = '#4CAF50' if setup.direction == "BUY" else '#F44336'
        outcome_color = '#4CAF50' if setup.outcome == "WIN" else '#F44336' if setup.outcome == "LOSS" else '#FFC107'

        # Mark indication
        ax.axvline(x=setup.indication_idx, color='blue', linestyle=':', alpha=0.3, linewidth=0.8)
        # Mark correction
        ax.axvline(x=setup.correction_idx, color='orange', linestyle=':', alpha=0.3, linewidth=0.8)
        # Mark continuation (entry)
        ax.annotate(f"#{i+1} {setup.direction}\n{setup.outcome}\nR:R 1:{setup.risk_reward:.1f}",
                    (setup.continuation_idx, setup.entry_price),
                    textcoords="offset points", xytext=(15, 15 if setup.direction == "BUY" else -25),
                    fontsize=7, ha='left', color=outcome_color, fontweight='bold',
                    arrowprops=dict(arrowstyle='->', color=outcome_color, lw=1.5))

        # Draw SL and TP lines
        x_start = setup.continuation_idx
        x_end = min(setup.continuation_idx + 50, len(candles) - 1)
        ax.hlines(y=setup.entry_price, xmin=x_start, xmax=x_end, colors=color, linestyles='-', linewidth=1, alpha=0.7)
        ax.hlines(y=setup.stop_loss, xmin=x_start, xmax=x_end, colors='red', linestyles='--', linewidth=0.8, alpha=0.5)
        ax.hlines(y=setup.take_profit, xmin=x_start, xmax=x_end, colors='green', linestyles='--', linewidth=0.8, alpha=0.5)

    # Shade the region for each ICC setup
    for setup in setups:
        shade_color = '#4CAF5020' if setup.direction == "BUY" else '#F4433620'
        ax.axvspan(setup.indication_idx, setup.continuation_idx, alpha=0.05,
                  color='green' if setup.direction == "BUY" else 'red')

    ax.set_title(f"ICC Pattern Detection — Gold/USD H1 — {len(setups)} setups found", fontsize=14)
    ax.set_xlabel("Candle Index")
    ax.set_ylabel("Price (USD)")
    ax.grid(True, alpha=0.2)

    # Legend
    legend_elements = [
        mpatches.Patch(color='blue', alpha=0.3, label='Indication'),
        mpatches.Patch(color='orange', alpha=0.3, label='Correction'),
        mpatches.Patch(color='green', alpha=0.5, label='Entry / TP'),
        mpatches.Patch(color='red', alpha=0.5, label='Stop Loss'),
    ]
    ax.legend(handles=legend_elements, loc='upper left', fontsize=8)

    plt.tight_layout()
    filepath = Path("/Users/jimmykeli/personalprojects/trading-exp") / filename
    plt.savefig(filepath, dpi=150)
    plt.close()
    print(f"\nChart saved to: {filepath}")


# --- Main ---
async def main():
    candles = await fetch_candles()
    if not candles:
        print("Failed to fetch candles. Check your Deriv API token.")
        return

    print(f"\nDetecting swing points (lookback={SWING_LOOKBACK})...")
    swings = detect_swings(candles)
    swings = classify_structure(swings)

    print(f"Finding S/R levels...")
    levels = find_levels(swings)

    print(f"Scanning for ICC patterns...")
    setups = detect_icc_setups(candles, swings, levels)

    print(f"Simulating trade outcomes...")
    setups = simulate_outcomes(candles, setups)

    print_results(candles, swings, levels, setups)
    generate_chart(candles, swings, setups)


if __name__ == "__main__":
    asyncio.run(main())
