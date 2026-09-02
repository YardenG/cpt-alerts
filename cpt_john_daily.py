#!/usr/bin/env python3
"""
CPT DAILY JOHN-WATCH - the headless end-of-day read of EVERYTHING John sends, run by GitHub Actions
(no MCP connector, computer off). It exists to guarantee we never miss anything John posts - every
email that day: new trades (opens), closes, rolls, AND the teaching / market-commentary the weekly
ingest throws away (doctrine essays, member Q&A, trader lessons, 99-Delta / roll / assignment notes).

What it does each run (all pure stdlib + IMAP, same App-Password pattern as cpt_john_review.py):
  1) INGEST     cpt_john_ingest.run() - IMAP-pull John's new trade OPENS into john-alerts.json.
  2) FULL SWEEP classify EVERY John subject over the lookback window; report everything NET-NEW since
                the last run, grouped: opens / closes / rolls / teaching+notes / admin. De-duped
                against john-seen.json so a re-run never double-reports and a missed run still catches
                up (net-new, not just "today").
  3) DIGEST     post ONE Telegram digest every day - the full day's John activity, or "quiet today".
                Yarden chose a daily heartbeat so a silent stall (the failure that killed the old
                local scheduler) is impossible to miss.

NEVER silently no-ops: ingest failure, IMAP failure, or stale data print a LOUD flag in the digest.
The teaching lines are POINTERS - an agent still does the qualitative read-in on demand (cpt-doctrine.md).

First run seeds john-seen.json from the current lookback WITHOUT reporting the backlog (so day one
isn't a wall of 3 weeks of history), then reports net-new daily from there.

env (GitHub Secrets): GMAIL_ADDRESS, GMAIL_APP_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import os, re, json, html, urllib.request, urllib.parse, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
import cpt_john_ingest as ing

SEEN = os.path.join(HERE, "john-seen.json")

try:
    UNIVERSE = set(ing.UNIVERSE)
except Exception:
    UNIVERSE = set()

_ADMIN = ("DASHBOARD HAS BEEN UPDATED", "HAS BEEN UPDATED", "MEMBERSHIP LINKS", "MEMBERSHIP LINK",
          "PASSWORD", "UNSUBSCRIBE")


def classify(subject):
    """Bucket one John subject: 'open' | 'close' | 'roll' | 'teaching' | 'admin'.
    Trades (open/close/roll) are already modelled by the alert engine; teaching is the doctrine
    signal; admin is plumbing. Everything is reported so nothing is missed."""
    up = (subject or "").upper()
    if not up.strip():
        return "admin"
    if "ROLLED" in up:
        return "roll"
    if "CLOSED" in up or "CLOSE OUT" in up or "CLOSEOUT" in up:
        return "close"
    if any(k in up for k in ("NEW OPEN", "OPEN UCPTD", "OPEN PMCC", "OPEN OTM", "FIRST TRADE",
                             "DOUBLE DOWN", "TRADE IS LIVE", "TRADE IS  LIVE", "TRADE IS 1/2",
                             "TRADE 1/2", "TRADES HAVE BEEN POSTED", "TRADES | ")):
        return "open"
    if any(k in up for k in _ADMIN):
        return "admin"
    return "teaching"


def _ticker(subject):
    seg = subject.split("|", 1)[1] if "|" in subject else subject
    for tok in re.findall(r"\b([A-Z]{2,6})\b", seg):
        if tok in UNIVERSE:
            return tok
    return None


def _clean(subject):
    s = (subject or "").strip()
    for pre in ("🚨 UCPTD | ", "🚨 UCPTD ", "UCPTD | ", "Fwd: 🚨 UCPTD | ", "Fwd: "):
        if s.startswith(pre):
            s = s[len(pre):]
    return s.strip()


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
        print("[daily] no telegram token/chat - digest not sent (printed below)\n" + text)
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


def _fmt_trades(items, label):
    """One compact line per trade bucket, e.g. 'opens: 3 - DPST, TQQQ, LABU'."""
    if not items:
        return None
    tks = [t for t in (_ticker(s) for (s, d) in items) if t]
    tail = " - " + ", ".join(dict.fromkeys(tks)) if tks else ""
    return f"{label}: <b>{len(items)}</b>{tail}"


def main():
    today = dt.date.today()
    parts = [f"\U0001F4D3 <b>CPT daily John-watch</b> {today.isoformat()}"]

    # 1) INGEST new trade opens into john-alerts.json (loud on failure) -------------------------
    ingest_line, subjects = "", []
    try:
        added = ing.run()
        subjects = ing._fetch_john_subjects()          # (subject, date) over the 21d lookback
        ingest_line = ("ledger: +%d new open(s) captured" % len(added)) if added else \
                      "ledger: current"
    except Exception as e:
        parts.append(f"⚠️ INGEST/IMAP FAILED: {str(e)[:120]}")
        parts.append("John data may be STALE - check the workflow run.")
        tg("\n".join(parts))
        print("[daily] INGEST FAILED:", e)
        return

    # 2) FULL SWEEP - everything John sent, net-new since last run ------------------------------
    seen, meta = _load_seen()
    new = [(s, d) for (s, d) in subjects if f"{d}|{s}" not in seen]

    if meta.get("first_seeded") is None:
        # seed the whole lookback silently - don't wall the user with 3 weeks of history on day one
        for (s, d) in subjects:
            seen.add(f"{d}|{s}")
        meta["first_seeded"] = today.isoformat()
        _save_seen(seen, meta)
        parts.append(ingest_line)
        parts.append("full-sweep armed: seeded <b>%d</b> recent John email(s); "
                     "now reporting everything new, daily." % len(subjects))
        tg("\n".join(parts))
        print("[daily] seeded", len(subjects), "John emails")
        return

    for (s, d) in new:
        seen.add(f"{d}|{s}")
    _save_seen(seen, meta)

    # bucket the net-new
    buckets = {"open": [], "close": [], "roll": [], "teaching": [], "admin": []}
    for (s, d) in new:
        buckets[classify(s)].append((s, d))

    parts.append(ingest_line)

    trade_lines = [ln for ln in (
        _fmt_trades(buckets["open"], "opens"),
        _fmt_trades(buckets["close"], "closes"),
        _fmt_trades(buckets["roll"], "rolls"),
    ) if ln]
    if trade_lines:
        parts.append("🟢 <b>trades</b>")
        parts += ["  " + ln for ln in trade_lines]

    teach = sorted(buckets["teaching"], key=lambda r: r[1], reverse=True)
    if teach:
        parts.append("📚 <b>teaching / notes: %d</b> - read these in:" % len(teach))
        for (s, d) in teach[:10]:
            parts.append(f"• {html.escape(_clean(s))}")
        if len(teach) > 10:
            parts.append(f"… +{len(teach) - 10} more")

    if buckets["admin"]:
        parts.append("⚙️ admin: <b>%d</b>" % len(buckets["admin"]))

    if not new:
        parts.append("\n<i>John quiet today ✓ (watcher alive)</i>")

    sent = tg("\n".join(parts))
    print("[daily] digest sent:", sent, "| net-new:", len(new),
          "| teaching:", len(buckets["teaching"]))


if __name__ == "__main__":
    if not os.environ.get("GMAIL_APP_PASSWORD"):
        print("No GMAIL_APP_PASSWORD set - this is the live daily watcher; set the env vars (or run")
        print("it from the GitHub Actions workflow) to read John's inbox. Nothing to do locally.")
    else:
        main()
