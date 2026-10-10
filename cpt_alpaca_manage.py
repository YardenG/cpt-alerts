#!/usr/bin/env python3
"""
CPT -> Alpaca  |  Phase 2, step 2  -  THE MANAGEMENT LOOP  (dry-run by default)

Reads the REAL Alpaca paper positions, groups them into campaigns, marks each leg
on live Alpaca data, and applies JOHN'S management rules to decide the action -
then, in dry-run, PRINTS the exact order(s) it would place and submits nothing.
--live submits them, confirms the fills, and updates this book's banked-premium
state (alpaca_book.json) so rolls accrue income across weeks.

Doctrine is reused, not reinvented. Thresholds come straight from cpt_paper (the
proven sim engine) and the decisions mirror its _mark_one branches:
  CCW (long ~99d call + short weekly OTM call):
    - short ITM near expiry (CASE B): close BOTH legs only if that locks a WIN
      (banked + long P&L + short P&L >= 0); else buy back + re-write up-and-out
      (John never books a campaign loss - he rolls).
    - short OTM: expired / -50% / cheap-to-close / near-expiry -> realize + roll.
    - else HOLD.
  CSP (lone short put):
    - OTM at expiry -> re-sell a fresh weekly put.
    - ITM near expiry -> roll DOWN-and-out for a fresh credit (lower cost basis).
    - else HOLD. (Full assignment -> hold-shares -> write-CC state machine is
      parked here, same as the sim.)
Entry prices come from Alpaca (avg_entry_price = what we sold/paid). The decision
functions are PURE (marks passed in) so they unit-test on synthetic scenarios.

  python3 cpt_alpaca_manage.py            # dry-run: decide + show the orders
  python3 cpt_alpaca_manage.py --live     # submit + confirm fills + update banked state
  python3 cpt_alpaca_manage.py test       # unit-test the pure decision logic (no network)
"""
import datetime as dt, json, os, sys, time
import alpaca_api as A
from cpt_alpaca_open import _snaps, _expiry_near, parse_occ, _quote, _dte, SHORT_DTE
from cpt_paper import HALF, CHEAP, CHEAP_MIN_DTE, NEAR_EXPIRY, MULT

HERE = os.path.dirname(os.path.abspath(__file__))
BOOK = os.path.join(HERE, "alpaca_book.json")


# --- book state -----------------------------------------------------------------------------------
def load_book():
    if os.path.exists(BOOK):
        with open(BOOK) as f:
            return json.load(f)
    return {"campaigns": {}, "closed": []}


def save_book(b):
    with open(BOOK, "w") as f:
        json.dump(b, f, indent=2)


def realize(sold, buyback, qty):
    """Income banked on closing one short leg = (sold - buyback), never negative, x100 x contracts."""
    return round(max(0.0, (sold or 0) - (buyback or 0)) * MULT * qty, 0)


# --- market reads ---------------------------------------------------------------------------------
def _mark(sym, occ):
    root, exp, right, strike = parse_occ(occ)
    snaps = _snaps(sym, exp, right, strike_price_gte=strike, strike_price_lte=strike)
    v = snaps.get(occ) or (next(iter(snaps.values())) if snaps else None)
    if not v:
        return None
    b, a = _quote(v)
    mark = (b + a) / 2 if (b and a) else (b or a)
    return dict(bid=b, ask=a, mark=mark)


def _is_occ(s):
    return (len(s) >= 15 and s[-9] in "CP" and s[-15:-9].isdigit() and s[-8:].isdigit())


def group_campaigns():
    """Group open Alpaca option positions into campaigns keyed by underlying."""
    camps = {}
    for p in A.get_positions():
        occ = p["symbol"]
        if not _is_occ(occ):
            continue
        root, exp, right, strike = parse_occ(occ)
        qty = int(float(p["qty"]))
        leg = dict(occ=occ, exp=exp, right=right, strike=strike, qty=qty,
                   avg_entry=float(p["avg_entry_price"]), dte=_dte(exp))
        c = camps.setdefault(root, {"underlying": root, "long": None, "short": None, "short_put": None})
        if right == "C" and qty > 0:
            c["long"] = leg
        elif right == "C" and qty < 0:
            c["short"] = leg
        elif right == "P" and qty < 0:
            c["short_put"] = leg
    return camps


# --- re-write pickers -----------------------------------------------------------------------------
def _exps_after(sym, right, after_exp):
    today = dt.date.today()
    cs = A.option_contracts(sym, limit=500, type=("call" if right == "C" else "put"),
                            expiration_date_gte=today.isoformat()).get("option_contracts") or []
    exps = sorted({c["expiration_date"] for c in cs if _dte(c["expiration_date"]) > 0})
    return [e for e in exps if (after_exp is None or e > after_exp)]


def _pick_new_short(sym, spot, after_exp=None):
    """Fresh OTM call to re-write on a roll - the NEXT weekly after the current short (rolls OUT in
    time), nearest strike above spot with a bid (= up-and-out when the old short is now ITM)."""
    exps = _exps_after(sym, "C", after_exp)
    if after_exp and exps:
        exp = exps[0]
    else:
        exp = _expiry_near(sym, "C", SHORT_DTE)
    if not exp:
        return None
    snaps = _snaps(sym, exp, "C", strike_price_gte=round(spot, 2))
    cand = [(parse_occ(o)[3], o, _quote(v)[0]) for o, v in snaps.items()]
    cand = [(k, o, b) for (k, o, b) in cand if k > spot and b]
    if not cand:
        return None
    k, occ, bid = min(cand, key=lambda x: x[0])
    return dict(occ=occ, strike=k, bid=bid, exp=exp)


def _pick_new_put(sym, spot, after_exp=None, max_strike=None):
    """Fresh OTM put to re-sell / roll down-and-out - NEXT weekly after the current put, highest
    strike strictly BELOW spot (and below max_strike on a roll-down) with a bid = lower cost basis."""
    exps = _exps_after(sym, "P", after_exp)
    if after_exp and exps:
        exp = exps[0]
    else:
        exp = _expiry_near(sym, "P", SHORT_DTE)
    if not exp:
        return None
    snaps = _snaps(sym, exp, "P", strike_price_lte=round(spot, 2))
    cap = spot if max_strike is None else min(spot, max_strike)
    cand = [(parse_occ(o)[3], o, _quote(v)[0]) for o, v in snaps.items()]
    cand = [(k, o, b) for (k, o, b) in cand if k < cap and b]
    if not cand:
        return None
    k, occ, bid = max(cand, key=lambda x: x[0])
    return dict(occ=occ, strike=k, bid=bid, exp=exp)


# --- PURE decisions (marks passed in, so they unit-test with no network) --------------------------
def decide_ccw_pure(sold, strike, cur, dte, qty, spot, long_paid, long_mark, banked):
    itm = spot is not None and spot > strike
    if cur is None and not (dte is not None and dte <= 0):
        return ("HOLD", f"no live mark on {strike:g}C (thin) - re-run in-hours")
    if itm and dte is not None and dte <= NEAR_EXPIRY:
        buyback = cur if cur is not None else max(0.0, (spot or 0) - strike)
        long_pl = ((long_mark or 0) - (long_paid or 0)) * MULT * qty
        short_pl = ((sold or 0) - (buyback or 0)) * MULT * qty
        would = banked + long_pl + short_pl
        if would >= 0:
            return ("CLOSE_WIN", f"CASE B WIN: {spot:.2f} above {strike:g}C at {dte}d - close both green (~{would:+,.0f})")
        return ("ROLL", f"CASE B roll up-and-out (closing now = {would:+,.0f} red) - keep the long + campaign")
    if dte is not None and dte <= 0 and not itm:
        return ("ROLL", "short expired OTM - re-write the next weekly")
    if cur is not None and cur <= HALF * sold and (dte is None or dte > NEAR_EXPIRY):
        return ("ROLL", f"-50% rule: mark {cur:.2f} <= 50% of sold {sold:.2f}")
    if cur is not None and cur < CHEAP and dte is not None and dte >= CHEAP_MIN_DTE:
        return ("ROLL", f"cheap-to-close: mark {cur:.2f} < {CHEAP:.2f} with {dte}d left")
    if dte is not None and dte <= NEAR_EXPIRY and not itm:
        return ("ROLL", f"near expiry ({dte}d) OTM - let it go, re-write")
    return ("HOLD", f"CC {strike:g}C mark {cur and round(cur,2)} vs sold {sold:.2f}, {dte}d, {'ITM' if itm else 'OTM'} - keep selling time")


def decide_csp_pure(sold, strike, cur, dte, spot):
    put_itm = spot is not None and spot < strike
    if cur is None and not (dte is not None and dte <= 0):
        return ("HOLD", f"no live mark on {strike:g}P (thin) - re-run in-hours")
    if dte is not None and dte <= 0 and not put_itm:
        return ("CSP_RESELL", f"CSP expired OTM - re-sell a fresh ~{SHORT_DTE}d put")
    if put_itm and dte is not None and dte <= NEAR_EXPIRY:
        return ("CSP_ROLL_DOWN", f"CSP {strike:g}P tested (spot {spot:.2f} < {strike:g}) {dte}d - roll DOWN-and-out for a fresh credit")
    return ("HOLD", f"CSP {strike:g}P mark {cur and round(cur,2)} vs sold {sold:.2f}, {dte}d, {'ITM' if put_itm else 'OTM'}")


# --- decide wrappers (fetch marks, call the pure fn) ----------------------------------------------
def decide_ccw(camp, spot, banked):
    s, l = camp["short"], camp["long"]
    cur = (_mark(camp["underlying"], s["occ"]) or {}).get("mark")
    lm = (_mark(camp["underlying"], l["occ"]) or {}).get("mark") if l else None
    return decide_ccw_pure(s["avg_entry"], s["strike"], cur, s["dte"], abs(s["qty"]),
                           spot, l["avg_entry"] if l else 0, lm, banked), cur


def decide_csp(camp, spot):
    sp = camp["short_put"]
    cur = (_mark(camp["underlying"], sp["occ"]) or {}).get("mark")
    return decide_csp_pure(sp["avg_entry"], sp["strike"], cur, sp["dte"], spot), cur


# --- order planning -------------------------------------------------------------------------------
def _close(occ, qty, px):
    side = "sell"  # default close for a long
    return dict(symbol=occ, qty=str(qty), type="limit", time_in_force="day", limit_price=f"{px:.2f}")


def plan_orders(camp, action, spot):
    """Alpaca-native orders (return (orders, note)).

    KEY CONSTRAINT (verified live + in Alpaca docs): Alpaca rejects selling a new short CALL
    against a long you already hold ('uncovered'), so a CCW cannot be rolled in place. A CCW
    roll OR close therefore CLOSES the whole diagonal in one mleg, and do_open re-enters the
    name fresh next run (= the close+reopen model). CSP rolls stay standalone (a cash-secured
    put is covered by cash)."""
    uc = camp["underlying"]
    # --- CCW roll OR close -> close the whole diagonal (one mleg, market); reopens fresh next ---
    if action in ("ROLL", "CLOSE_WIN") and camp["long"] and camp["short"]:
        qty = abs(camp["short"]["qty"])
        orders = [dict(order_class="mleg", qty=str(qty), type="market", time_in_force="day",
                       legs=[{"symbol": camp["long"]["occ"], "ratio_qty": "1", "side": "sell",
                              "position_intent": "sell_to_close"},
                             {"symbol": camp["short"]["occ"], "ratio_qty": "1", "side": "buy",
                              "position_intent": "buy_to_close"}])]
        return orders, "close the diagonal (Alpaca can't roll the short in place) -> reopens fresh"
    # --- repair a stranded NAKED LONG (short leg lost in a pre-fix failed roll): close it ---
    if action == "REPAIR_CLOSE" and camp["long"]:
        qty = abs(camp["long"]["qty"])
        return [dict(symbol=camp["long"]["occ"], qty=str(qty), side="sell", type="market",
                     time_in_force="day", position_intent="sell_to_close")], "close the stranded long leg"
    # --- CSP: standalone roll is allowed (a cash-secured put is covered by cash) ---
    if action in ("CSP_RESELL", "CSP_ROLL_DOWN") and camp["short_put"]:
        sp = camp["short_put"]; qty = abs(sp["qty"]); pm = _mark(uc, sp["occ"]) or {}
        o = []
        if not (sp["dte"] is not None and sp["dte"] <= 0):           # expired-worthless needs no buyback
            o.append(dict(symbol=sp["occ"], qty=str(qty), side="buy", type="limit", time_in_force="day",
                          position_intent="buy_to_close", limit_price=f"{(pm.get('ask') or pm.get('mark') or 0.05):.2f}"))
        max_strike = sp["strike"] if action == "CSP_ROLL_DOWN" else None
        np_ = _pick_new_put(uc, spot, after_exp=sp["exp"], max_strike=max_strike)
        if np_:
            o.append(dict(symbol=np_["occ"], qty=str(qty), side="sell", type="limit", time_in_force="day",
                          position_intent="sell_to_open", limit_price=f"{round(np_['bid']*0.97, 2):.2f}"))
        return o, (f"{'roll down' if max_strike else 're-sell'} -> {np_['strike']:g}P {np_['exp']}" if np_ else "NO new put strike found")
    return [], ""


# --- live submit + state update -------------------------------------------------------------------
def _submit_and_fill(order):
    r = A.submit_order(**order)
    oid = r.get("id")
    for _ in range(8):
        time.sleep(1.2)
        o = A.get_order(oid)
        if o.get("status") in ("filled", "rejected", "canceled", "expired"):
            return o
    return A.get_order(oid)


def _execute(orders, action, camp, st):
    """Submit the orders, confirm fills, log. Handles single and mleg/market orders. Records a
    terminal close in the book (the Alpaca positions are the source of truth; the close+reopen
    scorecard accounting is a separate layer)."""
    fills = []
    for o in orders:
        f = _submit_and_fill(o)
        who = o.get("position_intent") or ("mleg-close" if o.get("order_class") == "mleg" else "order")
        sym = o.get("symbol") or "+".join(l["symbol"] for l in (o.get("legs") or []))
        fills.append(f"{who} {sym}: {f.get('status')}@{f.get('filled_avg_price')}")
    if action in ("ROLL", "CLOSE_WIN", "REPAIR_CLOSE"):
        st.setdefault("closes", []).append(dict(date=dt.date.today().isoformat(), action=action,
                                                underlying=camp["underlying"]))
    return f"{action}: " + " | ".join(fills)


# --- main -----------------------------------------------------------------------------------------
def run(live=False):
    book = load_book()
    camps = group_campaigns()
    ccws = {k: v for k, v in camps.items() if v["short"] and v["long"]}
    csps = {k: v for k, v in camps.items() if v["short_put"] and not v["short"] and not v["long"]}
    nakeds = {k: v for k, v in camps.items() if v["long"] and not v["short"]}   # broken: long only (a failed roll)
    print(f"\n  CPT->Alpaca MANAGEMENT  ({'LIVE' if live else 'DRY-RUN, no orders'})  "
          f"{len(ccws)} CCW + {len(csps)} CSP + {len(nakeds)} stranded-long")
    if not (ccws or csps or nakeds):
        print("  no open campaigns to manage.\n")
        return []

    dirty = False
    results = []
    for sym, camp in list(ccws.items()) + list(csps.items()) + list(nakeds.items()):
        spot = A.latest_trade(sym)
        st = book["campaigns"].setdefault(sym, {"opened": dt.date.today().isoformat(), "banked": 0.0, "rolls": []})
        if camp["short"] and camp["long"]:
            (action, detail), cur = decide_ccw(camp, spot, float(st.get("banked", 0.0)))
            legs = [camp["long"], camp["short"]]
        elif camp["long"] and not camp["short"]:
            action, detail, legs = "REPAIR_CLOSE", "stranded naked long (short lost in a pre-fix roll) - close it; open re-enters fresh", [camp["long"]]
        else:
            (action, detail), cur = decide_csp(camp, spot)
            legs = [camp["short_put"]]
        print(f"\n  == {sym}  spot {spot and round(spot,2)} ==")
        for lg in legs:
            print(f"     {('long ' if lg['qty']>0 else 'short')} {lg['strike']:g}{lg['right']} {lg['exp']} ({lg['dte']}d)  "
                  f"{'paid' if lg['qty']>0 else 'sold'} {lg['avg_entry']}")
        print(f"     >> {action}: {detail}")
        results.append(dict(sym=sym, action=action, detail=detail))
        orders, note = plan_orders(camp, action, spot)
        if note:
            print(f"        ({note})")
        for o in orders:
            desc = o.get("position_intent") or o.get("order_class") or "order"
            sym_d = o.get("symbol") or " + ".join(l["symbol"] for l in (o.get("legs") or []))
            print(f"        ORDER {desc:14} {sym_d}  x{o.get('qty')} {o.get('type')}")
        if live and orders:
            print("        submitting + confirming fills...")
            log = _execute(orders, action, camp, st)
            print(f"        -> {log}")
            dirty = True
    if live and dirty:
        save_book(book)
        print("\n  book state saved (alpaca_book.json).")
    if not live:
        print("\n  DRY-RUN: nothing submitted. Add --live to place the orders.\n")
    return results


# --- self-test (pure logic, no network) -----------------------------------------------------------
def test():
    ok = 0
    def chk(name, got, want):
        nonlocal ok
        hit = got == want
        ok += hit
        print(f"  [{'PASS' if hit else 'FAIL'}] {name}: {got}" + ("" if hit else f"  (want {want})"))

    # CCW
    chk("ccw HOLD (otm, fresh)", decide_ccw_pure(1.90, 60, 1.97, 10, 1, 59.9, 31.75, 29.0, 0)[0], "HOLD")
    chk("ccw -50% (decayed)", decide_ccw_pure(2.00, 60, 0.90, 9, 1, 58.0, 31.75, 28.0, 0)[0], "ROLL")
    chk("ccw cheap-to-close", decide_ccw_pure(2.00, 60, 0.08, 20, 1, 58.0, 31.75, 28.0, 0)[0], "ROLL")
    chk("ccw near-expiry OTM", decide_ccw_pure(2.00, 60, 1.50, 3, 1, 58.0, 31.75, 28.0, 0)[0], "ROLL")
    chk("ccw CASE-B WIN (long carries it)", decide_ccw_pure(1.90, 60, 2.50, 3, 1, 62.0, 31.75, 32.5, 0)[0], "CLOSE_WIN")
    chk("ccw CASE-B ROLL (would be red)", decide_ccw_pure(1.90, 60, 5.00, 3, 1, 62.0, 31.75, 31.80, 0)[0], "ROLL")
    # CSP
    chk("csp HOLD (otm)", decide_csp_pure(1.50, 55, 1.40, 10, 57.0)[0], "HOLD")
    chk("csp re-sell (expired otm)", decide_csp_pure(1.50, 55, 0.0, 0, 57.0)[0], "CSP_RESELL")
    chk("csp roll-down (itm near expiry)", decide_csp_pure(1.50, 55, 2.80, 3, 53.0)[0], "CSP_ROLL_DOWN")
    # realize math
    chk("realize income", realize(2.00, 0.80, 10), 1200.0)
    chk("realize never negative", realize(1.00, 1.50, 10), 0.0)
    print(f"\n  {ok}/11 checks passed.\n")


def main():
    args = sys.argv[1:]
    if args and args[0] == "test":
        test(); return
    run(live="--live" in args)


if __name__ == "__main__":
    main()
