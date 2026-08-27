#!/usr/bin/env python3
"""
CPT John INGEST - pull John's new entry alerts from Gmail over IMAP and append them to
john-alerts.json. This is the HEADLESS replacement for the old Gmail-connector step: it runs in
GitHub Actions (or anywhere) using a Google APP PASSWORD, so no MCP connector and no app-open
dependency. Pure stdlib (imaplib + email + zoneinfo).

Auth (from env, set as GitHub Secrets - NEVER hard-coded):
  GMAIL_ADDRESS        the mailbox to read (yardenoon@gmail.com)
  GMAIL_APP_PASSWORD   a 16-char Google App Password (requires 2-Step Verification)

READ-ONLY: opens the mailbox with readonly=True and only ever SEARCHes/FETCHes headers. It never
sends, deletes, labels, or marks-read anything. It parses John's structured subject lines (the same
templated alerts the dataset was seeded from) into OPEN entries and appends the net-new ones,
de-duped by (date, ticker, structure). Closes / rolls / videos / notes are skipped.

Run standalone:  python3 cpt_john_ingest.py         (dry-run prints what it WOULD add, unless creds set)
Imported:        cpt_john_ingest.run() -> list[dict] of appended entries
"""
import imaplib, email, os, re, json, sys
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")          # John trades US hours; date the entry by ET calendar day
except Exception:
    ET = None

LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "john-alerts.json")
SENDERS = ("from:(john@cptdashboard.com OR john@ucptdashboard.com OR "
           "john@corepositiontrading.com)")
LOOKBACK_DAYS = 21                              # search window; de-dup handles the overlap

# Wheel universe = the only valid tickers (guards against parsing a stray word as a ticker).
try:
    import cpt_data_spike as ds
    UNIVERSE = set(ds.UNIVERSE)
except Exception:
    UNIVERSE = {"DPST", "TQQQ", "TNA", "IREN", "NVDL", "LABU", "NAIL", "GGLL",
                "METU", "SOXL", "AAPU", "AMZU"}

_SKIP = ("CLOSE", "CLOSED", "ROLLED", "MEMBERS VIDEO", "VIDEO LINK", "DASHBOARD HAS BEEN UPDATED",
         "IN FOCUS", "TRADER LESSON", "MEMBERS QUESTION", "UNDERSTAND YOUR", "STILL A GOOD TRADE",
         "NO BRAINER", "SEE I TOLD YOU", "CHART UPDATE")


def _subject(msg):
    try:
        return str(make_header(decode_header(msg.get("Subject", "") or "")))
    except Exception:
        return msg.get("Subject", "") or ""


def _date_et(msg):
    try:
        d = parsedate_to_datetime(msg.get("Date"))
        if ET and d.tzinfo:
            d = d.astimezone(ET)
        return d.date().isoformat()
    except Exception:
        return None


def parse_open(subject, date):
    """Turn one John subject line into an OPEN entry dict, or None if it is not a fresh open.
    Handles the formal 'NEW OPEN ... CCW' / 'OPEN PMCC' alerts and the 'THIS <TKR> TRADE IS LIVE'
    heads-ups. Ticker = first universe symbol after the first '|' (or from the LIVE phrase)."""
    up = subject.upper()
    if any(k in up for k in _SKIP):
        return None

    ticker = structure = None
    is_ccw = ("NEW OPEN" in up and "CCW" in up)
    is_pmcc = ("OPEN" in up and "PMCC" in up)
    live = re.search(r"THIS\s+([A-Z]{2,6})\s+TRADE IS\s+(?:1/2\s+)?LIVE", up)

    if is_ccw or is_pmcc:
        after = subject.split("|", 1)[1] if "|" in subject else subject
        for tok in re.findall(r"\b([A-Z]{2,6})\b", after):
            if tok in UNIVERSE:
                ticker = tok
                break
        structure = "OPEN PMCC" if is_pmcc else "OPEN CCW"
    elif live and live.group(1) in UNIVERSE:
        ticker, structure = live.group(1), "OPEN CCW"

    if not ticker or ticker not in UNIVERSE or not date:
        return None
    entry = {"date": date, "ticker": ticker, "structure": structure}
    m = re.search(r"CSP Prem ~?\$?([0-9]+\.?[0-9]*)", subject)
    if m:
        entry["csp_prem"] = float(m.group(1))
    return entry


def _fetch_john_subjects():
    """IMAP-connect read-only, Gmail-search John's senders over the lookback window, return
    [(subject, date_iso), ...]. Raises on missing creds or connect/login failure (caller reports)."""
    addr, pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
    if not addr or not pw:
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set (add them as GitHub Secrets)")
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        M.login(addr, pw)
        M.select('"[Gmail]/All Mail"', readonly=True)          # read-only; covers archived mail too
        query = f'{SENDERS} newer_than:{LOOKBACK_DAYS}d'
        typ, data = M.uid('SEARCH', 'X-GM-RAW', '"%s"' % query)
        if typ != "OK" or not data or not data[0]:
            return []
        out = []
        for uid in data[0].split():
            typ, md = M.uid('FETCH', uid, '(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE FROM)])')
            if typ != "OK" or not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            out.append((_subject(msg), _date_et(msg)))
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _load():
    with open(LEDGER) as f:
        return json.load(f)


def run():
    """Ingest John's new opens into john-alerts.json. Returns the list of appended entries."""
    book = _load()
    seen = {(e["date"], e["ticker"], e["structure"]) for e in book["entries"]}
    added = []
    for subject, date in _fetch_john_subjects():
        e = parse_open(subject, date)
        if not e:
            continue
        k = (e["date"], e["ticker"], e["structure"])
        if k in seen:
            continue
        seen.add(k)
        book["entries"].append(e)
        added.append(e)
    if added:
        book["entries"].sort(key=lambda e: (e["date"], e["ticker"]), reverse=True)
        with open(LEDGER, "w") as f:
            json.dump(book, f, indent=2)
    return added


def main():
    if not os.environ.get("GMAIL_APP_PASSWORD"):
        print("No GMAIL_APP_PASSWORD set - this is the live ingest tool; set the env vars (or run it")
        print("from the GitHub Actions workflow) to pull John's real alerts. Nothing to do locally.")
        return
    added = run()
    if added:
        print(f"appended {len(added)} new John open(s):")
        for e in added:
            print(f"  {e['date']}  {e['ticker']:6} {e['structure']}"
                  + (f"  CSP ~${e['csp_prem']}" if "csp_prem" in e else ""))
    else:
        print("no new John opens to add (already current).")


if __name__ == "__main__":
    main()
