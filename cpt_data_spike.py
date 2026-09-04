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
import json, urllib.request, urllib.error, sys, time

KC_LEN, KC_MULT, ATR_LEN = 10, 5.0, 10   # confirmed vs Yarden's Yahoo chart


def net_retry(call, tries=4, backoff=3):
    """Run a zero-arg network `call`, retrying TRANSIENT blips (DNS/connection/timeout/5xx) with a
    3/6/9s backoff. A single flaky moment on the runner (e.g. URLError [Errno -2] Name or service
    not known) used to kill a whole run; now it's absorbed. Permanent 4xx (bad request) raise at once."""
    last = None
    for i in range(tries):
        try:
            return call()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        if i < tries - 1:
            time.sleep(backoff * (i + 1))
    raise last

UNIVERSE = ["DPST", "TQQQ", "TNA", "IREN", "NVDL", "LABU", "NAIL", "GGLL",
            "METU", "SOXL", "AAPU", "AMZU"]

def fetch(ticker, rng="3mo", interval="1d"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range={rng}&interval={interval}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = net_retry(lambda: json.load(urllib.request.urlopen(req, timeout=20)))
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
    """Which structure fits THIS entry, per John's doctrine (cpt-doctrine.md sec.4 + 6B/6C + the
    v0.5 CSP->99-Delta shift). `a` is an analyze() dict. Returns (label, why). SUGGESTION only.

    2026 SHIFT (his 2026-09-01 email, captured in doctrine v0.5): John moved his DEFAULT structure
    from the OTM cash-secured put to the deep-ITM / 99-Delta covered call, for CAPITAL EFFICIENCY -
    ~half the capital tied up for ~2x the weekly cash return on the SAME trade (his own framing:
    'the trade is Strike/Expire/Premium; CSP vs 99-Delta is just how we STRUCTURE it'). So we now
    LEAD with the 99-Delta ITM CCW in every normal entry, and demote the CSP to the noted
    alternative. He has NOT abandoned the CSP - it stays valid for IRA / unsettled cash, when you
    actually want the shares, or to place the strike at the expected-move downside on a red day.

    Confirmed rules still honored:
      - Down days still fatten PUT premium / give downside cushion -> CSP is the day-appropriate
        ALTERNATIVE, not the lead.
      - Long-dated ATM CSP = down-market 'parking money' (300-361d, ~26-30%) on a beaten-down ETF -
        a distinct deep-in-range play, kept as-is.
    """
    pos, day, verdict = a["pos"], a.get("day"), a.get("verdict")
    if verdict == "BELOW CH" or pos < 15:
        return ("Long-dated ATM CSP (LEAPS-like)",
                "deep in the range / below the channel = John's down-market 'parking money' play "
                "(300-361d ATM CSP, ~26-30%). A near-term CSP works too if you want the shares.")
    if day == "red":
        return ("99-delta ITM CCW",
                "John now leads with the 99-Delta ITM CC even on down days for capital efficiency "
                "(~half the capital, same strike/expire/premium). ALT: a CSP has fatter put premium "
                "+ downside cushion on red days - use it for IRA / unsettled cash or if you want the "
                "shares.")
    if day == "green":
        return ("99-delta ITM CCW",
                "up day: call premium is fattest AND it's John's capital-efficient core since his "
                "2026 shift (~half the capital, ~2x the weekly cash vs a CSP). ALT: CSP for IRA / "
                "unsettled cash or if you want the shares.")
    return ("99-delta ITM CCW",
            "his primary structure since the 2026 shift - capital-efficient (~half the capital, "
            "~2x weekly cash vs the CSP) on the same trade. ALT: CSP on a down day, for IRA / "
            "unsettled cash, or when you want the shares.")

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
