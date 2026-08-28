#!/usr/bin/env python3
"""
ACCOUNT LAYER + WEB DASHBOARD - make the paper ledger behave like a real trading account.

The paper ledger (cpt_paper.py) is a SCORECARD vs John, normalized to 10 contracts. It tracks P&L
per campaign but has no concept of a cash account. This module adds that layer WITHOUT touching the
proven John-scoring engine: it reads the same paper-ledger.json, opens the account at $300,000, and
rolls every realized close into the balance so it grows / declines over time - exactly what a real
account statement shows. It also marks the open legs to the current (delayed) market for a live
UNREALIZED P&L, per strategy and per leg, and renders a self-contained HTML dashboard.

Decoupled by design (doctrine: never refactor live-proven tooling on a new requirement):
  * realized P&L is read straight from the ledger's own numbers (banked weeklies + closed campaigns)
    so the account balance always reconciles with the scorecard to the penny.
  * `refresh` marks the CURRENTLY-OPEN legs to last market and STORES the marks back on the ledger
    (pos['acct']), so the browser renders stored numbers and never fetches (cloud-safe, CSP-safe).
  * `build` renders the HTML from stored state only - no network - so it runs anywhere, any hour.

Commands:
  python3 cpt_account.py refresh     mark open legs to last market, store unrealized on the ledger
  python3 cpt_account.py build       render dashboard.html from stored state (no network)
  python3 cpt_account.py live        refresh + build in one go (the desk one-liner)
  python3 cpt_account.py summary     print the account summary to the terminal
"""
import json, os, sys, html, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "paper-ledger.json")
OUT = os.path.join(HERE, "dashboard.html")

STARTING_CASH = 300000.0     # the account opens here; every realized close moves it from this base.
MULT = 100                   # option contract multiplier (shares per contract).


# --- ledger IO -----------------------------------------------------------------------------------
def load():
    with open(LEDGER) as f:
        return json.load(f)


def save(book):
    with open(LEDGER, "w") as f:
        json.dump(book, f, indent=2)


def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


# --- realized: read straight from the ledger's own numbers ---------------------------------------
def position_realized(p):
    """Realized $ booked by a position. A CLOSED campaign's realized already includes its banked
    weeklies, so use it directly; an open/rolled position's realized is its banked weeklies."""
    if p["status"] == "CLOSED":
        return p["closed"]["realized"]
    return p["premium_banked"]


def equity_curve(book):
    """Reconstruct the dated realized-equity curve from $300k. Each weekly close adds its income on
    its close date; a closed campaign adds only its INCREMENTAL leg P&L (realized - already-banked
    weeklies) on the close date, so nothing is double-counted. Returns [(date, balance), ...]."""
    events = []
    for p in book["positions"]:
        for w in p["weeklies"]:
            events.append((w["closed"], w["income_usd"]))
        if p["status"] == "CLOSED":
            incr = p["closed"]["realized"] - p["premium_banked"]
            events.append((p["closed"]["date"], incr))
    events.sort(key=lambda e: e[0])
    curve, bal = [], STARTING_CASH
    # seed the curve at the first open date so the line starts flat at $300k
    opens = sorted(p["opened"] for p in book["positions"])
    if opens:
        curve.append((opens[0], STARTING_CASH))
    for date, amt in events:
        bal += amt
        curve.append((date, round(bal, 2)))
    return curve


# --- unrealized: mark the CURRENTLY-OPEN legs to last market -------------------------------------
def _mark(op, crumb, legsmod, ticker, strike, right, exp_ts):
    """Last-available mark for one option leg. Prefers mid; falls back to last trade (pre/post-market
    the book is empty but lastPrice carries). Returns (mark, note) or (None, why)."""
    if not exp_ts:
        return None, "no expiry stored"
    try:
        ch = legsmod._chain(op, crumb, ticker, exp_ts)
    except Exception as e:
        return None, f"fetch failed ({e.__class__.__name__})"
    side = ch["options"][0]["calls" if right == "C" else "puts"]
    if not side:
        return None, "expired / no chain"
    o = min(side, key=lambda x: abs(x["strike"] - strike))
    bid, ask = o.get("bid"), o.get("ask")
    if bid and ask:
        return round((bid + ask) / 2, 2), "mid"
    last = o.get("lastPrice")
    return (round(last, 2), "last") if last else (None, "no quote")


def refresh(live=True):
    """Mark every open/rolled position's live legs to last market and store the result on the ledger
    as pos['acct'] = {unrealized, legs:[...], marked, note}. Best-effort: a leg with no live chain
    (e.g. an already-expired short) is flagged pending, never guessed."""
    book = load()
    op = crumb = legsmod = None
    if live:
        try:
            import cpt_legs_web as legsmod
            op, crumb = legsmod._session()
        except Exception as e:
            print(f"No market session ({e.__class__.__name__}) - storing legs as pending.")
            legsmod = None

    today = dt.date.today().isoformat()
    for p in book["positions"]:
        if p["status"] == "CLOSED":
            p.pop("acct", None)
            continue
        legs, unreal, pending = [], 0.0, False
        n = p["contracts"]
        lc, inc = p.get("long_call"), p.get("income")

        if lc:  # PMCC long call (a debit we paid; gains as it rises)
            mark = note = None
            if legsmod:
                mark, note = _mark(op, crumb, legsmod, p["ticker"], lc["strike"], "C", lc.get("exp_ts"))
            paid = lc.get("ask_paid")
            pl = round((mark - paid) * MULT * n, 0) if (mark is not None and paid is not None) else None
            if pl is None:
                pending = True
            else:
                unreal += pl
            legs.append(dict(role="LONG CALL", side="long", desc=f"{lc['strike']:g}C {lc.get('exp')}",
                             entry=paid, mark=mark, pl=pl, note=note))

        if inc:  # the short income leg (a credit we sold; gains as it decays toward 0)
            mark = note = None
            if legsmod:
                mark, note = _mark(op, crumb, legsmod, p["ticker"], inc["strike"], inc["right"], inc.get("exp_ts"))
            sold = inc.get("sold")
            pl = round((sold - mark) * MULT * n, 0) if (mark is not None and sold is not None) else None
            if pl is None:
                pending = True
            else:
                unreal += pl
            rt = "PUT" if inc["right"] == "P" else "CALL"
            legs.append(dict(role=f"SHORT {rt}", side="short", desc=f"{inc['strike']:g}{inc['right']} {inc.get('exp')}",
                             entry=sold, mark=mark, pl=pl, note=note))

        p["acct"] = dict(unrealized=round(unreal, 0), legs=legs, marked=today, pending=pending)

    save(book)
    tot = sum(p.get("acct", {}).get("unrealized", 0) for p in book["positions"] if p["status"] != "CLOSED")
    npd = sum(1 for p in book["positions"] if p.get("acct", {}).get("pending"))
    print(f"Refreshed marks for {sum(1 for p in book['positions'] if p['status']!='CLOSED')} open position(s). "
          f"Unrealized (marked legs) = {tot:+,.0f}. {npd} position(s) have a leg pending (expired/off-hours).")


# --- account rollup (from stored state; no network) ----------------------------------------------
def account_state(book):
    realized = sum(position_realized(p) for p in book["positions"])
    unreal = sum(p.get("acct", {}).get("unrealized", 0) for p in book["positions"] if p["status"] != "CLOSED")
    value = STARTING_CASH + realized + unreal
    opens = [p for p in book["positions"] if p["status"] != "CLOSED"]
    closed = [p for p in book["positions"] if p["status"] == "CLOSED"]
    weeklies = [w for p in book["positions"] for w in p["weeklies"]]
    marked_dates = [p["acct"]["marked"] for p in opens if p.get("acct", {}).get("marked")]
    return dict(starting=STARTING_CASH, realized=realized, unrealized=unreal, value=value,
                pct=(value / STARTING_CASH - 1) * 100, opens=opens, closed=closed,
                weekly_count=len(weeklies), curve=equity_curve(book),
                as_of=max(marked_dates) if marked_dates else book.get("meta", {}).get("last_marked", "-"))


def summary():
    a = account_state(load())
    print("=" * 56)
    print("  PAPER ACCOUNT  -  Wheel strategy (John Greathouse method)")
    print("=" * 56)
    print(f"  Starting cash      ${a['starting']:>14,.0f}")
    print(f"  Realized P&L       ${a['realized']:>+14,.0f}")
    print(f"  Unrealized (open)  ${a['unrealized']:>+14,.0f}")
    print(f"  {'-'*40}")
    print(f"  ACCOUNT VALUE      ${a['value']:>14,.0f}   ({a['pct']:+.2f}%)")
    print(f"  Open {len(a['opens'])}  |  Closed {len(a['closed'])}  |  Weekly closes {a['weekly_count']}  |  as of {a['as_of']}")


# --- HTML dashboard ------------------------------------------------------------------------------
def _money(x, sign=False):
    s = f"{x:+,.0f}" if sign else f"{x:,.0f}"
    return s


def _cls(x):
    return "up" if x > 0 else ("down" if x < 0 else "flat")


def _sparkline(curve, w=520, h=120):
    if len(curve) < 2:
        return ""
    ys = [b for _, b in curve]
    lo, hi = min(ys + [STARTING_CASH]), max(ys + [STARTING_CASH])
    span = (hi - lo) or 1
    n = len(curve)
    def X(i): return round(i / (n - 1) * w, 1)
    def Y(v): return round(h - (v - lo) / span * h, 1)
    pts = [(X(i), Y(v)) for i, (_, v) in enumerate(curve)]
    line = " ".join(f"{x},{y}" for x, y in pts)
    area = f"0,{h} " + line + f" {w},{h}"
    base_y = Y(STARTING_CASH)
    end = pts[-1]
    up = ys[-1] >= STARTING_CASH
    stroke = "var(--up)" if up else "var(--down)"
    fill = "url(#gUp)" if up else "url(#gDn)"
    return f"""<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" class="spark" role="img" aria-label="Account equity curve">
  <defs>
    <linearGradient id="gUp" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="var(--up)" stop-opacity=".28"/><stop offset="1" stop-color="var(--up)" stop-opacity="0"/></linearGradient>
    <linearGradient id="gDn" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="var(--down)" stop-opacity=".28"/><stop offset="1" stop-color="var(--down)" stop-opacity="0"/></linearGradient>
  </defs>
  <line x1="0" y1="{base_y}" x2="{w}" y2="{base_y}" class="baseline"/>
  <polygon points="{area}" fill="{fill}"/>
  <polyline points="{line}" fill="none" stroke="{stroke}" stroke-width="2" vector-effect="non-scaling-stroke"/>
  <circle cx="{end[0]}" cy="{end[1]}" r="3.5" fill="{stroke}"/>
</svg>"""


def _leg_rows(p):
    acct = p.get("acct") or {}
    rows = []
    for lg in acct.get("legs", []):
        markv = lg["mark"]
        mark = "-" if markv is None else f"{markv:g}"
        entryv = lg["entry"]
        entry = "-" if entryv is None else f"{entryv:g}"
        pl = lg["pl"]
        pl_txt = "<span class='muted'>pending</span>" if pl is None else f"<span class='{_cls(pl)}'>{_money(pl, True)}</span>"
        badge = "long" if lg["side"] == "long" else "short"
        note = f" &middot; {html.escape(lg['note'])}" if lg.get("note") and markv is not None else ""
        role = html.escape(lg["role"]); desc = html.escape(lg["desc"])
        rows.append(
            f"<tr class='leg'><td><span class='legtag {badge}'>{role}</span> "
            f"<span class='mono'>{desc}</span></td>"
            f"<td class='num'>{entry}</td>"
            f"<td class='num'>{mark}{note}</td>"
            f"<td class='num'>{pl_txt}</td></tr>")
    return "".join(rows)


def _open_rows(a):
    rows = []
    for p in sorted(a["opens"], key=lambda p: -position_realized(p)):
        acct = p.get("acct") or {}
        realized = position_realized(p)
        unreal = acct.get("unrealized", 0)
        pending = acct.get("pending")
        strat = "PMCC" if p.get("long_call") else "CSP"
        unreal_txt = f"<span class='{_cls(unreal)}'>{_money(unreal, True)}</span>"
        if pending:
            unreal_txt += " <span class='pill warn'>leg pending</span>"
        rows.append(f"""<tbody class="pos">
  <tr class="pos-head" tabindex="0">
    <td class="tk"><span class="chev">&rsaquo;</span> <b>{p['ticker']}</b> <span class="tag">{strat}</span></td>
    <td class="hide-sm">{html.escape(p['structure'])}</td>
    <td class="hide-sm">{p['opened']}</td>
    <td class="num"><span class="{_cls(realized)}">{_money(realized, True)}</span></td>
    <td class="num">{unreal_txt}</td>
  </tr>
  <tr class="legwrap"><td colspan="5"><table class="legs"><thead><tr><th>leg</th><th class="num">entry</th><th class="num">mark</th><th class="num">unreal. P&amp;L</th></tr></thead><tbody>{_leg_rows(p)}</tbody></table>
  <div class="posmeta">Banked so far: <b class="{_cls(realized)}">{_money(realized, True)}</b> &nbsp;·&nbsp; weekly closes: {len(p['weeklies'])} &nbsp;·&nbsp; contracts: {p['contracts']} &nbsp;·&nbsp; regime: {html.escape(str(p.get('regime') or '-'))}</div></td></tr>
</tbody>""")
    return "".join(rows)


def _closed_rows(a):
    if not a["closed"]:
        return "<tr><td colspan='6' class='muted' style='text-align:center;padding:24px'>No campaigns fully closed yet.</td></tr>"
    rows = []
    for p in sorted(a["closed"], key=lambda p: p["closed"]["date"], reverse=True):
        c = p["closed"]
        strat = "PMCC" if p.get("long_call") else "CSP"
        rows.append(f"""<tr>
    <td><b>{p['ticker']}</b> <span class="tag">{strat}</span></td>
    <td class="hide-sm">{html.escape(c.get('detail',''))[:60]}</td>
    <td class="num hide-sm">{p['opened']} &rarr; {c['date']}</td>
    <td class="num">{c.get('days','-')}d</td>
    <td class="num">{'' if c.get('coc') is None else f"{c['coc']:+.1f}%"}</td>
    <td class="num"><b class="{_cls(c['realized'])}">{_money(c['realized'], True)}</b></td>
  </tr>""")
    return "".join(rows)


def build():
    book = load()
    a = account_state(book)
    val_cls = _cls(a["value"] - STARTING_CASH)
    spark = _sparkline(a["curve"])
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Account</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{
  --bg:#f4f6fa; --surface:#ffffff; --surface2:#eef1f7; --border:#d8deea;
  --ink:#1a2233; --muted:#66708a; --accent:#b07a2b;
  --up:#188f5a; --down:#cc4257; --flat:#66708a;
  --shadow:0 1px 3px rgba(20,30,60,.06),0 8px 28px rgba(20,30,60,.05);
}}
:root:not([data-theme="light"]){{ }}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{
  --bg:#0e1420; --surface:#161d2b; --surface2:#1c2536; --border:#28324708;
  --border:#2a3448; --ink:#e6ecf5; --muted:#8b97ad; --accent:#e0a44b;
  --up:#3fbf7f; --down:#e5687a; --flat:#8b97ad;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
}}}}
:root[data-theme="dark"]{{
  --bg:#0e1420; --surface:#161d2b; --surface2:#1c2536; --border:#2a3448;
  --ink:#e6ecf5; --muted:#8b97ad; --accent:#e0a44b;
  --up:#3fbf7f; --down:#e5687a; --flat:#8b97ad;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font-family:"IBM Plex Sans",system-ui,-apple-system,sans-serif;
  font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased;}}
.mono,.num{{font-family:"IBM Plex Mono","SF Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums;}}
.wrap{{max-width:1040px;margin:0 auto;padding:28px 20px 64px;}}
.up{{color:var(--up)}} .down{{color:var(--down)}} .flat{{color:var(--flat)}} .muted{{color:var(--muted)}}
header.top{{display:flex;align-items:baseline;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:22px}}
.brand{{font-weight:600;letter-spacing:.02em}}
.brand .dot{{color:var(--accent)}}
.sub{{color:var(--muted);font-size:12.5px;letter-spacing:.03em;text-transform:uppercase}}

.hero{{background:var(--surface);border:1px solid var(--border);border-radius:16px;box-shadow:var(--shadow);
  padding:26px 28px;display:grid;grid-template-columns:1fr minmax(280px,1.05fr);gap:28px;align-items:center;margin-bottom:20px}}
.hero .label{{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}}
.value{{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;
  font-size:clamp(38px,6vw,58px);font-weight:600;letter-spacing:-.02em;line-height:1;margin:6px 0 10px}}
.delta{{display:inline-flex;align-items:center;gap:8px;font-family:"IBM Plex Mono",monospace;font-weight:600;
  padding:5px 12px;border-radius:999px;font-size:14px}}
.delta.up{{background:color-mix(in srgb,var(--up) 15%,transparent)}}
.delta.down{{background:color-mix(in srgb,var(--down) 15%,transparent)}}
.delta.flat{{background:var(--surface2)}}
.breakdown{{display:flex;gap:22px;margin-top:18px;flex-wrap:wrap}}
.breakdown .b .k{{font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}}
.breakdown .b .v{{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;font-size:19px;font-weight:600;margin-top:2px}}
.chartbox{{position:relative}}
.chartbox .cap{{display:flex;justify-content:space-between;font-size:11.5px;color:var(--muted);letter-spacing:.04em;margin-bottom:6px}}
.spark{{width:100%;height:120px;display:block}}
.spark .baseline{{stroke:var(--border);stroke-width:1;stroke-dasharray:3 4}}

.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:26px}}
.kpi{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px}}
.kpi .k{{font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}}
.kpi .v{{font-family:"IBM Plex Mono",monospace;font-size:22px;font-weight:600;margin-top:3px}}

h2.sec{{font-size:13px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);
  margin:30px 0 12px;font-weight:600}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);overflow:hidden}}
table{{width:100%;border-collapse:collapse}}
.num{{text-align:right;white-space:nowrap}}
thead.main th{{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600;
  text-align:left;padding:12px 16px;border-bottom:1px solid var(--border)}}
thead.main th.num{{text-align:right}}
tbody.pos{{border-bottom:1px solid var(--border)}}
tbody.pos:last-child{{border-bottom:none}}
.pos-head{{cursor:pointer}}
.pos-head td{{padding:14px 16px;vertical-align:middle}}
.pos-head:hover{{background:var(--surface2)}}
.pos-head:focus-visible{{outline:2px solid var(--accent);outline-offset:-2px}}
.tk b{{font-size:15.5px}}
.chev{{display:inline-block;transition:transform .15s;color:var(--muted);font-size:18px;width:12px}}
.pos.open .chev{{transform:rotate(90deg)}}
.tag{{font-size:10.5px;font-weight:600;letter-spacing:.04em;padding:2px 7px;border-radius:5px;
  background:var(--surface2);color:var(--muted);border:1px solid var(--border);vertical-align:middle}}
.pill{{font-size:10px;font-weight:600;letter-spacing:.03em;padding:2px 7px;border-radius:999px;vertical-align:middle}}
.pill.warn{{background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent)}}
.legwrap{{display:none}}
.pos.open .legwrap{{display:table-row}}
.legwrap>td{{padding:0 16px 16px;background:var(--surface2)}}
table.legs{{margin:4px 0 0;background:transparent}}
table.legs th{{font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);font-weight:600;padding:8px 8px 6px;border-bottom:1px solid var(--border)}}
table.legs td{{padding:8px;border-bottom:1px solid var(--border);font-size:13.5px}}
table.legs tr:last-child td{{border-bottom:none}}
.legtag{{font-size:10px;font-weight:700;letter-spacing:.04em;padding:2px 6px;border-radius:4px;margin-right:6px}}
.legtag.long{{background:color-mix(in srgb,var(--up) 16%,transparent);color:var(--up)}}
.legtag.short{{background:color-mix(in srgb,var(--down) 16%,transparent);color:var(--down)}}
.posmeta{{font-size:12px;color:var(--muted);padding:10px 8px 4px}}
.posmeta b{{font-family:"IBM Plex Mono",monospace}}
thead.main tr th:first-child, .closed td:first-child{{padding-left:16px}}
.closed td{{padding:13px 16px;border-bottom:1px solid var(--border)}}
.closed tr:last-child td{{border-bottom:none}}
.note{{font-size:12px;color:var(--muted);margin-top:14px;line-height:1.6}}
.note b{{color:var(--ink)}}
footer{{margin-top:30px;font-size:11.5px;color:var(--muted);text-align:center;letter-spacing:.03em}}
@media (max-width:720px){{
  .hero{{grid-template-columns:1fr;gap:18px}}
  .kpis{{grid-template-columns:repeat(2,1fr)}}
  .hide-sm{{display:none}}
}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <div class="brand">CPT Paper Account <span class="dot">&bull;</span> Wheel</div>
      <div class="sub">John Greathouse method &bull; simulated on delayed market data</div>
    </div>
    <div class="sub">as of {a['as_of']}</div>
  </header>

  <section class="hero">
    <div>
      <div class="label">Account value</div>
      <div class="value">${_money(a['value'])}</div>
      <span class="delta {val_cls}">{'&#9650;' if a['value']>=STARTING_CASH else '&#9660;'} {_money(a['value']-STARTING_CASH, True)} &nbsp;({a['pct']:+.2f}%)</span>
      <div class="breakdown">
        <div class="b"><div class="k">Starting cash</div><div class="v">${_money(a['starting'])}</div></div>
        <div class="b"><div class="k">Realized</div><div class="v {_cls(a['realized'])}">{_money(a['realized'], True)}</div></div>
        <div class="b"><div class="k">Unrealized</div><div class="v {_cls(a['unrealized'])}">{_money(a['unrealized'], True)}</div></div>
      </div>
    </div>
    <div class="chartbox">
      <div class="cap"><span>Equity curve</span><span>opened at $300,000</span></div>
      {spark}
    </div>
  </section>

  <section class="kpis">
    <div class="kpi"><div class="k">Open positions</div><div class="v">{len(a['opens'])}</div></div>
    <div class="kpi"><div class="k">Closed campaigns</div><div class="v">{len(a['closed'])}</div></div>
    <div class="kpi"><div class="k">Weekly closes banked</div><div class="v">{a['weekly_count']}</div></div>
    <div class="kpi"><div class="k">Return on account</div><div class="v {_cls(a['pct'])}">{a['pct']:+.2f}%</div></div>
  </section>

  <h2 class="sec">Open positions &bull; click a row for the legs</h2>
  <div class="card">
    <table>
      <thead class="main"><tr><th>Position</th><th class="hide-sm">Structure</th><th class="hide-sm">Opened</th><th class="num">Realized</th><th class="num">Unrealized</th></tr></thead>
      {_open_rows(a)}
    </table>
  </div>

  <h2 class="sec">Closed trades</h2>
  <div class="card">
    <table class="closed">
      <thead class="main"><tr><th>Position</th><th class="hide-sm">Detail</th><th class="num hide-sm">Held</th><th class="num">Days</th><th class="num">CoC</th><th class="num">Final P&amp;L</th></tr></thead>
      <tbody>{_closed_rows(a)}</tbody>
    </table>
  </div>

  <p class="note">
    <b>How to read this.</b> The account opens at <b>$300,000</b>. <b>Realized</b> is every closed weekly
    covered-call + closed campaign, booked to the balance exactly as the scorecard records it.
    <b>Unrealized</b> marks the open legs to the last available (Yahoo, delayed) price - it moves intraday and is an estimate.
    Positions are normalized to 10 contracts for comparability with John, so deployed notional can exceed cash;
    the account value (starting + realized + unrealized) is the growth number. A <span class="pill warn">leg pending</span>
    tag means a short leg already expired and awaits the next mark cycle (assignment / roll).
  </p>

  <footer>Generated {generated} &bull; read-only view of paper-ledger.json &bull; not financial advice</footer>
</div>
<script>
document.querySelectorAll('.pos-head').forEach(function(h){{
  function t(){{h.closest('.pos').classList.toggle('open');}}
  h.addEventListener('click',t);
  h.addEventListener('keydown',function(e){{if(e.key==='Enter'||e.key===' '){{e.preventDefault();t();}}}});
}});
</script>
</body>
</html>"""
    with open(OUT, "w") as f:
        f.write(html_doc)
    print(f"Wrote {OUT}  ({len(html_doc):,} bytes).  Account value ${a['value']:,.0f} ({a['pct']:+.2f}%).")


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if cmd == "refresh":
        refresh(live="--offline" not in sys.argv)
    elif cmd == "build":
        build()
    elif cmd == "live":
        try:
            refresh(live=True)
        except Exception as e:
            print(f"refresh failed ({e.__class__.__name__}: {e}) - rendering from last stored marks.")
        build()
    elif cmd == "summary":
        summary()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
