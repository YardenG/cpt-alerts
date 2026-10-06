#!/usr/bin/env python3
"""
CPT -> Alpaca  |  Phase 2, step 1  -  THE ENTRY EXECUTOR  (dry-run by default)

Turns a live CPT entry alert into the EXACT Alpaca paper order it would place, so
Yarden can eyeball a real example before anything auto-submits. It reuses the SAME
doctrine as the rest of the system - the gate and the structure pick come straight
from cpt_data_spike (analyze + strategy_pick), and the sizing mirrors cpt_account
(10% per trade, min(10 lots, budget // capital-per-contract)). Only the LEG SOURCE
changes: strikes/greeks/prices come from Alpaca (the broker the order goes to), not
Yahoo, so the plan is broker-grade.

  - 99-Delta ITM CCW (the primary): buy a ~99-delta ~35-DTE call, sell a ~7-DTE OTM
    call against it = a multi-leg (mleg) diagonal, the exact structure the probe
    proved Alpaca accepts.
  - Long-dated ATM CSP (below-channel case): sell one ~330-DTE ATM put (Level 1).

DRY-RUN prints the full plan and submits NOTHING. --live submits + reconciles
(kept behind an explicit flag; wire-up for the next increment).

  python3 cpt_alpaca_open.py TQQQ            # dry-run plan for one name
  python3 cpt_alpaca_open.py TQQQ --force     # ignore the ENTRY gate (manual test)
  python3 cpt_alpaca_open.py scan             # gate the whole universe, plan every valid entry
"""
import datetime as dt, math, os, sys
import alpaca_api as A

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cpt_data_spike as ds
from cpt_legs_web import bs_call_delta        # same BS delta the sim uses (one source of truth)

# --- doctrine constants (source of truth noted beside each) --------------------------------------
LC_DTE      = 35     # 99-delta long call expiry  (cpt_legs_web.LC_DTE / doctrine sec.6B)
SHORT_DTE   = 7      # near-term OTM covered call / weekly cadence  (cpt_legs_web.CSP_DTE)
LONGCSP_DTE = 330    # long-dated ATM CSP "parking money"  (strategy_pick: 300-361d)
UNIT        = 10     # engine normalized lot size  (cpt_paper.CONTRACTS)
PER_TRADE_PCT = 0.10 # 10% of the account per trade  (cpt_account.PER_TRADE_PCT)


# --- OCC symbol parsing (avoids a second round-trip for strike/expiry) ---------------------------
def parse_occ(s):
    """'TQQQ261113C00076000' -> ('TQQQ', '2026-11-13', 'C', 76.0)."""
    strike = int(s[-8:]) / 1000.0
    right = s[-9]
    d = s[-15:-9]
    root = s[:-15]
    exp = f"20{d[0:2]}-{d[2:4]}-{d[4:6]}"
    return root, exp, right, strike


def _dte(exp):
    return (dt.date.fromisoformat(exp) - dt.date.today()).days


def _quote(v):
    q = v.get("latestQuote") or {}
    return q.get("bp"), q.get("ap")        # bid, ask


def _expiry_near(sym, right, target_dte):
    """Nearest listed expiry to target_dte (via the contracts list; cheap, no greeks)."""
    today = dt.date.today()
    gte = (today + dt.timedelta(days=max(1, target_dte - 12))).isoformat()
    cs = A.option_contracts(sym, limit=500, type=("call" if right == "C" else "put"),
                            expiration_date_gte=gte).get("option_contracts") or []
    exps = sorted({c["expiration_date"] for c in cs})
    return min(exps, key=lambda e: abs(_dte(e) - target_dte)) if exps else None


def _snaps(sym, exp, right, **strikefilt):
    """All option snapshots (greeks + quote) for one expiry/right, paged fully."""
    out, token = {}, None
    while True:
        p = dict(type=("call" if right == "C" else "put"), expiration_date=exp, limit=100)
        p.update(strikefilt)
        if token:
            p["page_token"] = token
        r = A.option_snapshots(sym, **p)
        out.update(r.get("snapshots") or {})
        token = r.get("next_page_token")
        if not token:
            break
    return out


# --- leg builders --------------------------------------------------------------------------------
def build_ccw(sym, spot):
    long_exp = _expiry_near(sym, "C", LC_DTE)
    short_exp = _expiry_near(sym, "C", SHORT_DTE)
    if not long_exp or not short_exp:
        raise RuntimeError(f"missing a call expiry (long={long_exp}, short={short_exp})")

    # long = ~99-delta deep-ITM call. Prefer Alpaca's OWN delta; where Alpaca omits greeks on a
    # deep strike that still HAS a quote, fall back to BS delta (same math as the sim) so we don't
    # settle for a shallow strike just because the broker skipped its greeks.
    longs = _snaps(sym, long_exp, "C", strike_price_lte=round(spot, 2))
    ldte = _dte(long_exp)
    ivs = sorted(((parse_occ(o)[3], v.get("impliedVolatility")) for o, v in longs.items()
                  if v.get("impliedVolatility")), key=lambda x: x[0])
    ref_iv = ivs[0][1] if ivs else 0.6        # deepest available IV; delta is ~IV-insensitive this deep
    cands = []
    for occ, v in longs.items():
        _, _, _, k = parse_occ(occ)
        b, a = _quote(v)
        if not a:                              # must be buyable (has an ask)
            continue
        d = (v.get("greeks") or {}).get("delta")
        est = d is None
        if est:
            d = bs_call_delta(spot, k, ref_iv, ldte)
        if d is None:
            continue
        cands.append((k, occ, v, d, est, b, a))
    if not cands:
        raise RuntimeError("no tradeable ITM call for the long leg")
    long_strike, long_occ, long_v, long_delta, long_est, long_bid, long_ask = \
        min(cands, key=lambda x: abs(x[3] - 0.99))

    # short = nearest OTM call above spot with a live bid
    shorts = _snaps(sym, short_exp, "C", strike_price_gte=round(spot, 2))
    cand = []
    for occ, v in shorts.items():
        _, _, _, k = parse_occ(occ)
        b, a = _quote(v)
        if k > spot and b:
            cand.append((k, occ, v, b, a))
    if not cand:
        raise RuntimeError("no OTM call with a bid for the short leg")
    short_strike, short_occ, short_v, short_bid, short_ask = min(cand, key=lambda x: x[0])

    net_debit = round((long_ask or 0) - (short_bid or 0), 2)
    return dict(kind="CCW", long_occ=long_occ, long_strike=long_strike, long_exp=long_exp,
                long_delta=long_delta, long_est=long_est, long_ask=long_ask, long_bid=long_bid,
                short_occ=short_occ, short_strike=short_strike, short_exp=short_exp,
                short_bid=short_bid, short_ask=short_ask,
                net_debit=net_debit, cpc=round((long_ask or 0) * 100, 2))


def build_csp(sym, spot, dte_target=LONGCSP_DTE):
    exp = _expiry_near(sym, "P", dte_target)
    if not exp:
        raise RuntimeError("no put expiry found")
    puts = _snaps(sym, exp, "P")
    cand = []
    for occ, v in puts.items():
        _, _, _, k = parse_occ(occ)
        b, a = _quote(v)
        if b:
            cand.append((k, occ, v, b, a))
    if not cand:
        raise RuntimeError("no put with a bid")
    # ATM = strike nearest spot
    strike, occ, v, bid, ask = min(cand, key=lambda x: abs(x[0] - spot))
    return dict(kind="CSP", put_occ=occ, put_strike=strike, put_exp=exp,
                put_bid=bid, put_ask=ask, cpc=round(strike * 100, 2))


# --- sizing (mirrors cpt_account.size_positions for a single new trade) --------------------------
def size(cpc, equity, avail):
    budget = equity * PER_TRADE_PCT
    if not cpc or cpc <= 0:
        return 0, budget, "no capital-per-contract (missing price)"
    n = min(UNIT, int(budget // cpc))
    reason = ""
    if n == 0:
        return 0, budget, f"needs ${cpc:,.0f}/contract > {PER_TRADE_PCT*100:.0f}% slice (${budget:,.0f})"
    if avail is not None and cpc > 0:
        cap_by_bp = int(avail // cpc)
        if cap_by_bp < n:
            n, reason = max(0, cap_by_bp), f"trimmed to fit ${avail:,.0f} options buying power"
    return n, budget, reason


# --- plan one entry ------------------------------------------------------------------------------
def plan(ticker, force=False, acct=None, qty_override=None):
    ticker = ticker.upper()
    a = ds.analyze(ticker)
    print(f"\n{'='*66}\n  {ticker}  entry {a['price']:.2f}  pos {a['pos']:.1f}%  RSI {a['rsi']:.1f}  "
          f"({a['day']} day, gate {a['verdict']})")
    if not a["valid"] and not force:
        print(f"  not a live ENTRY (gate {a['verdict']}). Skipping - the book only opens real alerts.")
        print(f"  (override for a manual test:  python3 cpt_alpaca_open.py {ticker} --force)")
        return None
    structure, why = ds.strategy_pick(a)
    print(f"  >> STRUCTURE: {structure}\n     {why[:120]}")

    spot = A.latest_trade(ticker) or a["price"]
    acct = acct or A.get_account()
    equity = float(acct.get("equity") or 0)
    avail = float(acct.get("options_buying_power") or 0)

    # dup guard: one campaign per name (mirror cpt_paper) - count both open POSITIONS and RESTING orders
    held = set()
    for p in A.get_positions():
        s = p["symbol"]
        if len(s) >= 15 and s[-9] in "CP":
            held.add(parse_occ(s)[0])
    for o in A.get_orders(status="open"):
        for s in ([lg.get("symbol") for lg in (o.get("legs") or [])] or [o.get("symbol")]):
            if s and len(s) >= 15 and s[-9] in "CP":
                held.add(parse_occ(s)[0])
    if ticker in held and not force:
        print(f"  NOTE: an Alpaca option position on {ticker} already exists - one campaign per name, skipping.")
        return None

    try:
        legs = build_ccw(ticker, spot) if "CCW" in structure else build_csp(ticker, spot)
    except Exception as e:
        print(f"  could not build legs from Alpaca: {e}")
        return None

    n_sized, budget, reason = size(legs["cpc"], equity, avail)
    print(f"  spot ${spot:.2f}   sizing: 10% budget ${budget:,.0f}  |  "
          f"capital/contract ${legs['cpc']:,.0f}  |  contracts {n_sized}" + (f"  ({reason})" if reason else ""))
    n = n_sized if qty_override is None else qty_override
    if qty_override is not None:
        print(f"  (forced TEST qty: {n} lot{'s' if n != 1 else ''}, overriding the sized {n_sized})")
    if n == 0:
        print("  -> 0 contracts: not sized in. No order.")
        return None

    if legs["kind"] == "CCW":
        dmark = f"~{legs['long_delta']:.2f}d" + ("*est" if legs.get("long_est") else "")
        print(f"  LONG   buy_to_open  {legs['long_occ']}  ({legs['long_strike']:g}C {legs['long_exp']}, "
              f"{_dte(legs['long_exp'])}d, {dmark})  ask {legs['long_ask']}")
        if legs["long_delta"] < 0.90:
            print(f"  NOTE: deepest tradeable strike here only reaches {legs['long_delta']:.2f}d "
                  f"(chain gap) - NOT a true 99-delta. Consider skipping {ticker} or a longer expiry.")
        print(f"  SHORT  sell_to_open {legs['short_occ']}  ({legs['short_strike']:g}C {legs['short_exp']}, "
              f"{_dte(legs['short_exp'])}d, OTM)  bid {legs['short_bid']}")
        print(f"  net debit/spread ${legs['net_debit']:.2f}  x{n}  =  ${legs['net_debit']*100*n:,.0f} capital")
        payload = dict(order_class="mleg", qty=str(n), type="limit", time_in_force="day",
                       limit_price=f"{legs['net_debit']:.2f}",
                       legs=[{"symbol": legs["long_occ"], "ratio_qty": "1", "side": "buy",
                              "position_intent": "buy_to_open"},
                             {"symbol": legs["short_occ"], "ratio_qty": "1", "side": "sell",
                              "position_intent": "sell_to_open"}])
    else:
        c = legs["put_strike"] and round(legs["put_bid"] / legs["put_strike"] * 100, 2)
        print(f"  SELL   sell_to_open {legs['put_occ']}  ({legs['put_strike']:g}P {legs['put_exp']}, "
              f"{_dte(legs['put_exp'])}d, ATM)  bid {legs['put_bid']}" + (f"  ~{c:.1f}% CoC" if c else ""))
        print(f"  cash secured ${legs['cpc']:,.0f}/contract  x{n}  =  ${legs['cpc']*n:,.0f}")
        payload = dict(symbol=legs["put_occ"], qty=str(n), side="sell", type="limit",
                       time_in_force="day", limit_price=f"{legs['put_bid']:.2f}",
                       position_intent="sell_to_open")

    print(f"  ORDER (would submit): {payload}")
    return payload


def submit_live(payload):
    """Submit, poll to a terminal state, and RECONCILE: fill price + buying-power/margin used +
    the resulting position(s). This is the proof the probe could not give (acceptance != fill)."""
    import time
    before = A.get_account()
    bp0 = float(before.get("options_buying_power") or 0)
    print(f"\n  submitting to Alpaca paper...  (options BP before ${bp0:,.0f})")
    o = A.submit_order(**payload)
    oid = o.get("id")
    for _ in range(8):
        time.sleep(1.5)
        o = A.get_order(oid)
        if o.get("status") in ("filled", "rejected", "canceled", "expired"):
            break
    print(f"  >> order {oid}")
    print(f"     status '{o.get('status')}'  filled {o.get('filled_qty')}/{o.get('qty')}"
          f"  avg fill {o.get('filled_avg_price')}")
    for lg in (o.get("legs") or []):
        print(f"     leg {str(lg.get('side')):4} {lg.get('symbol')}  {lg.get('status')}  "
              f"filled {lg.get('filled_qty')} @ {lg.get('filled_avg_price')}")
    after = A.get_account()
    bp1 = float(after.get("options_buying_power") or 0)
    print(f"     options BP after ${bp1:,.0f}   (used ${bp0 - bp1:,.0f})")
    pos = A.get_positions()
    print(f"     positions now: {len(pos)}")
    for p in pos:
        print(f"       {p['symbol']}  qty {p['qty']}  avg {p.get('avg_entry_price')}  "
              f"mkt_val {p.get('market_value')}  uPL {p.get('unrealized_pl')}")
    return o


def main():
    args = sys.argv[1:]
    live = "--live" in args
    force = "--force" in args
    qty = None
    if "--qty" in args:
        try:
            qty = int(args[args.index("--qty") + 1])
        except (ValueError, IndexError):
            qty = None
    names, skip = [], False
    for x in args:
        if skip:
            skip = False
            continue
        if x == "--qty":
            skip = True
            continue
        if not x.startswith("--"):
            names.append(x)
    mode = (names[0] if names else "scan")

    acct = A.get_account()
    print(f"  CPT->Alpaca ENTRY EXECUTOR  ({'LIVE' if live else 'DRY-RUN, no orders'})  "
          f"acct equity ${float(acct.get('equity') or 0):,.0f}")

    if mode.lower() == "scan":
        targets = ds.UNIVERSE
        for tk in targets:
            try:
                p = plan(tk, force=force, acct=acct)       # qty override is single-name only
            except Exception as e:
                print(f"\n  {tk}: error {e.__class__.__name__}: {e}")
                continue
            if p and live:
                submit_live(p)
        print(f"\n  scanned {len(targets)} names.{'' if live else '  (dry-run - nothing submitted)'}\n")
        return

    p = plan(mode, force=force, acct=acct, qty_override=qty)
    if p and live:
        submit_live(p)
    elif p:
        print("\n  DRY-RUN: nothing submitted. Add --live to send this order.\n")


if __name__ == "__main__":
    main()
