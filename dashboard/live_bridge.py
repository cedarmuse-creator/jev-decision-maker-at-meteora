"""JEV desk — LIVE bridge for dashboard/index.html.

Serves this folder and generates /state.json from Condor's real state:
the routine snapshots, the running executors, balances and the live gate.
No mock universe: if there is nothing live, the rows say so.

    python dashboard/live_bridge.py [port]      (default 8099)

The page polls /state.json every 2.5s (see index.html), so this handler
regenerates it per request instead of running a timer.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Everything host-specific comes from the environment, so this file carries no
# machine paths, no account ids and no credentials. Point CONDOR_HOME at the
# Condor checkout; the rest default sensibly off it.
CONDOR_HOME = Path(os.environ.get("CONDOR_HOME", str(Path.home() / "condor")))
STATE_DIR = Path(os.environ.get("JEV_STATE_DIR", str(CONDOR_HOME / "state")))
API = os.environ.get("HUMMINGBOT_API_URL", "http://127.0.0.1:8000")
USER_ID = int(os.environ.get("CONDOR_USER_ID", "0"))
MEMORY_DIR = Path(os.environ.get(
    "JEV_MEMORY_DIR",
    str(CONDOR_HOME / ".condor" / "agents" / "jev" / "store" /
        f"user_{USER_ID}" / "memories")))
BOOK = 100.0

M = None
sys.path.insert(0, str(HERE.parent / "agents" / "jev" / "routines"))
try:
    import _jev_math as M  # noqa: E402
    BOOK = float(M.RACE_USD)
except Exception:  # noqa: BLE001
    BOOK = 100.0


def _auth() -> dict:
    """Hummingbot API basic auth, read from the environment.

    Credentials are never baked into this file: it ships in a public repo.
    Set HUMMINGBOT_API_USER / HUMMINGBOT_API_PASS (the API's own default is
    admin/admin, which it falls back to silently — so a missing password here
    looks like an auth failure, not a misconfiguration).
    """
    user = os.environ.get("HUMMINGBOT_API_USER", "admin")
    pw = os.environ.get("HUMMINGBOT_API_PASS", "")
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": "Basic " + token, "Content-Type": "application/json"}


AUTH = _auth()


def api(path: str, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=AUTH,
                                 method="POST" if data else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def snap(name: str) -> dict:
    try:
        return json.loads((STATE_DIR / f"jev_{name}.json").read_text())
    except Exception:  # noqa: BLE001
        return {}


def live_gate() -> bool:
    """The key the agent itself reads: jev.live must be exactly 'yes'.

    Read through Condor's own memory store rather than by filename. The store
    slugs the name on write ("jev.live" -> "jevlive.md"), so looking for the
    literal "jev.live.md" never matched and pinned this dashboard to DRY-RUN
    while the desk was actually trading live.
    """
    try:
        if str(CONDOR_HOME) not in sys.path:
            sys.path.insert(0, str(CONDOR_HOME))
        from condor.memory.store import MemoryStore
        val = MemoryStore(USER_ID, "jev").read("jev.live")
        if val is not None:
            return str(val).strip().lower() == "yes"
    except Exception:  # noqa: BLE001
        pass
    # Fallback: match the slugged filename and read its body.
    for cand in sorted(MEMORY_DIR.glob("*")):
        if "jevlive" in cand.name.lower().replace(".", "").replace("_", ""):
            try:
                return cand.read_text().strip().lower().startswith("yes")
            except Exception:  # noqa: BLE001
                continue
    return False


def _name_maps() -> tuple[dict[str, str], dict[str, str]]:
    """(pool address -> display pair, base mint -> symbol) from the snapshots.

    Executors carry the venue's mint pair (`CARDScc...-USDC`); the readable name
    only exists on the routine rows, so resolve it from there. Rows written before
    the scan learned to name a pair carry only a truncated label (`6p6xgH-EPjF`)
    and no `base_symbol`, so trustworthy rows win regardless of snapshot age.
    """
    by_pool: dict[str, str] = {}
    by_mint: dict[str, str] = {}
    trusted: list[dict] = []
    rest: list[dict] = []
    for name in ("select", "rank", "enrich", "scan"):
        for r in (snap(name).get("candidates") or []):
            (trusted if r.get("base_symbol") else rest).append(r)
    for rows in (trusted, rest):
        for r in rows:
            p, b = str(r.get("pool") or ""), str(r.get("base") or "")
            if p and r.get("pair"):
                by_pool.setdefault(p, str(r["pair"]))
            if b and r.get("base_symbol"):
                by_mint.setdefault(b, str(r["base_symbol"]))
    return by_pool, by_mint


def _display(trading_pair: str, pool_addr: str, by_pool: dict, by_mint: dict) -> str:
    """A readable label for an executor's pair, never a truncated mint."""
    if pool_addr and pool_addr in by_pool:
        return by_pool[pool_addr]
    base_mint = str(trading_pair or "").split("-")[0]
    quote = str(trading_pair or "").split("-")[1] if "-" in str(trading_pair) else "USDC"
    if base_mint in by_mint:
        return f"{by_mint[base_mint]}-{quote}"
    return trading_pair or "—"


def tab_of(pair: str, picks: dict) -> str:
    for p in picks:
        if p.get("pool") and p.get("pair") == pair:
            return p.get("tab", "top")
        if p.get("pool"):
            pass
    return "trending"


def build() -> dict:
    scan = snap("scan")
    select = snap("select")
    size = snap("size")
    gate = snap("gate")
    picks = select.get("picks") or select.get("candidates") or []
    live = live_gate()
    by_pool, by_mint = _name_maps()

    # --- live executors -------------------------------------------------
    # The strategy namespaces its controller per session (``jev.jev_desk_5``),
    # so an exact ``controller_ids=["jev"]`` filter matched only the legacy
    # controller and the desk's own live executors were invisible: the dashboard
    # read "OPEN POSITIONS 0" while a CARDS-USDC position was actually RUNNING.
    # Match the "jev" prefix client-side, over a page big enough to hold every
    # executor on the account (the account holds 100+ across all controllers).
    execs = []
    try:
        rows = (api("/executors/search",
                    {"filters": {}, "limit": 300}).get("data") or [])
        execs = [e for e in rows
                 if str(e.get("controller_id") or "").split(".")[0] == "jev"]
    except Exception:  # noqa: BLE001
        pass

    # --- balances -------------------------------------------------------
    bal = {}
    try:
        st = api("/portfolio/state", {"account_name": "master_account"}, timeout=40)
        for conn, rows in (st.get("master_account") or {}).items():
            if "solana" in conn.lower():
                for r in rows:
                    bal[r.get("token")] = float(r.get("value") or 0)
    except Exception:  # noqa: BLE001
        pass

    # --- positions: live executors only; terminated ones belong in Trades --
    positions = []
    used = 0.0
    major_pct = minor_pct = 0.0
    for e in execs:
        if e.get("executor_type") != "lp_executor":
            continue
        if e.get("status") not in ("RUNNING", "stopping"):
            continue          # closed positions are history, not holdings
        cfg = e.get("config") or {}
        ci = e.get("custom_info") or {}
        amount = float(cfg.get("quote_amount") or ci.get("total_value_quote") or 0)
        used += amount
        pair = e.get("trading_pair", "")
        tab = tab_of(pair, picks)
        display = _display(pair, str(cfg.get("pool_address") or ""), by_pool, by_mint)
        is_major = tab in ("top", "rwa")
        if is_major:
            major_pct += amount / BOOK * 100
        else:
            minor_pct += amount / BOOK * 100
        lo, hi = cfg.get("lower_price"), cfg.get("upper_price")
        w = 0.0
        bins = 0
        try:
            if lo and hi:
                w = abs((float(hi) - float(lo)) / float(hi)) * 100
                bins = round(M.bin_count(float(lo), float(hi), float(cfg.get("bin_step") or 20)))
        except Exception:  # noqa: BLE001
            pass
        verb = {"IN_RANGE": "READY", "OUT_OF_RANGE": "MOVE", "COMPLETE": "SIT"}.get(
            str(ci.get("state") or "").upper(), e.get("status", "—"))
        positions.append({
            "pair": display,
            "base": pair.split("-")[0], "tab": tab, "role": "portfolio",
            "verb": verb, "amount": round(amount, 2), "width_pct": round(w, 2),
            "bin_count": bins, "open_cost": round(float(ci.get("position_rent") or 0) * 100, 2),
            "expected_fee": round(float(ci.get("fees_earned_quote") or 0), 4),
            "worth": e.get("status") != "TERMINATED", "worth_tier": None,
            "conf": 0.0, "pnl": round(float(e.get("net_pnl_quote") or 0), 4),
            "status": e.get("status"), "pool": cfg.get("pool_address", ""),
        })

    dry_rows = []
    if not positions and picks:
        for p in picks[:5]:
            pool_usd = None
            tier = None
            if size.get("pool_usd") is not None and size.get("role") == "portfolio":
                pool_usd = size.get("pool_usd")
                tier = size.get("worth_tier")
            dry_rows.append({
                "pair": p.get("pair", "?"), "base": p.get("base", ""),
                "tab": p.get("tab", "top"), "role": "portfolio",
                "verb": "READY" if (tier in ("GO", "MARGINAL")
                                    and float(pool_usd or 0) > 0) else "SIT",
                # `pool_usd or fallback` treated a genuine 0.0 (the size routine
                # abstained) as missing and showed the max slice instead — so the
                # card read "$50" while the decision panel said "$0.00".
                "amount": round(float(pool_usd if pool_usd is not None
                                      else (BOOK * getattr(M, "PORTFOLIO_PCT_MAX", 0.5))), 2),
                "width_pct": size.get("width_pct"), "bin_count": size.get("bin_count"),
                "open_cost": size.get("open_cost"), "expected_fee": size.get("expected_fee"),
                "worth": tier in ("GO", "MARGINAL"), "worth_tier": tier,
                "conf": round(float(p.get("rug_noul") or 0), 2),
                "pnl": 0.0, "status": "PREVIEW", "pool": p.get("pool", ""),
            })
            amount = dry_rows[-1]["amount"]
            if p.get("tab") in ("top", "rwa"):
                major_pct += amount / BOOK * 100
            else:
                minor_pct += amount / BOOK * 100
        positions = dry_rows
        # Previews are the desk's intent, not deployed capital: counting them as
        # "book used" made a live, fully-cashed desk read as $40 committed.
        used = 0.0
        major_pct = minor_pct = 0.0

    # --- decisions: the real routine verdicts ---------------------------
    decisions = []
    for p in picks[:6]:
        decisions.append({
            "role": "portfolio", "verdict": "SELECT",
            "pool": p.get("pair") or p.get("pool", ""),
            "conf": round(float(p.get("rug_noul") or 0), 2),
            "reason": f"comp {float(p.get('composite') or 0):.1f} · tab {p.get('tab')} · "
                      f"tvl ${float(p.get('tvl') or 0):,.0f}",
        })
    if size:
        decisions.append({
            "role": "portfolio", "verdict": str(size.get("worth_tier") or "—"),
            "pool": "size", "conf": 0.0,
            "reason": (f"pct {size.get('pct')} of book · ${size.get('pool_usd', 0):.2f} · "
                       f"fee ${size.get('expected_fee', 0):.2f} vs open ${size.get('open_cost', 0):.2f} "
                       f"({size.get('worth_ratio')}x) · {size.get('pct_source')}"
                       + (f" · trend {float(size['momentum_pct']):+.1f}% "
                          f"→ +${float(size.get('spread_credit') or 0):.2f} spread "
                          f"(income ${float(size.get('income') or 0):.2f})"
                          if size.get("momentum_pct") is not None else "")),
        })

    # --- confidence stream: real routine outputs, newest first ----------
    conf = []
    for name, label in (("size", "SCORE"), ("select", "CHOICE"), ("rank", "RANK"),
                        ("scan", "SCAN")):
        s = snap(name)
        if not s:
            continue
        ts = str(s.get("_ts") or "")[11:19] or "--:--:--"
        if name == "scan":
            msg = (f"{s.get('pulled')} pulled · {len(s.get('candidates') or [])} candidates "
                   f"across tabs")
        elif name == "rank":
            msg = f"{len(s.get('candidates') or [])} scored survivors"
        elif name == "select":
            msg = f"{len(s.get('picks') or [])} pools earned a seat (book ${s.get('book_usd')})"
        else:
            msg = (f"{s.get('role')} {s.get('pct')} of book → ${s.get('pool_usd')} · "
                   f"{s.get('worth_tier')} ({s.get('worth_ratio')}x open cost)")
        conf.append({"t": ts, "jev": "JEV on" if s.get("pct_source") == "model"
                     or (s.get("model") or {}).get("jev") == "JEV on" else "JEV off",
                     "msg": f"{label} · {msg}"})
    conf = sorted(conf, key=lambda c: c["t"], reverse=True)

    # --- trades: real executor history ----------------------------------
    trades = []
    for e in execs:
        created = str(e.get("created_at") or "")[11:19]
        if not created:
            continue
        cfg = e.get("config") or {}
        trades.append({
            "time": created,
            "pool": _display(e.get("trading_pair") or "",
                             str(cfg.get("pool_address") or ""), by_pool, by_mint),
            "side": "BUY" if cfg.get("side") in (1, "BUY", "TradeType.BUY") else "SELL",
            "usd": float(cfg.get("quote_amount") or 0),
            "verb": e.get("close_type") or e.get("status"),
        })
    trades = sorted(trades, key=lambda t: t["time"], reverse=True)[:8]

    # --- metrics ---------------------------------------------------------
    summary = {}
    try:
        summary = api("/executors/summary")
    except Exception:  # noqa: BLE001
        pass
    open_n = sum(1 for e in execs if e.get("status") == "RUNNING")
    metrics = {
        "realized_pnl": float(summary.get("total_pnl_quote") or 0),
        "unrealized_pnl": sum(float(e.get("net_pnl_quote") or 0)
                              for e in execs if e.get("status") == "RUNNING"),
        "fees_collected": sum(float(e.get("cum_fees_quote") or 0) for e in execs),
        "open_positions": open_n,
    }

    return {
        "tick": int(time.time()),
        "mode": (getattr(M, "MODE_LABEL", "TEST") if M else "TEST"),
        "max_positions": (getattr(M, "MAX_POSITIONS", 2) if M else 2),
        "book": BOOK,
        "book_used": round(used, 2),
        "free_book": round(BOOK - used, 2),
        "live": live,
        "pairs_watched": len(scan.get("candidates") or []),
        "positions": positions,
        "decisions": decisions,
        "alloc": {"major": round(major_pct, 1), "minor": round(minor_pct, 1)},
        "trades": trades,
        "conf": conf,
        "metrics": metrics,
        "balances": bal,
        "note": ("live — real executors" if open_n else (
            "live — no open positions yet" if live
            else "dry-run — showing the desk's current picks (no live executors)")),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] in ("/state.json", "/live-state.json"):
            try:
                body = json.dumps(build(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:  # noqa: BLE001
                err = json.dumps({"error": str(exc)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return
        return super().do_GET()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    print(f"JEV live bridge → http://127.0.0.1:{port}/  (state: /state.json)")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
