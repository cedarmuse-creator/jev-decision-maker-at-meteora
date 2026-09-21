"""JEV Enrich — live eight-flag scout on top candidates before rank/select.

Takes the scan universe (or a pre-sliced list), runs the rug card on the best
proxy-scored names (unique bases), and writes:

  flags, rug_noul, scout_allowed, scout_red, creator_pct, sell_ok, scout_source

onto each row. Rank then hard-fails any red card. Fail closed on fetch errors
for that mint (blocked card), so a dead API cannot green-light a pool.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path as _P

import aiohttp
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


def _scout():
    path = _P(__file__).with_name("jev_scout_card.py")
    spec = importlib.util.spec_from_file_location("jev_scout_card_mod", path)
    if spec is None or spec.loader is None:
        raise ImportError("jev_scout_card")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_m = _math()
_sc = _scout()


class Config(BaseModel):
    """Stamp live rug cards onto the best scan candidates."""

    candidates: list[dict] = Field(
        default_factory=list,
        description="Rows from jev_scan (pool, base mint, tvl, vol, tab, …)",
    )
    top_k: int = Field(default=12, description="How many unique bases to scout")
    concurrency: int = Field(default=4, description="Parallel scout fetches")
    scout_usd: float = Field(default=20.0)
    creator_cap_pct: float = Field(default=30.0)
    output_path: str = Field(
        default="",
        description="If set, write enriched candidates JSON here for the next routine",
    )


def _proxy_key(r: dict) -> float:
    """Cheap pre-order so we scout the most promising names first."""
    if r.get("composite") is not None:
        return float(r.get("composite") or 0.0)
    if r.get("score") is not None:
        return float(r.get("score") or 0.0)
    tvl = float(r.get("tvl") or 0.0)
    vol = float(r.get("vol") or r.get("vol24") or 0.0)
    return (tvl ** 0.5) * (1.0 + vol / max(tvl, 1.0))


def pick_scout_targets(candidates: list[dict], top_k: int) -> list[dict]:
    """Unique-base slice, best proxy score first."""
    ordered = sorted(candidates, key=_proxy_key, reverse=True)
    seen: set[str] = set()
    out: list[dict] = []
    for r in ordered:
        base = str(r.get("base") or "")
        if not base or base in seen:
            continue
        # Skip pure quote mints as "base"
        if len(base) < 20 and base.upper() in {"USDC", "SOL", "USDT"}:
            continue
        seen.add(base)
        out.append(r)
        if len(out) >= top_k:
            break
    return out


async def enrich_candidates(
    candidates: list[dict],
    *,
    top_k: int = 12,
    concurrency: int = 4,
    scout_usd: float = 20.0,
    creator_cap_pct: float = 30.0,
) -> dict:
    """Return {candidates, scouted, allowed, blocked, errors} with cards applied."""
    rows = [dict(c) for c in candidates]
    # A pool address appears once per tab it is listed under, and one token can
    # have several pools. `pick_scout_targets` dedups by mint because one card per
    # NAME is what the scout buys -- but the result must then be stamped onto
    # every row carrying that pool address or that base mint. Stamping only the
    # row a `{pool: row}` map happened to keep left the other copy with no card
    # at all, so its clean proxy default could reach select while its twin was red.
    by_pool: dict[str, list[dict]] = {}
    by_base: dict[str, list[dict]] = {}
    for _r in rows:
        if _r.get("pool"):
            by_pool.setdefault(str(_r["pool"]), []).append(_r)
        if _r.get("base"):
            by_base.setdefault(str(_r["base"]), []).append(_r)
    targets = pick_scout_targets(rows, top_k)
    if not targets:
        return {
            "candidates": rows, "scouted": 0, "allowed": 0, "blocked": 0,
            "errors": 0, "targets": [],
        }

    sem = asyncio.Semaphore(max(1, concurrency))
    scouted = allowed = blocked = errors = 0

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=25),
        headers={"Accept": "application/json"},
    ) as session:

        async def one(row: dict) -> None:
            nonlocal scouted, allowed, blocked, errors
            mint = str(row.get("base") or "")
            pool = str(row.get("pool") or "")
            async with sem:
                try:
                    result = await _sc.evaluate_mint_pool(
                        session,
                        mint=mint,
                        pool=pool,
                        scout_usd=scout_usd,
                        creator_cap_pct=creator_cap_pct,
                        tvl_hint=float(row.get("tvl") or 0.0) or None,
                        vol_hint=float(row.get("vol") or row.get("vol24") or 0.0) or None,
                        bin_step_hint=float(row.get("bin_step") or 0.0) or None,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("enrich scout failed %s: %s", mint[:8], exc)
                    errors += 1
                    # Fail closed: synthetic all-blocking card.
                    flags = {k: True for k in _m.FLAG_KEYS}
                    result = {
                        "flags": flags, "creator_pct": None, "sell_ok": False,
                        "allowed": False, "reason": f"error:{exc}",
                    }
            copies: list[dict] = []
            for group in (by_pool.get(pool, []), by_base.get(mint, [])):
                for c in group:
                    if c not in copies:
                        copies.append(c)
            if not copies:
                copies = [row]
            for target in copies:
                _m.apply_scout_to_candidate(
                    target,
                    flags=result["flags"],
                    creator_pct=result.get("creator_pct"),
                    sell_ok=result.get("sell_ok"),
                    scout_source="live",
                )
                target["scout_reason"] = result.get("reason", "")
                # Carry the trend read onto every copy too, so size can credit
                # the round trip without another lookup.
                target["momentum_pct"] = result.get("momentum_pct")
                target["momentum_short_pct"] = result.get("momentum_short_pct")
                # A stable base quoted in a stable is a stable-stable pair; flag
                # it from the venue's own tag so rank/select refuse it even if
                # the scan's symbol list missed it.
                if result.get("base_is_stable"):
                    target["base_is_stable"] = True
                    target["stable_pair"] = _m.pair_is_stable(target)
            scouted += 1
            if copies[0].get("scout_allowed"):
                allowed += 1
            else:
                blocked += 1

        await asyncio.gather(*(one(t) for t in targets))

    return {
        "candidates": rows,
        "scouted": scouted,
        "allowed": allowed,
        "blocked": blocked,
        "errors": errors,
        "targets": [
            {
                "pair": r.get("pair"), "base": str(r.get("base", ""))[:8],
                "allowed": r.get("scout_allowed"), "rug_noul": r.get("rug_noul"),
                "red": r.get("scout_red"), "tab": r.get("tab"),
            }
            for r in rows if r.get("scout_source") == "live"
        ],
    }


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    import importlib.util as _ilu
    from pathlib import Path as _Pp
    _rp = _Pp(__file__).with_name("_jev_report.py")
    _spec = _ilu.spec_from_file_location("jev__report", _rp)
    _rep = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_rep)

    cands, src = _rep.load_candidates(config.candidates, "scan")
    result = await enrich_candidates(
        cands,
        top_k=config.top_k,
        concurrency=config.concurrency,
        scout_usd=config.scout_usd,
        creator_cap_pct=config.creator_cap_pct,
    )
    # If live scout produced zero targets (empty list / total API fail), keep demo cards stamped.
    if not result.get("targets") and src in ("demo", "snapshot:scan"):
        # stamp demo rows with clean cards so rank still has rows
        stamped = []
        for r in cands:
            row = dict(r)
            row.setdefault("flags", {k: False for k in _m.FLAG_KEYS})
            row.setdefault("rug_noul", float(r.get("rug_noul") or 0.9))
            row.setdefault("allowed", True)
            row.setdefault("red", [])
            row.setdefault("scout_source", "demo")
            stamped.append(row)
        result = {
            "candidates": stamped,
            "targets": stamped[: config.top_k],
            "scouted": len(stamped[: config.top_k]),
            "allowed": len(stamped[: config.top_k]),
            "blocked": 0,
            "errors": 0,
        }

    lines = [
        "JEV ENRICH · live rug cards before rank",
        f"source={src} universe={len(cands)} scouted={result['scouted']} "
        f"allowed={result['allowed']} blocked={result['blocked']} "
        f"errors={result['errors']} top_k={config.top_k}",
        "",
        "pair                 tab    rug   allowed  red",
    ]
    for t in result["targets"]:
        red = ",".join(t.get("red") or []) or "—"
        lines.append(
            f"{str(t.get('pair') or '?'):20} {str(t.get('tab') or ''):6} "
            f"{float(t.get('rug_noul') or 0):.2f}  "
            f"{'yes' if t.get('allowed') else 'NO ':3}     {red}"
        )
    if not result["targets"]:
        lines.append("(no targets — empty candidate list)")

    snap = {
        "source": src,
        "scouted": result["scouted"],
        "allowed": result["allowed"],
        "blocked": result["blocked"],
        "errors": result.get("errors", 0),
        "candidates": result["candidates"],
        "targets": result["targets"],
    }
    path = _rep.write_snapshot("enrich", snap)
    await _rep.persist_memory("enrich", snap, "JEV enriched candidates for rank")
    if path:
        lines.append(f"snapshot → {path}")

    out_path = (config.output_path or "").strip()
    if out_path:
        try:
            import json
            from pathlib import Path
            p = Path(out_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
            lines.append(f"wrote enriched candidates → {p}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("enrich write failed: %s", exc)
            lines.append(f"enrich write failed: {exc}")

    table = [
        {"pair": t.get("pair"), "tab": t.get("tab"),
         "rug": f"{float(t.get('rug_noul') or 0):.2f}",
         "allowed": "yes" if t.get("allowed") else "no",
         "red": ",".join(t.get("red") or []) or "—",
         "source": t.get("scout_source") or t.get("source") or src}
        for t in result["targets"]
    ] or [{"pair": "(none)", "tab": "-", "rug": "0", "allowed": "no", "red": "empty", "source": src}]

    rid = await _rep.save_report(
        title="JEV — Enrich (live rug cards)",
        source="jev/jev_enrich",
        kpis=[
            ("Source", src),
            ("Scouted", str(result["scouted"])),
            ("Allowed", str(result["allowed"])),
            ("Blocked", str(result["blocked"])),
        ],
        sections=[
            ("01 / RUG CARDS", "Live eight-flag cards before rank.",
             table, ["pair", "tab", "rug", "allowed", "red", "source"]),
        ],
    )
    lines.append(_rep.report_line(rid))
    lines.append("Pass candidates → jev_rank (snapshot:enrich or agent state).")
    return "\n".join(lines)
