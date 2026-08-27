#!/usr/bin/env python3
"""
Phase 3 - SELF-CRITIQUE vs John: "why didn't WE flag the same trade?"

Yarden's design: our system keeps its OWN opinion (the live alerts stay exactly as they are - this
tool NEVER touches the gate). But every time John actually opens a trade, we make our system look at
that same name AT THAT MOMENT and answer honestly: did our independent gate ALSO flag it (AGREE), or
did we miss it - and WHY? Over time the miss-reasons cluster into a learning signal about where our
judgment diverges from John's, which we can feed back into calibration (advise-only, Yarden approves).

Key honesty: it reads our gate AS OF John's alert DATE (recomputing the Keltner channel + RSI up to
that day's bar), not today - so a name that drifted after John entered is judged on what we would have
seen when he entered, not on where it sits now. Forward, when a John email arrives, "as of" = that day.

Data: free Yahoo daily bars (same engine as the live alert). Pure stdlib. READ-ONLY / advise-only.

Run:
  python3 cpt_vs_john.py --john            # diagnose every entry in john-alerts.json (as-of each date)
  python3 cpt_vs_john.py TQQQ NAIL         # diagnose these names as of today (quick check)
"""
import json, os, sys, datetime as dt
import cpt_data_spike as ds

HERE = os.path.dirname(os.path.abspath(__file__))
JOHN_FILE = os.path.join(HERE, "john-alerts.json")


def analyze_asof(ticker, date=None):
    """Our gate read for `ticker`, computed AS OF `date` (YYYY-MM-DD) - the Keltner channel + RSI up
    to that day's daily bar. date=None -> latest/live (identical to cpt_data_spike.analyze)."""
    rows, live_price, times = ds.fetch(ticker)
    closes = [r[3] for r in rows]
    if date:
        target = dt.date.fromisoformat(date)
        idx = None
        for i, t in enumerate(times):
            if dt.datetime.utcfromtimestamp(t).date() <= target:
                idx = i
        if idx is None:
            idx = len(rows) - 1
    else:
        idx = len(rows) - 1
    sub_rows, sub_closes = rows[:idx + 1], closes[:idx + 1]
    mid = ds.ema(sub_closes, ds.KC_LEN)[-1]
    atr = ds.wilder_atr(sub_rows, ds.ATR_LEN)[-1]
    rsi = ds.wilder_rsi(sub_closes, 14)[-1]
    px = live_price if date is None else sub_closes[-1]
    upper, lower = mid + ds.KC_MULT * atr, mid - ds.KC_MULT * atr
    pos = (px - lower) / (upper - lower) * 100 if upper > lower else float("nan")
    strong = (38 <= pos <= 55) and (42 <= rsi <= 58)
    valid = (0 <= pos <= 60) and rsi < 70
    verdict = ("BELOW CH" if pos < 0 else "ENTRY *" if strong else "ENTRY" if valid else
               "watch" if pos <= 68 and rsi < 70 else "TOO HIGH")
    prev = sub_closes[-2] if len(sub_closes) >= 2 else px
    return dict(t=ticker, price=round(px, 2), pos=pos, rsi=rsi, strong=strong, valid=valid,
                verdict=verdict, day="green" if px >= prev else "red", asof=date or "now")


def critique(a):
    """Did our INDEPENDENT gate agree with John on this name? If not, the honest reason.
    Returns (agree_bool, bucket, reason). bucket groups misses for the learning summary."""
    pos, rsi, v = a["pos"], a["rsi"], a["verdict"]
    if a["strong"]:
        return True, "agree", f"AGREE - ENTRY* (pos {pos:.0f}, RSI {rsi:.0f}), our prime cluster"
    if a["valid"]:
        return True, "agree", f"AGREE - ENTRY (pos {pos:.0f}, RSI {rsi:.0f})"
    if v == "watch":
        return False, "just-over-gate", (f"MISS - watch: pos {pos:.0f} just over our 60 line "
                                         f"(RSI {rsi:.0f}); John enters a touch higher in the channel")
    if v == "BELOW CH":
        return False, "below-channel", (f"MISS - below channel: pos {pos:.0f} under the lower band; "
                                        f"we read oversold/knife, John bought the dip")
    hi = "RSI>=70 (overbought)" if rsi >= 70 else "pos in the upper third (extended)"
    return False, "too-high", f"MISS - too high: pos {pos:.0f}, RSI {rsi:.0f} - {hi}; above our gate"


def run_john():
    if not os.path.exists(JOHN_FILE):
        print(f"No {JOHN_FILE}. Seed it with John's real entries first."); return
    with open(JOHN_FILE) as f:
        entries = json.load(f)["entries"]
    print("SELF-CRITIQUE vs John  -  'why didn't WE flag the same trade?'  (our gate as-of his date)")
    print("=" * 90)
    print(f"{'date':11} {'ticker':6} {'John did':10} {'our gate as-of':16} verdict")
    print("-" * 90)
    agree = 0
    buckets = {}
    misses = []
    for e in entries:
        try:
            a = analyze_asof(e["ticker"], e.get("date"))
        except Exception as ex:
            print(f"{e.get('date',''):11} {e['ticker']:6} ERROR: {str(ex)[:50]}"); continue
        ok, bucket, reason = critique(a)
        agree += ok
        if not ok:
            buckets[bucket] = buckets.get(bucket, 0) + 1
            misses.append((e, a, reason))
        jd = e.get("structure", "OPEN")
        print(f"{e.get('date',''):11} {e['ticker']:6} {jd:10} {a['verdict']:16} {reason}")
    n = len(entries)
    print("-" * 90)
    print(f"\nAGREEMENT: our independent gate flagged {agree}/{n} of John's real entries "
          f"({agree/n*100:.0f}%) on its own.")
    if buckets:
        print("Where we diverged (the learning signal):")
        labels = {"just-over-gate": "just OVER our pos<=60 gate (John enters higher in the channel)",
                  "too-high": "TOO HIGH for us (overbought / upper third)",
                  "below-channel": "BELOW our channel (we call it a knife)"}
        for b, c in sorted(buckets.items(), key=lambda x: -x[1]):
            print(f"  - {c}x  {labels.get(b, b)}")
        print("\nThese clusters are the calibration hint - advise-only; the gate changes only if Yarden")
        print("approves after we see a pattern hold (protect the proven gate).")
    else:
        print("No divergences: our gate independently agreed with every John entry in this set.")


def run_now(tickers):
    print(f"{'ticker':6} {'verdict':10} {'day':6} our read (as of now)")
    print("-" * 78)
    for tk in tickers:
        try:
            a = analyze_asof(tk.upper())
        except Exception as ex:
            print(f"{tk.upper():6} ERROR: {str(ex)[:50]}"); continue
        _, _, reason = critique(a)
        print(f"{a['t']:6} {a['verdict']:10} {a['day']:6} {reason}")


def main():
    args = sys.argv[1:]
    if args and args[0] == "--john":
        run_john()
    elif args:
        run_now(args)
    else:
        print("usage:\n  python3 cpt_vs_john.py --john        diagnose John's real entries (john-alerts.json)\n"
              "  python3 cpt_vs_john.py TQQQ NAIL     diagnose these names as of now")


if __name__ == "__main__":
    main()
