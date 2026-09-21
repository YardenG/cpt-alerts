#!/usr/bin/env python3
"""
Tests for the intrinsic-value floor guardrail (cpt_paper._floor_intrinsic).

Root cause it fixes: the free Yahoo feed can mis-mark a deep-ITM option BELOW its intrinsic value.
That raw mark flows into the realized-$ math and fabricates P&L. Real case: AAPU 2026-09-18, a
long 22C with spot 45.40 (intrinsic 23.40) was marked 17.10 -> the called-away campaign booked
-2,800 on what was really a ~+3,500 win.

Pure stdlib, no network. Run: python3 test_intrinsic_floor.py
"""
import sys
import cpt_paper as P

MULT = P.MULT
passed = failed = 0

def check(name, got, want):
    global passed, failed
    ok = round(got, 4) == round(want, 4) if isinstance(got, (int, float)) and isinstance(want, (int, float)) else got == want
    passed += ok; failed += (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   got={got!r} want={want!r}"))


print("1) _floor_intrinsic - the guardrail itself")
# Call worth less than intrinsic (the bug) -> floored UP to intrinsic.
check("call below intrinsic is floored", P._floor_intrinsic(17.10, "C", 22.0, 45.40), 23.40)
# Call with genuine time value above intrinsic -> untouched (no over-correction).
check("healthy call mark passes through", P._floor_intrinsic(24.10, "C", 22.0, 45.40), 24.10)
# OTM call -> intrinsic is 0, a real (small) time-value mark is kept.
check("OTM call keeps time-value mark", P._floor_intrinsic(0.30, "C", 50.0, 45.40), 0.30)
# Missing mark (None) -> falls back to intrinsic, exactly like the old behavior.
check("None mark falls back to intrinsic", P._floor_intrinsic(None, "C", 22.0, 45.40), 23.40)
# Short call we buy back, mis-marked below intrinsic -> floored (can't buy back below intrinsic).
check("short-call buyback floored", P._floor_intrinsic(0.90, "C", 44.0, 45.40), round(45.40 - 44.0, 2))
# Put support (future-proofing; CSPs are managed elsewhere but the helper is general).
check("put below intrinsic is floored", P._floor_intrinsic(1.00, "P", 50.0, 45.40), round(50.0 - 45.40, 2))
# No spot -> can't compute intrinsic, return the mark unchanged (or 0 for None).
check("no spot returns mark as-is", P._floor_intrinsic(5.0, "C", 22.0, None), 5.0)
check("no spot + None mark -> 0", P._floor_intrinsic(None, "C", 22.0, None), 0.0)


print("\n2) AAPU regression - the phantom -2,800 loss")
# Exact ledger inputs: long 22C paid 19.20, short 44C sold 0.70, no banked weeklies, 10 contracts,
# spot 45.40 at called-away expiry. Feed mis-marked the long call at 17.10 (intrinsic was 23.40).
paid, sold, contracts, spot = 19.20, 0.70, 10, 45.40
lc_strike, sh_strike = 22.0, 44.0
bad_lc_mark = 17.10
short_buyback = round(spot - sh_strike, 2)  # 1.40, the short's intrinsic at expiry

def realized(long_mark, buyback):
    pos = {"long_call": {"ask_paid": paid}, "income": {"sold": sold}, "contracts": contracts,
           "premium_banked": 0.0, "opened": "2026-09-15"}
    P._close_campaign(pos, long_mark, buyback, "test")
    return pos["closed"]["realized"]

old = realized(bad_lc_mark, short_buyback)                                  # what the ledger booked
new = realized(P._floor_intrinsic(bad_lc_mark, "C", lc_strike, spot), short_buyback)  # with the floor
check("old (bad mark) reproduces the booked loss", old, -2800.0)
check("exit-only floor removes the phantom loss", new, 3500.0)
print(f"     -> exit floored alone: {old:+,.0f} -> {new:+,.0f}  (still overstated - entry also bad; see section 5)")


print("\n3) No over-correction - a genuine red close still books red")
# If the long call is legitimately below entry (real loss, mark >= intrinsic), the floor must NOT
# rescue it. Long 22C paid 19.20, spot 40.00 -> intrinsic 18.00, honest mark 18.20; short bought
# back at its sold price (short_pl = 0) to isolate the long-leg loss: a real -1,000 that must stay.
honest = realized(P._floor_intrinsic(18.20, "C", lc_strike, 40.00), sold)
check("real loss is preserved (no rescue)", honest, round((18.20 - 19.20) * MULT * contracts, 0))
print(f"     -> honest red close stays {honest:+,.0f}")


print("\n4) Entry-side floor - a fake-cheap cost basis can't inflate the win")
# AAPU entry: feed quoted the long 22C at 19.20 when spot was 43.92 (intrinsic 21.92) - impossible.
# The capture path floors ask_paid at intrinsic so the cost basis is honest.
entry_ask, entry_spot, k = 19.20, 43.92, 22.0
floored_paid = P._floor_intrinsic(entry_ask, "C", k, entry_spot)
check("impossible entry ask is floored to intrinsic", floored_paid, 21.92)
# A normal entry (ask above intrinsic, real time value) is left alone.
check("normal entry ask passes through", P._floor_intrinsic(25.20, "C", 19.0, 42.14), 25.20)

print("\n5) AAPU with BOTH ends floored - the honest number")
# paid floored 21.92, long exit floored 23.40 (intrinsic at spot 45.40), short buyback 1.40, no banked.
def realized_paid(paid, long_mark, buyback):
    pos = {"long_call": {"ask_paid": paid}, "income": {"sold": sold}, "contracts": contracts,
           "premium_banked": 0.0, "opened": "2026-09-15"}
    P._close_campaign(pos, long_mark, buyback, "test")
    return pos["closed"]["realized"]

honest_both = realized_paid(21.92, 23.40, 1.40)
check("both-ends floored gives the true +780", honest_both, 780.0)
# Independent cross-check: deep-ITM long tracks the stock 1:1, which rose 45.40-43.92 = +1.48/share.
stock_move = round((45.40 - 43.92) * MULT * contracts, 0)          # +1,480 long-leg gain
short_cost = round((0.70 - 1.40) * MULT * contracts, 0)            # -700 short leg
check("cross-check: stock move + short leg = +780", round(stock_move + short_cost, 0), 780.0)
print(f"     -> AAPU honest: long +{stock_move:,.0f} (stock +1.48) short {short_cost:,.0f} = +780")

print(f"\n{'='*48}\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
