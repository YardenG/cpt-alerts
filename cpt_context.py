#!/usr/bin/env python3
"""
Phase 3 - SITUATIONAL AWARENESS layer (Step 1: deterministic engine).

John reads the whole tape before an entry, though he never calls it that: on the
AAPU trade (video 2026-08) he entered the day AFTER Apple's -7% earnings drop, once
the "IV crush" was behind him, because the story was intact (iPhone 18 + holiday
season) and the drop itself "was the downside protection." The Keltner gate says
WHEN (price pulled back); THIS layer answers "is THIS the good kind of pullback?" -
post-event blip on an intact story into a supportive tape (enter), vs a pre-earnings
landmine / risk-off tape / broken story (skip). It is the anti-Zillow overlay.

DETERMINISTIC ONLY (this step): market regime, sector trend, earnings proximity +
recent event-move, seasonality. No news/LLM yet (that is the on-demand Tier-2 `/why`).
ADVISE-ONLY: this NEVER changes the calibrated entry gate - it enriches the alert so
Yarden decides. (We reversed bolt-on entry filters twice; the gate stays untouched.)

Data: free Yahoo (chart + quoteSummary earnings via cookie+crumb). Pure stdlib.
Reuses the validated fetch/ema from cpt_data_spike. Run:
  python3 cpt_context.py TQQQ           # one ticker's context card
  python3 cpt_context.py AAPU NVDX DPST # several
"""
import json, sys, urllib.request, urllib.parse, http.cookiejar, datetime as dt
from cpt_data_spike import fetch, ema, analyze

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Single-stock 2x ETF -> its underlying (for earnings + the Tier-2 news layer later).
UNDERLYING = {"AAPU": "AAPL", "NVDX": "NVDA", "NVDL": "NVDA", "AMZU": "AMZN",
              "GGLL": "GOOGL", "METU": "META", "MSFU": "MSFT", "CONL": "COIN",
              "SMCX": "SMCI"}

# Ticker -> the sector/benchmark ETF whose trend is the relevant tailwind/headwind.
SECTOR = {"TQQQ": "QQQ", "AAPU": "QQQ", "MSFU": "QQQ", "METU": "QQQ", "GGLL": "QQQ",
          "AMZU": "QQQ", "SOXL": "SMH", "NVDX": "SMH", "NVDL": "SMH", "SMCX": "SMH",
          "TNA": "IWM", "DPST": "KRE", "LABU": "XBI", "YINN": "FXI", "NAIL": "XHB",
          "CONL": "IBIT", "IREN": "IBIT", "BITX": "IBIT"}

# Hand-kept seasonality flags (month -> note). Small on purpose; John cites these by feel.
SEASON_MONTH = {12: "Santa-rally / Q4 retail window", 11: "holiday retail ramp",
                1: "January effect / new-year flows"}
SEASON_TICKER = {  # (ticker or underlying) -> {month: note}
    "AAPL": {9: "iPhone launch (Sept)", 10: "iPhone launch tailwind", 11: "holiday iPhone demand",
             12: "holiday iPhone demand"},
    "NVDA": {}, "AMZN": {11: "holiday / Prime + AWS", 12: "holiday retail"},
}

PRE_EARN_DAYS = 10       # earnings THIS imminent = don't open (his avoid rule, sharpest form)
EARN_WINDOW_DAYS = 25    # earnings this soon lands INSIDE a ~30d covered call = flag it (fine for a weekly)
EVENT_MOVE_PCT = 4.0     # a >=4% single-day move in the underlying = earnings-scale shock
EVENT_LOOKBACK = 5       # sessions

# --- Vol-regime / complacency (Yarden's "market at its own Keltner top" instinct, done honestly) ---
# Low VIX ALONE is just calm (vol clusters low - it does NOT forecast a near-term selloff). FRAGILE =
# low VIX *and* an extended market = the true macro Keltner-top: thin premium, size down, tighten
# protection. Not a timing/predict signal - a sizing/posture one. VOL WAKING UP (VIX 1-day pop) is the
# real early warning. Thresholds are tunable; this is OUR extension of John's "bag holder at the top."
VIX_LOW_PCT = 10.0       # bottom-decile VIX (of the trailing year) = complacency
EXTENDED_A50 = 5.0       # avg of SPY & QQQ >= 5% above their 50-day MA = market extended
VIX_ROC = 12.0           # VIX up >=12% in a day = vol waking up (even from a low base)


def _session():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        op.open(urllib.request.Request("https://fc.yahoo.com", headers=UA), timeout=15)
    except Exception:
        pass
    crumb = op.open(urllib.request.Request(
        "https://query1.finance.yahoo.com/v1/test/getcrumb", headers=UA), timeout=15).read().decode()
    return op, crumb


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _trend(ticker):
    """Sector/benchmark ETF trend from its own price vs the 50 & 200-day SMA."""
    try:
        rows, price, _ = fetch(ticker, rng="1y")
        closes = [r[3] for r in rows]
        s50, s200 = sma(closes, 50), sma(closes, 200)
        if not (s50 and s200):
            return dict(t=ticker, label="n/a", price=price)
        score = (price > s50) + (price > s200)
        label = "uptrend" if score == 2 else "downtrend" if score == 0 else "mixed"
        a50 = (price / s50 - 1) * 100 if s50 else None   # % above the 50-day MA = "extension"
        return dict(t=ticker, label=label, price=price, s50=s50, s200=s200, a50=a50)
    except Exception as e:
        return dict(t=ticker, label="n/a", err=type(e).__name__)


def regime():
    """Risk-on / neutral / risk-off from SPY & QQQ vs their MAs + the VIX level/percentile,
    PLUS a complacency read: FRAGILE (low VIX + extended market) and VOL-WAKING-UP (VIX 1d pop)."""
    spy, qqq = _trend("SPY"), _trend("QQQ")
    score = 0
    for x in (spy, qqq):
        score += {"uptrend": 2, "mixed": 1, "downtrend": 0}.get(x["label"], 1)
    vix_lvl = vix_pct = vix_roc = None
    try:
        vrows, vprice, _ = fetch("%5EVIX", rng="1y")
        vcloses = [r[3] for r in vrows]
        vix_lvl = vprice
        vix_pct = round(100 * sum(c <= vprice for c in vcloses) / len(vcloses))
        if len(vcloses) >= 2 and vcloses[-2]:
            vix_roc = (vcloses[-1] - vcloses[-2]) / vcloses[-2] * 100
    except Exception:
        pass
    # market extension = avg of SPY & QQQ distance above their own 50-day MA
    ext_vals = [x["a50"] for x in (spy, qqq) if x.get("a50") is not None]
    ext_pct = sum(ext_vals) / len(ext_vals) if ext_vals else None
    extended = ext_pct is not None and ext_pct >= EXTENDED_A50
    # FRAGILE = complacency (bottom-decile VIX) AND an extended market. Low VIX alone is NOT fragile.
    fragile = (vix_pct is not None and vix_pct <= VIX_LOW_PCT) and extended
    vol_waking = vix_roc is not None and vix_roc >= VIX_ROC
    risk_off = (score <= 1) or (vix_lvl is not None and vix_lvl >= 25)
    risk_on = (score >= 3) and (vix_lvl is None or vix_lvl < 20)
    label = "RISK-OFF" if risk_off else "RISK-ON" if risk_on else "NEUTRAL"
    return dict(label=label, score=score, spy=spy, qqq=qqq, vix=vix_lvl, vix_pct=vix_pct,
                vix_roc=vix_roc, ext_pct=ext_pct, extended=extended,
                fragile=fragile, vol_waking=vol_waking)


def earnings(underlying, op, crumb):
    """Days to next earnings for the underlying (John's avoid window) + the recent
    earnings-scale event move (his post-earnings-dip trigger)."""
    out = dict(u=underlying, next=None, days=None, state="clear", move=None, move_day=None)
    today = dt.date.today()
    try:
        url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{underlying}"
               f"?modules=calendarEvents&crumb={urllib.parse.quote(crumb)}")
        d = json.load(op.open(urllib.request.Request(url, headers=UA), timeout=20))
        ed = d["quoteSummary"]["result"][0]["calendarEvents"]["earnings"]["earningsDate"]
        if ed:
            nd = dt.datetime.utcfromtimestamp(ed[0]["raw"]).date()
            out["next"] = nd.isoformat()
            out["days"] = (nd - today).days
    except Exception as e:
        out["err"] = type(e).__name__
    # recent earnings-scale single-day move in the underlying (deterministic post-event proxy)
    try:
        rows, _, _ = fetch(underlying, rng="1mo")
        closes = [r[3] for r in rows]
        for i in range(len(closes) - 1, max(0, len(closes) - 1 - EVENT_LOOKBACK), -1):
            pct = (closes[i] - closes[i - 1]) / closes[i - 1] * 100
            if abs(pct) >= EVENT_MOVE_PCT:
                out["move"], out["move_day"] = round(pct, 1), len(closes) - 1 - i
                break
    except Exception:
        pass
    d = out["days"]
    if d is not None and 0 <= d <= PRE_EARN_DAYS:
        out["state"] = "PRE-EARNINGS"          # landmine: John avoids opening into the print
    elif d is not None and PRE_EARN_DAYS < d <= EARN_WINDOW_DAYS:
        out["state"] = "EARNINGS SOON"         # lands inside a ~30d CC; fine for a weekly - check your expiry
    elif out["move"] is not None and out["move"] < 0:
        out["state"] = "POST-EVENT DIP"        # his AAPU moment: event behind, IV crushed, drop = cushion
    elif out["move"] is not None and out["move"] > 0:
        out["state"] = "POST-EVENT POP"        # gapped up on the event - chasing risk
    return out


def context(ticker, reg=None, a=None):
    # reg/a can be passed in so a batch scan computes the (expensive) regime ONCE and reuses the
    # analyze() it already has, instead of refetching SPY/QQQ/VIX per ticker.
    a = a if a is not None else analyze(ticker)   # where price sits in the Keltner range
    reg = reg if reg is not None else regime()
    sec = _trend(SECTOR.get(ticker, "SPY"))
    sec["name"] = SECTOR.get(ticker, "SPY")
    und = UNDERLYING.get(ticker)
    earn = None
    if und:
        op, crumb = _session()
        earn = earnings(und, op, crumb)
    # seasonality
    m = dt.date.today().month
    seas = []
    if m in SEASON_MONTH:
        seas.append(SEASON_MONTH[m])
    tkey = und or ticker
    if tkey in SEASON_TICKER and m in SEASON_TICKER[tkey]:
        seas.append(SEASON_TICKER[tkey][m])
    return dict(t=ticker, a=a, regime=reg, sector=sec, earn=earn, season=seas)


def read_line(c):
    """One-line advise-only synthesis, John-grounded."""
    a, reg, earn = c["a"], c["regime"], c["earn"]
    bits = []
    if reg.get("vol_waking"):
        bits.append(f"VOL WAKING UP (VIX {reg['vix_roc']:+.0f}%/d) - the low-vol tape may be flipping; "
                    f"tighten protection, don't add size")
    elif reg.get("fragile"):
        bits.append(f"RISK-ON but FRAGILE (VIX p{reg['vix_pct']} + market extended) - John's 'bag holder "
                    f"at the top' at the index level: thin premium, take smaller bites, favor downside protection")
    if earn and earn["state"] == "PRE-EARNINGS":
        bits.append(f"{earn['u']} reports in {earn['days']}d - John AVOIDS opening into earnings; "
                    f"wait for the print, then the post-earnings IV crush is his prime entry")
    elif earn and earn["state"] == "EARNINGS SOON":
        bits.append(f"{earn['u']} reports in {earn['days']}d ({earn['next']}) - lands inside a ~30d CC; "
                    f"OK for a weekly, but keep your expiry BEFORE the print or accept the event risk")
    elif earn and earn["state"] == "POST-EVENT DIP" and a["pos"] <= 55:
        bits.append(f"{earn['u']} dropped {earn['move']}% {earn['move_day']}d ago and price sits low in "
                    f"the range = the John post-event dip ('the drop is the downside protection') - "
                    f"confirm the story is intact")
    elif earn and earn["state"] == "POST-EVENT POP":
        bits.append(f"{earn['u']} popped {earn['move']}% on a recent event - chasing; John waits for the pullback")
    calm = not (reg.get("fragile") or reg.get("vol_waking"))
    if reg["label"] == "RISK-OFF":
        bits.append("RISK-OFF tape - John sizes DOWN in risk-off (smaller bites)")
    elif reg["label"] == "RISK-ON" and c["sector"]["label"] == "uptrend" and calm:
        bits.append(f"RISK-ON tape + {c['sector']['name']} uptrend = supportive backdrop")
    if not bits:
        bits.append(f"{reg['label']} tape, {c['sector']['name']} {c['sector']['label']} - neutral backdrop")
    return " | ".join(bits)


def card(c):
    """Phone-scannable context block (drops into the Telegram alert as a card)."""
    reg, sec, earn = c["regime"], c["sector"], c["earn"]
    vix = f"{reg['vix']:.1f} (p{reg['vix_pct']})" if reg["vix"] is not None else "n/a"
    if reg.get("vix_roc") is not None:
        vix += f" {reg['vix_roc']:+.0f}%/d"
    L = [f"\U0001F30D SITUATIONAL AWARENESS - {c['t']}",
         f"• Tape: {reg['label']}  (SPY {reg['spy']['label']}, QQQ {reg['qqq']['label']}, VIX {vix})"]
    if reg.get("fragile"):
        L.append(f"• ⚠ FRAGILE: VIX p{reg['vix_pct']} + market extended (+{reg['ext_pct']:.1f}% vs 50MA) "
                 f"- thin premium, size DOWN, favor downside protection")
    if reg.get("vol_waking"):
        L.append(f"• ⚠ VOL WAKING UP: VIX {reg['vix_roc']:+.0f}% today - complacency may be breaking")
    L.append(f"• Sector {sec['name']}: {sec['label']}")
    if earn:
        e = f"• Earnings ({earn['u']}): {earn['state']}"
        if earn["days"] is not None:
            e += f"  next {earn['next']} ({earn['days']}d)"
        if earn["move"] is not None:
            e += f"  [{earn['move']:+}% {earn['move_day']}d ago]"
        L.append(e)
    if c["season"]:
        L.append(f"• Season: {'; '.join(c['season'])}")
    L.append(f"→ {read_line(c)}")
    return "\n".join(L)


def main():
    tickers = sys.argv[1:] or ["TQQQ"]
    for i, tk in enumerate(tickers):
        if i:
            print()
        try:
            print(card(context(tk.upper())))
        except Exception as e:
            print(f"{tk}: ERROR {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
