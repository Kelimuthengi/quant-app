"""
ICC Trading Dashboard — Streamlit UI
Multi-timeframe: H4 sets direction, M30 finds entry.
One clear signal at the top: BUY / SELL / WAIT.

Run: source .venv/bin/activate && streamlit run app.py
"""

import asyncio
import json
import os
import subprocess
import signal
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import websockets
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="ICC Trading", page_icon="📊", layout="wide",
                   initial_sidebar_state="expanded")

# --- Config ---
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1")
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
STATE_FILE = Path("icc_state.json")
LOG_FILE = Path("icc_monitor_log.json")
PID_FILE = Path("icc_monitor.pid")
MONITOR_LOG = Path("icc_output.log")
PROJECT_DIR = Path("/Users/jimmykeli/personalprojects/trading-exp")
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"


def is_monitor_running():
    """Check if the background monitor process is alive."""
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # Signal 0 = check if process exists
        return True, pid
    except (ProcessLookupError, ValueError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return False, None


def start_monitor():
    """Start icc_monitor.py as a background process."""
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-u", str(PROJECT_DIR / "icc_monitor.py")],
        stdout=open(MONITOR_LOG, "a"),
        stderr=subprocess.STDOUT,
        cwd=str(PROJECT_DIR),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))
    return proc.pid


def stop_monitor():
    """Stop the background monitor process."""
    running, pid = is_monitor_running()
    if running and pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        PID_FILE.unlink(missing_ok=True)
        return True
    PID_FILE.unlink(missing_ok=True)
    return False

SYMBOLS = {"Gold/USD": "frxXAUUSD", "EUR/USD": "frxEURUSD",
           "GBP/USD": "frxGBPUSD", "USD/JPY": "frxUSDJPY"}


# --- Data ---
@st.cache_data(ttl=90)
def fetch_candles(symbol_id, granularity, count=300):
    async def _fetch():
        async with websockets.connect(WS_URL) as ws:
            req = {"ticks_history": symbol_id, "adjust_start_time": 1,
                   "count": count, "end": "latest", "granularity": granularity,
                   "style": "candles"}
            await ws.send(json.dumps(req))
            resp = json.loads(await ws.recv())
            return resp.get("candles", []) if "error" not in resp else []
    try:
        raw = asyncio.run(_fetch())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        raw = loop.run_until_complete(_fetch())
        loop.close()
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["time"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
    return df


# --- Analysis ---
def compute_atr(df, period=14):
    atrs = [0.0]
    for i in range(1, len(df)):
        tr = max(df["high"].iloc[i] - df["low"].iloc[i],
                 abs(df["high"].iloc[i] - df["close"].iloc[i-1]),
                 abs(df["low"].iloc[i] - df["close"].iloc[i-1]))
        if i < period:
            atrs.append(sum(max(df["high"].iloc[j] - df["low"].iloc[j],
                               abs(df["high"].iloc[j] - df["close"].iloc[j-1]) if j > 0 else 0,
                               abs(df["low"].iloc[j] - df["close"].iloc[j-1]) if j > 0 else 0)
                            for j in range(1, i+1)) / i)
        else:
            atrs.append((atrs[-1] * (period - 1) + tr) / period)
    return atrs


def detect_swings(df, lookback=3):
    swings = []
    h, lo = df["high"].values, df["low"].values
    for i in range(lookback, len(df) - lookback):
        if all(h[i] > h[j] for j in range(i-lookback, i+lookback+1) if j != i):
            swings.append({"idx": i, "price": h[i], "type": "HIGH", "cls": "",
                          "time": df["time"].iloc[i], "epoch": int(df["epoch"].iloc[i])})
        if all(lo[i] < lo[j] for j in range(i-lookback, i+lookback+1) if j != i):
            swings.append({"idx": i, "price": lo[i], "type": "LOW", "cls": "",
                          "time": df["time"].iloc[i], "epoch": int(df["epoch"].iloc[i])})
    swings.sort(key=lambda s: s["idx"])
    prev_h = prev_l = None
    for s in swings:
        if s["type"] == "HIGH":
            if prev_h: s["cls"] = "HH" if s["price"] > prev_h["price"] else "LH"
            prev_h = s
        else:
            if prev_l: s["cls"] = "HL" if s["price"] > prev_l["price"] else "LL"
            prev_l = s
    return swings


def get_trend(swings, n=6):
    recent = [s for s in swings if s["cls"]][-n:]
    hh = sum(1 for s in recent if s["cls"] == "HH")
    hl = sum(1 for s in recent if s["cls"] == "HL")
    lh = sum(1 for s in recent if s["cls"] == "LH")
    ll = sum(1 for s in recent if s["cls"] == "LL")
    if hh >= 1 and hl >= 1 and hh + hl > lh + ll:
        return "UPTREND", {"HH": hh, "HL": hl, "LH": lh, "LL": ll}
    if lh >= 1 and ll >= 1 and lh + ll > hh + hl:
        return "DOWNTREND", {"HH": hh, "HL": hl, "LH": lh, "LL": ll}
    return "RANGING", {"HH": hh, "HL": hl, "LH": lh, "LL": ll}


def find_icc_setups(df, swings, atrs, params, trend_bias=None):
    """Find ICC setups, filtered by trend_bias if provided."""
    setups = []
    highs = [s for s in swings if s["type"] == "HIGH"]
    lows = [s for s in swings if s["type"] == "LOW"]

    # BUY: L1 → H1(HH) → HL
    if trend_bias in (None, "UPTREND", "RANGING"):
        for i in range(len(lows) - 1):
            l1, hl = lows[i], lows[i+1]
            if hl["cls"] != "HL" or hl["price"] <= l1["price"]:
                continue
            h1 = None
            for h in highs:
                if l1["idx"] < h["idx"] < hl["idx"]: h1 = h
            if not h1 or h1["cls"] not in ("HH", ""): continue
            move = h1["price"] - l1["price"]
            atr = atrs[h1["idx"]] if h1["idx"] < len(atrs) else 20
            if move < atr * params["min_move_atr"]: continue
            corr = h1["price"] - hl["price"]
            if move > 0 and corr / move > params["correction_depth_max"]: continue
            entry = hl["price"]
            sl_d = max(atr * params["sl_atr_mult"], params["min_sl"])
            sl = entry - sl_d
            if sl > l1["price"]: sl = l1["price"] - params["min_sl"] * 0.5
            tp = entry + move * params["tp_move_mult"]
            risk = entry - sl
            if risk <= 0: continue
            rr = (tp - entry) / risk
            if rr < params["min_rr"]: continue
            setups.append({"direction": "BUY", "entry": round(entry, 2), "sl": round(sl, 2),
                          "tp": round(tp, 2), "rr": round(min(rr, 8), 1), "move": round(move, 2),
                          "depth": round(corr/move*100, 1), "atr": round(atr, 2),
                          "l1": l1, "h1": h1, "entry_swing": hl})

    # SELL: H1 → L1(LL) → LH
    if trend_bias in (None, "DOWNTREND", "RANGING"):
        for i in range(len(highs) - 1):
            h1, lh = highs[i], highs[i+1]
            if lh["cls"] != "LH" or lh["price"] >= h1["price"]: continue
            l1 = None
            for lo in lows:
                if h1["idx"] < lo["idx"] < lh["idx"]: l1 = lo
            if not l1 or l1["cls"] not in ("LL", ""): continue
            move = h1["price"] - l1["price"]
            atr = atrs[l1["idx"]] if l1["idx"] < len(atrs) else 20
            if move < atr * params["min_move_atr"]: continue
            corr = lh["price"] - l1["price"]
            if move > 0 and corr / move > params["correction_depth_max"]: continue
            entry = lh["price"]
            sl_d = max(atr * params["sl_atr_mult"], params["min_sl"])
            sl = entry + sl_d
            if sl < h1["price"]: sl = h1["price"] + params["min_sl"] * 0.5
            tp = entry - move * params["tp_move_mult"]
            risk = sl - entry
            if risk <= 0: continue
            rr = (entry - tp) / risk
            if rr < params["min_rr"]: continue
            setups.append({"direction": "SELL", "entry": round(entry, 2), "sl": round(sl, 2),
                          "tp": round(tp, 2), "rr": round(min(rr, 8), 1), "move": round(move, 2),
                          "depth": round(corr/move*100, 1), "atr": round(atr, 2),
                          "l1": l1, "h1": h1, "entry_swing": lh})
    return setups


def find_forming(swings, df):
    """Find patterns that are forming but not yet confirmed."""
    forming = []
    last_highs = [s for s in swings if s["type"] == "HIGH" and s["cls"]]
    last_lows = [s for s in swings if s["type"] == "LOW" and s["cls"]]
    if not last_highs or not last_lows: return forming

    lh, ll = last_highs[-1], last_lows[-1]

    if ll["idx"] > lh["idx"] and ll["cls"] in ("LL", ""):
        move = lh["price"] - ll["price"]
        if move > 0:
            bounce = df["high"].iloc[ll["idx"]:].max()
            retrace = (bounce - ll["price"]) / move * 100
            forming.append({"type": "SELL", "h1": lh["price"], "l1": ll["price"],
                           "move": round(move, 2), "bounce": round(bounce, 2),
                           "retrace": round(retrace, 1),
                           "entry_zone": (round(ll["price"] + move*0.38, 2), round(ll["price"] + move*0.62, 2)),
                           "tp": round(ll["price"], 2), "invalidation": round(lh["price"], 2),
                           "status": "Bounce forming LH — waiting for rejection"})

    if lh["idx"] > ll["idx"] and lh["cls"] in ("HH", ""):
        move = lh["price"] - ll["price"]
        if move > 0:
            pullback = df["low"].iloc[lh["idx"]:].min()
            retrace = (lh["price"] - pullback) / move * 100
            forming.append({"type": "BUY", "h1": lh["price"], "l1": ll["price"],
                           "move": round(move, 2), "pullback": round(pullback, 2),
                           "retrace": round(retrace, 1),
                           "entry_zone": (round(lh["price"] - move*0.62, 2), round(lh["price"] - move*0.38, 2)),
                           "tp": round(lh["price"], 2), "invalidation": round(ll["price"], 2),
                           "status": "Pullback forming HL — waiting for bounce"})
    return forming


def generate_signal(h4_trend, entry_setups, entry_forming, current_price):
    """Generate one clear signal based on H4 direction + entry TF setups."""
    # Filter entry setups to match H4 bias
    aligned = [s for s in entry_setups if
               (s["direction"] == "BUY" and h4_trend in ("UPTREND", "RANGING")) or
               (s["direction"] == "SELL" and h4_trend in ("DOWNTREND", "RANGING"))]

    # Also filter forming patterns by H4 bias
    aligned_forming = [f for f in entry_forming if
                       (f["type"] == "BUY" and h4_trend in ("UPTREND", "RANGING")) or
                       (f["type"] == "SELL" and h4_trend in ("DOWNTREND", "RANGING"))]

    if aligned:
        # Pick the best setup (closest to current price and in trend direction)
        best = min(aligned, key=lambda s: abs(s["entry"] - current_price))
        dist = abs(best["entry"] - current_price)
        atr = best["atr"]
        # Is price near the entry zone?
        if dist < atr * 0.5:
            return {
                "action": best["direction"],
                "state": "ACTIVE",
                "entry": best["entry"],
                "sl": best["sl"],
                "tp": best["tp"],
                "rr": best["rr"],
                "reason": f"ICC {best['direction']} — H4 {h4_trend}, price near entry zone",
                "setup": best,
            }
        else:
            return {
                "action": best["direction"],
                "state": "WAIT",
                "entry": best["entry"],
                "sl": best["sl"],
                "tp": best["tp"],
                "rr": best["rr"],
                "reason": f"ICC {best['direction']} signal — wait for price to reach ${best['entry']:.2f}",
                "setup": best,
            }

    if aligned_forming:
        f = aligned_forming[0]
        return {
            "action": f["type"],
            "state": "FORMING",
            "entry": f["entry_zone"],
            "tp": f["tp"],
            "invalidation": f["invalidation"],
            "reason": f"{f['status']} | Entry zone: ${f['entry_zone'][0]:.2f} - ${f['entry_zone'][1]:.2f}",
            "forming": f,
        }

    return {
        "action": "WAIT",
        "state": "NO_SIGNAL",
        "reason": f"No ICC setup aligned with H4 trend ({h4_trend}). Waiting for structure.",
    }


# --- Chart ---
def build_chart(df, swings, setups, forming, title=""):
    fig = make_subplots(rows=1, cols=1)
    fig.add_trace(go.Candlestick(
        x=df["time"], open=df["open"], high=df["high"],
        low=df["low"], close=df["close"],
        increasing_line_color="#26a69a", decreasing_line_color="#ef5350", name="Price"))

    sh = [s for s in swings if s["type"] == "HIGH"]
    sl = [s for s in swings if s["type"] == "LOW"]
    if sh:
        fig.add_trace(go.Scatter(
            x=[s["time"] for s in sh], y=[s["price"] for s in sh],
            mode="markers+text", text=[s["cls"] or "H" for s in sh],
            textposition="top center", textfont=dict(size=9),
            marker=dict(symbol="triangle-down", size=9,
                       color=["#2196F3" if s["cls"]=="HH" else "#FF9800" for s in sh]),
            name="Swing Highs"))
    if sl:
        fig.add_trace(go.Scatter(
            x=[s["time"] for s in sl], y=[s["price"] for s in sl],
            mode="markers+text", text=[s["cls"] or "L" for s in sl],
            textposition="bottom center", textfont=dict(size=9),
            marker=dict(symbol="triangle-up", size=9,
                       color=["#2196F3" if s["cls"]=="HL" else "#FF9800" for s in sl]),
            name="Swing Lows"))

    for s in setups:
        c = "#4CAF50" if s["direction"] == "BUY" else "#F44336"
        et = s["entry_swing"]["time"]
        end = df["time"].iloc[-1]
        fig.add_shape(type="line", x0=et, x1=end, y0=s["entry"], y1=s["entry"],
                     line=dict(color=c, width=2, dash="solid"))
        fig.add_shape(type="line", x0=et, x1=end, y0=s["sl"], y1=s["sl"],
                     line=dict(color="#F44336", width=1, dash="dash"))
        fig.add_shape(type="line", x0=et, x1=end, y0=s["tp"], y1=s["tp"],
                     line=dict(color="#4CAF50", width=1, dash="dash"))
        fig.add_trace(go.Scatter(
            x=[s["l1"]["time"], s["h1"]["time"], s["entry_swing"]["time"]],
            y=[s["l1"]["price"], s["h1"]["price"], s["entry_swing"]["price"]],
            mode="lines+markers", line=dict(color=c, width=2),
            marker=dict(size=8), showlegend=False, name=f"ICC {s['direction']}"))
        # Annotate entry
        fig.add_annotation(x=et, y=s["entry"], text=f"{s['direction']} ${s['entry']:.2f}",
                          showarrow=True, arrowhead=2, arrowcolor=c, font=dict(color=c, size=10),
                          ax=40, ay=-30 if s["direction"]=="BUY" else 30)

    for f in forming:
        c = "#4CAF50" if f["type"] == "BUY" else "#F44336"
        fig.add_hrect(y0=f["entry_zone"][0], y1=f["entry_zone"][1], fillcolor=c, opacity=0.1,
                     line_width=0, annotation_text=f"{f['type']} Entry Zone",
                     annotation_position="top left", annotation_font_color=c)

    fig.update_layout(template="plotly_dark", paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
                     height=500, margin=dict(l=50, r=20, t=40, b=20),
                     xaxis_rangeslider_visible=False, title=title,
                     legend=dict(orientation="h", yanchor="bottom", y=1.02, x=1, xanchor="right"),
                     font=dict(family="monospace"))
    fig.update_xaxes(gridcolor="rgba(255,255,255,0.05)")
    fig.update_yaxes(gridcolor="rgba(255,255,255,0.05)")
    return fig


# --- Sidebar ---
with st.sidebar:
    st.title("ICC Dashboard")
    symbol_name = st.selectbox("Symbol", list(SYMBOLS.keys()), index=0)
    symbol_id = SYMBOLS[symbol_name]

    st.divider()
    st.subheader("Parameters")
    min_move = st.slider("Min Move (xATR)", 0.5, 4.0, 1.5, 0.1)
    max_depth = st.slider("Max Correction %", 30, 90, 75)
    tp_mult = st.slider("TP Multiplier", 0.5, 3.0, 1.5, 0.1)
    sl_mult = st.slider("SL (xATR)", 0.5, 3.0, 1.0, 0.1)
    min_rr = st.slider("Min R:R", 1.0, 4.0, 1.5, 0.1)

    params = {"min_move_atr": min_move, "correction_depth_max": max_depth/100,
              "tp_move_mult": tp_mult, "sl_atr_mult": sl_mult, "min_sl": 10.0, "min_rr": min_rr}

    if st.button("Refresh", type="primary", use_container_width=True):
        st.cache_data.clear()
    auto = st.checkbox("Auto-refresh (90s)", value=False)


# ================================================================
# MONITOR CONTROLS — right at the top of the main page
# ================================================================
monitor_col1, monitor_col2, monitor_col3 = st.columns([2, 1, 1])
running, pid = is_monitor_running()

with monitor_col1:
    if running:
        st.markdown(f"**Background Monitor:** 🟢 Running (PID {pid}) — tracking setups, outcomes & learning 24/7")
    else:
        st.markdown("**Background Monitor:** 🔴 Stopped — not tracking trades or learning")

with monitor_col2:
    if not running:
        if st.button("Start Monitor", type="primary", use_container_width=True):
            new_pid = start_monitor()
            st.rerun()

with monitor_col3:
    if running:
        if st.button("Stop Monitor", type="secondary", use_container_width=True):
            stop_monitor()
            st.rerun()

# Show monitor log if running
if running and MONITOR_LOG.exists():
    with st.expander("Monitor Output (last 10 lines)"):
        try:
            lines = MONITOR_LOG.read_text().strip().split("\n")
            last_lines = lines[-10:] if len(lines) > 10 else lines
            st.code("\n".join(last_lines), language=None)
        except Exception:
            st.caption("No output yet")


# --- Fetch Data: all 3 timeframes in one connection ---
@st.cache_data(ttl=60)
def fetch_all_timeframes(sym_id):
    """Fetch H4, H1, M30 in a single WebSocket connection to avoid race conditions."""
    async def _fetch():
        results = {}
        async with websockets.connect(WS_URL) as ws:
            for label, gran, cnt in [("h4", 14400, 100), ("h1", 3600, 200), ("m30", 1800, 300)]:
                req = {"ticks_history": sym_id, "adjust_start_time": 1,
                       "count": cnt, "end": "latest", "granularity": gran, "style": "candles"}
                await ws.send(json.dumps(req))
                resp = json.loads(await ws.recv())
                raw = resp.get("candles", []) if "error" not in resp else []
                if raw:
                    df = pd.DataFrame(raw)
                    for col in ("open", "high", "low", "close"):
                        df[col] = df[col].astype(float)
                    df["time"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
                    results[label] = df
                else:
                    results[label] = pd.DataFrame()
        return results

    try:
        return asyncio.run(_fetch())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        r = loop.run_until_complete(_fetch())
        loop.close()
        return r

all_data = fetch_all_timeframes(symbol_id)
df_h4 = all_data["h4"]
df_h1 = all_data["h1"]
df_m30 = all_data["m30"]

if df_m30.empty or df_h4.empty or df_h1.empty:
    st.error("Failed to fetch data. Check connection and try Refresh.")
    st.stop()

price = df_m30["close"].iloc[-1]
prev = df_m30["close"].iloc[-2] if len(df_m30) > 1 else price
change = price - prev

# --- Analysis ---
# H4: direction
swings_h4 = detect_swings(df_h4, 3)
trend_h4, detail_h4 = get_trend(swings_h4)

# H1: structure context
atrs_h1 = compute_atr(df_h1)
swings_h1 = detect_swings(df_h1, 3)
trend_h1, detail_h1 = get_trend(swings_h1)

# M30: entry signals (filtered by H4 direction)
atrs_m30 = compute_atr(df_m30)
swings_m30 = detect_swings(df_m30, 3)
trend_m30, detail_m30 = get_trend(swings_m30)
setups_m30 = find_icc_setups(df_m30, swings_m30, atrs_m30, params, trend_bias=trend_h4)
recent_m30 = [s for s in setups_m30 if s["entry_swing"]["idx"] >= len(df_m30) - 40]
forming_m30 = find_forming(swings_m30, df_m30)

# Generate THE signal
# Priority: if monitor is running and has active setups, use those (single source of truth)
monitor_signal = None
if running:
    state_data = {}
    if STATE_FILE.exists():
        try: state_data = json.loads(STATE_FILE.read_text())
        except: pass
    active_from_monitor = state_data.get("active_setups", [])
    if active_from_monitor:
        # Use the most recent monitor setup as THE signal
        ms = active_from_monitor[-1]
        dist = abs(price - ms["entry_price"])
        atr_est = ms.get("atr_at_entry", 20)
        is_near = dist < atr_est * 0.5
        monitor_signal = {
            "action": ms["direction"],
            "state": "ACTIVE" if is_near else "WAIT",
            "entry": ms["entry_price"],
            "sl": ms["stop_loss"],
            "tp": ms["take_profit"],
            "rr": ms["risk_reward"],
            "reason": f"ICC {ms['direction']} — Confidence {ms.get('confidence', '?')}/100 | "
                      f"H1 {ms.get('h1_trend', '?')} | Move ${ms.get('move_size', 0):.0f} | "
                      f"Correction {ms.get('correction_depth', 0):.0f}%",
            "setup": ms,
        }

# Use monitor signal if available, otherwise fall back to dashboard analysis
if monitor_signal:
    signal = monitor_signal
else:
    signal = generate_signal(trend_h4, recent_m30, forming_m30, price)


# ================================================================
# SIGNAL BANNER — the most important thing on screen
# ================================================================
last_candle_time = df_m30["time"].iloc[-1].strftime("%Y-%m-%d %H:%M UTC")
signal_source = "Monitor" if monitor_signal else "Dashboard"

st.markdown("---")
if signal["state"] == "ACTIVE":
    color = "green" if signal["action"] == "BUY" else "red"
    st.markdown(f"""
    <div style="background: linear-gradient(90deg, {'#1b5e20' if color=='green' else '#b71c1c'}, #0e1117);
                padding: 25px; border-radius: 12px; border-left: 6px solid {'#4CAF50' if color=='green' else '#F44336'};">
        <h1 style="color: {'#4CAF50' if color=='green' else '#F44336'}; margin:0;">
            {'▲' if signal['action']=='BUY' else '▼'} {signal['action']} NOW — ${signal['entry']:.2f}
        </h1>
        <h3 style="color: #ccc; margin:5px 0;">
            Stop Loss: ${signal['sl']:.2f} &nbsp;|&nbsp; Take Profit: ${signal['tp']:.2f} &nbsp;|&nbsp; R:R 1:{signal['rr']:.1f}
        </h3>
        <p style="color: #aaa; margin:0;">{signal['reason']}</p>
        <p style="color: #666; margin:5px 0 0; font-size: 12px;">Source: {signal_source} | Price: ${price:.2f} | Data: {last_candle_time}</p>
    </div>
    """, unsafe_allow_html=True)

elif signal["state"] == "WAIT":
    color = "green" if signal["action"] == "BUY" else "red"
    border_color = "#4CAF50" if signal["action"] == "BUY" else "#F44336"
    st.markdown(f"""
    <div style="background: linear-gradient(90deg, {'#1b3a1b' if signal['action']=='BUY' else '#3a1b1b'}, #0e1117);
                padding: 25px; border-radius: 12px; border-left: 6px solid {border_color};">
        <h1 style="color: {border_color}; margin:0;">
            {'▲' if signal['action']=='BUY' else '▼'} {signal['action']} — Entry at ${signal['entry']:.2f}
        </h1>
        <h3 style="color: #ccc; margin:5px 0;">
            Stop Loss: ${signal['sl']:.2f} &nbsp;|&nbsp; Take Profit: ${signal['tp']:.2f} &nbsp;|&nbsp; R:R 1:{signal['rr']:.1f}
        </h3>
        <p style="color: #FFC107; margin:0;">Current price: ${price:.2f} — {'price is ${0:.2f} above entry, wait for pullback'.format(price - signal['entry']) if price > signal['entry'] else 'price is ${0:.2f} below entry, wait for bounce'.format(signal['entry'] - price)}</p>
        <p style="color: #aaa; margin:5px 0 0;">{signal['reason']}</p>
        <p style="color: #666; margin:5px 0 0; font-size: 12px;">Source: {signal_source} | Data: {last_candle_time}</p>
    </div>
    """, unsafe_allow_html=True)

elif signal["state"] == "FORMING":
    color = "green" if signal["action"] == "BUY" else "red"
    f = signal.get("forming", {})
    st.markdown(f"""
    <div style="background: linear-gradient(90deg, #33333a, #0e1117);
                padding: 25px; border-radius: 12px; border-left: 6px solid {'#4CAF50' if color=='green' else '#F44336'};">
        <h1 style="color: {'#4CAF50' if color=='green' else '#F44336'}; margin:0;">
            {'▲' if signal['action']=='BUY' else '▼'} {signal['action']} SETUP FORMING
        </h1>
        <h3 style="color: #ccc; margin:5px 0;">
            Entry Zone: ${f.get('entry_zone', (0,0))[0]:.2f} — ${f.get('entry_zone', (0,0))[1]:.2f} &nbsp;|&nbsp; TP: ${signal.get('tp', 0):.2f}
        </h3>
        <p style="color: #aaa; margin:0;">{signal['reason']}</p>
        <p style="color: #F44336; margin:5px 0 0;">Dead if price breaks {'above' if signal['action']=='SELL' else 'below'} ${signal.get('invalidation', 0):.2f}</p>
        <p style="color: #666; margin:5px 0 0; font-size: 12px;">Source: {signal_source} | Price: ${price:.2f} | Data: {last_candle_time}</p>
    </div>
    """, unsafe_allow_html=True)

else:
    st.markdown(f"""
    <div style="background: #1a1a2e; padding: 25px; border-radius: 12px; border-left: 6px solid #666;">
        <h1 style="color: #888; margin:0;">NO SIGNAL — WAIT</h1>
        <p style="color: #aaa; margin:5px 0 0;">{signal.get('reason', 'Waiting for ICC structure to form.')}</p>
        <p style="color: #666; margin:5px 0 0; font-size: 12px;">Source: {signal_source} | Price: ${price:.2f} | Data: {last_candle_time}</p>
    </div>
    """, unsafe_allow_html=True)

st.markdown("---")

# --- Metrics Row ---
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric(symbol_name, f"${price:.2f}", f"{change:+.2f}")
c2.metric("H4 Trend", f"{'▲' if trend_h4=='UPTREND' else '▼' if trend_h4=='DOWNTREND' else '◆'} {trend_h4}")
c3.metric("M30 Trend", f"{'▲' if trend_m30=='UPTREND' else '▼' if trend_m30=='DOWNTREND' else '◆'} {trend_m30}")
c4.metric("ATR (M30)", f"${atrs_m30[-1]:.2f}" if atrs_m30 else "$0")
c5.metric("Setups", f"{len(recent_m30)} active | {len(forming_m30)} forming")


# --- Two Charts: H4 (direction) + M30 (entry) ---
col_left, col_right = st.columns(2)

with col_left:
    st.markdown("### H4 — Direction (where is the trend going?)")
    fig_h4 = build_chart(df_h4.tail(60), detect_swings(df_h4.tail(60).reset_index(drop=True), 3),
                         [], [], f"H4 {symbol_name} — {trend_h4}")
    st.plotly_chart(fig_h4, use_container_width=True)

    # H4 structure table
    classified_h4 = [s for s in swings_h4 if s["cls"]][-6:]
    if classified_h4:
        rows = [{"": "🟢" if s["cls"] in ("HH","HL") else "🔴",
                "Swing": s["cls"], "Price": f"${s['price']:.2f}",
                "Time": pd.Timestamp(s["time"]).strftime("%b %d %H:%M")}
               for s in classified_h4]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

with col_right:
    st.markdown("### M30 — Entry (where do I get in?)")
    # Only show last 80 M30 candles for clarity
    df_m30_recent = df_m30.tail(80).reset_index(drop=True)
    swings_m30_chart = detect_swings(df_m30_recent, 3)
    atrs_m30_chart = compute_atr(df_m30_recent)
    setups_chart = find_icc_setups(df_m30_recent, swings_m30_chart, atrs_m30_chart, params, trend_bias=trend_h4)
    recent_chart = [s for s in setups_chart if s["entry_swing"]["idx"] >= len(df_m30_recent) - 40]
    forming_chart = find_forming(swings_m30_chart, df_m30_recent)
    fig_m30 = build_chart(df_m30_recent, swings_m30_chart, recent_chart, forming_chart,
                          f"M30 {symbol_name} — Entry Signals")
    st.plotly_chart(fig_m30, use_container_width=True)

    # M30 structure
    classified_m30 = [s for s in swings_m30 if s["cls"]][-6:]
    if classified_m30:
        rows = [{"": "🟢" if s["cls"] in ("HH","HL") else "🔴",
                "Swing": s["cls"], "Price": f"${s['price']:.2f}",
                "Time": pd.Timestamp(s["time"]).strftime("%b %d %H:%M")}
               for s in classified_m30]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


# --- How It Works ---
with st.expander("How does this work?"):
    st.markdown("""
    **Everything is ICC (Indication, Correction, Continuation).**

    **Two timeframes, one decision:**

    | Timeframe | Role | What it does |
    |-----------|------|-------------|
    | **H4** (left chart) | **Direction** | Tells you the overall trend — are we buying or selling? |
    | **M30** (right chart) | **Entry** | Finds the exact ICC setup — where to enter, SL, and TP |

    **The rule:** Only take M30 entries that match the H4 direction.
    - H4 says UPTREND → only take BUY signals on M30
    - H4 says DOWNTREND → only take SELL signals on M30
    - H4 says RANGING → both directions allowed (lower confidence)

    **The signal banner at the top combines both:**
    - **BUY/SELL NOW** = ICC setup confirmed on M30, aligned with H4, price is near entry
    - **WAIT — BUY/SELL at $X** = Setup confirmed but price hasn't reached the entry zone yet
    - **FORMING** = Pattern is building, not confirmed yet — watch for it
    - **NO SIGNAL** = No setup aligned with H4 trend — stay out
    """)


# --- Monitor State ---
with st.expander("Monitor State & Event Log"):
    state = {}
    if STATE_FILE.exists():
        try: state = json.loads(STATE_FILE.read_text())
        except: pass
    if state:
        completed = state.get("completed_setups", [])
        if completed:
            recent = completed[-20:]
            wins = sum(1 for s in recent if s.get("outcome") == "WIN")
            st.metric("Track Record (last 20)", f"{wins}W / {len(recent)-wins}L ({wins/len(recent)*100:.0f}%)")
        learning = state.get("learning_log", [])
        if learning:
            st.caption(f"Last learned: {learning[-1].get('time','')[:16]}")
            for a in learning[-1].get("adjustments", []):
                st.caption(f"  → {a}")
    else:
        st.caption("Run `python icc_monitor.py` in the background to track trades and enable self-learning.")

    log = []
    if LOG_FILE.exists():
        try: log = json.loads(LOG_FILE.read_text())
        except: pass
    if log:
        for e in reversed(log[-10:]):
            t = e.get("type", "")
            if t == "NEW_SETUP": st.success(f"**{e['time'][:16]}** {e['message']}")
            elif t == "OUTCOME": st.info(f"**{e['time'][:16]}** {e['message']}")


# --- Auto-refresh ---
if auto:
    import time as _t
    _t.sleep(90)
    st.rerun()
