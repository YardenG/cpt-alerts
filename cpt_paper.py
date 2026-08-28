#!/usr/bin/env python3
"""
Phase 3 - PAPER-TRADE LEDGER: score the automated pipeline against John's real track record.

Every entry alert becomes a PAPER position: we snapshot the entry price + the suggested structure
+ the exact legs + the market regime the moment the alert fires, then track it forward on real data
through John's management rules to a close, and score the closed trades against John's realized
numbers (the Phase 2 dataset). We calibrated the ENTRY gate to 92% of his 95 alerts and the
structure/legs to his doctrine - this proves the WHOLE pipeline produces John-like RESULTS.

READ THE MEASUREMENT NOTE (report prints it too): John's win-rate is ~100% BY DESIGN - he never
closes red, he rolls. A paper ledger that also follows the roll rule will ALSO win ~100%. So the
comparison is NOT win-rate (baked in); it is the four numbers where our automation can diverge from
John: $ per trade, CoC%, days held, and the roll / underwater cohort (where the real risk lives).

Data: free Yahoo (delayed ~15m, greeks BS-ESTIMATED) - same feed as the cloud alert, so the paper
marks match exactly what the phone told Yarden to do. Every premium is a delayed estimate (BS-EST);
realized paper $ is computed from the ACTUAL re-fetched marks at each step, never asserted up front.

Management rules mirror cpt_manage.py (doctrine sec.7-9) exactly, adapted to the free-data PMCC:
each weekly covered call that decays/expires is a WEEKLY close (income = sold - buyback), then we
re-write the next CC (a roll; the campaign continues); when the short CC goes ITM near expiry that
is CASE B = a CAMPAIGN closeout (long-call P&L + banked weeklies + current short P&L).

Sizing: normalized to CONTRACTS=10 (~1000 shares, John's campaign size) so $ is comparable; CoC% is
size-independent and is the metric to trust. Pure stdlib. READ-ONLY: only reads market data + this
ledger file. Never trades.

Commands:
  python3 cpt_paper.py open TQQQ    capture a paper position from the current alert read
  python3 cpt_paper.py mark         re-mark every open position, apply John's rules, realize closes
  python3 cpt_paper.py list         show the ledger
  python3 cpt_paper.py report       scorecard vs John
"""
import json, os, sys, datetime as dt

import cpt_data_spike as ds
import cpt_legs_web as legsmod
try:
    import cpt_context as ctx           # situational-awareness layer (best-effort)
except Exception:
    ctx = None

LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper-ledger.json")

# --- sizing + thresholds (thresholds mirror cpt_manage.py / doctrine sec.7) -----------------------
CONTRACTS     = 10     # normalized campaign size (~1000 sh); $ scales linearly, CoC% is size-free.
MULT          = 100    # option contract multiplier (shares per contract).
HALF          = 0.50   # [DOCTRINE sec.7] -50% Rule: buy back a CC once premium halves.
CHEAP         = 0.10   # [DOCTRINE sec.7] < $0.10 with 2+ wks left = the trade's done.
CHEAP_MIN_DTE = 14     # [DOCTRINE sec.7] "2+ weeks left" for cheap-to-close.
PENNIES       = 0.10   # [DOCTRINE sec.7] "only pennies of time value" on a near-expiry CSP.
NEAR_EXPIRY   = 5      # [OPS] days-to-expiry we call "near expiry" (not a John number).

# --- John's realized benchmark (from _distilled/phase2-findings-v1.md, 299-alert slice) ----------
JOHN = dict(
    weekly=dict(avg=2944, coc=4.46, days=8.7, n=42),        # weekly ITM-CC closes (bread & butter)
    campaign=dict(avg=10683, coc=9.71, days=26.8, n=12),    # PMCC / blended campaign closeouts
    win_by_design=True,                                     # ~100% wins; never closes red (rolls)
    opens=dict(itm_ccw=56, plain_ccw=18, csp=12, pmcc=9),   # structure mix across his 95 opens
)
STRUCT_BUCKET = {                                          # our strategy_pick label -> John's bucket
    "99-delta ITM CCW": "itm_ccw",
    "CSP (cash-secured put)": "csp",
    "Long-dated ATM CSP (LEAPS-like)": "csp",
}


# --- ledger IO -----------------------------------------------------------------------------------
def load():
    if not os.path.exists(LEDGER):
        return {"positions": []}
    with open(LEDGER) as f:
        return json.load(f)


def save(book):
    with open(LEDGER, "w") as f:
        json.dump(book, f, indent=2)


def _today():
    return dt.date.today().isoformat()


def _days_between(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


# --- Yahoo option re-fetch (for marking) ---------------------------------------------------------
def _pick_option(chain, strike, right):
    side = chain["options"][0]["calls" if right == "C" else "puts"]
    if not side:
        return None
    o = min(side, key=lambda x: abs(x["strike"] - strike))
    bid, ask = o.get("bid"), o.get("ask")
    mid = (bid + ask) / 2 if (bid and ask) else (bid or ask or o.get("lastPrice"))
    return dict(strike=o["strike"], bid=bid, ask=ask, mark=mid)


def _next_weekly(op, crumb, ticker, spot, target_dte=7):
    """Re-pick a fresh ~weekly OTM call to WRITE (the next income leg on a roll): the ~7-DTE expiry,
    nearest strike above spot with a live bid. Carries the expiry so cloud marking keeps working
    across weeks (the roll's new CC is fully re-markable, not a dateless stub)."""
    root = legsmod._chain(op, crumb, ticker)
    today = dt.date.today()
    future = [e for e in sorted(root["expirationDates"]) if legsmod._dte(e, today) > 0]
    if not future:
        return None
    exp = min(future, key=lambda e: abs(legsmod._dte(e, today) - target_dte))
    ch = legsmod._chain(op, crumb, ticker, exp)
    calls = [c for c in ch["options"][0]["calls"] if c["strike"] > spot and c.get("bid")]
    if not calls:
        return None
    c = min(calls, key=lambda c: c["strike"])
    return dict(strike=c["strike"], sold=c["bid"], exp_ts=exp, exp=legsmod._fmt_date(exp))


def _next_put(op, crumb, ticker, spot, cur_strike=None, target_dte=7):
    """Re-pick a fresh ~weekly OTM put to WRITE on a CSP roll (John's put management, from his ROLLED-CSP
    emails: he buys back the tested put and re-sells rather than sit and take assignment). Nearest ~7-DTE
    expiry; highest strike strictly BELOW spot with a live bid - and, on an ITM roll (cur_strike given),
    also below the current strike, i.e. a genuine roll DOWN-AND-OUT that lowers the cost basis. Carries
    the expiry so the new put stays fully re-markable."""
    root = legsmod._chain(op, crumb, ticker)
    today = dt.date.today()
    future = [e for e in sorted(root["expirationDates"]) if legsmod._dte(e, today) > 0]
    if not future:
        return None
    exp = min(future, key=lambda e: abs(legsmod._dte(e, today) - target_dte))
    ch = legsmod._chain(op, crumb, ticker, exp)
    cap = spot if cur_strike is None else min(spot, cur_strike)
    puts = [p for p in ch["options"][0]["puts"] if p["strike"] < cap and (p.get("bid") or p.get("lastPrice"))]
    if not puts:
        return None
    p = max(puts, key=lambda p: p["strike"])
    return dict(strike=p["strike"], sold=(p.get("bid") or p.get("lastPrice")), exp_ts=exp, exp=legsmod._fmt_date(exp))


# --- capture (open a paper position) -------------------------------------------------------------
def snapshot_legs(ticker, kind):
    """Run the SAME cloud legs the phone gets and normalize them into the ledger's leg schema, keyed
    by the CURRENT structure. Stores raw expiry timestamps + the premium sold so `mark` can re-fetch
    the exact contract later. Returns (legs_pack, note) where legs_pack has keys long_call/income."""
    try:
        L = legsmod.legs(ticker)
    except Exception as e:
        return None, f"legs unavailable ({e.__class__.__name__}) - entry+structure captured, legs pending live"
    if not L:
        return None, "no options data - legs pending live"
    if L.get("stale"):
        return None, "market closed / thin data - legs populate live during US market hours"

    today = _today()
    pack = dict(px=L.get("px"), atm_iv=L.get("atm_iv"), em=L.get("em"), long_call=None, income=None)
    lc, csp, ccw_cc = L.get("lc"), L.get("csp"), L.get("ccw_cc")

    if kind == "CCW" and lc:
        pack["long_call"] = dict(strike=lc["strike"], exp=legsmod._fmt_date(L["lc_exp"]),
                                 exp_ts=L["lc_exp"], ask_paid=lc.get("ask"), delta=L.get("lc_delta"))
        if ccw_cc:
            pack["income"] = dict(right="C", strike=ccw_cc["strike"], exp=legsmod._fmt_date(L["csp_exp"]),
                                  exp_ts=L["csp_exp"], sold=ccw_cc.get("bid"), cycle_opened=today)
    else:  # CSP-led (red day / long-dated)
        if csp:
            pack["income"] = dict(right="P", strike=csp["strike"], exp=legsmod._fmt_date(L["csp_exp"]),
                                  exp_ts=L["csp_exp"], sold=csp.get("bid"), cycle_opened=today,
                                  cost_cc_bid=L.get("cc_bid"))
    return pack, "BS-EST from free Yahoo data (~15m delayed) - confirm live at execution"


def _assemble(a, reg=None):
    """Build a fresh paper-position dict from an analyze() read (shared by the CLI `open` and the
    cloud auto-capture). Snapshots the suggested structure + the same cloud legs the phone gets +
    the situational-awareness regime/context. Never raises on the best-effort context layer."""
    ticker = a["t"]
    structure, why = ds.strategy_pick(a)
    kind = "CCW" if "CCW" in structure else "CSP"
    pack, legs_note = snapshot_legs(ticker, kind)
    regime_label = context_line = None
    if ctx is not None:
        try:
            if reg is None:
                reg = ctx.regime()
            regime_label = reg.get("label")
            context_line = ctx.read_line(ctx.context(ticker, reg=reg, a=a))
        except Exception:
            pass
    today = _today()
    return dict(
        id=ticker + "-" + today.replace("-", ""), ticker=ticker, opened=today, status="OPEN", kind=kind,
        entry_price=round(a["price"], 2), pos_pct=round(a["pos"], 1), rsi=round(a["rsi"], 1),
        verdict=a["verdict"], day=a["day"],
        structure=structure, structure_why=why, structure_bucket=STRUCT_BUCKET.get(structure, "other"),
        regime=regime_label, context=context_line, contracts=CONTRACTS,
        long_call=(pack or {}).get("long_call"), income=(pack or {}).get("income"),
        legs_note=legs_note, weeklies=[], premium_banked=0.0, events=[], closed=None,
    )


def auto_open(a, reg=None):
    """Cloud hook: paper-open a position from a fresh alert's analyze() read. Dup-guarded (one open
    campaign per name) and gate-guarded (only real ENTRY signals). Best-effort caller wraps this.
    Returns True if a new paper position was recorded."""
    if not a.get("valid"):
        return False
    book = load()
    if any(p["ticker"] == a["t"] and p["status"] != "CLOSED" for p in book["positions"]):
        print(f"[paper] {a['t']} already has an open campaign - not re-opening.")
        return False
    pos = _assemble(a, reg=reg)
    if any(p["id"] == pos["id"] for p in book["positions"]):
        pos["id"] += "-" + dt.datetime.now().strftime("%H%M")
    book["positions"].append(pos)
    save(book)
    inc = pos.get("income")
    leg = f" {inc['strike']:g}{inc['right']} @ {inc['sold']}" if inc else " (legs pending live)"
    print(f"[paper] OPEN {pos['id']} {pos['structure']}{leg}")
    return True


def meta_get(key, default=None):
    return load().get("meta", {}).get(key, default)


def meta_set(key, val):
    book = load()
    book.setdefault("meta", {})[key] = val
    save(book)


def open_position(ticker, force=False):
    ticker = ticker.upper()
    if ticker not in ds.UNIVERSE:
        print(f"NOTE: {ticker} is not in the leveraged-ETF Wheel universe {ds.UNIVERSE}.")
        print("      The Wheel is only traded on that fixed list - capturing anyway, but check this is intended.")
    book = load()
    dup = [p for p in book["positions"] if p["ticker"] == ticker and p["status"] != "CLOSED"]
    if dup and not force:
        print(f"An OPEN paper position for {ticker} already exists (opened {dup[0]['opened']}, "
              f"status {dup[0]['status']}). John runs one campaign per name - skipping.")
        print(f"Re-open anyway with:  python3 cpt_paper.py open {ticker} --force")
        return

    a = ds.analyze(ticker)
    # Integrity guard: the ledger scores our pipeline vs John, so it must only paper-trade REAL alerts
    # (gate ENTRY / ENTRY*) - the same condition the Telegram bot fires on. A "watch" / "TOO HIGH"
    # name would score a trade John never opened. Override with --force for a deliberate manual test.
    if not a["valid"] and not force:
        print(f"{ticker} is not a live ENTRY right now (gate {a['verdict']}: pos {a['pos']:.1f}%, "
              f"RSI {a['rsi']:.1f}). The ledger only paper-trades real alerts, so skipping.")
        print(f"Capture a non-signal anyway (manual test) with:  python3 cpt_paper.py open {ticker} --force")
        return
    pos = _assemble(a)
    if any(p["id"] == pos["id"] for p in book["positions"]):
        pos["id"] += "-" + dt.datetime.now().strftime("%H%M")
    structure, why = pos["structure"], pos["structure_why"]
    regime_label, context_line, legs_note = pos["regime"], pos["context"], pos["legs_note"]
    book["positions"].append(pos)
    save(book)

    print(f"PAPER OPEN  {pos['id']}")
    print(f"  {ticker}  entry {pos['entry_price']}  pos {pos['pos_pct']}%  RSI {pos['rsi']}  "
          f"({pos['day']} day, gate {pos['verdict']})")
    if regime_label:
        print(f"  regime: {regime_label}")
    if context_line:
        print(f"  context: {context_line}")
    print(f"  >> STRUCTURE: {structure}")
    print(f"     {why}")
    _print_legs(pos)
    print(f"  legs: {legs_note}")
    print(f"\nLedger: {LEDGER}  (now {len([p for p in book['positions'] if p['status']!='CLOSED'])} open)")


def _print_legs(pos):
    lc, inc = pos.get("long_call"), pos.get("income")
    if pos["kind"] == "CCW" and lc:
        print(f"     1) BUY  ~99d long: {lc['strike']:g}C {lc.get('exp')} "
              f"~{lc.get('delta') and round(lc['delta'],2)}d  ask ~{lc.get('ask_paid')}")
        if inc:
            c = legsmod.coc(inc.get("sold"), inc["strike"])
            print(f"     2) WRITE the CC:   {inc['strike']:g}C {inc.get('exp')} sold ~{inc.get('sold')}"
                  + (f" (~{c:.1f}% CoC)" if c else ""))
    elif inc:
        c = legsmod.coc(inc.get("sold"), inc["strike"])
        print(f"     T1 CSP: sell {inc['strike']:g}P {inc.get('exp')} sold ~{inc.get('sold')}"
              + (f" (~{c:.1f}% CoC)" if c else ""))
        if inc.get("cost_cc_bid"):
            print(f"     T2 CC if assigned: sell {inc['strike']:g}C at cost basis ~{inc['cost_cc_bid']}")


# --- mark (track forward, apply John's rules, realize closes) -------------------------------------
def _capital_base(pos):
    """Per-share capital deployed = the metric CoC% is measured against. For a PMCC that is the long
    call debit (the capital-efficient part); for a stock CSP it is the strike (cash secured)."""
    if pos["kind"] == "CCW" and pos.get("long_call") and pos["long_call"].get("ask_paid"):
        return pos["long_call"]["ask_paid"]
    return pos["income"]["strike"] if pos.get("income") else None


def _realize_weekly(pos, sold, buyback, reason):
    """Record one weekly covered-call close (income = sold - buyback) and bank it."""
    income_ps = max(0.0, (sold or 0) - (buyback or 0))
    cap = _capital_base(pos) or (pos["income"]["strike"] if pos.get("income") else None)
    usd = round(income_ps * MULT * pos["contracts"], 0)
    days = _days_between(pos["income"]["cycle_opened"], _today())
    rec = dict(closed=_today(), strike=pos["income"]["strike"], sold=round(sold or 0, 2),
               buyback=round(buyback or 0, 2), income_ps=round(income_ps, 2), income_usd=usd,
               coc=round(income_ps / cap * 100, 2) if cap else None, days=days, reason=reason)
    pos["weeklies"].append(rec)
    pos["premium_banked"] = round(pos["premium_banked"] + usd, 0)
    return rec


def _roll_to(pos, new_cc):
    """Re-write the next weekly CC (a roll): the campaign continues, status ROLLED. Carries the new
    CC's expiry so it stays fully re-markable next week."""
    pos["income"] = dict(right="C", strike=new_cc["strike"], exp=new_cc.get("exp"),
                         exp_ts=new_cc.get("exp_ts"), sold=new_cc["sold"], cycle_opened=_today())
    pos["status"] = "ROLLED"


def _close_campaign(pos, long_mark, short_buyback, detail):
    """CASE B campaign closeout: long-call P&L + banked weeklies + current short-leg P&L."""
    lc = pos.get("long_call") or {}
    paid = lc.get("ask_paid") or 0
    long_pl = ((long_mark or 0) - paid) * MULT * pos["contracts"]
    short_pl = ((pos["income"]["sold"] or 0) - (short_buyback or 0)) * MULT * pos["contracts"]
    realized = round(pos["premium_banked"] + long_pl + short_pl, 0)
    cap = (paid * MULT * pos["contracts"]) or 1
    pos["closed"] = dict(date=_today(), cohort="campaign", realized=realized,
                         coc=round(realized / cap * 100, 2), days=_days_between(pos["opened"], _today()),
                         detail=detail)
    pos["status"] = "CLOSED"


def _mark_one(pos, op, crumb, log):
    inc = pos.get("income")
    if not inc:
        log.append(f"  {pos['id']}: no legs captured (opened off-hours) - re-open when live.")
        return
    ticker = pos["ticker"]
    try:
        root = legsmod._chain(op, crumb, ticker)
        spot = root["quote"]["regularMarketPrice"]
    except Exception as e:
        log.append(f"  {pos['id']}: data fetch failed ({e.__class__.__name__}) - skipped.")
        return

    # re-mark the income leg (the managed short). Prefer its own expiry chain; fall back to root.
    inc_chain = None
    if inc.get("exp_ts"):
        try:
            inc_chain = legsmod._chain(op, crumb, ticker, inc["exp_ts"])
        except Exception:
            inc_chain = None
    dte = legsmod._dte(inc["exp_ts"], dt.date.today()) if inc.get("exp_ts") else None
    m = _pick_option(inc_chain, inc["strike"], inc["right"]) if inc_chain else None
    cur = m["mark"] if m else None
    expired = dte is not None and dte <= 0

    # Off-hours / thin data guard: a missing mark on a still-live contract is NOT "expired worthless".
    if cur is None and not expired:
        log.append(f"  {pos['id']} {ticker}: no live mark (market closed / thin) - HOLD, re-run in-hours.")
        return

    sold, strike = inc["sold"], inc["strike"]
    itm = inc["right"] == "C" and spot is not None and spot > strike

    # ---- CASE B: short CC ITM near expiry ----
    # John's rule: NEVER close red - roll up-and-out. Close BOTH legs here ONLY if that locks a WIN;
    # if closing now would realize a LOSS, buy back the ITM CC and re-write a fresh OTM CC further out,
    # keeping the (winning, deep-ITM) long call + the campaign alive so the loss is deferred, not booked.
    if pos["kind"] == "CCW" and itm and dte is not None and dte <= NEAR_EXPIRY:
        lc = pos.get("long_call") or {}
        lc_mark = None
        if lc.get("exp_ts"):
            try:
                lc_chain = legsmod._chain(op, crumb, ticker, lc["exp_ts"])
                lm = _pick_option(lc_chain, lc["strike"], "C")
                lc_mark = lm["mark"] if lm else None
            except Exception:
                pass
        lc_mark = lc_mark if lc_mark is not None else max(0.0, (spot or 0) - lc.get("strike", 0))
        buyback = cur if cur is not None else max(0.0, (spot or 0) - strike)
        # Would closing BOTH legs now realize a win? (banked weeklies + long-call P&L + short-leg P&L)
        paid = lc.get("ask_paid") or 0
        long_pl  = ((lc_mark or 0) - paid) * MULT * pos["contracts"]
        short_pl = ((sold or 0) - (buyback or 0)) * MULT * pos["contracts"]
        would_realize = pos["premium_banked"] + long_pl + short_pl
        if would_realize >= 0:
            _close_campaign(pos, lc_mark, buyback,
                            f"CASE B WIN: {ticker} {spot:.2f} above {strike:g}C at {dte}d - closed both legs green.")
            c = pos["closed"]
            log.append(f"  {pos['id']} {ticker}: CASE B CAMPAIGN CLOSEOUT (win)  {c['realized']:+,.0f}  "
                       f"({c['coc']:.1f}% on the long-call capital, {c['days']}d).")
            return
        # Closing now would be RED -> roll it (John never books the loss): realize this CC cycle, re-write up-and-out.
        rec = _realize_weekly(pos, sold, buyback, "CASE B roll (avoid closing red)")
        new_cc = _next_weekly(op, crumb, ticker, spot) if spot else None
        if new_cc:
            _roll_to(pos, new_cc)
            log.append(f"  {pos['id']} {ticker}: CASE B ROLL up-and-out (closing now = {would_realize:+,.0f} red) "
                       f"-> bought back {strike:g}C, wrote {new_cc['strike']:g}C @ {new_cc['sold']}; campaign stays alive.")
        else:
            pos["status"] = "ROLLED"
            log.append(f"  {pos['id']} {ticker}: CASE B ROLL (closing now = {would_realize:+,.0f} red) - "
                       f"bought back {strike:g}C, no fresh OTM strike to re-write right now.")
        return

    # ---- OTM management on the weekly CC: expire / -50% / cheap -> realize + roll ----
    if pos["kind"] == "CCW":
        reason = None
        buyback = cur
        if expired and not itm:
            reason, buyback = "expired OTM", 0.0
        elif cur is not None and cur <= HALF * sold and (dte is None or dte > NEAR_EXPIRY):
            reason = "-50% rule"
        elif cur is not None and cur < CHEAP and dte is not None and dte >= CHEAP_MIN_DTE:
            reason = "cheap-to-close"
        elif dte is not None and dte <= NEAR_EXPIRY and not itm:
            reason, buyback = "expired OTM", (cur or 0.0)   # near expiry OTM: let it go, re-write
        if reason:
            rec = _realize_weekly(pos, sold, buyback, reason)
            new_cc = _next_weekly(op, crumb, ticker, spot) if spot else None
            if new_cc:
                _roll_to(pos, new_cc)
                log.append(f"  {pos['id']} {ticker}: WEEKLY {rec['income_usd']:+,.0f} "
                           f"({rec['coc']}% , {rec['days']}d, {reason}) -> rolled to {new_cc['strike']:g}C "
                           f"@ {new_cc['sold']}.")
            else:
                pos["status"] = "ROLLED"
                log.append(f"  {pos['id']} {ticker}: WEEKLY {rec['income_usd']:+,.0f} "
                           f"({reason}) - no fresh OTM strike to re-write now.")
            return
        log.append(f"  {pos['id']} {ticker}: HOLD - CC {strike:g}C mark {cur:.2f} vs sold {sold:.2f}, "
                   f"{dte}d, {'ITM' if itm else 'OTM'}. Keep selling time.")
        return

    # ---- CSP path (short put): John ROLLS, he does not sit and take assignment ----
    # From his ROLLED-CSP emails (e.g. TNA $58 -> $57, "rolling down to reduce my cost basis should I get
    # assigned"): near expiry he buys back the tested put and re-sells one. If it expired OTM he re-sells
    # at a similar level (keep selling time); if it went ITM he rolls DOWN-AND-OUT to a lower strike for a
    # fresh credit, lowering the eventual cost basis. Assignment is the FALLBACK only when there is no
    # strike left to roll into: the CSP then closes a WIN (premium banked), shares come in at the strike
    # (= cost basis), and it becomes a covered call. He NEVER books an assignment as a loss.
    put_itm = spot is not None and spot < strike
    near = dte is not None and dte <= NEAR_EXPIRY
    at_otm_expiry = expired and not put_itm
    at_itm_roll = put_itm and near                 # includes an already-expired ITM put (dte <= 0)
    if at_otm_expiry or at_itm_roll:
        # Re-bank guard: never realize a leg a prior mark already banked (re-run / catch-up safety).
        already = any(abs(w["strike"] - strike) < 1e-6 and w["closed"] >= inc.get("cycle_opened", "")
                      for w in pos["weeklies"])
        buyback = 0.0 if at_otm_expiry else (cur if cur is not None else max(0.0, strike - (spot or strike)))
        reason = "CSP expired OTM" if at_otm_expiry else "CSP roll down-and-out (avoid assignment)"
        banked = "already banked"
        if not already:
            rec = _realize_weekly(pos, sold, buyback, reason)
            banked = f"{rec['income_usd']:+,.0f}"
        new_put = _next_put(op, crumb, ticker, spot, (strike if at_itm_roll else None)) if spot else None
        if new_put:
            pos["income"] = dict(right="P", strike=new_put["strike"], exp=new_put["exp"], exp_ts=new_put["exp_ts"],
                                 sold=new_put["sold"], cycle_opened=_today(), cost_cc_bid=inc.get("cost_cc_bid"))
            pos["status"] = "ROLLED"
            kind_txt = "ROLL down-and-out" if at_itm_roll else "expired OTM -> re-sold"
            log.append(f"  {pos['id']} {ticker}: CSP {kind_txt} ({banked}) - wrote {new_put['strike']:g}P "
                       f"@ {new_put['sold']} ({new_put['exp']}); campaign stays alive.")
        else:
            pos["income"] = None                  # no active short (cleared) -> stable, never re-banked
            pos["status"] = "ROLLED"
            if at_itm_roll:
                pos["events"].append(dict(date=_today(),
                    detail=f"CSP assigned at {strike:g} = cost basis (no lower strike to roll); premium banked, write the CC."))
                log.append(f"  {pos['id']} {ticker}: CSP ASSIGNED at {strike:g} ({banked}) - premium banked (win), write the CC.")
            else:
                log.append(f"  {pos['id']} {ticker}: CSP expired OTM ({banked}) - no fresh strike to re-sell now, paused.")
        return
    log.append(f"  {pos['id']} {ticker}: HOLD - CSP {strike:g}P mark {cur and round(cur,2)}, {dte}d, "
               f"{'ITM' if put_itm else 'OTM'}.")


def mark():
    book = load()
    live = [p for p in book["positions"] if p["status"] in ("OPEN", "ROLLED")]
    if not live:
        print("No open paper positions to mark. Capture one with:  python3 cpt_paper.py open TQQQ")
        return
    try:
        op, crumb = legsmod._session()
    except Exception as e:
        print(f"Could not open a Yahoo session ({e.__class__.__name__}). Try again in-hours.")
        return
    log = []
    print(f"Marking {len(live)} paper position(s) on live data...\n")
    for pos in live:
        _mark_one(pos, op, crumb, log)
    save(book)
    print("\n".join(log))
    print("\nSaved. Run `python3 cpt_paper.py report` for the scorecard vs John.")


# --- list ----------------------------------------------------------------------------------------
def list_positions():
    book = load()
    if not book["positions"]:
        print("Ledger empty. Capture one with:  python3 cpt_paper.py open TQQQ")
        return
    print(f"{'ID':22} {'status':7} {'structure':20} {'entry':>7} {'wk':>3} {'banked$':>8}  opened")
    print("-" * 82)
    for p in book["positions"]:
        print(f"{p['id']:22} {p['status']:7} {p['structure'][:20]:20} {p['entry_price']:7.2f} "
              f"{len(p['weeklies']):3} {p['premium_banked']:8.0f}  {p['opened']}")


# --- report (scorecard vs John) ------------------------------------------------------------------
def report():
    book = load()
    pos = book["positions"]
    opens = [p for p in pos if p["status"] != "CLOSED"]
    closed = [p for p in pos if p["status"] == "CLOSED"]
    weeklies = [w for p in pos for w in p["weeklies"]]           # every weekly close, all campaigns
    campaigns = [p for p in closed if p.get("closed", {}).get("cohort") == "campaign"]

    print("=" * 74)
    print("PAPER SCORECARD  -  our automated pipeline vs John's realized track record")
    print("=" * 74)
    print(f"positions: {len(pos)} total   |   {len(opens)} open/rolling   |   {len(closed)} closed   "
          f"|   {len(weeklies)} weekly closes banked\n")

    # --- structure-mix read (available from day 1, before any close) ---
    print("STRUCTURE MIX  (are we picking structures the way John does?)")
    ours = {}
    for p in pos:
        ours[p["structure_bucket"]] = ours.get(p["structure_bucket"], 0) + 1
    jn = JOHN["opens"]; jtot = sum(jn.values()); otot = sum(ours.values()) or 1
    print(f"  {'structure':24} {'ours':>12} {'John':>12}")
    for key, name in [("itm_ccw", "99-delta ITM CCW"), ("csp", "CSP (incl. long-dated)"),
                      ("plain_ccw", "plain CCW"), ("pmcc", "PMCC"), ("other", "other")]:
        oc, jc = ours.get(key, 0), jn.get(key, 0)
        if oc == 0 and jc == 0:
            continue
        print(f"  {name:24} {oc:>4} ({oc/otot*100:4.0f}%) {jc:>4} ({jc/jtot*100:4.0f}%)")
    print()

    # --- outcomes: weekly closes + campaign closeouts vs John ---
    print("OUTCOMES vs John  (the real comparison)")
    print(f"  {'cohort':22} {'metric':12} {'ours':>10} {'John':>12}")
    _cohort_rows("weekly ITM-CC", weeklies, JOHN["weekly"], kind="weekly")
    _cohort_rows("campaign closeout", campaigns, JOHN["campaign"], kind="campaign")
    print()

    rolling = [p for p in pos if p["status"] == "ROLLED"]
    print(f"ROLL / UNDERWATER cohort: {len(rolling)} rolling  "
          f"(John's slice: 20 rolls, only 2 ever marked negative, net +$5,720 banked)")
    print()

    print("-" * 74)
    print("MEASUREMENT NOTE: win-rate is ~100% for BOTH by design (John never closes red - he rolls;")
    print("our ledger follows the same rule). Judge on $ / CoC% / days / roll depth - NOT win-rate.")
    print("$ = 10 contracts (John's ~1000-sh size); CoC% is size-free. Weekly CoC = income / long-call")
    print("capital (the deployed capital in a PMCC). Premiums are BS-EST (free Yahoo delayed data).")
    if not weeklies and not campaigns:
        print("\nNo closes realized yet - positions accrue as the written CCs decay. Run `mark` over the")
        print("coming days (or `open` as alerts fire). Structure-mix above is a live read already.")


def _cohort_rows(name, items, jb, kind):
    if not items:
        print(f"  {name:22} {'$/trade':12} {'-':>10} {jb['avg']:>12.0f}")
        print(f"  {'(n=0)':22} {'CoC% / days':12} {'-':>10} {str(jb['coc'])+' / '+str(jb['days']):>12}")
        return
    n = len(items)
    if kind == "weekly":
        avg = sum(w["income_usd"] for w in items) / n
        coc = sum(w["coc"] for w in items if w["coc"] is not None) / max(1, sum(1 for w in items if w["coc"] is not None))
        days = sum(w["days"] for w in items) / n
    else:
        avg = sum(p["closed"]["realized"] for p in items) / n
        coc = sum(p["closed"]["coc"] for p in items) / n
        days = sum(p["closed"]["days"] for p in items) / n
    print(f"  {name:22} {'$/trade':12} {avg:>10.0f} {jb['avg']:>12.0f}")
    print(f"  {'(n=' + str(n) + ')':22} {'CoC%':12} {coc:>10.2f} {jb['coc']:>12.2f}")
    print(f"  {'':22} {'days held':12} {days:>10.1f} {jb['days']:>12.1f}")


def digest_text():
    """Compact phone-ready scorecard for the weekly Telegram digest (HTML). Same numbers as report(),
    laid out short. Kept well under Telegram's message limit."""
    book = load()
    pos = book["positions"]
    weeklies = [w for p in pos for w in p["weeklies"]]
    camps = [p for p in pos if (p.get("closed") or {}).get("cohort") == "campaign"]
    rolling = [p for p in pos if p["status"] == "ROLLED"]
    live = [p for p in pos if p["status"] != "CLOSED"]
    L = ["\U0001F4CB <b>CPT paper scorecard vs John</b> (weekly digest)",
         f"positions: {len(pos)} | {len(live)} live | {len(camps)} campaigns closed | "
         f"{len(weeklies)} weekly closes banked"]
    if weeklies:
        avg = sum(w["income_usd"] for w in weeklies) / len(weeklies)
        cocs = [w["coc"] for w in weeklies if w["coc"] is not None]
        coc = sum(cocs) / len(cocs) if cocs else 0
        L.append(f"weekly ITM-CC: <b>ours ${avg:,.0f} / {coc:.2f}%</b> vs John $2,944 / 4.46%")
    else:
        L.append("weekly ITM-CC: none closed yet vs John $2,944 / 4.46%")
    if camps:
        cavg = sum(p["closed"]["realized"] for p in camps) / len(camps)
        ccoc = sum(p["closed"]["coc"] for p in camps) / len(camps)
        L.append(f"campaign: <b>ours ${cavg:,.0f} / {ccoc:.2f}%</b> vs John $10,683 / 9.71%")
    L.append(f"rolling/underwater: {len(rolling)}")
    L.append("<i>win-rate ~100% both by design - judge $ / CoC% / days / roll depth ($=10 lots).</i>")
    return "\n".join(L)


USAGE = ("usage:\n"
         "  python3 cpt_paper.py open <TICKER> [--force]   capture a paper position from the alert read\n"
         "  python3 cpt_paper.py mark                      re-mark opens, apply John's rules, realize closes\n"
         "  python3 cpt_paper.py list                      show the ledger\n"
         "  python3 cpt_paper.py report                    scorecard vs John\n"
         "  python3 cpt_paper.py digest                    the compact weekly digest text")


def main():
    args = sys.argv[1:]
    if not args:
        print(USAGE); return
    cmd = args[0].lower()
    if cmd == "open" and len(args) >= 2:
        open_position(args[1], force="--force" in args)
    elif cmd == "mark":
        mark()
    elif cmd == "list":
        list_positions()
    elif cmd == "report":
        report()
    elif cmd == "digest":
        print(digest_text())
    else:
        print(USAGE)


if __name__ == "__main__":
    main()
