"""
ICC Live Monitor — Self-Learning Trading Observer

Connects to Deriv WebSocket, monitors Gold/USD in real-time:
- Detects ICC setups as they form
- Tracks predictions and outcomes
- Learns which conditions produce winners vs losers
- Adjusts confidence scoring based on historical performance
- Logs everything to icc_monitor_log.json

Run: source .venv/bin/activate && python icc_monitor.py
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import websockets
from dotenv import load_dotenv

load_dotenv()

# --- Config ---
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1")
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
SYMBOL = "frxXAUUSD"
STATE_FILE = Path("/Users/jimmykeli/personalprojects/trading-exp/icc_state.json")
LOG_FILE = Path("/Users/jimmykeli/personalprojects/trading-exp/icc_monitor_log.json")

# Analysis runs every time a new H1 candle closes
GRANULARITY_H1 = 3600
GRANULARITY_H4 = 14400
CANDLE_FETCH_COUNT = 200

# Default parameters (will be adjusted by learning)
DEFAULT_PARAMS = {
    "swing_lookback": 3,
    "atr_period": 14,
    "sl_atr_mult": 1.0,
    "min_sl": 10.0,
    "min_move_atr": 1.5,
    "tp_move_mult": 1.5,
    "min_rr": 1.5,
    "correction_depth_max": 0.75,  # Max correction depth as % of indication move
}


# --- State Management ---
class MonitorState:
    """Persists between runs. Tracks setups, outcomes, and learned parameters."""

    def __init__(self):
        self.params = dict(DEFAULT_PARAMS)
        self.active_setups = []       # Setups waiting for entry or outcome
        self.completed_setups = []    # Setups with known outcomes
        self.predictions = []         # Trend predictions with timestamps
        self.learning_log = []        # What the system learned and when
        self.last_analysis_epoch = 0
        self.load()

    def load(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                self.params = data.get("params", dict(DEFAULT_PARAMS))
                self.active_setups = data.get("active_setups", [])
                self.completed_setups = data.get("completed_setups", [])
                self.predictions = data.get("predictions", [])
                self.learning_log = data.get("learning_log", [])
                self.last_analysis_epoch = data.get("last_analysis_epoch", 0)
            except (json.JSONDecodeError, KeyError):
                pass

    def save(self):
        data = {
            "params": self.params,
            "active_setups": self.active_setups,
            "completed_setups": self.completed_setups[-200:],  # Keep last 200
            "predictions": self.predictions[-100:],
            "learning_log": self.learning_log[-100:],
            "last_analysis_epoch": self.last_analysis_epoch,
        }
        STATE_FILE.write_text(json.dumps(data, indent=2, default=str))

    def log_event(self, event_type, message, data=None):
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "message": message,
            "data": data or {},
        }
        # Append to log file
        log = []
        if LOG_FILE.exists():
            try:
                log = json.loads(LOG_FILE.read_text())
            except (json.JSONDecodeError, ValueError):
                pass
        log.append(entry)
        # Keep last 1000 entries
        LOG_FILE.write_text(json.dumps(log[-1000:], indent=2, default=str))
        return entry


# --- Market Analysis ---
def compute_atr(candles, period=14):
    atrs = [0.0] * len(candles)
    for i in range(1, len(candles)):
        tr = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i - 1]["close"]),
            abs(candles[i]["low"] - candles[i - 1]["close"]),
        )
        if i < period:
            atrs[i] = sum(
                max(candles[j]["high"] - candles[j]["low"],
                    abs(candles[j]["high"] - candles[j - 1]["close"]) if j > 0 else 0,
                    abs(candles[j]["low"] - candles[j - 1]["close"]) if j > 0 else 0)
                for j in range(1, i + 1)
            ) / i
        else:
            atrs[i] = (atrs[i - 1] * (period - 1) + tr) / period
    return atrs


def detect_swings(candles, lookback=3):
    swings = []
    for i in range(lookback, len(candles) - lookback):
        h = candles[i]["high"]
        lo = candles[i]["low"]
        is_sh = all(h > candles[j]["high"] for j in range(i - lookback, i + lookback + 1) if j != i)
        is_sl = all(lo < candles[j]["low"] for j in range(i - lookback, i + lookback + 1) if j != i)
        if is_sh:
            swings.append({"index": i, "price": h, "type": "HIGH", "cls": "", "epoch": candles[i]["epoch"]})
        if is_sl:
            swings.append({"index": i, "price": lo, "type": "LOW", "cls": "", "epoch": candles[i]["epoch"]})
    swings.sort(key=lambda s: s["index"])

    # Classify
    prev_high = None
    prev_low = None
    for s in swings:
        if s["type"] == "HIGH":
            if prev_high:
                s["cls"] = "HH" if s["price"] > prev_high["price"] else "LH"
            prev_high = s
        elif s["type"] == "LOW":
            if prev_low:
                s["cls"] = "HL" if s["price"] > prev_low["price"] else "LL"
            prev_low = s
    return swings


def determine_trend(swings, n_recent=6):
    recent = [s for s in swings if s["cls"]][-n_recent:]
    hh = sum(1 for s in recent if s["cls"] == "HH")
    hl = sum(1 for s in recent if s["cls"] == "HL")
    lh = sum(1 for s in recent if s["cls"] == "LH")
    ll = sum(1 for s in recent if s["cls"] == "LL")

    if hh >= 1 and hl >= 1 and hh + hl > lh + ll:
        return "UPTREND", {"hh": hh, "hl": hl, "lh": lh, "ll": ll}
    elif lh >= 1 and ll >= 1 and lh + ll > hh + hl:
        return "DOWNTREND", {"hh": hh, "hl": hl, "lh": lh, "ll": ll}
    else:
        return "RANGING", {"hh": hh, "hl": hl, "lh": lh, "ll": ll}


def find_icc_setups(candles, swings, atrs, params):
    """Find active ICC setups in current market structure."""
    setups = []
    highs = [s for s in swings if s["type"] == "HIGH"]
    lows = [s for s in swings if s["type"] == "LOW"]

    min_move_atr = params["min_move_atr"]
    sl_atr_mult = params["sl_atr_mult"]
    min_sl = params["min_sl"]
    tp_mult = params["tp_move_mult"]
    min_rr = params["min_rr"]
    max_depth = params["correction_depth_max"]

    # BUY setups: L1 → H1 (HH) → HL (entry)
    for i in range(len(lows) - 1):
        l1 = lows[i]
        hl = lows[i + 1]
        if hl["cls"] != "HL":
            continue
        if hl["price"] <= l1["price"]:
            continue

        # Find H1 between L1 and HL
        h1 = None
        for h in highs:
            if l1["index"] < h["index"] < hl["index"]:
                h1 = h
        if not h1 or h1["cls"] not in ("HH", ""):
            continue

        move = h1["price"] - l1["price"]
        atr = atrs[h1["index"]] if h1["index"] < len(atrs) else 20.0
        if move < atr * min_move_atr:
            continue

        # Check correction depth
        correction = h1["price"] - hl["price"]
        if move > 0 and correction / move > max_depth:
            continue

        entry = hl["price"]
        sl_dist = max(atr * sl_atr_mult, min_sl)
        sl = entry - sl_dist
        if sl > l1["price"]:
            sl = l1["price"] - (min_sl * 0.5)

        tp = entry + move * tp_mult
        risk = entry - sl
        if risk <= 0:
            continue
        reward = tp - entry
        rr = reward / risk
        if rr < min_rr:
            continue

        setups.append({
            "direction": "BUY",
            "l1": l1, "h1": h1, "entry_swing": hl,
            "entry_price": round(entry, 2),
            "stop_loss": round(sl, 2),
            "take_profit": round(tp, 2),
            "risk_reward": round(rr, 1),
            "move_size": round(move, 2),
            "correction_depth": round(correction / move * 100, 1) if move > 0 else 0,
            "atr_at_entry": round(atr, 2),
            "status": "FORMING",
        })

    # SELL setups: H1 → L1 (LL) → LH (entry)
    for i in range(len(highs) - 1):
        h1 = highs[i]
        lh = highs[i + 1]
        if lh["cls"] != "LH":
            continue
        if lh["price"] >= h1["price"]:
            continue

        l1 = None
        for lo in lows:
            if h1["index"] < lo["index"] < lh["index"]:
                l1 = lo
        if not l1 or l1["cls"] not in ("LL", ""):
            continue

        move = h1["price"] - l1["price"]
        atr = atrs[l1["index"]] if l1["index"] < len(atrs) else 20.0
        if move < atr * min_move_atr:
            continue

        correction = lh["price"] - l1["price"]
        if move > 0 and correction / move > max_depth:
            continue

        entry = lh["price"]
        sl_dist = max(atr * sl_atr_mult, min_sl)
        sl = entry + sl_dist
        if sl < h1["price"]:
            sl = h1["price"] + (min_sl * 0.5)

        tp = entry - move * tp_mult
        risk = sl - entry
        if risk <= 0:
            continue
        reward = entry - tp
        rr = reward / risk
        if rr < min_rr:
            continue

        setups.append({
            "direction": "SELL",
            "l1": l1, "h1": h1, "entry_swing": lh,
            "entry_price": round(entry, 2),
            "stop_loss": round(sl, 2),
            "take_profit": round(tp, 2),
            "risk_reward": round(rr, 1),
            "move_size": round(move, 2),
            "correction_depth": round(correction / move * 100, 1) if move > 0 else 0,
            "atr_at_entry": round(atr, 2),
            "status": "FORMING",
        })

    return setups


def check_active_setups(active_setups, current_price, current_high, current_low):
    """Check if any active setup hit SL or TP."""
    completed = []
    still_active = []

    for setup in active_setups:
        hit = None
        if setup["direction"] == "BUY":
            if current_low <= setup["stop_loss"]:
                hit = "LOSS"
            elif current_high >= setup["take_profit"]:
                hit = "WIN"
        else:
            if current_high >= setup["stop_loss"]:
                hit = "LOSS"
            elif current_low <= setup["take_profit"]:
                hit = "WIN"

        if hit:
            setup["outcome"] = hit
            setup["exit_time"] = datetime.now(timezone.utc).isoformat()
            setup["exit_price"] = current_price
            completed.append(setup)
        else:
            still_active.append(setup)

    return still_active, completed


# --- Self-Learning ---
def learn_from_outcomes(state):
    """Analyze completed setups and adjust parameters."""
    completed = state.completed_setups
    if len(completed) < 5:
        return  # Not enough data

    recent = completed[-30:]  # Learn from last 30 trades
    wins = [s for s in recent if s.get("outcome") == "WIN"]
    losses = [s for s in recent if s.get("outcome") == "LOSS"]

    if not wins and not losses:
        return

    win_rate = len(wins) / len(recent)
    avg_rr_wins = sum(s.get("risk_reward", 1) for s in wins) / len(wins) if wins else 0
    avg_depth_wins = sum(s.get("correction_depth", 50) for s in wins) / len(wins) if wins else 50
    avg_depth_losses = sum(s.get("correction_depth", 50) for s in losses) / len(losses) if losses else 50
    avg_move_wins = sum(s.get("move_size", 0) for s in wins) / len(wins) if wins else 0
    avg_move_losses = sum(s.get("move_size", 0) for s in losses) / len(losses) if losses else 0

    adjustments = []

    # If win rate is low, tighten filters
    if win_rate < 0.35:
        # Increase minimum move size — skip small, noisy setups
        if state.params["min_move_atr"] < 2.5:
            state.params["min_move_atr"] = round(state.params["min_move_atr"] + 0.1, 1)
            adjustments.append(f"min_move_atr → {state.params['min_move_atr']} (win rate low: {win_rate:.0%})")

        # Tighten correction depth if losses have deeper corrections
        if avg_depth_losses > avg_depth_wins + 5:
            new_depth = round(max(0.4, state.params["correction_depth_max"] - 0.05), 2)
            state.params["correction_depth_max"] = new_depth
            adjustments.append(f"correction_depth_max → {new_depth} (losers avg {avg_depth_losses:.0f}% vs winners {avg_depth_wins:.0f}%)")

    # If win rate is high, can loosen slightly to find more setups
    elif win_rate > 0.55:
        if state.params["min_move_atr"] > 1.0:
            state.params["min_move_atr"] = round(state.params["min_move_atr"] - 0.1, 1)
            adjustments.append(f"min_move_atr → {state.params['min_move_atr']} (win rate high: {win_rate:.0%})")

    # Adjust TP multiplier based on realized R:R
    if wins:
        if avg_rr_wins < 1.5:
            # TP might be too aggressive — bring it in
            if state.params["tp_move_mult"] > 1.0:
                state.params["tp_move_mult"] = round(state.params["tp_move_mult"] - 0.1, 1)
                adjustments.append(f"tp_move_mult → {state.params['tp_move_mult']} (avg R:R on wins only {avg_rr_wins:.1f})")
        elif avg_rr_wins > 3.0:
            # TP is conservative — can push it out
            if state.params["tp_move_mult"] < 3.0:
                state.params["tp_move_mult"] = round(state.params["tp_move_mult"] + 0.1, 1)
                adjustments.append(f"tp_move_mult → {state.params['tp_move_mult']} (avg R:R on wins is {avg_rr_wins:.1f}, can push TP)")

    if adjustments:
        learn_entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "trades_analyzed": len(recent),
            "win_rate": round(win_rate, 3),
            "avg_rr_wins": round(avg_rr_wins, 2),
            "adjustments": adjustments,
        }
        state.learning_log.append(learn_entry)
        return learn_entry
    return None


# --- Display ---
def fmt_time(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%b %d %H:%M")


def print_analysis(candles, swings, trend, trend_detail, setups, active, state, current_price, atrs):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    atr_now = atrs[-1] if atrs else 0

    print(f"\n{'=' * 70}")
    print(f"  ICC MONITOR — Gold/USD | {now}")
    print(f"  Price: ${current_price:.2f} | ATR(14): ${atr_now:.2f} | Trend: {trend}")
    print(f"{'=' * 70}")

    # Structure
    last_swings = [s for s in swings if s["cls"]][-6:]
    print(f"\n  Market Structure (last 6 classified swings):")
    for s in last_swings:
        arrow = "^" if s["type"] == "HIGH" else "v"
        print(f"    {arrow} {s['cls']:>2} {s['price']:>9.2f}  ({fmt_time(s['epoch'])})")

    # Trend detail
    d = trend_detail
    print(f"\n  Trend score: HH:{d['hh']} HL:{d['hl']} LH:{d['lh']} LL:{d['ll']}")

    # Active setups being monitored
    if active:
        print(f"\n  Active Setups ({len(active)}):")
        for s in active:
            print(f"    [{s['direction']}] Entry: {s['entry_price']:.2f} | "
                  f"SL: {s['stop_loss']:.2f} | TP: {s['take_profit']:.2f} | "
                  f"R:R 1:{s['risk_reward']:.1f}")
            dist_sl = abs(current_price - s["stop_loss"])
            dist_tp = abs(current_price - s["take_profit"])
            print(f"           Distance to SL: ${dist_sl:.2f} | to TP: ${dist_tp:.2f}")

    # New setups detected
    new_setups = [s for s in setups if s not in active]
    if new_setups:
        print(f"\n  NEW ICC Setups Detected ({len(new_setups)}):")
        for s in new_setups:
            confidence = score_setup(s, trend, state)
            emoji_bar = "#" * min(int(confidence / 10), 10)
            print(f"    [{s['direction']}] Confidence: {confidence:.0f}/100 [{emoji_bar}]")
            print(f"      Entry zone: ${s['entry_price']:.2f}")
            print(f"      SL: ${s['stop_loss']:.2f} | TP: ${s['take_profit']:.2f}")
            print(f"      R:R = 1:{s['risk_reward']:.1f}")
            print(f"      Move: ${s['move_size']:.2f} | Correction: {s['correction_depth']:.0f}%")

            # Describe the setup in plain language
            if s["direction"] == "BUY":
                print(f"      Structure: Low {s['l1']['price']:.2f} → High {s['h1']['price']:.2f} → HL {s['entry_swing']['price']:.2f}")
                print(f"      → Price made a Higher Low, expecting continuation up to ${s['take_profit']:.2f}")
            else:
                print(f"      Structure: High {s['h1']['price']:.2f} → Low {s['l1']['price']:.2f} → LH {s['entry_swing']['price']:.2f}")
                print(f"      → Price made a Lower High, expecting continuation down to ${s['take_profit']:.2f}")
    else:
        print(f"\n  No new ICC setups at this time.")

    # Show what the system is watching for (pending patterns)
    print(f"\n  Watching For:")
    last_swings = [s for s in swings if s["cls"]]
    last_highs = [s for s in last_swings if s["type"] == "HIGH"]
    last_lows = [s for s in last_swings if s["type"] == "LOW"]

    if last_lows and last_highs:
        latest_high = last_highs[-1]
        latest_low = last_lows[-1]
        current = candles[-1]["close"]

        # What would complete a SELL ICC?
        # Need: H1 (existing high) → LL (existing low, after H1) → LH (bounce, not yet confirmed)
        if latest_low["index"] > latest_high["index"] and latest_low["cls"] in ("LL", ""):
            # We have H1 → L1 (indication done). Waiting for LH (correction bounce)
            potential_lh = max(c["high"] for c in candles[latest_low["index"]:])
            move = latest_high["price"] - latest_low["price"]
            if move > 0:
                retrace_pct = (potential_lh - latest_low["price"]) / move * 100
                print(f"    SELL: Indication complete (H:{latest_high['price']:.2f} → L:{latest_low['price']:.2f})")
                print(f"           Bounce so far: {potential_lh:.2f} ({retrace_pct:.0f}% retrace)")
                print(f"           Need: bounce to stall and form a LH (Lower High < {latest_high['price']:.2f})")
                print(f"           Then: sell at the LH with TP near {latest_low['price']:.2f}")
                ideal_entry = latest_low["price"] + move * 0.5  # 50% retrace
                print(f"           Ideal entry zone: ${latest_low['price'] + move * 0.38:.2f} - ${latest_low['price'] + move * 0.62:.2f}")

        # What would complete a BUY ICC?
        if latest_high["index"] > latest_low["index"] and latest_high["cls"] in ("HH", ""):
            # We have L1 → H1 (indication done). Waiting for HL (correction pullback)
            potential_hl = min(c["low"] for c in candles[latest_high["index"]:])
            move = latest_high["price"] - latest_low["price"]
            if move > 0:
                retrace_pct = (latest_high["price"] - potential_hl) / move * 100
                print(f"    BUY:  Indication complete (L:{latest_low['price']:.2f} → H:{latest_high['price']:.2f})")
                print(f"           Pullback so far: {potential_hl:.2f} ({retrace_pct:.0f}% retrace)")
                print(f"           Need: pullback to hold above {latest_low['price']:.2f} and form a HL")
                print(f"           Then: buy at the HL with TP above {latest_high['price']:.2f}")
                print(f"           Ideal entry zone: ${latest_high['price'] - move * 0.62:.2f} - ${latest_high['price'] - move * 0.38:.2f}")

        # If nothing is clearly forming
        if latest_low["index"] == latest_high["index"]:
            print(f"    Structure unclear — waiting for new swing to form")

    # Performance tracking
    completed = state.completed_setups
    if completed:
        recent = completed[-20:]
        wins = sum(1 for s in recent if s.get("outcome") == "WIN")
        print(f"\n  Track Record (last {len(recent)} trades): "
              f"{wins}W / {len(recent)-wins}L ({wins/len(recent)*100:.0f}%)")

    # Learned params
    if state.learning_log:
        last_learn = state.learning_log[-1]
        print(f"\n  Last learning update: {last_learn.get('time', 'N/A')}")
        for adj in last_learn.get("adjustments", []):
            print(f"    Adjusted: {adj}")

    # Current params
    print(f"\n  Parameters: move>{state.params['min_move_atr']}xATR | "
          f"depth<{state.params['correction_depth_max']*100:.0f}% | "
          f"TP={state.params['tp_move_mult']}x | "
          f"SL={state.params['sl_atr_mult']}xATR")

    print(f"\n{'=' * 70}")


def score_setup(setup, trend, state):
    """Score a setup 0-100 based on multiple factors."""
    score = 50  # Base score

    # Trend alignment (+20 / -20)
    if setup["direction"] == "BUY" and trend == "UPTREND":
        score += 20
    elif setup["direction"] == "SELL" and trend == "DOWNTREND":
        score += 20
    elif setup["direction"] == "BUY" and trend == "DOWNTREND":
        score -= 20
    elif setup["direction"] == "SELL" and trend == "UPTREND":
        score -= 20

    # R:R quality (+15 max)
    rr = setup["risk_reward"]
    if rr >= 3.0:
        score += 15
    elif rr >= 2.0:
        score += 10
    elif rr >= 1.5:
        score += 5

    # Correction depth — moderate corrections (40-60%) tend to be best
    depth = setup["correction_depth"]
    if 35 <= depth <= 65:
        score += 10
    elif depth > 75 or depth < 20:
        score -= 10

    # Move size relative to ATR — bigger moves are cleaner signals
    move = setup["move_size"]
    atr = setup["atr_at_entry"]
    if atr > 0:
        move_ratio = move / atr
        if move_ratio >= 3.0:
            score += 10
        elif move_ratio >= 2.0:
            score += 5
        elif move_ratio < 1.5:
            score -= 10

    # Historical performance of similar setups
    similar = [s for s in state.completed_setups[-50:]
               if s.get("direction") == setup["direction"]]
    if len(similar) >= 3:
        similar_wins = sum(1 for s in similar if s.get("outcome") == "WIN")
        similar_wr = similar_wins / len(similar)
        if similar_wr > 0.5:
            score += 10
        elif similar_wr < 0.3:
            score -= 10

    return max(0, min(100, score))


# --- WebSocket Data Fetching ---
async def fetch_candles(ws, symbol, granularity, count):
    req = {
        "ticks_history": symbol,
        "adjust_start_time": 1,
        "count": count,
        "end": "latest",
        "granularity": granularity,
        "style": "candles",
    }
    await ws.send(json.dumps(req))
    resp = json.loads(await ws.recv())
    if "error" in resp:
        print(f"  API Error: {resp['error']['message']}")
        return []
    raw = resp.get("candles", [])
    return [
        {"epoch": c["epoch"], "open": float(c["open"]), "high": float(c["high"]),
         "low": float(c["low"]), "close": float(c["close"])}
        for c in raw
    ]


# --- Main Loop ---
async def run_analysis(state):
    """Run one full analysis cycle."""
    print("\n  Connecting to Deriv...")
    async with websockets.connect(WS_URL) as ws:
        # Fetch H1 candles
        candles = await fetch_candles(ws, SYMBOL, GRANULARITY_H1, CANDLE_FETCH_COUNT)
        if not candles:
            print("  Failed to fetch candles")
            return

        # Fetch H4 for higher TF context
        candles_4h = await fetch_candles(ws, SYMBOL, GRANULARITY_H4, 50)

    current_price = candles[-1]["close"]
    current_high = candles[-1]["high"]
    current_low = candles[-1]["low"]
    last_epoch = candles[-1]["epoch"]

    # Compute ATR
    atrs = compute_atr(candles, state.params["atr_period"])

    # Detect swings on H1
    swings = detect_swings(candles, state.params["swing_lookback"])

    # Higher TF trend (H4)
    swings_4h = detect_swings(candles_4h, 3) if candles_4h else []
    trend_4h, _ = determine_trend(swings_4h) if swings_4h else ("UNKNOWN", {})

    # H1 trend
    trend, trend_detail = determine_trend(swings)

    # Check active setups against current price
    state.active_setups, newly_completed = check_active_setups(
        state.active_setups, current_price, current_high, current_low
    )

    for setup in newly_completed:
        state.completed_setups.append(setup)
        outcome = setup["outcome"]
        rr = setup["risk_reward"]
        state.log_event("OUTCOME", f"{setup['direction']} → {outcome} (R:R 1:{rr})", setup)
        print(f"\n  TRADE COMPLETED: {setup['direction']} → {outcome} | R:R 1:{rr}")

    # Detect new ICC setups
    new_setups = find_icc_setups(candles, swings, atrs, state.params)

    # Filter: only keep setups from the most recent swings (last 20 candles)
    recent_threshold = len(candles) - 25
    new_setups = [s for s in new_setups if s["entry_swing"]["index"] >= recent_threshold]

    # Avoid duplicates with already active setups
    active_entries = {(s["direction"], s["entry_price"]) for s in state.active_setups}
    truly_new = [s for s in new_setups if (s["direction"], s["entry_price"]) not in active_entries]

    for setup in truly_new:
        confidence = score_setup(setup, trend, state)
        setup["confidence"] = confidence
        setup["detected_time"] = datetime.now(timezone.utc).isoformat()
        setup["h1_trend"] = trend
        setup["h4_trend"] = trend_4h
        setup["status"] = "ACTIVE"

        # Serialize swing data for storage
        setup["l1"] = {"price": setup["l1"]["price"], "epoch": setup["l1"]["epoch"]}
        setup["h1"] = {"price": setup["h1"]["price"], "epoch": setup["h1"]["epoch"]}
        setup["entry_swing"] = {"price": setup["entry_swing"]["price"], "epoch": setup["entry_swing"]["epoch"]}

        state.active_setups.append(setup)
        state.log_event("NEW_SETUP", f"{setup['direction']} at {setup['entry_price']} | "
                       f"Confidence: {confidence}/100 | R:R 1:{setup['risk_reward']}", setup)

    # Make trend prediction
    prediction = {
        "time": datetime.now(timezone.utc).isoformat(),
        "price": current_price,
        "h1_trend": trend,
        "h4_trend": trend_4h,
        "h1_detail": trend_detail,
        "active_setups": len(state.active_setups),
        "new_setups": len(truly_new),
    }
    state.predictions.append(prediction)

    # Self-learning: analyze outcomes and adjust
    if newly_completed:
        learned = learn_from_outcomes(state)
        if learned:
            print(f"\n  LEARNING: Adjusted parameters based on {learned['trades_analyzed']} trades")
            for adj in learned["adjustments"]:
                print(f"    {adj}")

    # Display
    print_analysis(candles, swings, trend, trend_detail, new_setups,
                   state.active_setups, state, current_price, atrs)

    state.last_analysis_epoch = last_epoch
    state.save()


async def main():
    # Write PID file so the dashboard can track us
    pid_file = Path("/Users/jimmykeli/personalprojects/trading-exp/icc_monitor.pid")
    pid_file.write_text(str(os.getpid()))

    state = MonitorState()

    print("=" * 70)
    print("  ICC LIVE MONITOR — Gold/USD")
    print(f"  PID: {os.getpid()}")
    print("  Self-learning trading observer")
    print("  Analyzes every new H1 candle close")
    print("  Press Ctrl+C to stop")
    print("=" * 70)

    # Run initial analysis
    await run_analysis(state)

    # Then check every 5 minutes for new candle closes
    while True:
        await asyncio.sleep(300)  # Check every 5 minutes

        try:
            # Quick check: has a new H1 candle closed?
            async with websockets.connect(WS_URL) as ws:
                candles = await fetch_candles(ws, SYMBOL, GRANULARITY_H1, 1)

            if candles and candles[-1]["epoch"] > state.last_analysis_epoch:
                print(f"\n  New H1 candle closed. Running analysis...")
                await run_analysis(state)
            else:
                # Still print a heartbeat with current price
                if candles:
                    price = candles[-1]["close"]
                    now = datetime.now(timezone.utc).strftime("%H:%M")
                    active_str = ""
                    for s in state.active_setups:
                        dist_sl = abs(price - s["stop_loss"])
                        dist_tp = abs(price - s["take_profit"])
                        active_str += f" | {s['direction']} SL:{dist_sl:.0f} TP:{dist_tp:.0f}"
                    print(f"  [{now}] ${price:.2f}{active_str}", end="\r")

        except (websockets.exceptions.ConnectionClosed, ConnectionError, OSError) as e:
            print(f"\n  Connection error: {e}. Retrying in 30s...")
            await asyncio.sleep(30)
        except Exception as e:
            print(f"\n  Error: {e}. Retrying in 30s...")
            await asyncio.sleep(30)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n  Monitor stopped. State saved to icc_state.json")
    finally:
        pid_file = Path("/Users/jimmykeli/personalprojects/trading-exp/icc_monitor.pid")
        pid_file.unlink(missing_ok=True)
