"""JEV Select — portfolio Choice across the ranked universe.

Math disposes first: trust floor, rank hard-pass, distinct bases, slot budget,
per-position floor, soft tab diversity (cap how many 'new' slots). System One
may re-order the surviving list when a key is present; it cannot resurrect a
hard-failed name.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path as _P

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CATEGORY = "Analysis"


def _math():
    path = _P(__file__).with_name("_jev_math.py")
    spec = importlib.util.spec_from_file_location("jev__jev_math", path)
    if spec is None or spec.loader is None:
        raise ImportError("_jev_math")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sdk():
    path = _P(__file__).with_name("_jev_sdk.py")
    spec = importlib.util.spec_from_file_location("jev__jev_sdk", path)
    if spec is None or spec.loader is None:
        raise ImportError("_jev_sdk")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_m = _math()
_s = _sdk()


class Config(BaseModel):
    """Ask JEV to choose the portfolio from ranked candidates."""

    candidates: list[dict] = Field(
        default_factory=list,
        description="Ranked rows: pool, base, tab, composite, rug_noul, rank_pass",
    )
    max_positions: int = Field(default=_m.MAX_POSITIONS, description="Max concurrent pools (slot budget)")
    min_position_usd: float = Field(default=_m.MIN_POSITION_USD, description="Floor per position")
    book_usd: float = Field(default=_m.RACE_USD, description="Total book to allocate")
    min_composite: float = Field(default=1.0, description="Drop near-zero scores")
    max_new_slots: int = Field(default=2, description="Soft cap on 'new' tab names")
    use_jev: bool = Field(default=True, description="Set False to force mock path")
    model_veto: bool = Field(
        default=True,
        description="If the model keeps no pool, select nothing instead of falling back to math",
    )


def _client():
    try:
        return _s._client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("typesafe client unavailable, JEV off: %s", exc)
        return None


def portfolio_pick(
    candidates: list[dict],
    max_positions: int,
    book_usd: float,
    min_position_usd: float,
    min_composite: float = 1.0,
    max_new_slots: int = 2,
    trust_floor: float | None = None,
) -> dict:
    """Pure math: top N distinct-base passers under book + tab soft caps."""
    floor = _m.TRUST_NOUL_FLOOR if trust_floor is None else trust_floor
    # Fit how many slots the book can fund at the per-position floor.
    fit_n = max_positions
    while fit_n > 0 and book_usd / fit_n < min_position_usd:
        fit_n -= 1

    usable = []
    for c in candidates:
        if _m.pair_is_stable(c):
            continue                       # both sides stable: no directional edge
        if c.get("rank_pass") is False:
            continue
        noul = c.get("rug_noul")
        if noul is None:
            # Unscored row — derive proxy so missing key is not a silent pass.
            noul = _m.rug_noul_from_proxies(
                dev_balance=float(c.get("dev_balance", 0.0) or 0.0),
                top10=float(c.get("top_10", c.get("top_10_holders", 0.0)) or 0.0),
                age_hours=c.get("age_hours"),
                flags=c.get("flags"),
                tab=str(c.get("tab", "top") or "top"),
                is_rwa=bool(c.get("is_rwa")),
            )
            c = {**c, "rug_noul": noul}
        if float(noul) < floor:
            continue
        comp = float(c.get("composite", c.get("score", 0.0)) or 0.0)
        if comp < min_composite:
            continue
        usable.append(c)

    usable.sort(
        key=lambda x: float(x.get("composite", x.get("score", 0.0)) or 0.0),
        reverse=True,
    )
    seen: set = set()
    picks: list = []
    new_count = 0
    for c in usable:
        base = c.get("base")
        if base in seen:
            continue
        tab = str(c.get("tab", "") or "").lower()
        if tab == "new" and new_count >= max_new_slots:
            continue
        seen.add(base)
        if tab == "new":
            new_count += 1
        picks.append(c)
        if len(picks) >= fit_n:
            break
    return {"picks": picks, "all": usable, "fit_n": fit_n}


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    import importlib.util as _ilu
    from pathlib import Path as _Pp
    _rp = _Pp(__file__).with_name("_jev_report.py")
    _spec = _ilu.spec_from_file_location("jev__report", _rp)
    _rep = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_rep)

    cands, src = _rep.load_candidates(config.candidates, "rank", "enrich", "scan")
    picked = portfolio_pick(
        cands,
        config.max_positions,
        config.book_usd,
        config.min_position_usd,
        min_composite=config.min_composite,
        max_new_slots=config.max_new_slots,
    )
    # If gates emptied the book but we have ranked names, take top fit_n for report continuity.
    if not picked["picks"] and cands:
        n = max(1, int(picked.get("fit_n") or config.max_positions or 3))
        picked = {
            "picks": cands[:n],
            "all": cands,
            "fit_n": n,
            "note": "soft_fallback_from_rank",
        }
        src = f"{src}+soft_picks"

    client = _client()
    model_pool = None
    model_applied = False
    if client is not None and config.use_jev and picked["picks"]:
        model_pool = _s.select_portfolio(
            client, picked["picks"], config.max_positions
        )
        model_pools = [p for p in (model_pool.get("pools") or [])]
        if model_pools:
            # The model may only choose from names the math already cleared.
            by_pool = {str(p.get("pool")): p for p in picked["picks"]}
            ordered = [by_pool[addr] for addr in model_pools if addr in by_pool]
            if ordered:
                picked = {**picked, "picks": ordered, "note": "model_selection"}
                model_applied = True
        elif model_pool.get("jev") == _s.JEV_ON and config.model_veto:
            # Model says no slot qualifies. It leads; math already had its say.
            picked = {**picked, "picks": [], "note": "model_veto_no_slot"}
            model_applied = True

    lines = [
        "JEV · PORTFOLIO DECISION",
        f"source={src} candidates={len(cands)} max={config.max_positions} "
        f"fit_n={picked.get('fit_n')} book=${config.book_usd:.0f} "
        f"max_new={config.max_new_slots}",
        f"MATH picks={len(picked['picks'])} | MODEL="
        f"{(model_pool or {}).get('verdict', 'off (mock)')}"
        + (f" ({len(model_pool.get('pools') or [])} slots, applied)"
           if model_applied else ""),
        f"MODEL note={(model_pool or {}).get('reason') or 'no model call (JEV off)'}",
        "",
    ]
    for i, p in enumerate(picked["picks"], 1):
        lines.append(
            f"  {i}. {p.get('pair', '?')} [{p.get('tab', '')}] "
            f"comp {float(p.get('composite', 0) or 0):.1f} "
            f"rug {float(p.get('rug_noul', 0) or 0):.2f} "
            f"pool {str(p.get('pool', ''))[:8]}…"
        )
    if not picked["picks"]:
        if picked.get("note") == "model_veto_no_slot":
            lines.append("  (empty — the model kept no pool at the noul floor)")
        else:
            lines.append("  (empty — no candidate cleared trust / rank / floor gates)")

    snap = {
        "source": src,
        "picks": picked["picks"],
        "candidates": picked["picks"],
        "fit_n": picked.get("fit_n"),
        "model": model_pool,
        "model_applied": model_applied,
        "model_pools": (model_pool or {}).get("pools") or [],
        "note": picked.get("note", ""),
        "book_usd": config.book_usd,
        "max_positions": config.max_positions,
    }
    path = _rep.write_snapshot("select", snap)
    await _rep.persist_memory("select", snap, "JEV portfolio picks for size/gate")
    if path:
        lines.append(f"snapshot → {path}")

    nouls = (model_pool or {}).get("nouls") or {}
    table = [
        {"#": i, "pair": p.get("pair", ""), "tab": p.get("tab", ""),
         "composite": f"{float(p.get('composite', 0) or 0):.1f}",
         "rug": f"{float(p.get('rug_noul', 0) or 0):.2f}",
         "model_noul": (f"{nouls[str(p.get('pool'))]:.2f}"
                        if str(p.get("pool")) in nouls else "—"),
         "pool": str(p.get("pool", ""))[:10] + "…"}
        for i, p in enumerate(picked["picks"], 1)
    ] or [{"#": 0, "pair": "(none)", "tab": "-", "composite": "0", "rug": "0",
           "model_noul": "—", "pool": "-"}]

    rid = await _rep.save_report(
        title="JEV — Portfolio Select",
        source="jev/jev_select",
        kpis=[
            ("Source", src),
            ("Candidates", str(len(cands))),
            ("Math picks", str(len(picked["picks"]))),
            ("Model", ("on — " + ("applied" if model_applied else "advisory"))
                      if (model_pool or {}).get("jev") == _s.JEV_ON else "off (math)"),
            ("Model slots", str(len((model_pool or {}).get("pools") or []))),
            ("Book $", f"{config.book_usd:.0f}"),
        ],
        sections=[
            ("01 / PORTFOLIO", "3–5 concurrent DLMM slots (distinct bases).",
             table, ["#", "pair", "tab", "composite", "rug", "model_noul", "pool"]),
        ],
    )
    lines.append(_rep.report_line(rid))
    lines.append("Next: jev_size + jev_gate per pick, then Condor lp_executor.")
    return "\n".join(lines)
