#!/usr/bin/env python3
"""
CPT JOHN TEACHING-CAPTURE - the headless body-grabber the daily watch was missing.

Until now the daily John-watch (cpt_john_daily.py) classified John's teaching / commentary emails
by SUBJECT only and printed them as pointer lines; the actual BODIES were thrown away, so the
qualitative lesson (99-Delta roll math, pullback classification, regime reads) had to be re-pulled
by hand every time. This module captures those bodies headlessly.

DESIGN (deliberately no LLM in the cloud):
  - It only CAPTURES the raw body into a holding-pen file (john-teaching-inbox.md), newest-first,
    de-duped by john-teaching-seen.json. It does NOT distill and does NOT touch the curated
    john-thinking-log.md. Distilling (dedupe vs prior captures, catching a correction, calling
    saturation) is judgment = an agent's desk job reading from the inbox. The cloud's only promise
    is "the body is never lost."
  - Wired into cpt_john_daily.py: after the daily sweep buckets the net-new teaching subjects, it
    hands that list here to fetch + append the bodies. NON-FATAL: any failure is swallowed with a
    flag so the daily heartbeat/digest still goes out.

Pure stdlib (imaplib + email), same read-only App-Password IMAP pattern as cpt_john_ingest.py.
Reuses cpt_john_ingest._subject / _date_et for header parsing.

env (GitHub Secrets): GMAIL_ADDRESS, GMAIL_APP_PASSWORD
"""
import imaplib, email, os, re, json
import cpt_john_ingest as ing

HERE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(HERE, "john-teaching-inbox.md")
SEEN = os.path.join(HERE, "john-teaching-seen.json")

_MARK = "<!-- entries below, newest first -->"
_HEADER = (
    "# John teaching inbox (raw auto-capture)\n\n"
    "Raw bodies of John's teaching / commentary emails, captured headless by `cpt_john_teaching.py`\n"
    "so nothing is lost. NOT curated: Adam distills the net-new into `john-thinking-log.md` at the\n"
    "desk. Most recently captured first (within a run, newest email first).\n\n"
    + _MARK + "\n"
)

# Footer markers - cut the body at the FIRST of these (John's sign-off / legal boilerplate).
_CUT = (
    "As always, do your due diligence",
    "Until next time",
    "\nRegards,\nJohn",
    "DISCLAIMER - The numbers in the video",
    "You received this message because you are",
    "To unsubscribe from this group",
)


def _strip_prefix(s):
    s = (s or "").strip()
    for pre in ("\U0001F6A8 UCPTD | ", "\U0001F6A8 UCPTD ", "UCPTD | ", "\U0001F6A8 UCPTD MEMBERS ",
                "Fwd: \U0001F6A8 UCPTD | ", "Fwd: "):
        if s.startswith(pre):
            s = s[len(pre):]
    return s.strip()


def _body_text(msg):
    """Best-effort plain-text body from a MIME message: prefer text/plain, else strip text/html."""
    plain, htmltext = None, None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if ctype == "text/plain" and plain is None:
                plain = text
            elif ctype == "text/html" and htmltext is None:
                htmltext = text
    else:
        try:
            text = (msg.get_payload(decode=True) or b"").decode(
                msg.get_content_charset() or "utf-8", "replace")
        except Exception:
            text = ""
        if msg.get_content_type() == "text/html":
            htmltext = text
        else:
            plain = text
    raw = plain if plain is not None else (_html_to_text(htmltext) if htmltext else "")
    return _clean_body(raw)


def _html_to_text(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</p>", "\n\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    return re.sub(r"[ \t]+\n", "\n", h)


def _clean_body(text):
    """Trim John's greeting + footer/legal, drop image placeholders, collapse blank runs."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # cut at the earliest footer marker present
    cut = len(text)
    for m in _CUT:
        i = text.find(m)
        if i != -1:
            cut = min(cut, i)
    body = text[:cut]
    # drop leading "Hi Everyone!" and stray separator dashes at the very top
    body = re.sub(r"^\s*Hi Everyone!\s*\n+", "", body)
    body = re.sub(r"^\s*-\s*\n+", "", body)
    body = body.replace("[image: image.png]", "")
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


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


def _fetch_bodies(wanted):
    """wanted = set of 'date|subject' keys. Returns {key: (sender, clean_body)} for those found.
    One read-only IMAP session over the same lookback the daily sweep uses."""
    addr, pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
    if not addr or not pw:
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
    got = {}
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        M.login(addr, pw)
        M.select('"[Gmail]/All Mail"', readonly=True)
        query = f'{ing.SENDERS} newer_than:{ing.LOOKBACK_DAYS}d'
        typ, data = M.uid('SEARCH', 'X-GM-RAW', '"%s"' % query)
        if typ != "OK" or not data or not data[0]:
            return got
        for uid in data[0].split():
            typ, md = M.uid('FETCH', uid, '(BODY.PEEK[])')   # PEEK = never marks read
            if typ != "OK" or not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            key = f"{ing._date_et(msg)}|{ing._subject(msg)}"
            if key in wanted and key not in got:
                got[key] = (str(msg.get("From", "") or ""), _body_text(msg))
        return got
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _sender_addr(frm):
    m = re.search(r"[\w.+-]+@[\w.-]+", frm or "")
    return m.group(0) if m else (frm or "").strip()


def _append(entries, today):
    """entries = [(date, subject, sender, body), ...] newest-first. Prepend to the inbox file."""
    old = ""
    if os.path.exists(INBOX):
        try:
            with open(INBOX, encoding="utf-8") as f:
                cur = f.read()
            old = cur.split(_MARK, 1)[1] if _MARK in cur else "\n" + cur
        except Exception:
            old = ""
    blocks = []
    for (date, subject, sender, body) in entries:
        blocks.append(
            f"\n## {date} | {_strip_prefix(subject)}\n"
            f"_{_sender_addr(sender)} - captured {today}_\n\n"
            f"{body}\n\n---\n"
        )
    with open(INBOX, "w", encoding="utf-8") as f:
        f.write(_HEADER + "".join(blocks) + old)


def capture_bodies(teaching, today):
    """teaching = list of (subject, date) net-new teaching emails from the daily sweep.
    Fetch their bodies and prepend net-new ones to the inbox. Idempotent via john-teaching-seen.json.
    Returns the list of (date, subject) actually captured this run."""
    teaching = [(s, d) for (s, d) in (teaching or []) if d]
    if not teaching:
        return []
    seen, meta = _load_seen()
    todo = {f"{d}|{s}": (s, d) for (s, d) in teaching if f"{d}|{s}" not in seen}
    if not todo:
        return []
    bodies = _fetch_bodies(set(todo))
    entries, captured = [], []
    # newest first
    for key in sorted(todo, key=lambda k: k.split("|", 1)[0], reverse=True):
        s, d = todo[key]
        sender, body = bodies.get(key, ("", ""))
        if not body:            # body not found/empty this run - leave UNSEEN so we retry tomorrow
            continue
        entries.append((d, s, sender, body))
        captured.append((d, s))
        seen.add(key)
    if entries:
        _append(entries, today)
        if meta.get("first_seeded") is None:
            meta["first_seeded"] = today
        _save_seen(seen, meta)
    return captured


if __name__ == "__main__":
    if not os.environ.get("GMAIL_APP_PASSWORD"):
        print("No GMAIL_APP_PASSWORD set - this module is called by cpt_john_daily.py in the cloud.")
    else:
        import datetime as _dt
        subs = ing._fetch_john_subjects()
        import cpt_john_daily as daily
        teach = [(s, d) for (s, d) in subs if daily.classify(s) == "teaching"]
        got = capture_bodies(teach, _dt.date.today().isoformat())
        print("captured", len(got), "teaching bodies:", [f"{d} {s[:40]}" for (d, s) in got])
