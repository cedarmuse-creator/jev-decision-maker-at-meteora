"""Enrich fan-out — one rug card must reach EVERY copy of a pool/token.

Regression: `enrich_candidates` built `by_pool = {pool: row}` (last row wins) and
stamped the card onto a single row. A pool listed under two tabs (e.g. trending
AND rwa) therefore left its other copy with no card at all, and that unscouted
duplicate kept the clean proxy default — reaching select while its scouted twin
was red on creator_pile.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROUTINES = Path(__file__).resolve().parents[1] / "routines"
sys.path.insert(0, str(ROUTINES))

import jev_enrich as E  # noqa: E402

CLEAN = {k: False for k in E._m.FLAG_KEYS}
RED = dict(CLEAN, creator_pile=True)


def _fake_scout(flags, creator_pct=4.0, sell_ok=True):
    async def _fn(session, *, mint, pool="", **kw):
        return {"flags": flags, "creator_pct": creator_pct, "sell_ok": sell_ok,
                "allowed": not any(flags.values()), "reason": "test"}
    return _fn


def _rows():
    """One pool under two tabs, plus a second pool for the same base mint."""
    return [
        {"pool": "POOL1", "base": "MINT1", "pair": "AAA-USDC", "tab": "trending",
         "tvl": 5e5, "vol": 1e6, "bin_step": 20},
        {"pool": "POOL1", "base": "MINT1", "pair": "AAA-USDC", "tab": "rwa",
         "tvl": 5e5, "vol": 1e6, "bin_step": 20},
        {"pool": "POOL2", "base": "MINT1", "pair": "AAA-USDC", "tab": "top",
         "tvl": 4e5, "vol": 9e5, "bin_step": 20},
    ]


def _run(flags):
    E._sc.evaluate_mint_pool = _fake_scout(flags)
    return asyncio.run(E.enrich_candidates(_rows(), top_k=12))


def test_a_red_card_reaches_every_copy_of_the_pool():
    out = _run(RED)
    for r in out["candidates"]:
        assert r.get("scout_source") == "live", f"tab {r['tab']} was never stamped"
        assert r.get("rug_noul") == 0.0, f"tab {r['tab']} kept a clean card"
        assert r.get("scout_allowed") is False
        assert r.get("scout_red") == ["creator_pile"]
    assert out["blocked"] >= 1 and out["allowed"] == 0


def test_a_clean_card_also_reaches_every_copy():
    out = _run(CLEAN)
    for r in out["candidates"]:
        assert r.get("scout_source") == "live"
        assert r.get("rug_noul") == 1.0
        assert r.get("scout_allowed") is True


def test_one_card_is_earned_per_name_not_per_tab():
    # Two tabs + a second pool for the same mint = still one scout call.
    out = _run(CLEAN)
    assert out["scouted"] == 1
    assert out["errors"] == 0


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print(f"ALL {len(tests)} PASSED")
