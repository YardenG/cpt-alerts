#!/usr/bin/env python3
"""
Alpaca PAPER connector for the CPT (John Greathouse Wheel) book - options-aware.

Separate from the Qullamaggie Alpaca integration BY DESIGN: its own paper
account, its own keys, its own config. Shares only the proven stdlib-only REST
pattern (no pip installs, cloud-ready), never any state.

Credentials come from a gitignored `alpaca_config.json` next to this file, OR
from env vars (ALPACA_CPT_KEY_ID / ALPACA_CPT_SECRET_KEY) so the same code runs
at the desk and headless in GitHub Actions. Your key + secret never appear in
this file, in git, or in anything this tool prints.
"""
import json, os, sys, urllib.request, urllib.parse, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "alpaca_config.json")
DEFAULT_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"


def load_creds():
    key = os.environ.get("ALPACA_CPT_KEY_ID")
    sec = os.environ.get("ALPACA_CPT_SECRET_KEY")
    base = os.environ.get("ALPACA_CPT_BASE_URL", DEFAULT_BASE)
    if key and sec:
        return key, sec, base
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            c = json.load(f)
        return c.get("key_id"), c.get("secret_key"), c.get("base_url", DEFAULT_BASE)
    sys.exit("No Alpaca credentials. Copy alpaca_config.example.json to "
             "alpaca_config.json (same folder) and paste your CPT PAPER key + "
             "secret, or set ALPACA_CPT_KEY_ID / ALPACA_CPT_SECRET_KEY.")


def api(method, path, body=None, params=None):
    key, sec, base = load_creds()
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": sec,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace").strip()
            return json.loads(raw) if raw else {}   # DELETE returns 204/empty
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Alpaca {e.code} on {method} {path}: {detail}")


# --- trading-api wrappers the verifier + (later) the engine use -------------------
def get_account():   return api("GET", "/v2/account")
def get_asset(sym):  return api("GET", f"/v2/assets/{sym}")
def get_positions(): return api("GET", "/v2/positions")


def get_orders(status="all", limit=200):
    return api("GET", "/v2/orders", params={"status": status, "limit": limit})


def option_contracts(underlying, limit=100, **filt):
    """List tradable option contracts for an underlying. Filters: expiration_date,
    expiration_date_gte/lte, type (call|put), strike_price_gte/lte, style, status."""
    p = {"underlying_symbols": underlying, "limit": limit}
    p.update({k: v for k, v in filt.items() if v is not None})
    return api("GET", "/v2/options/contracts", params=p)


def submit_order(**kw):  return api("POST", "/v2/orders", kw)
def get_order(oid):      return api("GET", f"/v2/orders/{oid}")
def cancel_order(oid):   return api("DELETE", f"/v2/orders/{oid}")


# --- market-data wrappers (different host; same keys) -----------------------------
def data_api(path, params=None):
    key, sec, _ = load_creds()
    url = DATA_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace").strip()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Alpaca DATA {e.code} on {path}: {detail}")


def latest_trade(sym, feed="iex"):
    """Spot for the underlying. Free IEX feed; returns None if unavailable (off-hours/plan)."""
    try:
        d = data_api(f"/v2/stocks/{sym}/trades/latest", {"feed": feed})
        return (d.get("trade") or {}).get("p")
    except Exception:
        return None


def option_snapshots(underlying, feed="indicative", **filt):
    """Option chain snapshots: per-contract greeks (delta!), implied vol, and latest quote/trade.
    Filters: type (call|put), expiration_date, expiration_date_gte/lte, strike_price_gte/lte, limit,
    page_token. Returns {'snapshots': {OCC: {...}}, 'next_page_token': ...}."""
    p = {"feed": feed, "limit": filt.pop("limit", 100)}
    p.update({k: v for k, v in filt.items() if v is not None})
    return data_api(f"/v1beta1/options/snapshots/{underlying}", p)
