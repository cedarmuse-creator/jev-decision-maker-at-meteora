"""JEV Score — size a pool AND choose its range width.

Single model decision per pool: how much of the book (pct) + how wide the
range should be (-> bin count), given depth, heat, rug cleanliness, and the
open-cost worth tier. The tier is a size trim, not a veto: fee >= worth_margin x
open keeps the full slice, down to the floor keeps a trimmed slice, below the
floor there is no slice at all.

bin_step is FIXED per pool on-chain; JEV chooses width, not bin_step.
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


def _report():
    path = _P(__file__).with_name("_jev_report.py")
    spec = importlib.util.spec_from_file_location("jev__report", path)
    if spec is None or spec.loader is None:
        raise ImportError("_jev_report")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _live_sol_usd() -> float:
    """0 means 'use the live price' — a stale constant skews every ratio."""
    try:
        return float(_report().live_sol_usd())
    except Exception:  # noqa: BLE001
        return 150.0


_m = _math()
_s = _sdk()


class Config(BaseModel):
    """Size one pool: % of book + range width + open/fee viability."""
    role: str = Field(default="portfolio", description="portfolio | major | minor")
    tvl: float = Field(default=0.0, description="Pool TVL (USD)")
    vol24: float = Field(default=0.0, description="Pool 24h volume (USD)")
    bin_step: float = Field(default=8.0, description="On-chain bin step (fixed per pool)")
    dynamic_fee_pct: float = Field(default=0.0, description="Pool dynamic fee %")
    outside_slots: int = Field(default=0)
    rug_noul: float = Field(default=1.0, description="Rug cleanliness 0..1")
    pool: str = Field(default="", description="Pool address — picks up the scouted trend read")
    base: str = Field(default="", description="Base mint")
    base_symbol: str = Field(default="", description="Base symbol (stable-pair guard)")
    quote: str = Field(default="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", description="Quote mint")
    quote_symbol: str = Field(default="USDC", description="Quote symbol (stable-pair guard)")
    momentum_pct: float | None = Field(default=None, description="24h % change; None = read from snapshots")
    momentum_short_pct: float | None = Field(default=None, description="6h % change; can only veto the trend")
    vol_daily_pct: float = Field(default=2.0, description="Est. daily price move %")
    sol_usd: float = Field(default=0.0, description="SOL price for open-cost USD (0 = live)")
    book_usd: float = Field(default=_m.RACE_USD, description="Total book the pct is a slice of")
    worth_margin: float = Field(default=1.2, description="Fee/open-cost ratio for a full-size slice")
    min_position_usd: float = Field(default=_m.MIN_POSITION_USD, description="Smallest position worth opening")
    use_jev: bool = Field(default=True, description="Set False to force the math path")


def _client():
    try:
        return _s._client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("typesafe client unavailable, JEV off: %s", exc)
        return None


def _snapshot_momentum(pool: str, base: str) -> tuple[float | None, float | None]:
    """The trend read already scouted for this pool/base, from the snapshots.

    The scout fetched it from Jupiter alongside the rug card, so size can credit
    the trend ride without a second network hop. Absent -> (None, None), which is
    neutral and leaves the fee-only behaviour untouched.
    """
    try:
        rep = _report()
    except Exception:  # noqa: BLE001
        return None, None
    for name in ("enrich", "rank", "select", "scan"):
        try:
            snap = rep.read_snapshot(name) or {}
        except Exception:  # noqa: BLE001
            continue
        for r in (snap.get("candidates") or []):
            if not isinstance(r, dict) or r.get("momentum_pct") is None:
                continue
            if pool and str(r.get("pool") or "") == str(pool):
                return r.get("momentum_pct"), r.get("momentum_short_pct")
            if base and str(r.get("base") or "") == str(base):
                return r.get("momentum_pct"), r.get("momentum_short_pct")
    return None, None


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    sol_usd = config.sol_usd if config.sol_usd and config.sol_usd > 0 else _live_sol_usd()
    momentum, momentum_short = config.momentum_pct, config.momentum_short_pct
    if momentum is None:
        momentum, momentum_short = _snapshot_momentum(config.pool, config.base)
    sym = {"base_mint": config.base, "base_symbol": config.base_symbol,
           "quote_mint": config.quote, "quote_symbol": config.quote_symbol}
    # Math baseline: score-derived pct, width sweep, open-cost worth tier.
    math_out = _m.jev_size(
        role=config.role, tvl=config.tvl, vol24=config.vol24, bin_step=config.bin_step,
        dynamic_fee_pct=config.dynamic_fee_pct, outside_slots=config.outside_slots,
        rug_noul=config.rug_noul, vol_daily_pct=config.vol_daily_pct,
        sol_usd=sol_usd, book_usd=config.book_usd,
        worth_margin=config.worth_margin,
        momentum_pct=momentum, momentum_short_pct=momentum_short, **sym,
    )

    # Model Score: % of the book. Falls back to math when JEV is off.
    client = _client() if config.use_jev else None
    model = None
    if client is not None:
        model = _s.size_position(
            client, role=config.role, tvl=config.tvl, vol24=config.vol24,
            bin_step=config.bin_step, dynamic_fee_pct=config.dynamic_fee_pct,
            outside_slots=config.outside_slots, rug_noul=config.rug_noul,
            math_pct=math_out["pct"],
        )

    out = math_out
    source = "math"
    if model is not None and model.get("jev") == _s.JEV_ON:
        # Re-run the envelope + width + worth trim on the model's amount.
        out = _m.jev_size(
            role=config.role, tvl=config.tvl, vol24=config.vol24, bin_step=config.bin_step,
            dynamic_fee_pct=config.dynamic_fee_pct, outside_slots=config.outside_slots,
            rug_noul=config.rug_noul, vol_daily_pct=config.vol_daily_pct,
            sol_usd=sol_usd, book_usd=config.book_usd,
            pct_override=model["pct"], worth_margin=config.worth_margin,
            momentum_pct=momentum, momentum_short_pct=momentum_short, **sym,
        )
        source = "model"

    tier = out.get("worth_tier") or ("GO" if out.get("worth") else "NO")
    pool_usd = out.get("size_usd", out["pct"] * config.book_usd)
    size_ok = pool_usd >= config.min_position_usd
    # The worth tier trims; it only stops the open when the fee cannot pay for
    # the position at all, or the trimmed slice is below the position floor.
    can_open = bool(out["pct"] > 0 and size_ok and tier != "NO")
    lines = [
        f"JEV SIZE · {config.role}",
        f"pct={out['pct']:.3f}  score={out['score']:.1f}  ({source})  "
        f"book=${config.book_usd:.0f} -> pool_usd=${pool_usd:.2f}",
        f"width_pct={out['width_pct']:.3f}  bins={out.get('bin_count', 0)}",
        f"open_cost=${out.get('open_cost', 0):.2f}  expected_fee=${out.get('expected_fee', 0):.2f}  "
        f"ratio={out.get('worth_ratio')}  tier={tier}",
        f"trend={momentum if momentum is not None else 'n/a'}  "
        f"spread_credit=${float(out.get('spread_credit') or 0):.2f}  "
        f"income=${float(out.get('income') or 0):.2f} (fees + credited spread)",
        f"viable_size={size_ok} (min ${config.min_position_usd:.0f})  -> "
        f"{'OPEN' if can_open else 'SIT'}",
        f"jev={model.get('jev') if model else _s.JEV_OFF}"
        + (f"  model_score={model.get('score')} conf={model.get('confidence')}"
           if model else ""),
        (model or {}).get("reason") or "",
        out["reason"],
    ]
    if model is not None and model.get("jev") == _s.JEV_ON and source == "math":
        lines.append(f"model proposed pct={model['pct']:.3f} but math envelope kept {out['pct']:.3f}")
    import importlib.util as _ilu
    from pathlib import Path as _Pp
    _rp = _Pp(__file__).with_name("_jev_report.py")
    _spec = _ilu.spec_from_file_location("jev__report", _rp)
    _rep = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_rep)
    snap = {"role": config.role, "tvl": config.tvl, "vol24": config.vol24,
            "bin_step": config.bin_step, "rug_noul": config.rug_noul,
            "book_usd": config.book_usd, "pool_usd": round(pool_usd, 2),
            "pct_source": source, "can_open": can_open, "viable_size": size_ok,
            "model": model, **out}
    path = _rep.write_snapshot("size", snap)
    await _rep.persist_memory("size", snap, "JEV size decision")
    if path:
        lines.append(f"snapshot → {path}")
    rid = await _rep.save_report(
        title="JEV — Pool Size & Width",
        source="jev/jev_size",
        kpis=[
            ("Role", config.role),
            ("% of book", f"{out['pct']:.1%}"),
            ("Pool $", f"{pool_usd:.0f}"),
            ("JEV", "on (model)" if source == "model" else "off (math)"),
            ("Width", f"{out['width_pct']:.2f}%"),
            ("Bins", str(out.get("bin_count", 0))),
            ("Worth", f"{tier} ({out.get('worth_ratio')}x)"),
            ("Open?", "YES" if can_open else "NO"),
        ],
        sections=[
            ("01 / DECISION", "Size + width + open-cost tier.",
             [{"field": k, "value": str(v)} for k, v in [
                 ("pct", f"{out['pct']:.3f}"),
                 ("pct_source", source),
                 ("pool_usd", f"{pool_usd:.2f}"),
                 ("min_position_usd", config.min_position_usd),
                 ("viable_size", size_ok),
                 ("model_score", (model or {}).get("score")),
                 ("model_confidence", (model or {}).get("confidence")),
                 ("model_pct", (model or {}).get("pct")),
                 ("score", out["score"]),
                 ("width_pct", out["width_pct"]),
                 ("bin_count", out.get("bin_count")),
                 ("open_cost", out.get("open_cost")),
                 ("expected_fee", out.get("expected_fee")),
                 ("worth_tier", tier),
                 ("worth_ratio", out.get("worth_ratio")),
                 ("momentum_pct", momentum),
                 ("trend_credit", out.get("trend_credit")),
                 ("spread_credit", out.get("spread_credit")),
                 ("income_fees_plus_spread", out.get("income")),
                 ("worth_margin", config.worth_margin),
                 ("reason", out["reason"]),
                 ("tvl", config.tvl),
                 ("vol24", config.vol24),
                 ("rug_noul", config.rug_noul),
                 ("book_usd", config.book_usd),
             ]],
             ["field", "value"]),
        ],
    )
    lines.append(_rep.report_line(rid))
    return "\n".join(lines)
