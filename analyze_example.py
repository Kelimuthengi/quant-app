"""
Analyze the user's ICC example on Gold/USD.
Find the exact candles matching the price action described.
"""

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import websockets

load_dotenv()

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1")
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
SYMBOL = "frxXAUUSD"


async def fetch_candles(granularity, start_epoch, end_epoch):
    async with websockets.connect(WS_URL) as ws:
        req = {
            "ticks_history": SYMBOL,
            "adjust_start_time": 1,
            "count": 5000,
            "start": start_epoch,
            "end": end_epoch,
            "granularity": granularity,
            "style": "candles"
        }
        await ws.send(json.dumps(req))
        resp = json.loads(await ws.recv())
        if "error" in resp:
            print(f"Error: {resp['error']['message']}")
            return []
        return resp.get("candles", [])


async def main():
    # The user mentioned "Monday 23" — check March 23, 2026
    # Let's fetch a wider range to find the pattern: March 20 to April 12
    start = int(datetime(2026, 3, 20, 0, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 4, 12, 0, 0, tzinfo=timezone.utc).timestamp())

    print("=== H1 candles (for entry detail) ===")
    h1_candles = await fetch_candles(3600, start, end)
    print(f"Fetched {len(h1_candles)} H1 candles\n")

    # Find key price levels the user mentioned:
    # Low ~4249, High ~4472, HL ~4327, HH ~4574, Correction ~4375, Continuation to ~4853

    print("--- Searching for key price levels ---\n")

    # Track swing highs and lows day by day
    daily_summary = {}
    for c in h1_candles:
        dt = datetime.fromtimestamp(c["epoch"], tz=timezone.utc)
        day_key = dt.strftime("%Y-%m-%d (%A)")
        if day_key not in daily_summary:
            daily_summary[day_key] = {
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "candle_count": 1
            }
        else:
            daily_summary[day_key]["high"] = max(daily_summary[day_key]["high"], float(c["high"]))
            daily_summary[day_key]["low"] = min(daily_summary[day_key]["low"], float(c["low"]))
            daily_summary[day_key]["close"] = float(c["close"])
            daily_summary[day_key]["candle_count"] += 1

    print("Daily OHLC Summary:")
    print(f"{'Date':<28} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10}")
    print("-" * 75)
    for day, data in daily_summary.items():
        print(f"{day:<28} {data['open']:>10.2f} {data['high']:>10.2f} {data['low']:>10.2f} {data['close']:>10.2f}")

    # Now print hourly detail around the key turning points
    print("\n\n--- Hourly detail: Finding the ICC swing points ---\n")

    # Find candles near the key prices
    key_prices = {
        "Low ~4249": 4249,
        "High ~4472": 4472,
        "HL ~4327": 4327,
        "HH ~4574": 4574,
        "Correction ~4375": 4375,
        "Peak ~4853": 4853,
    }

    for label, target in key_prices.items():
        closest = None
        closest_dist = float('inf')
        for c in h1_candles:
            # Check if low or high is near target
            for price_field in ["low", "high"]:
                dist = abs(float(c[price_field]) - target)
                if dist < closest_dist:
                    closest_dist = dist
                    closest = c
                    closest_field = price_field

        if closest:
            dt = datetime.fromtimestamp(closest["epoch"], tz=timezone.utc)
            print(f"  {label}:")
            print(f"    Closest candle: {dt.strftime('%Y-%m-%d %H:%M')} — "
                  f"O:{float(closest['open']):.2f} H:{float(closest['high']):.2f} "
                  f"L:{float(closest['low']):.2f} C:{float(closest['close']):.2f}")
            print(f"    Matched on {closest_field} = {float(closest[closest_field]):.2f} "
                  f"(diff: {closest_dist:.2f})")
            print()

    # Print all H1 candles with swing markers
    print("\n--- Full H1 candle sequence with structure ---\n")

    all_candles = []
    for c in h1_candles:
        all_candles.append({
            "epoch": c["epoch"],
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
        })

    # Detect swings with lookback 3
    lookback = 3
    for i in range(lookback, len(all_candles) - lookback):
        high_i = all_candles[i]["high"]
        low_i = all_candles[i]["low"]

        is_sh = all(high_i > all_candles[j]["high"] for j in range(i-lookback, i+lookback+1) if j != i)
        is_sl = all(low_i < all_candles[j]["low"] for j in range(i-lookback, i+lookback+1) if j != i)

        all_candles[i]["swing_high"] = is_sh
        all_candles[i]["swing_low"] = is_sl

    # Print swings only (to keep output manageable)
    print(f"{'Time':<20} {'Open':>9} {'High':>9} {'Low':>9} {'Close':>9} {'Swing':>10}")
    print("-" * 70)
    prev_sh_price = None
    prev_sl_price = None
    for c in all_candles:
        sh = c.get("swing_high", False)
        sl = c.get("swing_low", False)
        if sh or sl:
            dt = datetime.fromtimestamp(c["epoch"], tz=timezone.utc)
            markers = []
            if sh:
                if prev_sh_price:
                    markers.append("HH" if c["high"] > prev_sh_price else "LH")
                else:
                    markers.append("SH")
                prev_sh_price = c["high"]
            if sl:
                if prev_sl_price:
                    markers.append("HL" if c["low"] > prev_sl_price else "LL")
                else:
                    markers.append("SL")
                prev_sl_price = c["low"]

            marker_str = "/".join(markers)
            print(f"{dt.strftime('%Y-%m-%d %H:%M'):<20} {c['open']:>9.2f} {c['high']:>9.2f} "
                  f"{c['low']:>9.2f} {c['close']:>9.2f} {marker_str:>10}")


asyncio.run(main())
