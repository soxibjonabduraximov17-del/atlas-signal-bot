"""
ATLAS signal bot: XAU/USD, M5.
Rules (from Soxibjon's strategy):
 1. Series of at least 3 red candles.
 2. Fib 100 = series high, Fib 0 = series low.
 3. When a candle CLOSES above the 100 level -> BUY.
 4. Stop-loss slightly below the 0 level.
 5. TP1 = 1.618 (close 50%), TP2 = 2.618 (25%), TP3 = 4.236 (25%).
Levels are measured as: low + ratio * (high - low).
"""
import os
import json
import requests
import pandas as pd

SYMBOL = os.getenv("SYMBOL", "GC=F")   # gold futures; try "XAUUSD=X" for spot
INTERVAL = "5m"
MIN_RED = 3             # minimum candles in the red series
MAX_WAIT = 24           # breakout must happen within N candles after the series
RECENT = 2              # only signal if the breakout is within the last N closed candles
SL_BUFFER_PCT = 0.05    # stop = low - 5% of the series range
TP_RATIOS = [1.618, 2.618, 4.236]
TP_SHARES = [50, 25, 25]
STATE_FILE = "state.json"

TOKEN = os.getenv("BOT_TOKEN")
CHAT = os.getenv("CHANNEL_ID")


def send(text):
    if not TOKEN or not CHAT:
        print("[no token] " + text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT, "text": text},
        timeout=20,
    )
    print("telegram:", r.status_code)


def load_candles():
    import yfinance as yf
    df = yf.download(SYMBOL, period="2d", interval=INTERVAL, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    df = df.dropna()
    return df.iloc[:-1]  # drop the candle that is still forming


def find_signals(df):
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    n = len(df)
    red = c < o
    signals = []
    i = 0
    while i < n:
        if not red[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and red[j + 1]:
            j += 1
        if j - i + 1 >= MIN_RED:
            high, low = h[i:j + 1].max(), l[i:j + 1].min()
            rng = high - low
            for k in range(j + 1, min(n, j + 1 + MAX_WAIT)):
                if c[k] < low:      # setup invalidated
                    break
                if c[k] > high:     # Fib 100 broken -> BUY
                    if k >= n - RECENT and rng > 0:
                        tps = [low + r * rng for r in TP_RATIOS]
                        entry = float(c[k])
                        if entry < tps[0]:
                            signals.append({
                                "key": str(df.index[i]),
                                "time": str(df.index[k]),
                                "entry": entry,
                                "sl": float(low - rng * SL_BUFFER_PCT),
                                "tps": [float(t) for t in tps],
                                "hit": 0,
                                "status": "open",
                            })
                    break
        i = j + 1
    return signals


def update_trades(df, state):
    for t in state["trades"]:
        if t["status"] != "open":
            continue
        t0 = pd.Timestamp(t["time"])
        for ts, row in df[df.index > t0].iterrows():
            if row["low"] <= t["sl"]:  # if SL and TP in one candle, assume SL first
                send(f"🔴 XAU/USD: Stop-loss ishladi ({t['sl']:.2f}). Savdo yopildi.")
                t["status"] = "closed"
                break
            while t["hit"] < 3 and row["high"] >= t["tps"][t["hit"]]:
                k = t["hit"]
                send(f"✅ XAU/USD: TP{k + 1} ({t['tps'][k]:.2f}) ga yetdi. "
                     f"{TP_SHARES[k]}% yopildi.")
                t["hit"] += 1
            if t["hit"] == 3:
                send("🏁 XAU/USD: Barcha TP'lar bajarildi. Savdo yopildi.")
                t["status"] = "closed"
                break


def format_signal(s):
    lines = [
        "🟢 BUY XAU/USD (M5)",
        f"Kirish: {s['entry']:.2f}",
        f"Stop-loss: {s['sl']:.2f}",
    ]
    for k, tp in enumerate(s["tps"]):
        lines.append(f"TP{k + 1} ({TP_RATIOS[k]}): {tp:.2f} — {TP_SHARES[k]}% yopiladi")
    lines.append("")
    lines.append("⚠️ Moliyaviy maslahat emas. Risk o'zingizniki.")
    return "\n".join(lines)


def main():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"trades": []}

    df = load_candles()
    if len(df) < 10:
        print("not enough data")
        return

    update_trades(df, state)

    known = {t["key"] for t in state["trades"]}
    for s in find_signals(df):
        if s["key"] in known:
            continue
        send(format_signal(s))
        state["trades"].append(s)

    state["trades"] = state["trades"][-100:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


if __name__ == "__main__":
    main()
