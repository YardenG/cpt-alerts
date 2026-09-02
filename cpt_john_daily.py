#!/usr/bin/env python3
"""
CPT DAILY JOHN-WATCH - the headless end-of-day read of John's inbox, run by GitHub Actions
(no MCP connector, computer off). It exists to guarantee we NEVER miss new John information -
both new TRADES and, crucially, the TEACHING emails the weekly ingest deliberately throws away
(the doctrine essays, member Q&A, trader lessons, 99-Delta / roll / assignment notes, market reads).

What it does each run (all pure stdlib + IMAP, same App-Password pattern as cpt_john_review.py):
  1) INGEST     cpt_john_ingest.run() - IMAP-pull John's new trade OPENS into john-alerts.json.
  2) TEACHING-WATCH  classify every John subject over the lookback window; anything that is NOT a
                     trade transaction and NOT pure admin = a TEACHING/commentary email. De-dup
                     against john-teaching-seen.json and surface only the net-new ones.
  3) PROOF-OF-LIFE   post ONE compact Telegram line every day - either the day's opens + teaching,
                     or "John quiet today". Yarden chose a daily heartbeat so a silent stall (the
                     failure that killed the old local scheduler) is impossible to miss.

NEVER silently no-ops: ingest failure, IMAP failure, or stale data print a LOUD flag in the ping.
The teaching flag is a POINTER - an agent still does the qualitative read-in on demand (updating
cpt-doctrine.md). This job's job is to make sure nothing sits unread.

First run seeds john-teaching-seen.json from the current lookback WITHOUT alerting on the backlog
(so the heartbeat doesn't fire a 20-item wall of history), then watches incrementally from there.

env (GitHub Secrets): GMAIL_ADDRESS, GMAIL_APP_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import os, json, html, urllib.request, urllib.parse, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
import cpt_john_ingest as ing

SEEN = os.path.join(HERE, "john-teaching-seen.json")

# A John email is a TRADE TRANSACTION (already covered by the alert engine + ingest) if its subject
# carries any of these - we do NOT flag those as teaching.
_TXN = ("NEW OPEN", "OPEN UCPTD", "OPEN PMCC", "OPEN OTM", "CLOSED", "CLOSE OUT", "CLOSEOUT",
        "ROLLED", "TRADES HAVE BEEN POSTED", "TRADES | ", "TRADE IS LIVE", "TRADE IS  LIVE",
        "TRADE IS 1/2", "TRADE 1/2", "FIRST TRADE", "DOUBLE DOWN")
# Pure admin / plumbing - not teaching, not a trade.
_ADMIN = ("DASHBOARD HAS BEEN UPDATED", "HAS BEEN UPDATED", "MEMBERSHIP LINKS", "MEMBERSHIP LINK",
          "PASSWORD", "UNSUBSCRIBE")


def _is_teaching(subject):
    """True if this John subject is a teaching / commentary email worth a human's eye - i.e. NOT a
    trade transaction and NOT admin plumbing. Deliberately inclusive: better to over-flag John's
    discretionary reads (IN FOCUS, market notes) than to miss a doctrine shift."""
    up = (subject or "").upper()
    if not up.strip():
        return False
    if any(k in up for k in _TXN):
        return False
    if any(k in up for k in _ADMIN):
        return False
    return True


def _load_seen():
    try:
        with open(SEEN) as f:
            d = json.load(f)
        return set(d.get("keys", [])), d
    except Exception:
        return set(), {"keys": [], "first_seeded": None}


def _save_seen(keys, meta):
    meta["keys"] = sorted(keys)
    with open(SEEN, "w") as f:
        json.dump(meta, f, indent=2)


def tg(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[daily] no telegram token/chat - ping not sent (printed below)\n" + text)
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception as e:
        print("[daily] telegram send failed:", str(e)[:150])
        return False


def _clean(subject):
    """Trim John's boilerplate prefixes for a compact ping line."""
    s = (subject or "").strip()
    for pre in ("🚨 UCPTD | ", "🚨 UCPTD ", "UCPTD | ", "Fwd: 🚨 UCPTD | ", "Fwd: "):
        if s.startswith(pre):
            s = s[len(pre):]
    return s.strip()


def main():
    today = dt.date.today()
    parts = [f"\U0001F4D3 <b>CPT daily John-watch</b> {today.isoformat()}"]

    # 1) INGEST new trade opens (loud on failure) -----------------------------------------------
    ingest_line, subjects = "", []
    try:
        added = ing.run()
        subjects = ing._fetch_john_subjects()          # (subject, date) over the 21d lookback
        if added:
            ingest_line = "trades: <b>%d</b> new open(s) - %s" % (
                len(added), ", ".join(e["ticker"] for e in added))
        else:
            ingest_line = "trades: none new"
    except Exception as e:
        ingest_line = f"⚠️ INGEST/IMAP FAILED: {str(e)[:120]}"
        parts.append(ingest_line)
        parts.append("John data may be STALE - check the workflow run.")
        tg("\n".join(parts))
        print("[daily] INGEST FAILED:", e)
        return

    # 2) TEACHING-WATCH -------------------------------------------------------------------------
    seen, meta = _load_seen()
    teaching = [(s, d) for (s, d) in subjects if _is_teaching(s)]
    # key = date|subject so the same email is never re-flagged
    new_keys = [f"{d}|{s}" for (s, d) in teaching if f"{d}|{s}" not in seen]

    first_run = meta.get("first_seeded") is None
    if first_run:
        # seed the backlog silently - don't wall the user with 21 days of history on day one
        for (s, d) in teaching:
            seen.add(f"{d}|{s}")
        meta["first_seeded"] = today.isoformat()
        _save_seen(seen, meta)
        parts.append(ingest_line)
        parts.append("teaching-watch: seeded <b>%d</b> recent teaching email(s); "
                     "now watching daily for new ones." % len(teaching))
        tg("\n".join(parts))
        print("[daily] seeded", len(teaching), "teaching emails")
        return

    for k in new_keys:
        seen.add(k)
    _save_seen(seen, meta)

    # 3) PROOF-OF-LIFE ping ---------------------------------------------------------------------
    parts.append(ingest_line)
    if new_keys:
        # newest first
        rows = sorted(({"date": k.split("|", 1)[0], "subj": k.split("|", 1)[1]} for k in new_keys),
                      key=lambda r: r["date"], reverse=True)
        parts.append("📚 teaching: <b>%d</b> new - an agent should read these in:" % len(rows))
        for r in rows[:8]:
            parts.append(f"• {html.escape(_clean(r['subj']))}")
        if len(rows) > 8:
            parts.append(f"… +{len(rows) - 8} more")
    else:
        parts.append("📚 teaching: none new")

    if "none new" in ingest_line and not new_keys:
        parts.append("\n<i>John quiet today ✓ (watcher alive)</i>")

    sent = tg("\n".join(parts))
    print("[daily] ping sent:", sent, "| new teaching:", len(new_keys))


if __name__ == "__main__":
    if not os.environ.get("GMAIL_APP_PASSWORD"):
        print("No GMAIL_APP_PASSWORD set - this is the live daily watcher; set the env vars (or run")
        print("it from the GitHub Actions workflow) to read John's inbox. Nothing to do locally.")
    else:
        main()
