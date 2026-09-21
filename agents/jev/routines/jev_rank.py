"""JEV Rank — composite cross-tab score for the candidate universe.

Takes candidates from jev_scan and produces the ranking JEV uses to pick the
portfolio. Scoring is pure `_jev_math.jev_rank_row`:

  HARD filters (fail → composite 0, excluded from top slice)
    - TVL / volume floors
    - bin_step validity
    - rug_noul / trust floor (from card or holder/age proxies)
    - wash-tape (vol ≫ TVL)
    - any red rug flags when present

  SOFT score (economy first, safety weighted in)
    - fee yield (realized fees/TVL when known, else vol×fee)
    - depth (log TVL)
    - cleanliness (rug_noul)
    - tab multiplier (trending boost, new penalty, RWA steady)

TVL / VOL / Fee remain the yield engine. They do not override a failed gate.
System One may later re-slice the top; math is the baseline.
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


_m = _math()


class Config(BaseModel):
    """Rank a candidate universe. Output sorted list with composite + rug_noul."""

    candidates: list[dict] = Field(
        default_factory=list,
        description="Rows from jev_scan: pool, base, tvl, vol, fees, bin_step, tab, …",
    )
    top_n: int = Field(default=8, description="How many passers to return ranked")
    max_bin_step: float = Field(default=400.0)
    min_tvl: float = Field(default=20_000.0)
    min_vol: float = Field(default=5_000.0)
    trust_floor: float = Field(default=0.30)


def _age_hours(r: dict) -> float | None:
    if r.get("age_hours") is not None:
        try:
            return float(r["age_hours"])
        except (TypeError, ValueError):
            return None
    return None


def score_candidate(r: dict, cfg: Config) -> dict:
    """Annotate one row with composite / rug_noul / pass. Mutates and returns r."""
    if _m.pair_is_stable(r):
        # Both sides stable: no directional edge, so it can be neither a trend
        # ride nor a dip buy. The scan drops these at parse time; this is the
        # second line of defence for rows that arrived from a snapshot.
        r["composite"] = 0.0
        r["score"] = 0.0
        r["yield"] = 0.0
        r["depth"] = 0.0
        r["rug_noul"] = float(r.get("rug_noul") or 0.0)
        r["rank_pass"] = False
        r["rank_reason"] = "hard_fail:stable_pair"
        return r
    out = _m.jev_rank_row(
        tvl=float(r.get("tvl", 0.0) or 0.0),
        vol24=float(r.get("vol", r.get("vol24", 0.0)) or 0.0),
        bin_step=float(r.get("bin_step", 0.0) or 0.0),
        fee_rate_pct=float(r.get("fee_rate_pct", 0.0) or 0.0),
        fees24=float(r.get("fees", r.get("fees24", 0.0)) or 0.0),
        tab=str(r.get("tab", "top") or "top"),
        is_rwa=bool(r.get("is_rwa")),
        dev_balance=float(r.get("dev_balance", 0.0) or 0.0),
        top10=float(r.get("top_10", r.get("top_10_holders", 0.0)) or 0.0),
        age_hours=_age_hours(r),
        flags=r.get("flags"),
        rug_noul=(float(r["rug_noul"]) if r.get("rug_noul") is not None else None),
        min_tvl=cfg.min_tvl,
        min_vol=cfg.min_vol,
        max_bin_step=cfg.max_bin_step,
        trust_floor=cfg.trust_floor,
    )
    r["composite"] = out["composite"]
    r["rug_noul"] = out["rug_noul"]
    r["rank_pass"] = out["pass"]
    r["rank_reason"] = out["reason"]
    r["yield"] = out["yield"]
    r["depth"] = out["depth"]
    r["score"] = out["composite"]  # back-compat for older callers
    return r


def rank_universe(candidates: list[dict], cfg: Config | None = None) -> dict:
    """Pure entry: score, filter passers, sort, return top_n + rejects summary."""
    cfg = cfg or Config()
    scored = [score_candidate(dict(r), cfg) for r in candidates]
    passed = [r for r in scored if r.get("rank_pass")]
    failed = [r for r in scored if not r.get("rank_pass")]
    passed.sort(key=lambda r: float(r.get("composite", 0.0)), reverse=True)
    top = passed[: cfg.top_n]
    return {"top": top, "passed": passed, "failed": failed, "scored": scored}


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    import importlib.util as _ilu
    from pathlib import Path as _Pp
    _rp = _Pp(__file__).with_name("_jev_report.py")
    _spec = _ilu.spec_from_file_location("jev__report", _rp)
    _rep = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_rep)

    cands, src = _rep.load_candidates(config.candidates, "enrich", "scan")
    result = rank_universe(cands, config)
    top = result["top"]
    failed = result["failed"]
    # Never leave Condor with an empty rank table when demo/snapshot data exists.
    if not top and cands:
        # re-score without hard fail for display only — still mark pass false
        scored = result["scored"] or cands
        scored = sorted(scored, key=lambda r: float(r.get("composite", r.get("score", 0)) or 0), reverse=True)
        top = scored[: config.top_n]
        src = f"{src}+soft_display"

    lines = [
        "JEV RANK · yield + safety (hard gate, then score)",
        f"source={src} universe={len(cands)} passed={len(result['passed'])} "
        f"failed={len(failed)} returning={len(top)}",
        "",
        "rank  composite  rug   yld   tab    pair                 why",
    ]
    for i, r in enumerate(top, 1):
        lines.append(
            f"{i:<4} {float(r.get('composite', 0) or 0):6.2f}     {float(r.get('rug_noul', 0) or 0):.2f}  "
            f"{float(r.get('yield', 0) or 0):.2f}  {str(r.get('tab', '')):5}  "
            f"{str(r.get('pair', '')):18}  {r.get('rank_reason', '')}"
        )
    if not top:
        lines.append("none — no candidate cleared hard filters")
    if failed:
        from collections import Counter
        reasons = Counter()
        for r in failed:
            for part in str(r.get("rank_reason", "")).replace("hard_fail:", "").split(","):
                if part:
                    reasons[part] += 1
        top_fail = ", ".join(f"{k}×{v}" for k, v in reasons.most_common(6))
        lines.append(f"rejects ({len(failed)}): {top_fail}")

    snap = {
        "source": src,
        "top": top,
        "passed": result["passed"],
        "failed": [{"pair": r.get("pair"), "reason": r.get("rank_reason")} for r in failed[:30]],
        "candidates": top,  # next stage default
    }
    path = _rep.write_snapshot("rank", snap)
    await _rep.persist_memory("rank", snap, "JEV ranked candidates for select")
    if path:
        lines.append(f"snapshot → {path}")

    table = [
        {"rank": i, "pair": r.get("pair", ""), "composite": f"{float(r.get('composite', 0) or 0):.1f}",
         "rug": f"{float(r.get('rug_noul', 0) or 0):.2f}", "tab": r.get("tab", ""),
         "tvl": f"{float(r.get('tvl', 0) or 0):.0f}", "why": r.get("rank_reason", "")}
        for i, r in enumerate(top, 1)
    ] or [{"rank": 0, "pair": "(none)", "composite": "0", "rug": "0", "tab": "-", "tvl": "0", "why": "empty"}]

    rid = await _rep.save_report(
        title="JEV — Cross-Tab Rank",
        source="jev/jev_rank",
        kpis=[
            ("Source", src),
            ("Universe", str(len(cands))),
            ("Passed", str(len(result["passed"]))),
            ("Returned", str(len(top))),
        ],
        sections=[
            ("01 / TOP", "Hard-pass ranked slice for select.",
             table, ["rank", "pair", "composite", "rug", "tab", "tvl", "why"]),
        ],
    )
    lines.append(_rep.report_line(rid))
    lines.append("Pass top → jev_select (snapshot:rank).")
    return "\n".join(lines)
