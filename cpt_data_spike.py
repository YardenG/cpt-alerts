#!/usr/bin/env python3
"""
Phase 3 - Step 1a DATA SPIKE (free feed, price/indicator half).

Proves we can pull the exact inputs the CPT ENTRY rule needs and compute them
ourselves, with zero paid data and no heavy deps (pure stdlib). Validates the
Keltner-channel entry gate + RSI against Yahoo defaults (which is what John uses).

What it does NOT do yet (needs the IBKR/options layer - Step 1b):
  - true 99-delta strike selection, live IV, expected-move strike, his positions.

Doctrine inputs (cpt-doctrine.md sec.3-4):
  - Keltner Channel: CONFIRMED from Yarden's own Yahoo chart 2026-07-30 = "(10,5,ema)" =
    EMA(10) midline, bands +/- 5 x Wilder-ATR(10). (Reproduced his TQQQ 64.98/87.34/42.36
    to the penny.) This CORRECTS the old web-guessed (20, 2.0, 10).
  - Entry gate: price in the LOWER ~25% of the channel, NEVER above the top; RSI ~40-50.
Data source: Yahoo chart JSON (no key). Swap-in point for IBKR later = fetch().
"""
import json, urllib.request, sys

KC_LEN, KC_MULT, ATR_LEN = 10, 5.0, 10   # confirmed vs Yarden's Yahoo chart

UNIVERSE = ["DPST", "TQQQ", "TNA", "IREN", "NVDL", "LABU", "NAIL", "GGLL",
            "METU", "SOXL", "AAPU", "AMZU"]

def fetch(ticker, rng="3mo", interval="1d"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range={rng}&interval={interval}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    res = d["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    ts = res.get("timestamp", [])
    packed = [(t, o, h, l, c) for t, o, h, l, c in
              zip(ts, q["open"], q["high"], q["low"], q["close"])
              if None not in (o, h, l, c)]
    rows = [(o, h, l, c) for (t, o, h, l, c) in packed]
    times = [t for (t, o, h, l, c) in packed]
    price = res["meta"].get("regularMarketPrice") or (rows[-1][3] if rows else None)
    return rows, price, times

def ema(vals, n):
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n            # seed with SMA
    out = [None] * (n - 1) + [e]
    for v in vals[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def wilder_atr(rows, n=10):
    trs = []
    for i in range(1, len(rows)):
        h, l, pc = rows[i][1], rows[i][2], rows[i - 1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:n]) / n
    out = [None] * n + [atr]          # aligned to rows index
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
        out.append(atr)
    return out

def wilder_rsi(closes, n=14):
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0); losses += max(-ch, 0)
    ag, al = gains / n, losses / n
    rsi = [None] * n
    rsi.append(100 - 100 / (1 + (ag / al if al else 1e9)))
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
        rsi.append(100 - 100 / (1 + (ag / al if al else 1e9)))
    return rsi

def analyze(ticker):
    rows, price, _ = fetch(ticker)
    closes = [r[3] for r in rows]
    mid = ema(closes, KC_LEN)[-1]
    atr = wilder_atr(rows, ATR_LEN)[-1]
    rsi = wilder_rsi(closes, 14)[-1]
    upper, lower = mid + KC_MULT * atr, mid - KC_MULT * atr
    pos = (price - lower) / (upper - lower) * 100 if upper > lower else float("nan")
    # entry gate - CALIBRATED to 95 real entries (phase3-entry-calibration.md):
    #   VALID  = pos <= 60 (at/below mid, never upper third) AND RSI < 70   -> ~92% of his entries
    #   STRONG = pos 38-55 AND RSI 42-58 (the dense core)
    strong = (38 <= pos <= 55) and (42 <= rsi <= 58)
    valid = (0 <= pos <= 60) and rsi < 70
    verdict = ("BELOW CH" if pos < 0 else       # below lower band = oversold/knife
               "ENTRY *" if strong else
               "ENTRY" if valid else
               "watch" if pos <= 68 and rsi < 70 else
               "TOO HIGH")
    # day direction (for the strategy pick): current price vs the previous daily close.
    prev_close = closes[-2] if len(closes) >= 2 else closes[-1]
    chg = price - prev_close
    day = "green" if chg >= 0 else "red"
    return dict(t=ticker, price=price, mid=mid, up=upper, lo=lower, atr=atr,
                pos=pos, rsi=rsi, strong=strong, valid=valid, verdict=verdict,
                prev_close=prev_close, chg=chg, day=day)


def strategy_pick(a):
    """Which structure fits THIS entry, per John's doctrine (cpt-doctrine.md sec.4 + 6B/6C).
    `a` is an analyze() dict. Returns (label, why). SUGGESTION only: account type can override
    (sec.6C - he does the ITM CC in IRA / unsettled-cash situations; a CSP needs settled cash).

    Confirmed rules used:
      - "Buy on down days for CSPs" (put premium fattens on red days; enter with downside cushion).
      - "Sell covered calls on green/up days" (call premium fattens); the 99-delta ITM CCW is his
        capital-efficient core and most-common structure.
      - Long-dated ATM CSP = down-market "parking money" (300-361d, ~26-30%) on a beaten-down ETF.
    """
    pos, day, verdict = a["pos"], a.get("day"), a.get("verdict")
    if verdict == "BELOW CH" or pos < 15:
        return ("Long-dated ATM CSP (LEAPS-like)",
                "deep in the range / below the channel = John's down-market 'parking money' play "
                "(300-361d ATM CSP, ~26-30%). A near-term CSP works too if you want the shares.")
    if day == "red":
        return ("CSP (cash-secured put)",
                "down day: put premium is fatter and you enter with downside protection "
                "(John: 'buy on down days for CSPs').")
    if day == "green":
        return ("99-delta ITM CCW",
                "up day: call premium is fatter (John: 'sell covered calls on green days'); "
                "the 99-delta ITM covered call is his capital-efficient core.")
    return ("99-delta ITM CCW",
            "his primary/most-common structure. Prefer a CSP on a down day or if you want the "
            "shares; account type can override (ITM CC for IRA / unsettled cash).")

def series(ticker, n=40):
    """Last `n` daily CANDLES (o/h/l/c + date) + the Keltner band series, for charting (pure stdlib,
    same math as analyze / the TradingView indicator). Bands are {x:date, y:value} point-lists so
    they overlay the candlesticks on a time axis."""
    rows, price, times = fetch(ticker)
    closes = [r[3] for r in rows]
    mids = ema(closes, KC_LEN)
    atrs = wilder_atr(rows, ATR_LEN)
    pts = []
    for i in range(len(rows)):
        if mids[i] is None or atrs[i] is None:
            continue
        o, h, l, c = rows[i]
        x = times[i] * 1000   # epoch-ms: Chart.js v4's time axis needs a NUMERIC x, not a date string
        pts.append(dict(x=x, o=round(o, 2), h=round(h, 2), l=round(l, 2), c=round(c, 2),
                        up=round(mids[i] + KC_MULT * atrs[i], 2), mid=round(mids[i], 2),
                        lo=round(mids[i] - KC_MULT * atrs[i], 2)))
    tail = pts[-n:]
    return dict(
        candles=[{"x": p["x"], "o": p["o"], "h": p["h"], "l": p["l"], "c": p["c"]} for p in tail],
        upper=[{"x": p["x"], "y": p["up"]} for p in tail],
        mid=[{"x": p["x"], "y": p["mid"]} for p in tail],
        lower=[{"x": p["x"], "y": p["lo"]} for p in tail])


def main():
    tickers = sys.argv[1:] or ["DPST", "TQQQ", "TNA"]
    if tickers == ["ALL"]:
        tickers = UNIVERSE
    print(f"{'TKR':5} {'price':>8} {'KC_low':>8} {'KC_mid':>8} {'KC_up':>8} "
          f"{'pos%':>6} {'RSI':>5}  gate")
    print("-" * 72)
    for tk in tickers:
        try:
            a = analyze(tk)
        except Exception as e:
            print(f"{tk:5} ERROR: {e}"); continue
        print(f"{a['t']:5} {a['price']:8.2f} {a['lo']:8.2f} {a['mid']:8.2f} "
              f"{a['up']:8.2f} {a['pos']:6.1f} {a['rsi']:5.1f}  {a['verdict']:9}")
    print("\nGate (calibrated to 95 real entries): ENTRY = pos<=60 & RSI<70;")
    print("  ENTRY * (strong) = pos 38-55 & RSI 42-58;  TOO HIGH = upper third / RSI>=70.")
    print("Keltner (10,5,ema): EMA(10) mid, +/-5x Wilder-ATR(10) - matches Yarden's Yahoo chart.")
    print("Data: Yahoo (free, ~15min delayed).  BELOW CH = under the lower band (oversold).")

if __name__ == "__main__":
    main()
