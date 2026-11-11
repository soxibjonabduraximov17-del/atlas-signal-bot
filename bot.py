"""
ATLAS Fibonacci Reversal PRO v2.5 -> Telegram signal bot (XAU/USD, M5).
Python port of Soxibjon's TradingView Pine strategy.

Idea: every run downloads the last days of M5 candles, REPLAYS the whole
strategy bar by bar (exactly like the Pine script), and sends Telegram
messages for events that are new (entry, TP1/TP2/TP3, SL, breakeven move).
"""
import os
import json
import math
import time
import requests
import pandas as pd

SYMBOL = os.getenv("SYMBOL", "GC=F")     # gold futures; "XAUUSD=X" is spot (less reliable)
INTERVAL = "5m"
MIN_CANDLES = 3          # Minimal bir xil shamlar
ENTRY_MODE = "Both"      # "Breakout", "Retest" or "Both"
MAX_WAIT = 100           # Setup muddati (sham)
MINTICK = 0.001          # OANDA XAUUSD tick size
SL_BUFFER = 10 * MINTICK # SL buffer = 10 chart ticks
TP_RATIOS = [1.618, 2.618, 4.236]
TOTAL_LOTS = 0.03
LEG_LOTS = TOTAL_LOTS / 3
LOT_POINT_VALUE = 100.0  # USD per 1.00 price move per 1 lot
BE_PROFIT_USD = 1.0      # total profit locked for remaining legs after TP1
RECENT_MIN = 90          # only announce events from the last N minutes
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
    df = yf.download(SYMBOL, period="5d", interval=INTERVAL, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    df = df.dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    # drop the last candle only if it is still forming
    if len(df) and df.index[-1] + pd.Timedelta(minutes=5) > pd.Timestamp.now(tz="UTC"):
        df = df.iloc[:-1]
    return df


def be_offset(open_legs):
    raw = BE_PROFIT_USD / (open_legs * LEG_LOTS * LOT_POINT_VALUE)
    return math.ceil(raw / MINTICK - 1e-9) * MINTICK


def fmt_entry(side, entry, sl, tps):
    head = "🟢 BUY" if side == 1 else "🔴 SELL"
    lines = [
        f"{head} XAU/USD (M5)",
        f"Kirish: {entry:.2f}",
        f"Stop-loss: {sl:.2f}",
    ]
    for k, tp in enumerate(tps):
        lines.append(f"TP{k + 1} ({TP_RATIOS[k]}): {tp:.2f}")
    lines += [
        "",
        f"3 ta pozitsiya (har biri {LEG_LOTS:.2f} lot). Har biri o'z TP'sida yopiladi.",
        "TP1 dan keyin stop breakeven+ ga ko'chadi.",
        "⚠️ Moliyaviy maslahat emas. Risk o'zingizniki.",
    ]
    return "\n".join(lines)


def simulate(df):
    """Replay the Pine strategy. Returns a list of events (dicts)."""
    O, H, L, C = (df[x].values for x in ("open", "high", "low", "close"))
    idx = df.index
    n = len(df)
    events = []

    streak, streak_dir = 0, 0
    first_open = last_close = None
    setup_dir, fib0, fib100 = 0, None, None
    setup_bar = breakout_bar = None
    breakout_seen = False
    grp = None

    def ev(i, key, text):
        events.append({"time": idx[i], "key": key, "text": text})

    for i in range(n):
        o, h, l, c = O[i], H[i], L[i], C[i]

        # ---- 1. exits are filled by the broker emulator (orders from earlier bars)
        if grp is not None:
            side = grp["side"]
            high_first = abs(h - o) <= abs(o - l)
            fav_first = high_first if side == 1 else (not high_first)

            def do_tps():
                for leg in grp["legs"]:
                    if not leg["open"]:
                        continue
                    hit = h >= leg["tp"] if side == 1 else l <= leg["tp"]
                    if hit:
                        leg["open"] = False
                        if leg["n"] == 1:
                            grp["tp1_done"] = True
                        left = sum(1 for x in grp["legs"] if x["open"])
                        tail = " Hamma pozitsiyalar yopildi." if left == 0 else ""
                        ev(i, f"{grp['t']}:TP{leg['n']}",
                           f"✅ XAU/USD {'BUY' if side == 1 else 'SELL'}: "
                           f"TP{leg['n']} ({leg['tp']:.2f}) ga yetdi, {LEG_LOTS:.2f} lot yopildi.{tail}")

            def do_sl():
                hit = l <= grp["sl"] if side == 1 else h >= grp["sl"]
                open_legs = [x for x in grp["legs"] if x["open"]]
                if hit and open_legs:
                    for x in open_legs:
                        x["open"] = False
                    name = "Breakeven stop" if grp["sl_is_be"] else "Stop-loss"
                    icon = "🟡" if grp["sl_is_be"] else "🔴"
                    ev(i, f"{grp['t']}:SL",
                       f"{icon} XAU/USD {'BUY' if side == 1 else 'SELL'}: "
                       f"{name} ishladi ({grp['sl']:.2f}), {len(open_legs)} ta pozitsiya yopildi.")

            if fav_first:
                do_tps(); do_sl()
            else:
                do_sl(); do_tps()

            if all(not x["open"] for x in grp["legs"]):
                grp = None

        # ---- 2. breakeven management (affects the NEXT bars)
        if grp is not None:
            side = grp["side"]
            if grp["tp1_done"]:
                n_open = sum(1 for x in grp["legs"] if x["open"])
                be = grp["entry"] + side * be_offset(n_open)
                if side == 1 and be < c and be > grp["sl"]:
                    grp["sl"] = be
                    ev(i, f"{grp['t']}:BEMOVE:{be:.2f}",
                       f"🔒 XAU/USD: Stop breakeven+ ga ko'chirildi ({be:.2f}).")
                if side == -1 and be > c and be < grp["sl"]:
                    grp["sl"] = be
                    ev(i, f"{grp['t']}:BEMOVE:{be:.2f}",
                       f"🔒 XAU/USD: Stop breakeven+ ga ko'chirildi ({be:.2f}).")
                grp["sl_is_be"] = True

        flat = grp is None

        # ---- 3. candle series (streak) logic
        cur = 1 if c > o else (-1 if c < o else 0)
        if cur == 0:
            streak, streak_dir, first_open, last_close = 0, 0, None, None
        elif cur == streak_dir:
            streak += 1
            last_close = c
        else:
            if streak >= MIN_CANDLES and flat:
                setup_dir = -streak_dir
                if streak_dir == -1:
                    fib0 = min(first_open, last_close)
                    fib100 = max(first_open, last_close)
                else:
                    fib0 = max(first_open, last_close)
                    fib100 = min(first_open, last_close)
                setup_bar = i
                breakout_bar = None
                breakout_seen = False
            streak, streak_dir, first_open, last_close = 1, cur, o, c

        rng = abs(fib100 - fib0) if fib0 is not None else None
        if setup_dir != 0 and (i - setup_bar > MAX_WAIT
                               or (setup_dir == 1 and c < fib0)
                               or (setup_dir == -1 and c > fib0)):
            setup_dir = 0
            breakout_seen = False
        valid = setup_dir != 0 and rng is not None and rng > MINTICK

        prev_c = C[i - 1] if i > 0 else None
        buy_bo = (valid and setup_dir == 1 and i > setup_bar and prev_c is not None
                  and c > fib100 and prev_c <= fib100)
        sell_bo = (valid and setup_dir == -1 and i > setup_bar and prev_c is not None
                   and c < fib100 and prev_c >= fib100)
        if buy_bo or sell_bo:
            breakout_seen = True
            breakout_bar = i
        buy_rt = (valid and setup_dir == 1 and breakout_seen and i > breakout_bar
                  and l <= fib100 and c > fib100)
        sell_rt = (valid and setup_dir == -1 and breakout_seen and i > breakout_bar
                   and h >= fib100 and c < fib100)

        def want(bo, rt):
            if ENTRY_MODE == "Breakout":
                return bo
            if ENTRY_MODE == "Retest":
                return rt
            return bo or rt

        buy = flat and want(buy_bo, buy_rt)
        sell = flat and want(sell_bo, sell_rt)

        # ---- 4. new entry (market order at the close of the signal bar)
        if buy or sell:
            side = 1 if buy else -1
            sl = fib0 - side * SL_BUFFER
            tps = [fib100 + side * rng * (r - 1) for r in TP_RATIOS]
            grp = {
                "side": side, "entry": c, "sl": sl, "t": str(idx[i]),
                "legs": [{"n": k + 1, "tp": tps[k], "open": True} for k in range(3)],
                "tp1_done": False, "sl_is_be": False,
            }
            ev(i, f"{idx[i]}:ENTRY", fmt_entry(side, c, sl, tps))
            setup_dir = 0
            breakout_seen = False

    return events


def run_once():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    sent = list(state.get("sent", []))

    df = load_candles()
    if len(df) < 50:
        print("not enough data")
        return
    last_t = df.index[-1]

    for e in simulate(df):
        if e["key"] in sent:
            continue
        if last_t - e["time"] > pd.Timedelta(minutes=RECENT_MIN):
            continue
        send(e["text"])
        sent.append(e["key"])

    with open(STATE_FILE, "w") as f:
        json.dump({"sent": sent[-300:]}, f, indent=1)


def main():
    """LOOP_MINUTES=0 -> run once. LOOP_MINUTES>0 -> keep watching for that long."""
    loop_min = float(os.getenv("LOOP_MINUTES", "0") or 0)
    if loop_min <= 0:
        run_once()
        return
    end = time.time() + loop_min * 60
    while True:
        try:
            run_once()
        except Exception as exc:  # keep the loop alive on temporary errors
            print("error:", exc)
        now = time.time()
        nxt = (now // 300 + 1) * 300 + 25   # 25 s after the next 5-minute mark
        if nxt >= end:
            break
        time.sleep(nxt - now)


if __name__ == "__main__":
    main()
