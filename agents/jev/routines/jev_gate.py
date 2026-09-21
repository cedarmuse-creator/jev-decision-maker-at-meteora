"""One verb per pool (portfolio). Fee-vs-cost. No orders.

Decision Maker at Meteora gate: given a pool's CURRENT state, return WAIT /
SHIFT / REBUILD / SIT and the model-sized position amount. The amount comes
from `pool_pct` (a fraction of the book); there is no `pool_usd` input — it is
derived. The math gate still disposes on fee floor, churn, and the rug trust
floor. No second venue, one-sided only.
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
    """Print JEV verbs and the BUY/SELL bounds for one pool. No orders."""

    role: str = Field(default="portfolio", description="portfolio | major | minor (legacy)")
    wallet_usd: float = Field(default=_m.RACE_USD, description="Total book")
    pool_state: str = Field(default="NONE",
        description="The executor's CURRENT state on this pool — a state, never a "
                    "verb: NONE (no executor yet) | IN_RANGE | OUT_OF_RANGE | "
                    "FILLED_BUY. WAIT/SHIFT/REBUILD/SIT are this routine's OUTPUT "
                    "and are rejected as input.")
    pool_side: str = Field(default="NONE", description="BUY | SELL | NONE")
    price: float = Field(default=0.0)
    lower_price: float = Field(default=0.0)
    upper_price: float = Field(default=0.0)
    extra_fees_usd: float = Field(default=0.0)
    slip_usd: float = Field(default=2.0)
    priority_usd: float = Field(default=0.4)
    rent_usd: float = Field(default=0.6)
    outside_slots: int = Field(default=0)
    min_outside_slots: int = Field(default=3)
    dynamic_fee_pct: float = Field(default=0.0, description="Pool dynamic fee %")
    fee_floor_pct: float = Field(default=0.02)
    can_reuse_position: bool = Field(default=False)
    width_pct: float = Field(default=1.2)
    bin_step: float = Field(default=8.0)
    # Model-sized amount for this pool (pct of book -> usd).
    pool_pct: float = Field(default=0.0,
        description="JEV Score -> fraction of book, 0..1. This is the amount INPUT; "
                    "there is no pool_usd field — pool_usd is derived from this, so "
                    "passing pool_usd leaves the amount at $0.")
    tvl: float = Field(default=0.0)
    vol24: float = Field(default=0.0)
    rug_noul: float = Field(default=1.0)
    # Optional short-horizon leash (legacy minor / hot sleeve).
    hot_leash: bool = Field(default=False, description="If true, apply age/trust kill leash")
    position_age_sec: float = Field(default=0.0)
    flag_after: bool = Field(default=False)
    minor_live: bool = Field(default=False, description="legacy alias for hot_leash")
    minor_age_sec: float = Field(default=0.0, description="legacy alias")
    minor_flag_after: bool = Field(default=False, description="legacy alias")


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    state = (config.pool_state or "NONE").upper()
    side = (config.pool_side or "NONE").upper()
    # WAIT / SHIFT / REBUILD / SIT are the VERBS this routine RETURNS, not states
    # it accepts. Passing one used to fall through `has_position = state not in
    # {"NONE", ""}` -> True, take the "already holding" branch, and answer with a
    # plausible-looking SIT — a silent wrong answer. Refuse loudly instead.
    if state in {_m.WAIT, _m.SHIFT, _m.REBUILD, _m.SIT}:
        return (
            "JEV GATE · BAD INPUT\n"
            f"pool_state='{state}' is a VERB (this routine's output), not a state.\n"
            "Pass the executor's CURRENT state on this pool: NONE | IN_RANGE | "
            "OUT_OF_RANGE | FILLED_BUY.\n"
            "Opening a fresh wall with no existing executor => pool_state=\"NONE\"."
        )
    has_position = state not in {"NONE", ""}
    filled_buy = state == "FILLED_BUY"
    out = state == "OUT_OF_RANGE"

    verb = _m.pool_verb(
        has_position=has_position,
        filled_buy=filled_buy,
        position_side=side,
        out_of_range=out,
        outside_slots=config.outside_slots,
        extra_fees_usd=config.extra_fees_usd,
        slip_usd=config.slip_usd,
        priority_usd=config.priority_usd,
        rent_usd=config.rent_usd,
        dynamic_fee_pct=config.dynamic_fee_pct,
        fee_floor_pct=config.fee_floor_pct,
        can_reuse_position=config.can_reuse_position,
        min_slots=config.min_outside_slots,
    )

    # Optional short-horizon leash (hot / new sleeve).
    use_leash = config.hot_leash or config.minor_live or config.role in ("minor", "hot")
    age = config.position_age_sec or config.minor_age_sec
    flag_after = config.flag_after or config.minor_flag_after
    if use_leash and (config.minor_live or config.hot_leash or age > 0):
        leash = _m.minor_exit(rug_noul=config.rug_noul, age_sec=age,
                              flag_after=flag_after)
        if leash == _m.KILL:
            verb = _m.SIT

    pool_usd = max(0.0, config.wallet_usd * config.pool_pct)

    w = _m.clamp_width_pct(config.price, config.width_pct, config.bin_step, "BUY")
    buy_lo, buy_hi = _m.buy_only_bounds(config.price, w) if config.price > 0 else (0.0, 0.0)
    w_s = _m.clamp_width_pct(config.price, config.width_pct, config.bin_step, "SELL")
    sell_lo, sell_hi = _m.sell_only_bounds(config.price, w_s) if config.price > 0 else (0.0, 0.0)
    cost = config.slip_usd + config.priority_usd + config.rent_usd
    move_ok = _m.churn_ok(config.extra_fees_usd, config.slip_usd, config.priority_usd,
                          config.rent_usd, config.outside_slots, config.min_outside_slots)
    ticket = "same" if verb == _m.SHIFT else ("new" if verb == _m.REBUILD else "none")
    vol_ok = _m.vol_gate(config.dynamic_fee_pct, config.fee_floor_pct)

    lines = [
        f"JEV GATE · {config.role} pool · one verb",
        f"book=${config.wallet_usd:.2f} model_pct={config.pool_pct:.3f} -> pool_usd=${pool_usd:.2f}",
        f"pool_state={state} pool_side={side} can_reuse_position={config.can_reuse_position}",
        f"vol_gate dynamic_fee={config.dynamic_fee_pct}% floor={config.fee_floor_pct}% ok={vol_ok}",
        f"fee_vs_cost extra={config.extra_fees_usd:.2f} cost={cost:.2f} outside_slots={config.outside_slots} move_ok={move_ok}",
        f"BUY only  lo={buy_lo} hi={buy_hi}  (must be < P={config.price})",
        f"SELL only lo={sell_lo} hi={sell_hi}  (must be > P={config.price})",
        f"bins_buy={_m.bin_count(buy_lo, buy_hi, config.bin_step):.1f} bins_sell={_m.bin_count(sell_lo, sell_hi, config.bin_step):.1f} cap=69",
        f"VERB={verb} ticket={ticket}",
        "SHIFT = re-site to SELL-only one-sided AND reuse proven. REBUILD = stop keep_position=True + open SELL-only.",
        "Do not invent a bin. If VERB is REBUILD, do not write SHIFT in the journal.",
        "dry_run_writes default: print the create/stop, do not send it.",
    ]
    import importlib.util as _ilu
    from pathlib import Path as _Pp
    _rp = _Pp(__file__).with_name("_jev_report.py")
    _spec = _ilu.spec_from_file_location("jev__report", _rp)
    _rep = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_rep)
    snap = {
        "role": config.role, "verb": verb, "ticket": ticket, "pool_usd": pool_usd,
        "pool_pct": config.pool_pct, "state": state, "side": side,
        "buy": [buy_lo, buy_hi], "sell": [sell_lo, sell_hi],
        "vol_ok": vol_ok, "move_ok": move_ok, "price": config.price,
    }
    path = _rep.write_snapshot("gate", snap)
    await _rep.persist_memory("gate", snap, "JEV gate verb")
    if path:
        lines.append(f"snapshot → {path}")
    rid = await _rep.save_report(
        title="JEV — Pool Decision",
        source="jev/jev_gate",
        kpis=[
            ("Role", config.role),
            ("Verb", verb),
            ("Pool $", f"${pool_usd:.0f}"),
            ("Model %", f"{config.pool_pct:.1%}"),
            ("Ticket", ticket),
        ],
        sections=[
            ("01 / BOUNDS", "One-sided walls around live price.",
             [
                 {"side": "BUY-only", "lo": f"{buy_lo}", "hi": f"{buy_hi}", "must_be": f"< P={config.price}"},
                 {"side": "SELL-only", "lo": f"{sell_lo}", "hi": f"{sell_hi}", "must_be": f"> P={config.price}"},
             ], ["side", "lo", "hi", "must_be"]),
            ("02 / GATE CHECKS", "Math disposal before any write.",
             [
                 {"check": "vol fee >= floor", "value": f"{config.dynamic_fee_pct}% / {config.fee_floor_pct}%", "pass": "YES" if vol_ok else "NO"},
                 {"check": "fee >= cost", "value": f"extra {config.extra_fees_usd:.2f} vs cost {cost:.2f}", "pass": "YES" if move_ok else "NO"},
                 {"check": "outside slots", "value": f"{config.outside_slots} / {config.min_outside_slots}", "pass": "YES" if config.outside_slots >= config.min_outside_slots else "NO"},
                 {"check": "reuse position", "value": str(config.can_reuse_position), "pass": "YES" if config.can_reuse_position else "NO (REBUILD)"},
             ], ["check", "value", "pass"]),
        ],
    )
    lines.append(_rep.report_line(rid))
    return "\n".join(lines)
