"""JEV dashboard simulator — multi-pair portfolio, using the pure decision logic.

Lets judges open dashboard/index.html and watch a 3-5 pool portfolio breathe
without a live exchange. Pulls a small mock universe across tabs, then runs
jev_scan -> jev_rank -> jev_select -> jev_size on each. Writes DASHBOARD/state.json
each tick. No network, no orders. Run:  python agents/jev/dashboard/sim_jev.py
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "agents" / "jev" / "routines"))

import _jev_math as M  # noqa: E402

STATE = HERE / "state.json"
BOOK = 800.0

# Small mock universe tagged by tab (demo). Real agent pulls dlmm.datapi.meteora.ag.
UNIVERSE = [
    {"pool": "So11111111111111111111111111111111111111112EPjF", "base": "SOL", "quote": "USDC",
     "pair": "SOL-USDC", "tvl": 2_400_000, "vol": 1_900_000, "fee": 0.04, "bin_step": 20,
     "rug_noul": 1.0, "tab": "top", "dev_balance": 0.0, "top_10": 0.2},
    {"pool": "BONKxxxxxxxUSDCxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "base": "BONK", "quote": "USDC",
     "pair": "BONK-USDC", "tvl": 320_000, "vol": 540_000, "fee": 0.12, "bin_step": 50,
     "rug_noul": 0.92, "tab": "trending", "dev_balance": 0.03, "top_10": 0.35},
    {"pool": "WIFxxxxxxxUSDCxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "base": "WIF", "quote": "USDC",
     "pair": "WIF-USDC", "tvl": 180_000, "vol": 410_000, "fee": 0.15, "bin_step": 50,
     "rug_noul": 0.88, "tab": "trending", "dev_balance": 0.05, "top_10": 0.40},
    {"pool": "JUPxxxxxxxUSDCxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "base": "JUP", "quote": "USDC",
     "pair": "JUP-USDC", "tvl": 90_000, "vol": 130_000, "fee": 0.10, "bin_step": 50,
     "rug_noul": 0.95, "tab": "new", "dev_balance": 0.08, "top_10": 0.45},
    {"pool": "USDGxxxxxxxUSDCxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "base": "USDG", "quote": "USDC",
     "pair": "USDG-USDC", "tvl": 1_100_000, "vol": 600_000, "fee": 0.02, "bin_step": 20,
     "rug_noul": 1.0, "tab": "rwa", "dev_balance": 0.0, "top_10": 0.15},
    {"pool": "USDMxxxxxxxUSDCxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "base": "USDM", "quote": "USDC",
     "pair": "USDM-USDC", "tvl": 700_000, "vol": 300_000, "fee": 0.02, "bin_step": 20,
     "rug_noul": 1.0, "tab": "rwa", "dev_balance": 0.0, "top_10": 0.18},
]


def rank_universe(universe):
    """Mirror jev_rank_row: hard gate + yield/depth/clean composite."""
    scored = []
    for c in universe:
        out = M.jev_rank_row(
            tvl=c["tvl"], vol24=c["vol"], bin_step=c["bin_step"],
            fee_rate_pct=c.get("fee", 0.0), fees24=c.get("fees", 0.0),
            tab=c["tab"], is_rwa=c["tab"] == "rwa",
            dev_balance=c.get("dev_balance", 0.0),
            top10=c.get("top_10", 0.0),
            rug_noul=c.get("rug_noul"),
            min_tvl=10_000.0, min_vol=1_000.0,
        )
        row = dict(c)
        row["composite"] = out["composite"]
        row["rug_noul"] = out["rug_noul"]
        row["rank_pass"] = out["pass"]
        row["rank_reason"] = out["reason"]
        if out["pass"]:
            scored.append(row)
    scored.sort(key=lambda x: x["composite"], reverse=True)
    return scored


def select_portfolio(ranked, max_positions=5, min_position_usd=100):
    """Take top N distinct-base passers; soft-cap new-tab names."""
    out, bases, n_new = [], set(), 0
    for c in ranked:
        if not c.get("rank_pass", True):
            continue
        if c["base"] in bases:
            continue
        if c.get("tab") == "new" and n_new >= 2:
            continue
        if BOOK * M.PORTFOLIO_PCT_MAX < min_position_usd:
            pass
        bases.add(c["base"])
        if c.get("tab") == "new":
            n_new += 1
        out.append(c)
        if len(out) >= max_positions:
            break
    return out


def make_decision(c):
    """Size one pool with _jev_math.jev_size, derive a verb."""
    size = M.jev_size(role="portfolio", tvl=c["tvl"], vol24=c["vol"],
                      bin_step=c["bin_step"], dynamic_fee_pct=c["fee"],
                      outside_slots=4, rug_noul=c["rug_noul"], vol_daily_pct=3.0)
    if not size["worth"] or size["pct"] <= 0:
        verb = "SIT"
    else:
        verb = "HOLD" if random.random() < 0.7 else "BUILD"
    pnl = round(random.uniform(-3.0, 7.0), 2) if verb != "SIT" else 0.0
    return {
        "base": c["base"], "pair": c["pair"], "pool": c["pool"], "tab": c["tab"],
        "role": "portfolio", "verb": verb, "amount": round(BOOK * size["pct"], 2),
        "pct": size["pct"], "width_pct": size["width_pct"], "bin_count": size.get("bin_count"),
        "conf": round(size["score"] / 100.0, 2),
        "scores": {"depth": round(math.log10(max(c["tvl"], 1.0)) / 7.0, 2),
                   "heat": round(min(c["vol"] / c["tvl"], 3.0) / 3.0, 2),
                   "rug": c["rug_noul"], "fee": c["fee"]},
        "worth": size["worth"], "open_cost": size.get("open_cost"),
        "expected_fee": size.get("expected_fee"), "pnl": pnl,
    }


def tick():
    universe = [dict(c) for c in UNIVERSE]
    for c in universe:
        c["vol"] = c["vol"] * (0.8 + 0.4 * random.random())  # breathe
    ranked = rank_universe(universe)
    portfolio = select_portfolio(ranked, max_positions=5)
    positions = [make_decision(c) for c in portfolio]
    book_used = sum(p["amount"] for p in positions if p["verb"] != "SIT")
    total_pnl = round(sum(p["pnl"] for p in positions), 2)
    state = {
        "tick": int(time.time()), "book": BOOK, "book_used": round(book_used, 2),
        "free_book": round(BOOK - book_used, 2),
        "positions": positions, "decisions": [
            {"pool": p["base"], "role": p["role"], "verb": p["verb"],
             "amount": p["amount"], "conf": p["conf"], "scores": p["scores"]}
            for p in positions],
        "metrics": {"realized_pnl": total_pnl, "unrealized_pnl": 0.0,
                     "fees_collected": round(total_pnl * 0.6, 2),
                     "open_positions": sum(1 for p in positions if p["verb"] != "SIT")},
        "pairs_watched": len(UNIVERSE), "note": "simulated — no live exchange",
    }
    STATE.write_text(json.dumps(state, indent=2))
    return state


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    for _ in range(n):
        tick()
    last = tick() if n == 0 else json.loads(STATE.read_text())
    print("wrote", STATE)
    print(json.dumps(last, indent=2)[:1500])

