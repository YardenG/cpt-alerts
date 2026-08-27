# John vs. Our System - Learning Log

Our system keeps its OWN opinion (the live alerts never change here). This log is the self-critique
loop Yarden asked for: every time John opens a real trade, we make our gate look at that same name
AS OF that day and answer "did I flag it too, and if not WHY?" The miss-reasons accumulate here into
a calibration signal - **advise-only**: the proven gate changes only if a pattern holds AND Yarden
approves. Tool: `cpt_vs_john.py --john` (reads `john-alerts.json`, seeded from his real Gmail alerts).

This is NOT about matching John's cadence (he re-enters the same names weekly; our fire-once alerts on
the crossing - that timing gap is a separate, known design choice). This log is about our JUDGMENT:
when John says "this is a buy," does our independent read agree, and where does it systematically not?

---

## 2026-08-14 - first review (John's entries Aug 5-14, n=15)

**Agreement: 13/15 (87%).** Our gate independently flagged 13 of John's 15 real entries. Reads are
as-of each alert's date (Keltner + RSI recomputed to that day's bar), so drift-after-entry is not
misjudged (e.g. TNA Aug 12 correctly = ENTRY at pos 59, though it sits at watch/60.7 today).

**The 2 misses - both TQQQ, both "watch" at the boundary:**
- 2026-08-13 TQQQ: our read pos 62, RSI 59 -> watch (2 pts over our pos<=60 line).
- 2026-08-07 TQQQ: our read pos 60, RSI 55 -> watch (right on the line).

**Insight (the learning signal):** our divergences from John cluster RIGHT AT the pos-60 boundary -
John enters TQQQ a touch higher in the channel than our gate allows. Nothing else missed. Our
"lower-in-the-channel" calibration (pos<=60) is slightly tighter than John's actual TQQQ entries.

**WATCH (do NOT act yet):** if TQQQ (or other names) keep getting missed at pos 60-63 over the coming
weeks, consider nudging the entry gate `pos<=60 -> pos<=63`. Only 2 data points now, both one name -
not enough. Protect the proven gate (92% historical fit); change it only if the pattern holds and
Yarden approves. Re-run `cpt_vs_john.py --john` after each week's new John alerts and update this log.

Structure agreement is perfect: John's 14 CCW opens + 1 PMCC all map to our suggested structure logic
(green-day -> 99-delta ITM CCW), consistent with the paper-ledger picks.

---

## 2026-08-15 - weekly review

**No new John entries this week.** Gmail search (`label:"CPT Dashboard" newer_than:8d`) returned the
same 15 real opens already seeded last review (Aug 5-14) - nothing new posted since. Re-ran
`cpt_vs_john.py --john` as a sanity check: identical result, **13/15 (87%)**, same 2 misses (both
TQQQ, both a couple points over our pos<=60 line).

**Pattern status: still just 2 data points, 1 ticker (TQQQ).** Per last week's threshold (4+ instances
across 2+ tickers before flagging), this has NOT hardened yet - no gate-nudge recommendation this week.
Keep watching; re-run after John posts fresh entries.

Paper scorecard (`cpt_paper.py report`): 3 positions open/rolling, 0 closed yet, structure mix 100%
99-delta ITM CCW (matches John's dominant structure). No $ comparison possible until closes realize.

---

## 2026-08-27 - weekly review (BIG catch-up: John's entries Aug 17-26 ingested, n=33 total)

**The auto-review had silently stalled since 08-14** (the local scheduled task didn't run; `john-alerts.json`
was 13 days stale). Pulled John's real Gmail alerts Aug 15-27 by hand and appended 18 new opens (now 33
entries, Aug 5-26).

**Agreement: 31/33 (94%).** Our independent gate flagged 31 of John's 33 real entries as-of their date.
Crucially, **all 18 freshly-added entries (Aug 17-26) AGREED - zero new misses.** The gate held across a
much larger, 3-week sample.

**The 2 misses are the SAME two as before** - both TQQQ, both a hair over our pos<=60 line:
- 2026-08-13 TQQQ: pos 62, RSI 58 -> watch.
- 2026-08-07 TQQQ: pos 60, RSI 55 -> watch.

**Pattern status: NOT hardening - if anything, weakening as a concern.** Still exactly 2 data points, 1
ticker (TQQQ), and two weeks of fresh entries added ZERO new boundary misses. Per the 4+/2-ticker
threshold, no gate-nudge. The `pos<=60` gate looks well-calibrated against John's live behavior; leave it.
Re-run after next week's alerts.

Structure agreement remains perfect (every CCW/PMCC maps to our suggested structure logic).
