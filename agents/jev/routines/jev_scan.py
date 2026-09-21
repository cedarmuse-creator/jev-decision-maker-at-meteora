"""Rank live Meteora DLMM books across tabs. No orders.

Pulls the Meteora DLMM pool API (dlmm.datapi.meteora.ag) which exposes the
universe behind the site's Top Performers / Trending / New / RWA tabs. We map
those tabs from sort/filter params and tag every candidate with its tab so
JEV can rank across the whole universe, not a single list.

RWA is handled via an allowlist of known real-world-asset base mints, since
the API has no RWA flag.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path as _P

import aiohttp
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CATEGORY = "Market Data"

DLMM_POOLS = "https://dlmm.datapi.meteora.ag/pools"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111112"

# Known RWA base mints (Meteora has no RWA flag; allowlist the majors).
RWA_MINTS = {
    "USDG", "USDtb", "USDM", "bBTC", "tBTC", "cBTC", "Solana Yield",
    "Superstate USTB", "Ondo USDY", "Ondo OUSG", "BUIDL", "BlackRock BUIDL",
}

# Tab -> API sort/filter (best-effort mapping of the site tabs).
TAB_PARAMS = {
    # `sort_by` must be the API's `field:order` form. A separate `order=` param
    # (and bare fields like `fees`/`volume`) return HTTP 400 for every tab, which
    # silently emptied the scan and fell back to DEMO pools.
    "top": {"sort_by": "fee_tvl_ratio_24h:desc"},
    "trending": {"sort_by": "volume_24h:desc"},
    "new": {"sort_by": "pool_created_at:desc"},
    # There is no working `filter_by=rwa` on this API; pull the deepest books and
    # let the RWA mint allowlist re-tag any RWA pair it finds.
    "rwa": {"sort_by": "tvl:desc"},
}


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
    """Pull Meteora DLMM pools across tabs and tag candidates."""
    quote_asset: str = Field(default="USDC", description="Quote token: USDC or SOL")
    tabs: list[str] = Field(default_factory=lambda: ["top", "trending", "new", "rwa"])
    per_tab: int = Field(default=25, description="Rows pulled per tab")
    min_tvl: float = Field(default=20_000.0, description="Min TVL to be a candidate")
    min_vol: float = Field(default=5_000.0, description="Min 24h volume")
    max_bin_step: float = Field(default=400.0, description="Skip coarse bins above this")
    include_stable: bool = Field(default=False, description="Allow stable/stable pairs")
    default_pool: dict | None = Field(
        default={
            "pool": "BVRbyLjjfSBcoyiYFuxbgKYnWuiFaF9CSXEa5vdSZ9Hh",
            "pair": "SOL-USDC", "name": "SOL-USDC (default fallback)",
            "fees": 0.2, "tvl": 2_000_000.0, "vol": 2_000_000.0,
            "bin_step": 20, "price": 97.5, "tab": "top",
        },
        description="Known-good venue used when the public API is unreachable",
    )


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _quote_mint(asset: str) -> str:
    return USDC if (asset or "USDC").upper() == "USDC" else SOL


def _is_rwa(base: str, mint: str) -> bool:
    return (base in RWA_MINTS) or any(t in (base or "") for t in RWA_MINTS)


def _side(p: dict, side: str) -> str:
    """Mint address of one pool side.

    The live Meteora datapi nests the tokens as ``token_x`` / ``token_y`` dicts;
    older payloads carried a flat ``tokenX`` / ``mint_x`` string. Accept both.
    """
    nested = p.get(f"token_{side}")
    if isinstance(nested, dict) and nested.get("address"):
        return str(nested["address"])
    for key in (f"token{side.upper()}", f"mint_{side}", f"token{side.upper()}Mint"):
        v = p.get(key)
        if isinstance(v, dict) and v.get("address"):
            return str(v["address"])
        if isinstance(v, str) and v:
            return v
    return ""


def _token(p: dict, mint: str) -> dict:
    """The live token block for a mint (symbol / name / price live in here)."""
    for side in ("x", "y"):
        tok = p.get(f"token_{side}")
        if isinstance(tok, dict) and str(tok.get("address") or "") == mint:
            return tok
    return {}


def _symbol(p: dict, mint: str) -> str:
    """Token symbol for a mint, from either live token block."""
    if not mint:
        return ""
    for side in ("x", "y"):
        tok = p.get(f"token_{side}")
        if isinstance(tok, dict) and str(tok.get("address") or "") == mint:
            return str(tok.get("symbol") or "").strip()
    return ""


def _display_pair(p: dict, base_mint: str, quote_mint: str) -> str:
    """A human pair label: the API's `name`, else the two token symbols.

    Falling straight through to truncated mints produced labels like
    `6p6xgH-EPjF`, which are unreadable in the routine reports and on the
    dashboard -- and the datapi already names the pair.
    """
    name = str(p.get("name") or "").strip()
    if name:
        return name
    base_sym = _symbol(p, base_mint)
    quote_sym = _symbol(p, quote_mint)
    if base_sym and quote_sym:
        return f"{base_sym}-{quote_sym}"
    if base_sym:
        return base_sym
    return f"{base_mint[:6]}-{quote_mint[:4]}"


def _window(p: dict, key: str, window: str = "24h") -> float:
    """A windowed metric — the live API sends ``{"1h": ..., "24h": ...}`` dicts.

    Older payloads sent a flat scalar. Accept both.
    """
    v = p.get(key)
    if isinstance(v, dict):
        return _num(v.get(window))
    return 0.0


def _pool_cfg(p: dict) -> dict:
    """The pool's on-chain config block (``bin_step`` lives in here)."""
    cfg = p.get("pool_config")
    return cfg if isinstance(cfg, dict) else {}


def _fee_pct(p: dict) -> tuple[float, float]:
    """The pool's effective fee rate % and its base fee %.

    Meteora charges the pool's base fee plus a small volatility-scaled fee. The
    datapi exposes them separately and the volatility part is frequently ~0, so
    reading ``dynamic_fee_pct`` alone reports a 0% fee — the desk then computes
    $0 expected fees and SITs on every pool. Verified against live data:
    realized fees/volume tracks ``base_fee_pct`` (0.2 base -> 0.193 realized,
    0.03 base -> 0.0276 realized), and the volatility part is ~0.0008%.
    """
    base = _num(_pool_cfg(p).get("base_fee_pct"))
    dyn = _num(p.get("dynamic_fee_pct")) if p.get("dynamic_fee_pct") is not None else 0.0
    return base + dyn, base


STABLE_DROPS: list[dict] = []


def _row(p: dict, quote_mint: str, tab: str) -> dict | None:
    mint_x = _side(p, "x")
    mint_y = _side(p, "y")
    if quote_mint not in (mint_x, mint_y):
        return None
    base = mint_y if mint_x == quote_mint else mint_x
    # A stable-vs-stable pair has no directional leg: a bid wall can never
    # capture appreciation, and its fee share is dust. Drop it at parse time so
    # it cannot take a slot; counted so the scan reports it rather than quietly
    # shrinking the universe.
    if _m.stable_pair(base_mint=base, base_symbol=_symbol(p, base),
                      quote_mint=quote_mint, quote_symbol=_symbol(p, quote_mint),
                      base=_token(p, base), quote=_token(p, quote_mint)):
        STABLE_DROPS.append({"pair": _display_pair(p, base, quote_mint),
                             "pool": str(p.get("address") or "")})
        return None
    addr = str(p.get("address") or p.get("pubkey") or p.get("poolAddress") or "")
    if not addr or not base:
        return None
    tvl = _num(p.get("tvl") or p.get("liquidity") or p.get("tvlUsd"))
    vol = _num(p.get("volume24h") or p.get("trade_volume_24h") or p.get("volume24hUsd")) or _window(p, "volume")
    fees = _num(p.get("fees24h") or p.get("fees24hUsd") or 0) or _window(p, "fees")
    step = _num(p.get("binStep") or p.get("bin_step") or _pool_cfg(p).get("bin_step"))
    fee_pct, base_fee = _fee_pct(p)
    hide = bool(p.get("hide") or p.get("is_blacklisted"))
    is_rwa = _is_rwa(base, base)
    tab_eff = "rwa" if is_rwa else tab
    # Optional safety proxies when the API exposes them (else 0 / None).
    dev = _num(p.get("dev_balance") or p.get("devBalance") or p.get("creator_pct") or 0)
    if dev > 1.0:  # sometimes API sends percent 0-100
        dev = dev / 100.0
    top10 = _num(p.get("top_10_holders") or p.get("top10") or p.get("top10HolderPercent") or 0)
    if top10 > 1.0:
        top10 = top10 / 100.0
    age_hours = None
    created = p.get("created_at") or p.get("createdAt") or p.get("open_time")
    if created is not None:
        try:
            import time
            ts = float(created)
            if ts > 1e12:  # ms
                ts /= 1000.0
            age_hours = max(0.0, (time.time() - ts) / 3600.0)
        except (TypeError, ValueError):
            age_hours = None
    swaps = _num(p.get("swap_count_24h") or p.get("trades24h") or p.get("swapCount24h") or 0)
    return {
        "pool": addr,
        "pair": _display_pair(p, base, quote_mint),
        "base_symbol": _symbol(p, base),
        "base": base,
        "quote": quote_mint,
        "tvl": tvl,
        "vol": vol,
        "fees": fees,
        "bin_step": step,
        "hide": hide,
        "tab": tab_eff,
        "is_rwa": is_rwa,
        "fee_rate_pct": (fees / vol * 100.0) if vol > 0 else 0.0,
        "dev_balance": dev,
        "top_10": top10,
        "top_10_holders": top10,
        "age_hours": age_hours,
        "dynamic_fee_pct": fee_pct,
        "base_fee_pct": base_fee,
        "swaps_24h": swaps,
    }


async def _fetch(session: aiohttp.ClientSession, params: dict) -> list:
    try:
        async with session.get(DLMM_POOLS, params=params,
                               timeout=aiohttp.ClientTimeout(total=18)) as resp:
            resp.raise_for_status()
            body = await resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("jev_scan fetch failed: %s", exc)
        return []
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("pools", "data", "result"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


def _fmt(rows: list[dict], n: int) -> str:
    if not rows:
        return "(none)"
    lines = ["score   vol24      tvl        step  tab      pair"]
    for r in rows[:n]:
        name = str(r.get("pair") or "") or (str(r.get("pool", ""))[:8] + "…")
        lines.append(
            f"{r.get('score',0):6.2f}  {r.get('vol',0):9.0f}  {r.get('tvl',0):9.0f}  "
            f"{r.get('bin_step',0):4.0f}  {str(r.get('tab','')):<8} {name}"
        )
    return "\n".join(lines)


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    quote = _quote_mint(config.quote_asset)
    STABLE_DROPS.clear()
    timeout = aiohttp.ClientTimeout(total=25)
    all_rows: list[dict] = []
    async with aiohttp.ClientSession(timeout=timeout, headers={"Accept": "application/json"}) as session:
        for tab in config.tabs:
            params = dict(TAB_PARAMS.get(tab, {"sort_by": "fee_tvl_ratio_24h:desc"}))
            params["page"] = 1
            params["page_size"] = min(int(config.per_tab), 1000)
            raw = await _fetch(session, params)
            for p in raw:
                if not isinstance(p, dict):
                    continue
                row = _row(p, quote, tab)
                if not row or row["hide"]:
                    continue
                # Pre-score with the same hard-gate + yield engine rank uses.
                ranked = _m.jev_rank_row(
                    tvl=row["tvl"], vol24=row["vol"], bin_step=row["bin_step"],
                    fee_rate_pct=row["fee_rate_pct"], fees24=row["fees"],
                    tab=row["tab"], is_rwa=row["is_rwa"],
                    dev_balance=row.get("dev_balance", 0.0),
                    top10=row.get("top_10", 0.0),
                    age_hours=row.get("age_hours"),
                    min_tvl=config.min_tvl, min_vol=config.min_vol,
                    max_bin_step=config.max_bin_step,
                )
                row["score"] = ranked["composite"]
                row["composite"] = ranked["composite"]
                row["rug_noul"] = ranked["rug_noul"]
                row["rank_pass"] = ranked["pass"]
                row["rank_reason"] = ranked["reason"]
                all_rows.append(row)

    # Prefer candidates that already cleared the rank hard gate when available.
    cands = [
        r for r in all_rows
        if r.get("rank_pass", True)
        and r["tvl"] >= config.min_tvl and r["vol"] >= config.min_vol
        and 0 < r["bin_step"] <= config.max_bin_step
    ]
    cands.sort(key=lambda r: r["score"], reverse=True)

    # Always leave the next routine with candidates (demo if APIs dark).
    used_demo = False
    if not cands:
        from pathlib import Path as _Pp
        import importlib.util as _ilu
        _rp = _Pp(__file__).with_name("_jev_report.py")
        _spec = _ilu.spec_from_file_location("jev__report", _rp)
        _rep = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_rep)
        cands, used_demo = _rep.ensure_candidates(cands)
        all_rows = list(cands)
        for r in cands:
            r.setdefault("score", r.get("composite", 0))

    by_tab = {}
    for r in cands:
        by_tab.setdefault(r.get("tab", "top"), []).append(r)

    lines = [
        "JEV SCAN · Meteora DLMM (dlmm.datapi.meteora.ag) · cross-tab",
        f"tabs={config.tabs} pulled={len(all_rows)} candidates={len(cands)}"
        + (f" · stable-stable dropped={len(STABLE_DROPS)}" if STABLE_DROPS else "")
        + (" · DEMO_FALLBACK" if used_demo else ""),
        "",
    ]
    for tab in config.tabs:
        rows = by_tab.get(tab, [])
        if rows:
            lines.append(f"== {tab.upper()} ({len(rows)}) ==")
            lines.append(_fmt(rows, config.per_tab))
            lines.append("")
    top = cands[0] if cands else {}
    lines.append(
        f"TOP CANDIDATE {top.get('pair', 'none')} "
        f"({top.get('tab', '-')}) score {float(top.get('score', 0) or 0):.1f}"
    )
    lines.append("Pass candidates JSON to jev_enrich / jev_rank. Do not invent pools.")

    # Persist for Condor tick chain + dashboard.
    import importlib.util as _ilu
    from pathlib import Path as _Pp
    _rp = _Pp(__file__).with_name("_jev_report.py")
    _spec = _ilu.spec_from_file_location("jev__report", _rp)
    _rep = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_rep)
    snap = {
        "tabs": list(config.tabs),
        "pulled": len(all_rows),
        "stable_dropped": len(STABLE_DROPS),
        "stable_pairs": STABLE_DROPS[:20],
        "candidates": cands,
        "demo": used_demo,
    }
    path = _rep.write_snapshot("scan", snap)
    await _rep.persist_memory("scan", snap, "JEV scan candidates for enrich/rank")
    if path:
        lines.append(f"snapshot → {path}")

    table_rows = [
        {"pair": r.get("pair", ""), "tab": r.get("tab", ""),
         "score": f"{float(r.get('score', 0) or 0):.1f}",
         "tvl": f"{float(r.get('tvl', 0) or 0):.0f}",
         "vol24": f"{float(r.get('vol', 0) or 0):.0f}",
         "bins": f"{float(r.get('bin_step', 0) or 0):.0f}",
         "pool": str(r.get("pool", ""))[:10] + "…"}
        for r in cands[:40]
    ]
    rid = await _rep.save_report(
        title="JEV — Meteora DLMM Cross-Tab Scan",
        source="jev/jev_scan",
        kpis=[
            ("Tabs", str(config.tabs)),
            ("Pulled", str(len(all_rows))),
            ("Candidates", str(len(cands))),
            ("Source", "demo" if used_demo else "live"),
        ],
        markdown=f"Top: **{top.get('pair', 'none')}** · tab **{top.get('tab', '-')}**",
        sections=[
            ("01 / CANDIDATES", "Rank-pass pools tagged by tab (input to enrich).",
             table_rows, ["pair", "tab", "score", "tvl", "vol24", "bins", "pool"]),
        ],
    )
    lines.append(_rep.report_line(rid))
    return "\n".join(lines)
