"""
ICC (Indication, Correction, Continuation) — v2
Based on the user's actual trading logic:

BUY:
  1. Price makes a LOW (L1)
  2. Price makes a HIGH (H1) — INDICATION (shows buyers in control)
  3. Price pulls back but stays above L1 — CORRECTION (Higher Low forming)
  4. ENTER at the Higher Low zone — CONTINUATION expected
  5. SL just below the HL | TP at H1 or next resistance

SELL:
  Same logic flipped — LH structure, enter at the Lower High.
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import websockets
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from dotenv import load_dotenv

load_dotenv()

# --- Config ---
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1")
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
SYMBOL = "frxXAUUSD"
GRANULARITY = 3600     # H1
CANDLE_COUNT = 5000
SWING_LOOKBACK = 3     # Bars each side to confirm a swing
ATR_PERIOD = 14        # Period for ATR calculation (SL sizing)
SL_ATR_MULT = 1.0      # SL = HL price - (ATR * this multiplier)
MIN_SL_DOLLARS = 10.0  # Absolute minimum SL distance in USD for Gold
MAX_RR_CAP = 8.0       # Cap R:R display/sim at this (above = likely noise)
TP_MODE = "extended"   # "conservative" = H1/L1, "extended" = project move + trail
TP_MOVE_MULT = 1.5     # TP = entry + (H1-L1) * multiplier (extended mode)
TRAILING_STOP = False  # Disabled — trailing was cutting winners too short
TRAIL_ATR_MULT = 1.5   # Trail distance = ATR * multiplier
MIN_MOVE_ATR = 1.5     # Only take setups where H1-L1 >= ATR * this (filter noise)


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
    def is_bullish(self):
        return self.close > self.open


@dataclass
class SwingPoint:
    index: int
    price: float
    swing_type: str       # "HIGH" or "LOW"
    classification: str = ""  # "HH", "HL", "LH", "LL"


@dataclass
class ICCSetup:
    direction: str          # "BUY" or "SELL"
    # The three swing points that form the pattern
    swing_l1_idx: int       # First low (BUY) or first high (SELL)
    swing_h1_idx: int       # Indication — the HH (BUY) or LL (SELL)
    swing_entry_idx: int    # Correction/Entry — the HL (BUY) or LH (SELL)
    # Prices
    l1_price: float
    h1_price: float
    entry_zone_price: float
    # Trade params
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    # Outcome
    outcome: str = ""       # "WIN", "LOSS", "OPEN"
    pnl_rr: float = 0.0
    exit_idx: int = -1


# --- Fetch candles ---
async def fetch_candles():
    print(f"Connecting to Deriv API...")
    async with websockets.connect(WS_URL) as ws:
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
        raw = resp.get("candles", [])
        print(f"Fetched {len(raw)} candles for {SYMBOL} (H1)")
        return [Candle(
            epoch=c["epoch"],
            open=float(c["open"]),
            high=float(c["high"]),
            low=float(c["low"]),
            close=float(c["close"]),
            index=i
        ) for i, c in enumerate(raw)]


# --- ATR calculation ---
def compute_atr(candles, period=ATR_PERIOD):
    """Compute ATR at each candle index. Returns a list same length as candles."""
    atrs = [0.0] * len(candles)
    for i in range(1, len(candles)):
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )
        if i < period:
            # Simple average for warmup
            atrs[i] = sum(
                max(candles[j].high - candles[j].low,
                    abs(candles[j].high - candles[j - 1].close) if j > 0 else 0,
                    abs(candles[j].low - candles[j - 1].close) if j > 0 else 0)
                for j in range(1, i + 1)
            ) / i
        else:
            atrs[i] = (atrs[i - 1] * (period - 1) + tr) / period
    return atrs


# --- Detect swing points ---
def detect_swings(candles, lookback=SWING_LOOKBACK):
    swings = []
    for i in range(lookback, len(candles) - lookback):
        h = candles[i].high
        lo = candles[i].low
        is_sh = all(h > candles[j].high for j in range(i - lookback, i + lookback + 1) if j != i)
        is_sl = all(lo < candles[j].low for j in range(i - lookback, i + lookback + 1) if j != i)
        if is_sh:
            swings.append(SwingPoint(index=i, price=h, swing_type="HIGH"))
        if is_sl:
            swings.append(SwingPoint(index=i, price=lo, swing_type="LOW"))
    return sorted(swings, key=lambda s: s.index)


# --- Classify HH/HL/LH/LL ---
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


# --- Detect ICC setups based on market structure ---
def detect_icc_setups(candles, swings, atrs):
    """
    ICC BUY setup:
      Find sequence: swing_low (L1) → swing_high (H1, must be HH) → swing_low (HL, must be > L1)
      The HL is the ENTRY ZONE.
      Entry: at the HL price (or when price reaches HL zone and bounces)
      SL: just below the HL
      TP: at H1 (the indication high) or extended

    ICC SELL setup:
      Find sequence: swing_high (H1) → swing_low (L1, must be LL) → swing_high (LH, must be < H1)
      The LH is the ENTRY ZONE.
      Entry: at the LH price
      SL: just above the LH
      TP: at L1 (the indication low) or extended
    """
    setups = []

    # Separate swings by type
    highs = [s for s in swings if s.swing_type == "HIGH"]
    lows = [s for s in swings if s.swing_type == "LOW"]

    # --- BUY SETUPS ---
    # Look for: L1 → H1 (HH) → HL (entry)
    for i in range(len(lows) - 1):
        l1 = lows[i]
        hl = lows[i + 1]

        # HL must be a Higher Low
        if hl.classification != "HL":
            continue

        # Find the swing high between L1 and HL (the indication)
        h1 = None
        for h in highs:
            if l1.index < h.index < hl.index:
                h1 = h

        if h1 is None:
            continue

        # Verify H1 is a Higher High (strong indication of uptrend)
        # Allow HH or first swing high (no classification yet)
        if h1.classification not in ("HH", ""):
            continue

        # The HL must not go below L1 (this is the key ICC rule)
        if hl.price <= l1.price:
            continue

        # Filter: indication move must be significant (not noise)
        move_size = h1.price - l1.price
        atr_at_h1 = atrs[h1.index] if h1.index < len(atrs) else MIN_SL_DOLLARS
        if move_size < atr_at_h1 * MIN_MOVE_ATR:
            continue

        # --- Calculate trade parameters ---
        entry_zone = hl.price  # Enter at the HL

        # SL: use ATR-based distance, with a minimum floor
        atr_at_entry = atrs[hl.index] if hl.index < len(atrs) else MIN_SL_DOLLARS
        sl_distance = max(atr_at_entry * SL_ATR_MULT, MIN_SL_DOLLARS)
        sl = hl.price - sl_distance

        # SL must not be above L1 (otherwise invalidation makes no sense)
        # But also not too far — if HL is very close to L1, the SL should be below L1
        if sl > l1.price:
            sl = l1.price - (MIN_SL_DOLLARS * 0.5)

        # TP calculation
        if TP_MODE == "extended":
            # Project the move: TP = entry + (H1 - L1) * multiplier
            move_size = h1.price - l1.price
            tp = entry_zone + move_size * TP_MOVE_MULT
        else:
            tp = h1.price  # Conservative: just target H1

        risk = entry_zone - sl
        if risk <= 0:
            continue
        reward = tp - entry_zone
        rr = reward / risk if risk > 0 else 0
        if rr > MAX_RR_CAP:
            rr = MAX_RR_CAP
        if rr < 1.0:
            continue

        # Simulate realistic entry: HL confirmed SWING_LOOKBACK bars later
        confirmation_idx = hl.index + SWING_LOOKBACK
        actual_entry_idx = None
        actual_entry_price = None

        # Entry zone: from HL price up to 20% of the move above HL
        entry_zone_top = hl.price + (h1.price - hl.price) * 0.20

        for k in range(confirmation_idx, min(confirmation_idx + 30, len(candles))):
            if candles[k].low <= entry_zone_top:
                # Realistic entry: you get filled at the open of this candle
                # or at the entry zone, whichever is worse for you
                actual_entry_price = max(candles[k].open, entry_zone)
                actual_entry_idx = k
                break

        if actual_entry_idx is None:
            continue  # Price ran away — no fill, skip this setup

        # Recalc with actual entry
        risk = actual_entry_price - sl
        if risk <= 0:
            continue
        reward = tp - actual_entry_price
        rr = reward / risk if risk > 0 else 0
        if rr > MAX_RR_CAP:
            rr = MAX_RR_CAP
        if rr < 1.0:
            continue

        setup = ICCSetup(
            direction="BUY",
            swing_l1_idx=l1.index,
            swing_h1_idx=h1.index,
            swing_entry_idx=hl.index,
            l1_price=l1.price,
            h1_price=h1.price,
            entry_zone_price=hl.price,
            entry_price=actual_entry_price,
            stop_loss=sl,
            take_profit=tp,
            risk_reward=rr,
        )
        setups.append(setup)

    # --- SELL SETUPS ---
    # Look for: H1 → L1 (LL) → LH (entry)
    for i in range(len(highs) - 1):
        h1 = highs[i]
        lh = highs[i + 1]

        # LH must be a Lower High
        if lh.classification != "LH":
            continue

        # Find the swing low between H1 and LH (the indication)
        l1 = None
        for lo in lows:
            if h1.index < lo.index < lh.index:
                l1 = lo

        if l1 is None:
            continue

        # L1 should be LL (strong downtrend indication)
        if l1.classification not in ("LL", ""):
            continue

        # LH must not go above H1
        if lh.price >= h1.price:
            continue

        # Filter: indication move must be significant
        move_size = h1.price - l1.price
        atr_at_l1 = atrs[l1.index] if l1.index < len(atrs) else MIN_SL_DOLLARS
        if move_size < atr_at_l1 * MIN_MOVE_ATR:
            continue

        # --- Calculate trade parameters ---
        entry_zone = lh.price

        # SL: ATR-based with minimum floor
        atr_at_entry = atrs[lh.index] if lh.index < len(atrs) else MIN_SL_DOLLARS
        sl_distance = max(atr_at_entry * SL_ATR_MULT, MIN_SL_DOLLARS)
        sl = lh.price + sl_distance

        # SL must not be below H1
        if sl < h1.price:
            sl = h1.price + (MIN_SL_DOLLARS * 0.5)

        # TP calculation
        if TP_MODE == "extended":
            move_size = h1.price - l1.price
            tp = entry_zone - move_size * TP_MOVE_MULT
        else:
            tp = l1.price  # Conservative

        risk = sl - entry_zone
        if risk <= 0:
            continue
        reward = entry_zone - tp
        rr = reward / risk if risk > 0 else 0
        if rr > MAX_RR_CAP:
            rr = MAX_RR_CAP
        if rr < 1.0:
            continue

        confirmation_idx = lh.index + SWING_LOOKBACK
        actual_entry_idx = None
        actual_entry_price = None
        entry_zone_bottom = lh.price - (lh.price - l1.price) * 0.20

        for k in range(confirmation_idx, min(confirmation_idx + 30, len(candles))):
            if candles[k].high >= entry_zone_bottom:
                actual_entry_price = min(candles[k].open, entry_zone)
                actual_entry_idx = k
                break

        if actual_entry_idx is None:
            continue  # Price ran away — no fill

        risk = sl - actual_entry_price
        if risk <= 0:
            continue
        reward = actual_entry_price - tp
        rr = reward / risk if risk > 0 else 0
        if rr > MAX_RR_CAP:
            rr = MAX_RR_CAP
        if rr < 1.0:
            continue

        setup = ICCSetup(
            direction="SELL",
            swing_l1_idx=l1.index,
            swing_h1_idx=h1.index,
            swing_entry_idx=lh.index,
            l1_price=l1.price,
            h1_price=h1.price,
            entry_zone_price=lh.price,
            entry_price=actual_entry_price,
            stop_loss=sl,
            take_profit=tp,
            risk_reward=rr,
        )
        setups.append(setup)

    setups.sort(key=lambda s: s.swing_entry_idx)
    return setups


# --- Simulate outcomes (with optional trailing stop) ---
def simulate_outcomes(candles, setups, atrs):
    for setup in setups:
        start_idx = setup.swing_entry_idx + 1
        entry = setup.entry_price
        sl = setup.stop_loss
        tp = setup.take_profit
        risk = abs(entry - sl)
        trailing_active = False
        best_price = entry

        for i in range(start_idx, len(candles)):
            c = candles[i]
            atr_here = atrs[i] if i < len(atrs) else 20.0
            trail_dist = atr_here * TRAIL_ATR_MULT

            if setup.direction == "BUY":
                # Update best price (highest reached)
                if c.high > best_price:
                    best_price = c.high

                # Activate trailing after 1:1 R:R reached
                if TRAILING_STOP and best_price >= entry + risk:
                    trailing_active = True
                    # Trail SL: best_price - trail_distance, but never below original SL
                    trail_sl = best_price - trail_dist
                    if trail_sl > sl:
                        sl = trail_sl

                # Check SL
                if c.low <= sl:
                    if trailing_active:
                        # Trailed out — calculate actual P&L
                        exit_price = sl
                        setup.pnl_rr = (exit_price - entry) / risk
                        setup.outcome = "WIN" if setup.pnl_rr > 0 else "LOSS"
                    else:
                        setup.outcome = "LOSS"
                        setup.pnl_rr = -1.0
                    setup.exit_idx = i
                    break

                # Check TP
                if c.high >= tp:
                    setup.outcome = "WIN"
                    setup.pnl_rr = (tp - entry) / risk
                    setup.exit_idx = i
                    break

            else:  # SELL
                if c.low < best_price:
                    best_price = c.low

                if TRAILING_STOP and best_price <= entry - risk:
                    trailing_active = True
                    trail_sl = best_price + trail_dist
                    if trail_sl < sl:
                        sl = trail_sl

                if c.high >= sl:
                    if trailing_active:
                        exit_price = sl
                        setup.pnl_rr = (entry - exit_price) / risk
                        setup.outcome = "WIN" if setup.pnl_rr > 0 else "LOSS"
                    else:
                        setup.outcome = "LOSS"
                        setup.pnl_rr = -1.0
                    setup.exit_idx = i
                    break

                if c.low <= tp:
                    setup.outcome = "WIN"
                    setup.pnl_rr = (entry - tp) / risk
                    setup.exit_idx = i
                    break
        else:
            setup.outcome = "OPEN"

        # Update the R:R to reflect actual result
        if setup.outcome == "WIN" and setup.pnl_rr > 0:
            setup.risk_reward = setup.pnl_rr

    return setups


# --- Print results ---
def print_results(candles, swings, setups):
    print("\n" + "=" * 70)
    print("ICC v2 — STRUCTURE-BASED ENTRY AT CORRECTION")
    print(f"Symbol: Gold/USD | TF: H1 | Candles: {len(candles)}")
    if candles:
        print(f"Period: {candles[0].time.strftime('%Y-%m-%d')} to {candles[-1].time.strftime('%Y-%m-%d')}")
    print("=" * 70)

    print(f"\nSwing points: {len(swings)}")
    hh = sum(1 for s in swings if s.classification == "HH")
    hl = sum(1 for s in swings if s.classification == "HL")
    lh = sum(1 for s in swings if s.classification == "LH")
    ll = sum(1 for s in swings if s.classification == "LL")
    print(f"  HH: {hh} | HL: {hl} | LH: {lh} | LL: {ll}")

    print(f"\n--- ICC Setups: {len(setups)} ---")
    buys = [s for s in setups if s.direction == "BUY"]
    sells = [s for s in setups if s.direction == "SELL"]
    print(f"  BUY: {len(buys)} | SELL: {len(sells)}")

    for i, s in enumerate(setups):
        c_l1 = candles[s.swing_l1_idx]
        c_h1 = candles[s.swing_h1_idx]
        c_entry = candles[s.swing_entry_idx]
        print(f"\n  #{i+1} [{s.direction}] — {s.outcome}")
        if s.direction == "BUY":
            print(f"    L1 (low):        {s.l1_price:.2f}  ({c_l1.time.strftime('%b %d %H:%M')})")
            print(f"    H1 (indication): {s.h1_price:.2f}  ({c_h1.time.strftime('%b %d %H:%M')})")
            print(f"    HL (entry zone): {s.entry_zone_price:.2f}  ({c_entry.time.strftime('%b %d %H:%M')})")
        else:
            print(f"    H1 (high):       {s.h1_price:.2f}  ({c_h1.time.strftime('%b %d %H:%M')})")
            print(f"    L1 (indication): {s.l1_price:.2f}  ({c_l1.time.strftime('%b %d %H:%M')})")
            print(f"    LH (entry zone): {s.entry_zone_price:.2f}  ({c_entry.time.strftime('%b %d %H:%M')})")
        print(f"    Entry: {s.entry_price:.2f} | SL: {s.stop_loss:.2f} | TP: {s.take_profit:.2f}")
        print(f"    R:R = 1:{s.risk_reward:.1f} | P&L = {s.pnl_rr:+.1f}R")
        if s.exit_idx > 0:
            print(f"    Exit: {candles[s.exit_idx].time.strftime('%b %d %H:%M')}")

    # Summary
    closed = [s for s in setups if s.outcome in ("WIN", "LOSS")]
    if closed:
        wins = sum(1 for s in closed if s.outcome == "WIN")
        losses = sum(1 for s in closed if s.outcome == "LOSS")
        total_rr = sum(s.pnl_rr for s in closed)
        avg_rr_win = sum(s.risk_reward for s in closed if s.outcome == "WIN") / wins if wins else 0
        print(f"\n--- Performance ---")
        print(f"  Closed: {len(closed)} | Wins: {wins} | Losses: {losses}")
        print(f"  Win rate: {wins/len(closed)*100:.1f}%")
        print(f"  Avg R:R on wins: 1:{avg_rr_win:.1f}")
        print(f"  Total P&L: {total_rr:+.1f}R")
        print(f"  Expectancy: {total_rr/len(closed):+.2f}R per trade")

    still_open = [s for s in setups if s.outcome == "OPEN"]
    if still_open:
        print(f"  Still open: {len(still_open)}")


# --- Generate chart (zoomed sections for each setup) ---
def generate_charts(candles, swings, setups):
    if not setups:
        return

    # Full overview chart
    fig, ax = plt.subplots(figsize=(24, 10))
    for i, c in enumerate(candles):
        color = '#26a69a' if c.is_bullish else '#ef5350'
        ax.plot([i, i], [c.low, c.high], color=color, linewidth=0.5)
        body_bottom = min(c.open, c.close)
        body_height = max(abs(c.close - c.open), 0.01)
        ax.bar(i, body_height, bottom=body_bottom, width=0.6, color=color, edgecolor=color)

    # Mark all swing points
    for s in swings:
        if s.classification:
            y = s.price
            offset = 15 if s.swing_type == "HIGH" else -15
            color = '#2196F3' if s.classification in ("HH", "HL") else '#FF9800'
            ax.annotate(s.classification, (s.index, y),
                       textcoords="offset points", xytext=(0, offset),
                       fontsize=5, ha='center', color=color, alpha=0.5)

    # Mark ICC setups
    for i, setup in enumerate(setups):
        oc = '#4CAF50' if setup.outcome == "WIN" else '#F44336' if setup.outcome == "LOSS" else '#FFC107'

        # Draw the structure: L1 → H1 → Entry
        if setup.direction == "BUY":
            # Lines showing the structure
            ax.plot([setup.swing_l1_idx, setup.swing_h1_idx],
                   [setup.l1_price, setup.h1_price], color='green', linewidth=1.5, alpha=0.6)
            ax.plot([setup.swing_h1_idx, setup.swing_entry_idx],
                   [setup.h1_price, setup.entry_zone_price], color='green', linewidth=1.5, alpha=0.6)
        else:
            ax.plot([setup.swing_h1_idx, setup.swing_l1_idx],
                   [setup.h1_price, setup.l1_price], color='red', linewidth=1.5, alpha=0.6)
            ax.plot([setup.swing_l1_idx, setup.swing_entry_idx],
                   [setup.l1_price, setup.entry_zone_price], color='red', linewidth=1.5, alpha=0.6)

        # Entry marker
        ax.annotate(f"#{i+1} {setup.direction}\n{setup.outcome} | 1:{setup.risk_reward:.1f}R",
                    (setup.swing_entry_idx, setup.entry_price),
                    textcoords="offset points", xytext=(20, -10 if setup.direction == "BUY" else 10),
                    fontsize=7, fontweight='bold', color=oc,
                    arrowprops=dict(arrowstyle='->', color=oc, lw=1.5))

        # SL and TP lines
        x_end = setup.exit_idx if setup.exit_idx > 0 else min(setup.swing_entry_idx + 80, len(candles) - 1)
        ax.hlines(setup.entry_price, setup.swing_entry_idx, x_end, colors=oc, linestyles='-', linewidth=1, alpha=0.7)
        ax.hlines(setup.stop_loss, setup.swing_entry_idx, x_end, colors='red', linestyles='--', linewidth=0.8, alpha=0.4)
        ax.hlines(setup.take_profit, setup.swing_entry_idx, x_end, colors='green', linestyles='--', linewidth=0.8, alpha=0.4)

        # Shade the ICC zone
        ax.axvspan(setup.swing_l1_idx, setup.swing_entry_idx, alpha=0.03,
                  color='green' if setup.direction == "BUY" else 'red')

    ax.set_title(f"ICC v2 — Gold/USD H1 — {len(setups)} setups (enter at correction)", fontsize=14)
    ax.set_xlabel("Candle Index")
    ax.set_ylabel("Price (USD)")
    ax.grid(True, alpha=0.15)
    plt.tight_layout()

    filepath = Path("/Users/jimmykeli/personalprojects/trading-exp/icc_v2_results.png")
    plt.savefig(filepath, dpi=150)
    plt.close()
    print(f"\nFull chart saved: {filepath}")

    # Zoomed charts for each setup
    for i, setup in enumerate(setups):
        fig, ax = plt.subplots(figsize=(16, 8))

        # Show 40 bars before the pattern and 60 after
        start = max(0, setup.swing_l1_idx - 40)
        end_idx = setup.exit_idx if setup.exit_idx > 0 else setup.swing_entry_idx + 60
        end = min(len(candles), end_idx + 20)

        for j in range(start, end):
            c = candles[j]
            color = '#26a69a' if c.is_bullish else '#ef5350'
            ax.plot([j, j], [c.low, c.high], color=color, linewidth=0.8)
            body_bottom = min(c.open, c.close)
            body_height = max(abs(c.close - c.open), 0.01)
            ax.bar(j, body_height, bottom=body_bottom, width=0.6, color=color, edgecolor=color)

        # Swing labels in range
        for s in swings:
            if start <= s.index < end and s.classification:
                offset = 20 if s.swing_type == "HIGH" else -20
                clr = '#2196F3' if s.classification in ("HH", "HL") else '#FF9800'
                ax.annotate(f"{s.classification}\n{s.price:.0f}", (s.index, s.price),
                           textcoords="offset points", xytext=(0, offset),
                           fontsize=8, ha='center', color=clr, fontweight='bold')

        # Structure lines
        oc = '#4CAF50' if setup.outcome == "WIN" else '#F44336' if setup.outcome == "LOSS" else '#FFC107'
        if setup.direction == "BUY":
            ax.plot([setup.swing_l1_idx, setup.swing_h1_idx, setup.swing_entry_idx],
                   [setup.l1_price, setup.h1_price, setup.entry_zone_price],
                   color='green', linewidth=2, alpha=0.7, marker='o', markersize=6)
        else:
            ax.plot([setup.swing_h1_idx, setup.swing_l1_idx, setup.swing_entry_idx],
                   [setup.h1_price, setup.l1_price, setup.entry_zone_price],
                   color='red', linewidth=2, alpha=0.7, marker='o', markersize=6)

        # Entry, SL, TP
        x_end = setup.exit_idx if setup.exit_idx > 0 else end - 1
        ax.hlines(setup.entry_price, setup.swing_entry_idx, x_end,
                 colors=oc, linestyles='-', linewidth=2, alpha=0.8, label=f'Entry {setup.entry_price:.2f}')
        ax.hlines(setup.stop_loss, setup.swing_entry_idx, x_end,
                 colors='red', linestyles='--', linewidth=1.5, alpha=0.6, label=f'SL {setup.stop_loss:.2f}')
        ax.hlines(setup.take_profit, setup.swing_entry_idx, x_end,
                 colors='green', linestyles='--', linewidth=1.5, alpha=0.6, label=f'TP {setup.take_profit:.2f}')

        c_entry = candles[setup.swing_entry_idx]
        title = (f"ICC #{i+1} [{setup.direction}] — {setup.outcome} | "
                f"Entry: {setup.entry_price:.2f} | R:R 1:{setup.risk_reward:.1f} | "
                f"{c_entry.time.strftime('%b %d')}")
        ax.set_title(title, fontsize=12, fontweight='bold', color=oc)
        ax.legend(loc='upper left', fontsize=9)
        ax.grid(True, alpha=0.15)
        plt.tight_layout()

        filepath = Path(f"/Users/jimmykeli/personalprojects/trading-exp/icc_v2_setup_{i+1}.png")
        plt.savefig(filepath, dpi=150)
        plt.close()
        print(f"  Setup #{i+1} chart: {filepath}")


# --- Main ---
async def main():
    candles = await fetch_candles()
    if not candles:
        return

    print("\nComputing ATR...")
    atrs = compute_atr(candles)
    avg_atr = sum(atrs[ATR_PERIOD:]) / max(len(atrs) - ATR_PERIOD, 1)
    print(f"  Average ATR(14) on H1: ${avg_atr:.2f}")

    print("Detecting swings...")
    swings = detect_swings(candles)
    swings = classify_structure(swings)

    print("Detecting ICC setups (v2 — entry at correction, ATR-based SL)...")
    setups = detect_icc_setups(candles, swings, atrs)

    print("Simulating outcomes (trailing stop enabled)...")
    setups = simulate_outcomes(candles, setups, atrs)

    print_results(candles, swings, setups)
    generate_charts(candles, swings, setups)


if __name__ == "__main__":
    asyncio.run(main())
