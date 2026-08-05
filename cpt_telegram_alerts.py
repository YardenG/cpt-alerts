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

RUN:
  python3 cpt_telegram_alerts.py --test     # send a test message and exit (prove the pipe)
  python3 cpt_telegram_alerts.py            # one live scan; alert fresh entries (market hours)
  python3 cpt_telegram_alerts.py --force    # scan even when the US market is closed (testing)
Then schedule it (next step) - e.g. cron every 15-20 min during market hours.
"""
import json, os, sys, urllib.request, urllib.parse, datetime as dt
from cpt_data_spike import analyze, UNIVERSE

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

    for a in sorted(fresh, key=lambda x: (not x["strong"], x["t"])):
        tag   = "ENTRY *" if a["strong"] else "ENTRY"
        emoji = "\U0001F7E2" if a["strong"] else "\U0001F535"   # green / blue
        where = "in his PRIME cluster" if a["strong"] else "at/around the EMA mid"
        msg = (f"{emoji} <b>JohnG {tag}</b> - {a['t']} @ {a['price']:.2f}\n"
               f"pos {a['pos']:.0f}% of the Keltner range | RSI {a['rsi']:.0f}\n"
               f"Below the top, {where}, not overbought = John's entry setup.")
        send(token, chat, msg)
    print(f"scan {dt.datetime.now():%H:%M}: {len(fresh)} fresh alert(s) sent.")

if __name__ == "__main__":
    main()
