#!/usr/bin/env python3
"""
CPT->Alpaca cloud ORCHESTRATOR - the headless entry point GitHub Actions calls.

Tasks: OPEN new valid entries, MANAGE open campaigns (roll/close). Guards on the
REAL Alpaca market clock (skips when closed - options need live data + fills).
Telegrams a summary of every action AND a FAILURE alert on any exception (so a
silent stall can't happen). ALPACA_LIVE=1 places real paper orders; otherwise it
runs DRY and reports only what it WOULD do.

  python3 run_alpaca.py open
  python3 run_alpaca.py manage
  python3 run_alpaca.py both     # the daily cloud run
"""
import os, sys, urllib.request, urllib.parse, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # _alert-system/ (cpt_data_spike etc.)
import alpaca_api as A
import cpt_data_spike as ds
import cpt_alpaca_open as opener
import cpt_alpaca_manage as manager

LIVE = os.environ.get("ALPACA_LIVE", "0") == "1"
MAX_CAMPAIGNS = int(os.environ.get("ALPACA_MAX_CAMPAIGNS", "12"))


def tg(msg):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print("[tg] (no secrets - skipping send)"); return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=20).read()
    except Exception as e:
        print(f"[tg] send failed: {e}")


def market_open():
    try:
        return bool(A.api("GET", "/v2/clock").get("is_open"))
    except Exception:
        return False


def _summ(payload):
    if payload.get("order_class") == "mleg":
        return f"CCW net {payload.get('limit_price')} x{payload.get('qty')}"
    return f"CSP {payload.get('limit_price')} x{payload.get('qty')}"


def do_open(acct):
    camps = manager.group_campaigns()
    held = {k for k, v in camps.items() if v["short"] or v["short_put"] or v["long"]}
    # also exclude names with a RESTING (unfilled) order, so a not-yet-filled open isn't doubled
    for o in A.get_orders(status="open"):
        for s in ([lg.get("symbol") for lg in (o.get("legs") or [])] or [o.get("symbol")]):
            if s and len(s) >= 15 and s[-9] in "CP":
                held.add(opener.parse_occ(s)[0])
    n = len(held)
    lines = []
    for tk in ds.UNIVERSE:
        if tk in held:
            continue
        if n >= MAX_CAMPAIGNS:
            lines.append(f"(campaign cap {MAX_CAMPAIGNS} reached)"); break
        try:
            payload = opener.plan(tk, force=False, acct=acct)
        except Exception as e:
            print(f"{tk}: plan error {e}"); continue
        if not payload:
            continue
        if LIVE:
            try:
                o = opener.submit_live(payload)
                lines.append(f"\U0001F7E2 OPEN {tk} {_summ(payload)} [{o.get('status')}]")
                n += 1
            except Exception as e:
                lines.append(f"⚠ OPEN {tk} FAILED: {str(e)[:60]}")
        else:
            lines.append(f"(dry) would OPEN {tk} {_summ(payload)}")
            n += 1
    return lines


def do_manage():
    results = manager.run(live=LIVE) or []
    lines = []
    for r in results:
        if r["action"] == "HOLD":
            continue
        tag = "\U0001F501" if ("ROLL" in r["action"] or "RESELL" in r["action"]) else "\U0001F534"
        lines.append(f"{tag} {r['action']} {r['sym']}: {r['detail'][:70]}")
    return lines


def main():
    task = sys.argv[1] if len(sys.argv) > 1 else "both"
    mode = "LIVE" if LIVE else "DRY"
    try:
        if not market_open():
            print("market closed - orchestrator skipping (no action).")
            return
        acct = A.get_account()
        out = []
        if task in ("open", "both"):
            out += do_open(acct)
        if task in ("manage", "both"):
            out += do_manage()
        if out:
            body = f"CPT-Alpaca {mode} ({task})\n" + "\n".join(out)
            print("\n=== SUMMARY ===\n" + body + "\n")
            tg("\U0001F4D8 <b>" + f"CPT-Alpaca {mode}</b> ({task})\n" + "\n".join(out))
        else:
            print(f"CPT-Alpaca {mode} ({task}): no actions (all HOLD / no valid entries).")
    except Exception as e:
        traceback.print_exc()
        tg(f"⚠️ <b>CPT-Alpaca FAILED</b> ({task}, {mode}): {str(e)[:120]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
