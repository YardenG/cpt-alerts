#!/usr/bin/env python3
"""
Phase 3 - CLOUD legs: exact option legs from FREE (Yahoo) options data, no IBKR/Gateway.

This is the on-the-go twin of cpt_legs.py. cpt_legs.py uses Yarden's PAID IBKR real-time data at
his desk; THIS module uses free Yahoo option chains (cookie+crumb) + Black-Scholes delta so the
cloud alert can put the legs straight on his PHONE - independent of his Mac, and without the
IBKR-session conflict (logging into IBKR mobile kills the desk Gateway).

Data is ~15 min delayed and greeks are ESTIMATED, so treat it as strike/structure guidance:
the STRIKES + expiry + structure are what he punches into IBKR mobile; he confirms the LIVE
premium in the broker at execution. Every number here is labeled a delayed estimate.

Pure stdlib (runs on GitHub Actions with no install). READ-ONLY: it only reads market data.
"""
import json, math, urllib.request, urllib.parse, http.cookiejar, datetime as dt

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
RISK_FREE = 0.04
CSP_DTE = 7      # near-term CSP (weekly cadence)
LC_DTE = 35      # 99-delta long call expiry (doctrine sec.6B)


def bs_call_delta(S, K, iv, dte_days, r=RISK_FREE):
    """N(d1) - Black-Scholes call delta. Estimates the ~99-delta strike from free data (no greeks)."""
    if not (S and K and iv) or dte_days <= 0:
        return None
    T = dte_days / 365.0
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    return 0.5 * (1 + math.erf(d1 / math.sqrt(2)))


def _session():
    """Yahoo now gates the options endpoint behind a cookie + crumb. Get both once."""
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        op.open(urllib.request.Request("https://fc.yahoo.com", headers=UA), timeout=15)
    except Exception:
        pass  # the request 404s but still sets the A3 cookie
    crumb = op.open(urllib.request.Request(
        "https://query1.finance.yahoo.com/v1/test/getcrumb", headers=UA), timeout=15).read().decode()
    return op, crumb


def _chain(op, crumb, ticker, date_ts=None):
    q = {"crumb": crumb}
    if date_ts:
        q["date"] = date_ts
    url = f"https://query1.finance.yahoo.com/v7/finance/options/{ticker}?{urllib.parse.urlencode(q)}"
    d = json.load(op.open(urllib.request.Request(url, headers=UA), timeout=20))
    return d["optionChain"]["result"][0]


def _dte(ts, today):
    return (dt.datetime.utcfromtimestamp(ts).date() - today).days


def _fmt_date(ts):
    return dt.datetime.utcfromtimestamp(ts).strftime("%b-%d")


def legs(ticker, csp_dte=CSP_DTE, lc_dte=LC_DTE):
    """Return the Two-Trade legs + 99-delta call for `ticker` from free Yahoo options data.
    Raises on network/data failure (caller degrades gracefully)."""
    op, crumb = _session()
    root = _chain(op, crumb, ticker)
    px = root["quote"]["regularMarketPrice"]
    exps = sorted(root["expirationDates"])
    today = dt.date.today()
    future = [e for e in exps if _dte(e, today) > 0]
    if not future or not px:
        return None

    # --- CSP expiry (~weekly): expected-move downside strike, its bid, pre-dialed ATM CC ---
    csp_exp = min(future, key=lambda e: abs(_dte(e, today) - csp_dte))
    cc = _chain(op, crumb, ticker, csp_exp)
    calls, puts = cc["options"][0]["calls"], cc["options"][0]["puts"]
    cdte = _dte(csp_exp, today)
    atm_call = min(calls, key=lambda c: abs(c["strike"] - px))           # ATM ~ CC-at-assignment proxy
    atm_iv = atm_call.get("impliedVolatility")
    # Off-hours / thin data: Yahoo returns 0.0 bids and a missing/~0 IV, which would collapse the CSP
    # to the ATM strike and the 99-delta to a nonsense deep strike. Bail out with a clean marker so
    # the alert says "market closed" instead of printing garbage legs. (Real alerts fire in-hours.)
    if not atm_iv or atm_iv < 0.05:
        return dict(stale=True, px=px)
    em = px * atm_iv * math.sqrt(cdte / 365)
    csp = min(puts, key=lambda p: abs(p["strike"] - (px - em)))
    # Near-term OTM call to WRITE against the 99-delta long = the CCW's income leg (doctrine sec.6B:
    # "immediately write the near-term covered call"). Nearest strike above spot = highest-premium OTM,
    # John's "sell covered calls on green days, a little upside + premium." Same weekly expiry as the CSP.
    otm = [c for c in calls if c["strike"] > px and c.get("bid")]
    ccw_cc = min(otm, key=lambda c: c["strike"]) if otm else None

    # --- 99-delta ITM long call (~35 DTE), delta via Black-Scholes on each strike's IV ---
    lc_exp = min(future, key=lambda e: abs(_dte(e, today) - lc_dte))
    lc_chain = _chain(op, crumb, ticker, lc_exp) if lc_exp != csp_exp else cc
    lddte = _dte(lc_exp, today)
    scored = []
    for c in lc_chain["options"][0]["calls"]:
        iv = c.get("impliedVolatility") or atm_iv
        d = bs_call_delta(px, c["strike"], iv, lddte)
        if d is not None and c.get("ask"):
            scored.append((c, d))
    lc = min(scored, key=lambda cd: abs(cd[1] - 0.99)) if scored else None

    return dict(px=px, atm_iv=atm_iv, em=em,
                csp=csp, csp_exp=csp_exp, cdte=cdte,
                cc_bid=atm_call.get("bid"),        # ATM call bid = pre-dialed CC premium proxy
                ccw_cc=ccw_cc,                     # near-term OTM call = the CCW income leg
                lc=lc[0] if lc else None, lc_delta=lc[1] if lc else None,
                lc_exp=lc_exp, lddte=lddte)


def coc(prem, strike):
    return prem / strike * 100 if (prem and strike) else None


def format_legs(ticker, L, structure=None):
    """Phone-ready legs block for the Telegram alert (HTML), laid out to MATCH the suggested structure
    (`structure` = strategy_pick's label). When the 99-delta ITM CCW is suggested, lead with ITS two
    legs - buy the 99-delta long + write a near-term OTM CC against it - and demote the CSP to a
    one-line alternative. Otherwise keep the CSP Two-Trade lead. Everything labeled a delayed estimate."""
    if not L:
        return None
    if L.get("stale"):
        return ("\U0001F4CA <b>Exact legs</b>: market closed / data thin - legs populate live "
                "during US market hours.")

    # --- build each leg's core string once (whichever data is present) ---
    csp = L.get("csp")
    csp_core = costcc_core = None
    if csp and L.get("em") is not None:
        c = coc(csp.get("bid"), csp["strike"]); cctxt = f" ~{c:.1f}% CoC" if c else ""
        csp_core = (f"sell <b>{csp['strike']:g}P</b> {_fmt_date(L['csp_exp'])} "
                    f"({L['cdte']}d)  bid ~{csp.get('bid')}{cctxt}")
        cc2 = coc(L.get("cc_bid"), csp["strike"]); cc2txt = f" ~{cc2:.1f}% CoC" if cc2 else ""
        costcc_core = f"sell <b>{csp['strike']:g}C</b> at cost basis  ~{L.get('cc_bid')}{cc2txt}"
    lc = L.get("lc"); lc_core = None
    if lc:
        d = L.get("lc_delta"); dtxt = f" ~{d:.2f}d" if d is not None else ""
        lc_core = (f"buy <b>{lc['strike']:g}C</b> {_fmt_date(L['lc_exp'])} "
                   f"({L['lddte']}d){dtxt}  ask ~{lc.get('ask')}")
    ccw_cc = L.get("ccw_cc"); ccwcc_core = None
    if ccw_cc:
        cc = coc(ccw_cc.get("bid"), ccw_cc["strike"]); cctxt = f" ~{cc:.1f}% CoC" if cc else ""
        ccwcc_core = (f"sell <b>{ccw_cc['strike']:g}C</b> {_fmt_date(L['csp_exp'])} "
                      f"({L['cdte']}d)  bid ~{ccw_cc.get('bid')}{cctxt}")

    hdr = "\U0001F4CA <b>Exact legs</b> (Yahoo ~15m delayed - confirm live in IBKR):"
    blocks = [hdr]
    is_ccw = bool(structure) and "CCW" in structure

    if is_ccw and lc_core:
        # Lead with the CCW's TWO legs: buy the deep-ITM long, write the near-term OTM CC against it.
        ccw = ["⭐ <b>99-delta ITM CCW</b> (suggested - green day):",
               f"1) buy the ~99d long: {lc_core}"]
        ccw.append(f"2) write the CC against it: {ccwcc_core}" if ccwcc_core
                   else "2) write a near-term OTM covered call against it (no OTM strike quoting now)")
        blocks.append("\n".join(ccw))
        if csp_core:                                   # CSP demoted to a one-line alternative
            alt = f"Alt - CSP path: {csp_core}"
            if costcc_core:
                alt += f"\nif assigned, {costcc_core}"
            blocks.append(alt)
    else:
        # CSP-led (red day / long-dated / default): T1 CSP, then T2 cost-basis CC + the 99d long.
        if csp_core:
            blocks.append(f"T1 CSP: {csp_core}")
        tail = []
        if costcc_core:
            tail.append(f"T2 CC if assigned: {costcc_core}")
        if lc_core:
            tail.append(f"99d call: {lc_core}")
        if tail:
            blocks.append("\n".join(tail))
    return "\n\n".join(blocks)


if __name__ == "__main__":
    import sys
    tk = (sys.argv[1] if len(sys.argv) > 1 else "TQQQ").upper()
    L = legs(tk)
    if not L:
        print("no data")
    elif L.get("stale"):
        print(f"px={L['px']}  (market closed / thin data - no live legs)")
    else:
        print(f"px={L['px']}  ATM IV={L['atm_iv']:.1%}  expected move=+/-{L['em']:.2f}")
    print(format_legs(tk, L))
