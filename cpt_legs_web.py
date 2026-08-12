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
                lc=lc[0] if lc else None, lc_delta=lc[1] if lc else None,
                lc_exp=lc_exp, lddte=lddte)


def coc(prem, strike):
    return prem / strike * 100 if (prem and strike) else None


def format_legs(ticker, L):
    """Phone-ready legs block for the Telegram alert (HTML). Blocks joined by blank lines:
    header / T1 CSP / (T2 CC + 99d call together). Everything labeled a delayed estimate."""
    if not L:
        return None
    if L.get("stale"):
        return ("\U0001F4CA <b>Exact legs</b>: market closed / data thin - legs populate live "
                "during US market hours.")
    blocks = ["\U0001F4CA <b>Exact legs</b> (Yahoo ~15m delayed - confirm live in IBKR):"]
    t2 = None
    csp = L.get("csp")
    if csp and L.get("em") is not None:
        c = coc(csp.get("bid"), csp["strike"])
        cctxt = f" ~{c:.1f}% CoC" if c else ""
        blocks.append(f"T1 CSP: sell <b>{csp['strike']:g}P</b> {_fmt_date(L['csp_exp'])} "
                      f"({L['cdte']}d)  bid ~{csp.get('bid')}{cctxt}")
        cc2 = coc(L.get("cc_bid"), csp["strike"])
        cc2txt = f" ~{cc2:.1f}% CoC" if cc2 else ""
        t2 = (f"T2 CC if assigned: sell <b>{csp['strike']:g}C</b> at cost basis  "
              f"~{L.get('cc_bid')}{cc2txt}")
    lc_line = None
    lc = L.get("lc")
    if lc:
        d = L.get("lc_delta")
        dtxt = f" ~{d:.2f}d" if d is not None else ""
        lc_line = (f"99d call: buy <b>{lc['strike']:g}C</b> {_fmt_date(L['lc_exp'])} "
                   f"({L['lddte']}d){dtxt}  ask ~{lc.get('ask')}")
    tail = "\n".join(x for x in (t2, lc_line) if x)   # T2 + 99d in one block, no blank between
    if tail:
        blocks.append(tail)
    return "\n\n".join(blocks)                          # blank line between header / T1 / (T2+99d)


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
