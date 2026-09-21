"""Eight-flag rug card. Any red = no size. No orders.

Exposes `evaluate_mint_pool` so jev_enrich can stamp live flags + rug_noul onto
candidates before rank/select. The routine `run` stays the single-mint report.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path as _P

import aiohttp
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CATEGORY = "Analysis"

JUP_TOKEN = "https://lite-api.jup.ag/tokens/v2/search"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DLMM_PAIR = "https://dlmm.datapi.meteora.ag/pools"


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
    """Screen one mint / pool before any USDC leaves."""

    mint: str = Field(default="", description="Base token mint")
    pool_address: str = Field(default="", description="Meteora DLMM pool address")
    scout_usd: float = Field(default=20.0, description="Probe size in USDC")
    creator_cap_pct: float = Field(default=30.0, description="Creator pile red at/above this %")


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def _json(session: aiohttp.ClientSession, url: str, params: dict | None = None):
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status >= 400:
                return None
            return await resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("jev_scout_card fetch failed %s: %s", url, exc)
        return None


def _token_row(body, mint: str) -> dict:
    if isinstance(body, list):
        for row in body:
            if isinstance(row, dict) and str(row.get("id") or row.get("address") or "") == mint:
                return row
        return body[0] if body and isinstance(body[0], dict) else {}
    if isinstance(body, dict):
        return body
    return {}


def _auth(row: dict, *keys: str) -> str | None:
    """Legacy flat authority reader (kept for older payload shapes)."""
    for k in keys:
        v = row.get(k)
        if v in (None, "", "null", "None"):
            continue
        if isinstance(v, str) and v.lower() in {"null", "none", ""}:
            continue
        return str(v)
    return None


def _authority(row: dict, kind: str) -> str | None:
    """Authority state from either shape.

    Jupiter's v2 payload inverts this: it sends ``mintAuthorityDisabled`` /
    ``freezeAuthorityDisabled`` booleans, not the authority address. Reading only
    the legacy keys returned None for every token — which silently mislabelled a
    live mint authority as absent. A disabled authority means the flag is OFF
    (safe); an enabled one means the flag is ON (red).
    """
    audit = row.get("audit")
    audit = audit if isinstance(audit, dict) else {}
    disabled = audit.get(f"{kind}AuthorityDisabled")
    if isinstance(disabled, bool):
        return None if disabled else "enabled"
    # A token with no audit block at all: fall back to the flat keys.
    return _auth(row, kind, f"{kind}_authority")


async def evaluate_mint_pool(
    session: aiohttp.ClientSession,
    *,
    mint: str,
    pool: str = "",
    scout_usd: float = 20.0,
    creator_cap_pct: float = 30.0,
    tvl_hint: float | None = None,
    vol_hint: float | None = None,
    bin_step_hint: float | None = None,
) -> dict:
    """Live eight-flag evaluation. Returns structured fields for enrich/rank.

    Never places orders. On total failure, returns a blocked card (all-red-safe
    defaults) so callers fail closed.
    """
    mint = (mint or "").strip()
    pool = (pool or "").strip()
    if not mint:
        flags = {k: True for k in _m.FLAG_KEYS}
        return {
            "ok": False, "mint": mint, "pool": pool, "flags": flags,
            "rug_noul": 0.0, "allowed": False, "sell_ok": False,
            "creator_pct": None, "vol": 0.0, "tvl": 0.0, "bin_step": None,
            "reason": "mint_missing",
        }

    token_body = await _json(session, JUP_TOKEN, {"query": mint})
    token = _token_row(token_body, mint)
    # Momentum for the trend allowance. Jupiter's token payload already carries
    # it, so this costs no extra request. Absent reads stay None (neutral) --
    # defaulting them to 0.0 would fake a "flat market" reading.
    _s24 = token.get("stats24h") if isinstance(token.get("stats24h"), dict) else {}
    _s6 = token.get("stats6h") if isinstance(token.get("stats6h"), dict) else {}
    momentum_24h = _num(_s24.get("priceChange")) if _s24.get("priceChange") is not None else None
    momentum_6h = _num(_s6.get("priceChange")) if _s6.get("priceChange") is not None else None
    # The venue's own `stable` tag is the most reliable stablecoin read we get.
    _tags = token.get("tags")
    base_is_stable = bool(isinstance(_tags, list)
                          and any(str(t).strip().lower() == "stable" for t in _tags)) \
        or _m.is_stable(mint, str(token.get("symbol") or ""))
    probe = max(1, int(scout_usd * 1_000_000))
    buy = await _json(
        session,
        JUP_QUOTE,
        {"inputMint": USDC, "outputMint": mint, "amount": str(probe), "slippageBps": "150"},
    )
    out_amt = 0
    if isinstance(buy, dict):
        out_amt = int(_num(buy.get("outAmount")))
    sell_ok = False
    if out_amt > 0:
        sell = await _json(
            session,
            JUP_QUOTE,
            {"inputMint": mint, "outputMint": USDC, "amount": str(out_amt), "slippageBps": "150"},
        )
        sell_ok = isinstance(sell, dict) and _num(sell.get("outAmount")) > 0

    pair = None
    if pool:
        pair = await _json(session, f"{DLMM_PAIR}/{pool}")
        if isinstance(pair, dict) and "data" in pair and isinstance(pair["data"], dict):
            pair = pair["data"]

    mint_auth = _authority(token, "mint")
    freeze_auth = _authority(token, "freeze")
    audit = token.get("audit")
    top = audit if isinstance(audit, dict) else {}
    # Holder concentration across all top accounts. This is NOT the creator pile.
    top_pct = _num(top.get("topHoldersPercentage") or token.get("holderConcentration"))
    if top_pct > 1.5:
        top_holders_pct = top_pct if top_pct <= 100 else 100.0
    else:
        top_holders_pct = top_pct * 100.0 if top_pct else None
    # The creator/dev pile is `devBalancePercentage` (percent, 0-100). Reading
    # `topHoldersPercentage` as the creator pile flagged wrapped SOL -- 58% of
    # supply in top accounts is normal for SOL -- as a rug on every pool.
    dev_raw = top.get("devBalancePercentage")
    if dev_raw is None:
        _dev = token.get("dev")
        dev_raw = _dev.get("balancePercentage") if isinstance(_dev, dict) else None
    if mint in _m.BLUECHIP_MINTS:
        creator_pct = 0.0                      # bluechips have no creator pile
    else:
        creator_pct = _num(dev_raw) if dev_raw is not None else None

    vol = float(vol_hint or 0.0)
    tvl = float(tvl_hint or 0.0)
    step = float(bin_step_hint) if bin_step_hint not in (None, 0, 0.0) else None
    # The live pool detail nests these: volume is a window dict, tvl is top
    # level, and bin_step lives in pool_config. The flat keys below are kept for
    # older shapes but no longer matched anything live.
    pool_blacklisted = False
    base_verified = None
    if isinstance(pair, dict):
        vol = _num(pair.get("trade_volume_24h") or pair.get("volume_24h")) or vol
        _vwin = pair.get("volume")
        if isinstance(_vwin, dict):
            vol = _num(_vwin.get("24h")) or vol
        tvl = _num(pair.get("liquidity") or pair.get("tvl")) or tvl
        _cfg = pair.get("pool_config")
        _cfg = _cfg if isinstance(_cfg, dict) else {}
        step = (_num(pair.get("bin_step") or pair.get("binStep"))
                or _num(_cfg.get("bin_step")) or step)
        pool_blacklisted = bool(pair.get("is_blacklisted"))
        for _side in ("token_x", "token_y"):
            _t = pair.get(_side)
            if isinstance(_t, dict) and str(_t.get("address")) == mint:
                base_verified = bool(_t.get("is_verified"))
    if base_verified is None:
        _iv = token.get("isVerified")
        base_verified = bool(_iv) if isinstance(_iv, bool) else None
    # LP-lock state has no live source: Meteora's pair endpoint -- the only
    # provider of `is_locked`/`freeze_duration` -- is retired (404). Substitute
    # the venue's own verification signals, which cover the same risk: the base
    # token must be verified, the pool not blacklisted, no live freeze authority.
    if pool_blacklisted or base_verified is False:
        lp_locked = False
    elif base_verified is True and not freeze_auth:
        lp_locked = True
    else:
        lp_locked = None

    flags = _m.flag_card(
        mint_authority=mint_auth,
        freeze_authority=freeze_auth,
        sell_ok=sell_ok,
        creator_pct=creator_pct,
        lp_locked=lp_locked,
        has_route=sell_ok,
        volume_24h=vol,
        tvl=tvl if tvl else (1.0 if sell_ok else 0.0),
        bin_step=step if step is not None else 20.0,
        creator_cap_pct=creator_cap_pct,
        top_holders_pct=top_holders_pct,
    )
    allowed = _m.scout_allowed(flags)
    noul = _m.rug_noul_from_flags(flags, creator_pct=creator_pct)
    return {
        "ok": True,
        "mint": mint,
        "pool": pool,
        "flags": flags,
        "rug_noul": noul,
        "allowed": allowed,
        "sell_ok": sell_ok,
        "creator_pct": creator_pct,
        "mint_auth": mint_auth,
        "freeze_auth": freeze_auth,
        "vol": vol,
        "tvl": tvl,
        "bin_step": step,
        "lp_locked": lp_locked,
        "base_verified": base_verified,
        "pool_blacklisted": pool_blacklisted,
        "top_holders_pct": top_holders_pct,
        "momentum_pct": momentum_24h,
        "momentum_short_pct": momentum_6h,
        "base_symbol": str(token.get("symbol") or ""),
        "base_is_stable": base_is_stable,
        "reason": "clean" if allowed else "red:" + _m.fmt_flags(flags),
    }


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    mint = (config.mint or "").strip()
    pool = (config.pool_address or "").strip()
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers={"Accept": "application/json"}) as session:
        result = await evaluate_mint_pool(
            session,
            mint=mint,
            pool=pool,
            scout_usd=config.scout_usd,
            creator_cap_pct=config.creator_cap_pct,
        )

    flags = result["flags"]
    allowed = result["allowed"]
    lines = [
        f"JEV SCOUT CARD · mint={(mint[:8] + '…') if mint else 'none'} "
        f"pool={(pool[:8] + '…') if pool else 'none'}",
        f"sell_ok={result.get('sell_ok')} mint_auth={result.get('mint_auth') or 'revoked'} "
        f"freeze={result.get('freeze_auth') or 'off'}",
        f"creator_pct={result.get('creator_pct')} vol24={result.get('vol', 0):.0f} "
        f"tvl={result.get('tvl', 0):.0f} bin_step={result.get('bin_step')} "
        f"rug_noul={result.get('rug_noul', 0):.2f}",
        f"flags={_m.fmt_flags(flags)}",
        "SCOUT ALLOWED" if allowed else "ANY RED. NO SIZE.",
    ]
    if not allowed:
        lines.append("Skip is a win. Capital stays free.")
    else:
        lines.append("Card clean. Size now — the cost tier scales the slice.")

    import importlib.util as _ilu
    from pathlib import Path as _Pp
    _rp = _Pp(__file__).with_name("_jev_report.py")
    _spec = _ilu.spec_from_file_location("jev__report", _rp)
    _rep = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_rep)
    snap = {
        "mint": mint, "pool": pool, "allowed": allowed,
        "flags": flags, "rug_noul": result.get("rug_noul"),
        "creator_pct": result.get("creator_pct"),
        "vol": result.get("vol"), "tvl": result.get("tvl"),
        "bin_step": result.get("bin_step"),
        "red": result.get("red") or [k for k, v in (flags or {}).items() if v],
    }
    path = _rep.write_snapshot("scout", snap)
    await _rep.persist_memory("scout", snap, "JEV single-mint rug card")
    if path:
        lines.append(f"snapshot → {path}")
    flag_rows = [{"flag": k, "status": "RED" if flags.get(k) else "ok"} for k in _m.FLAG_KEYS]
    rid = await _rep.save_report(
        title="JEV — Rug Card",
        source="jev/jev_scout_card",
        kpis=[
            ("Verdict", "ALLOWED" if allowed else "BLOCKED"),
            ("rug_noul", f"{float(result.get('rug_noul', 0) or 0):.2f}"),
            ("Mint", (mint[:8] + "…") if mint else "none"),
            ("Pool", (pool[:8] + "…") if pool else "none"),
        ],
        sections=[
            ("01 / EIGHT-FLAG CARD", "Any red = no size.",
             flag_rows, ["flag", "status"]),
            ("02 / FACTS", "Pool / mint facts used on the card.",
             [{"field": k, "value": str(v)} for k, v in [
                 ("sell_ok", result.get("sell_ok")),
                 ("mint_auth", result.get("mint_auth") or "revoked"),
                 ("freeze", result.get("freeze_auth") or "off"),
                 ("creator_pct", result.get("creator_pct")),
                 ("vol24", result.get("vol")),
                 ("tvl", result.get("tvl")),
                 ("bin_step", result.get("bin_step")),
             ]], ["field", "value"]),
        ],
    )
    lines.append(_rep.report_line(rid))
    return "\n".join(lines)
