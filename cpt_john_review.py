#!/usr/bin/env python3
"""
CPT WEEKLY JOHN-REVIEW - the headless weekly self-critique, run by GitHub Actions (no MCP connector).

Chains the pieces that DON'T need Claude reasoning, all pure stdlib + free Yahoo data:
  1) INGEST     cpt_john_ingest.run()  - IMAP-pull John's new opens into john-alerts.json.
  2) SELF-CRITIQUE  cpt_vs_john.py --john - our INDEPENDENT gate as-of each John entry: agreement %
                    and any NEW misses (the divergences worth an agent's attention).
  3) SCORECARD  cpt_paper.digest_text() - the paper ledger vs John, phone-formatted.
  4) DIGEST     post a compact HTML digest to Telegram, and flag if the data is STALE or ingest FAILED.

NEVER silently no-ops: if ingest fails or the dataset is stale, the digest says so loudly, so a
silent stall (the reason we moved off the local scheduler) can't recur. The narrative logs
(john-vs-system-log.md / john-thinking-log.md) still need an agent's qualitative read - the digest
flags when there is something new to look at.

env (GitHub Secrets): GMAIL_ADDRESS, GMAIL_APP_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import os, sys, json, subprocess, urllib.request, urllib.parse, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
import cpt_john_ingest
import cpt_paper


def tg(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[telegram] no token/chat set - digest not sent (printed below)\n" + text)
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception as e:
        print("[telegram] send failed:", str(e)[:150])
        return False


def _newest_date():
    try:
        with open(os.path.join(HERE, "john-alerts.json")) as f:
            ds = [e["date"] for e in json.load(f)["entries"]]
        return max(ds) if ds else None
    except Exception:
        return None


def main():
    today = dt.date.today()

    # 1) INGEST (loud on failure - never silent)
    ingest_line = ""
    try:
        added = cpt_john_ingest.run()
        if added:
            ingest_line = f"ingested <b>{len(added)}</b> new John opens: " + ", ".join(
                f"{e['ticker']}" for e in added)
        else:
            ingest_line = "no new John opens (already current)"
        print("[review]", ingest_line)
    except Exception as e:
        added = []
        ingest_line = f"⚠️ INGEST FAILED: {str(e)[:120]} - John data may be STALE"
        print("[review] INGEST FAILED:", e)

    # staleness watchdog (independent of ingest success)
    newest = _newest_date()
    stale_line = ""
    if newest:
        age = (today - dt.date.fromisoformat(newest)).days
        if age > 8:
            stale_line = f"⚠️ John data STALE: newest entry {newest} ({age}d old) - check ingest"

    # 2) SELF-CRITIQUE
    agree_line, miss_line = "self-critique: n/a", ""
    try:
        out = subprocess.run([sys.executable, os.path.join(HERE, "cpt_vs_john.py"), "--john"],
                             capture_output=True, text=True, timeout=900, cwd=HERE)
        print(out.stdout[-2000:])
        for ln in out.stdout.splitlines():
            if "flagged" in ln and "%" in ln:
                agree_line = ln.strip()
        misses = [ln for ln in out.stdout.splitlines() if "MISS" in ln]
        if misses:
            miss_line = f"{len(misses)} miss(es) vs John - review the log"
    except Exception as e:
        agree_line = f"self-critique failed: {str(e)[:100]}"

    # 3+4) build + send digest
    parts = [f"\U0001F4CA <b>CPT weekly John-review</b> {today.isoformat()}",
             ingest_line]
    if stale_line:
        parts.append(stale_line)
    parts += [agree_line]
    if miss_line:
        parts.append(miss_line)
    parts += ["", cpt_paper.digest_text()]
    sent = tg("\n".join(p for p in parts if p is not None))
    print("[review] digest sent:", sent)


if __name__ == "__main__":
    main()
