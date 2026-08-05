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
    rows = [(o, h, l, c) for o, h, l, c in
            zip(q["open"], q["high"], q["low"], q["close"])
            if None not in (o, h, l, c)]
    price = res["meta"].get("regularMarketPrice") or rows[-1][3]
    return rows, price

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
    rows, price = fetch(ticker)
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
    return dict(t=ticker, price=price, mid=mid, up=upper, lo=lower, atr=atr,
                pos=pos, rsi=rsi, strong=strong, valid=valid, verdict=verdict)

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
