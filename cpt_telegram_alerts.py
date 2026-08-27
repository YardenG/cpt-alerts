#!/usr/bin/env python3
"""
Real-time JohnG entry alerts over TELEGRAM.

Scans the leveraged-ETF universe for John's entry setup (the SAME calibrated gate as
cpt_data_spike.py and the TradingView indicator) on near-live data and sends a Telegram
message for each FRESH entry - fire-once, so it will not re-alert a name until that name
leaves John's gate and later comes back in.

John's entry gate (calibrated to his 95 real CPT Dashboard alerts):
  ENTRY  = pos <= 60 AND RSI < 70        (~92% of his real entries)
  ENTRY* = pos 38-55 AND RSI 42-58       (his densest cluster - flagged green)

Data: Yahoo (free, near-live). No IBKR/Gateway needed for the ENTRY alert. (The exact option
legs - 99-delta call + expected-move CSP strike - come from the IBKR connector in a later step;
this alert tells you WHEN John's setup appears.)

ONE-TIME SETUP (2 minutes):
  1. In Telegram, open a chat with @BotFather -> send /newbot -> pick a name -> copy the TOKEN.
  2. Open a chat with your new bot and send it any message (e.g. "hi") so it may message you.
  3. Get your chat id: visit  https://api.telegram.org/bot<TOKEN>/getUpdates  in a browser and
     find  "chat":{"id":<NUMBER> ...}  - that <NUMBER> is your chat_id.
  4. Copy telegram_config.example.json -> telegram_config.json and paste in your token + chat id.

Each alert now carries: the entry read, the SUGGESTED structure (CSP / 99-delta ITM CCW /
long-dated CSP - John's up/down-day + structure rules), a chart image (price + Keltner bands,
rendered via a chart-image URL so the cloud run stays pure-stdlib), a TradingView link, the
exact-legs command for the desk, and a SITUATIONAL AWARENESS line (market regime + fragility +
sector trend + earnings proximity, from cpt_context) - advise-only, computed once per scan.

RUN:
  python3 cpt_telegram_alerts.py --test          # send a plain test message (prove the pipe)
  python3 cpt_telegram_alerts.py --preview TQQQ   # send ONE full enriched alert for TQQQ to your
                                                  #   phone NOW (chart+strategy+links), bypassing
                                                  #   the fire-once gate - see it before deploying
  python3 cpt_telegram_alerts.py                  # one live scan; alert fresh entries (mkt hours)
  python3 cpt_telegram_alerts.py --force          # scan even when the US market is closed (testing)
Then schedule it (next step) - e.g. cron every 15-20 min during market hours.
"""
import json, os, sys, urllib.request, urllib.parse, datetime as dt
from cpt_data_spike import analyze, strategy_pick, series, UNIVERSE
try:
    import cpt_legs_web                    # cloud exact-legs from free options data (best-effort)
except Exception:
    cpt_legs_web = None
try:
    import cpt_context                     # SITUATIONAL AWARENESS layer (regime/sector/earnings; best-effort)
except Exception:
    cpt_context = None
try:
    import cpt_paper                       # PAPER-TRADE LEDGER: auto-capture + daily mark + weekly digest (best-effort)
except Exception:
    cpt_paper = None

HERE   = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "telegram_config.json")
STATE  = os.path.join(HERE, ".telegram_alert_state.json")

def load_config():
    # Cloud (GitHub Actions) passes secrets as env vars; local uses telegram_config.json.
    env_tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    env_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if env_tok and env_chat:
        return {"bot_token": env_tok, "chat_id": env_chat}
    if not os.path.exists(CONFIG):
        sys.exit("Missing telegram_config.json - copy telegram_config.example.json and fill it in "
                 "(or set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars).")
    with open(CONFIG) as f:
        c = json.load(f)
    if "PASTE" in str(c.get("bot_token", "")) or "PASTE" in str(c.get("chat_id", "")):
        sys.exit("Fill in your real bot_token + chat_id in telegram_config.json first.")
    return c

def send(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                   "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            return json.load(r).get("ok", False)
    except Exception as e:
        print("telegram send failed:", str(e)[:120]); return False

def chart_config(sym, s, subtitle):
    """Chart.js config: daily CANDLESTICKS + the three Keltner bands overlaid, last ~40 bars."""
    def line(label, data, color, dash=None):
        d = {"type": "line", "label": label, "data": data, "borderColor": color,
             "backgroundColor": "rgba(0,0,0,0)", "pointRadius": 0, "borderWidth": 1, "fill": False}
        if dash:
            d["borderDash"] = dash
        return d
    return {"type": "candlestick",
            "data": {"datasets": [
                {"label": sym, "data": s["candles"]},
                line("KC upper", s["upper"], "rgb(192,57,43)", [5, 4]),
                line("KC mid", s["mid"], "rgb(41,128,185)"),
                line("KC lower", s["lower"], "rgb(39,174,96)", [5, 4])]},
            # Chart.js v3/v4 syntax (title/legend under plugins; x/y scales). QuickChart's default
            # v2 does NOT render candlesticks (returns a 400 error-image), so chart_url pins v4.
            "options": {"plugins": {"title": {"display": True,
                                              "text": [f"{sym}  Keltner(10,5,ema)", subtitle]},
                                    "legend": {"display": True, "position": "bottom"}},
                        "scales": {"x": {"type": "time", "time": {"unit": "day"}}, "y": {}}}}

def chart_url(sym, s, subtitle):
    """Render via quickchart.io's POST endpoint -> a SHORT hosted image URL Telegram fetches (no
    local plotting dep, and no query-string length limit from the bulkier candlestick payload).
    version=4 is REQUIRED: candlesticks are unsupported on QuickChart's default Chart.js v2."""
    body = json.dumps({"chart": chart_config(sym, s, subtitle),
                       "width": 640, "height": 400, "backgroundColor": "white",
                       "version": "4"}).encode()
    req = urllib.request.Request("https://quickchart.io/chart/create", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["url"]

def send_photo(token, chat_id, photo_url, caption):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = urllib.parse.urlencode({"chat_id": chat_id, "photo": photo_url,
                                   "caption": caption, "parse_mode": "HTML"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=25) as r:
            return json.load(r).get("ok", False)
    except Exception as e:
        print("telegram sendPhoto failed:", str(e)[:160]); return False

def build_caption(a):
    """The enriched alert text: entry read + day + SUGGESTED structure + why + EXACT LEGS (from
    free options data so they reach the phone on the go) + TV link. Legs are best-effort: if the
    cloud can't fetch options, fall back to the desk command so the alert still fires complete."""
    # Header/brain reflect the TRUE verdict. A live scan only ever alerts in-gate names, but
    # --preview can send an out-of-gate name to eyeball - so don't mislabel it "ENTRY".
    if a["strong"]:
        tag, emoji = "ENTRY *", "\U0001F7E2"                       # green
        brain = "\U0001F9E0 Below the top, in his PRIME cluster, not overbought = John's entry setup."
    elif a["valid"]:
        tag, emoji = "ENTRY", "\U0001F535"                         # blue
        brain = "\U0001F9E0 Below the top, at/around the EMA mid, not overbought = John's entry setup."
    else:
        tag, emoji = f"{a['verdict'].upper()} (not in gate)", "⚪"   # preview-only path
        brain = (f"\U0001F9E0 pos {a['pos']:.0f}% / RSI {a['rsi']:.0f} is OUTSIDE John's gate "
                 f"(pos<=60 & RSI<70) - preview only, not a live entry signal.")
    daytxt = f" ({a['day']} day {a['chg']:+.2f})" if a.get("day") else ""
    rec_label, rec_why = strategy_pick(a)
    tv = f"https://www.tradingview.com/chart/?symbol={a['t']}"

    # Sections separated by a blank line (join with \n\n); emojis flag each section for scanning.
    parts = [f"{emoji} <b>JohnG {tag}</b> - {a['t']} @ {a['price']:.2f}{daytxt}\n"
             f"pos {a['pos']:.0f}% of the Keltner range | RSI {a['rsi']:.0f}",
             brain,
             f"\U0001F4A1 <b>Suggested:</b> {rec_label}\n{rec_why}"]

    legs_txt = None
    if cpt_legs_web is not None:
        try:
            legs_txt = cpt_legs_web.format_legs(a["t"], cpt_legs_web.legs(a["t"]), structure=rec_label)
        except Exception as e:
            print("cloud legs failed (sending desk pointer):", str(e)[:120])
    if legs_txt:
        parts.append(legs_txt.strip())
        parts.append(f"Exact live at desk: <code>python3 cpt_legs.py {a['t']}</code>")
    else:
        parts.append(f"\U0001F4CA <b>Exact legs</b> (desk, Gateway up): "
                     f"<code>python3 cpt_legs.py {a['t']}</code>")
    parts.append(f"\U0001F4CA <b>Live chart:</b> {tv}")
    return "\n\n".join(parts)

def shared_regime():
    """Compute the market regime ONCE (SPY/QQQ/VIX) to share across every alert in a scan. Best-effort."""
    if cpt_context is None:
        return None
    try:
        return cpt_context.regime()
    except Exception as e:
        print("regime failed:", str(e)[:120])
        return None

def situational_line(a, reg):
    """Compact one-line situational-awareness read for the caption (tape/fragility/sector/earnings),
    computed from a SHARED regime so a scan pays for SPY/QQQ/VIX once. Best-effort -> None on failure."""
    if cpt_context is None:
        return None
    try:
        c = cpt_context.context(a["t"], reg=reg, a=a)
        return f"\U0001F30D <b>Context:</b> {cpt_context.read_line(c)}"
    except Exception as e:
        print("situational read failed:", str(e)[:120])
        return None

def send_alert(token, chat, a, reg=None):
    """Send the full enriched alert: chart image + caption (with the situational line inlined when it
    fits Telegram's 1024-char caption cap; otherwise as a short follow-up). Falls back to text if the
    chart fails."""
    caption = build_caption(a)
    ctx = situational_line(a, reg)
    full = caption + (f"\n\n{ctx}" if ctx else "")
    inline = bool(ctx) and len(full) <= 1000
    to_send = full if inline else caption
    sent = False
    try:
        s = series(a["t"])
        url = chart_url(a["t"], s, f"pos {a['pos']:.0f}% | RSI {a['rsi']:.0f} | {a['verdict']}")
        sent = send_photo(token, chat, url, to_send)
    except Exception as e:
        print("chart build failed, sending text:", str(e)[:120])
    if not sent:
        return send(token, chat, full if ctx else caption)
    if ctx and not inline:
        send(token, chat, ctx)          # caption was full; send context as its own short message
    return True

def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return None                      # None = first run (seed silently, do not flood)

def save_state(s):
    with open(STATE, "w") as f:
        json.dump(s, f)

def market_open_now():
    """US regular hours, approx. EDT (summer) = 9:30-16:00 ET = 13:30-20:00 UTC, Mon-Fri."""
    now = dt.datetime.utcnow()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 13 * 60 + 30 <= mins <= 20 * 60

def main():
    cfg = load_config()
    token, chat = cfg["bot_token"], cfg["chat_id"]

    if "--test" in sys.argv:
        ok = send(token, chat, "✅ <b>CPT alert bot connected.</b> You'll get JohnG entry alerts here.")
        print("test message sent:", ok); return

    if "--preview" in sys.argv:
        args = [x for x in sys.argv[1:] if not x.startswith("-")]
        tk = (args[0] if args else "TQQQ").upper()
        a = analyze(tk)
        reg = shared_regime()
        ok = send_alert(token, chat, a, reg=reg)
        print(f"preview enriched alert for {tk} sent:", ok)
        if cpt_paper is not None:            # also paper-capture: cloud self-test for the ledger (best-effort)
            try:
                if not cpt_paper.auto_open(a, reg=reg):
                    print(f"[paper] {tk} not captured (already open, or not a live ENTRY gate).")
            except Exception as e:
                print("paper preview-capture skipped:", str(e)[:120])
        return

    if not market_open_now() and "--force" not in sys.argv:
        print(f"{dt.datetime.now():%H:%M} US market closed - no scan (use --force to override).")
        return

    prev = load_state()
    first_run = prev is None
    state = {} if first_run else dict(prev)
    fresh = []
    for tk in UNIVERSE:
        try:
            a = analyze(tk)
        except Exception:
            continue
        in_gate = a["valid"]                      # John's ENTRY gate (pos<=60 & RSI<70)
        if not first_run and in_gate and not state.get(tk, False):
            fresh.append(a)                       # crossed INTO the gate -> fresh entry
        state[tk] = in_gate
    save_state(state)

    if first_run:
        print(f"Baseline set for {len(state)} names. Fresh entries will be alerted from the next scan.")
        return

    reg = shared_regime() if fresh else None      # compute the regime ONCE, only if we have alerts to send
    for a in sorted(fresh, key=lambda x: (not x["strong"], x["t"])):
        send_alert(token, chat, a, reg=reg)
        if cpt_paper is not None:             # auto-capture the fired alert into the paper ledger (best-effort)
            try:
                cpt_paper.auto_open(a, reg=reg)
            except Exception as e:
                print("paper auto-open skipped:", str(e)[:120])
    print(f"scan {dt.datetime.now():%H:%M}: {len(fresh)} fresh alert(s) sent.")

    paper_maintenance(token, chat)            # once/day mark + Friday digest (self-gated, best-effort)


def paper_maintenance(token, chat):
    """Run the paper ledger's daily upkeep from inside the live scan (no extra cron needed):
      - MARK every open position ONCE per day, near the close (19:45-20:00 UTC, in-session so the
        option quotes are live), guarded by `last_marked` in the ledger meta.
      - Send the WEEKLY scorecard digest to Telegram on Friday near close, guarded by `last_digest_week`.
    All best-effort: any failure here must never touch the proven-live alert path."""
    if cpt_paper is None:
        return
    now = dt.datetime.utcnow()
    mins = now.hour * 60 + now.minute
    if not (market_open_now() and mins >= 18 * 60):   # last ~2h in-session (18:00-20:00 UTC): live quotes, robust to cron jitter dropping the exact close slot; last_marked keeps it once/day
        return
    today = dt.date.today().isoformat()
    try:
        if cpt_paper.meta_get("last_marked") != today:
            cpt_paper.mark()
            cpt_paper.meta_set("last_marked", today)
    except Exception as e:
        print("paper daily mark skipped:", str(e)[:120])
    try:
        if now.weekday() == 4:                              # Friday
            wk = now.strftime("%G-W%V")
            if cpt_paper.meta_get("last_digest_week") != wk:
                if send(token, chat, cpt_paper.digest_text()):
                    cpt_paper.meta_set("last_digest_week", wk)
    except Exception as e:
        print("paper weekly digest skipped:", str(e)[:120])

if __name__ == "__main__":
    main()
