"""Condor report + snapshot helpers for JEV routines.

One pattern, used by every routine in this package:
  - ReportBuilder with .source("routine", slug).tags([...]).kpi().section().table()
  - .manual_order() before .save()
  - Never raise — reports must not break the tick
  - Persist machine JSON so the next routine / agent tick has data even if the
    LLM only saw the text summary
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default place the agent/dashboard can read between ticks (relative to CWD).
STATE_DIR = Path("state")
TAGS = ["jev", "meteora", "dlmm"]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


_SOL_CACHE: dict[str, Any] = {"ts": 0.0, "usd": 0.0}


def live_sol_usd(fallback: float = 150.0, ttl_sec: float = 60.0) -> float:
    """Live SOL/USD, cached briefly.

    Sizing and the gate both price the one-time open cost in SOL, so a stale
    constant silently skews every fee-vs-cost ratio (a hardcoded 150 against a
    ~110 market overstates the open cost by ~38% and refuses viable pools).
    Falls back rather than raising — this is a report cost, it must never break
    the tick.
    """
    import time
    import urllib.request

    now = time.time()
    if _SOL_CACHE["usd"] and now - _SOL_CACHE["ts"] < ttl_sec:
        return float(_SOL_CACHE["usd"])
    try:
        url = ("https://dlmm.datapi.meteora.ag/pools"
               "?page=1&page_size=25&sort_by=tvl:desc")
        # The API 403s a bare urllib User-Agent; curl works, urllib does not.
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "curl/8.5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode())
        for pool in body.get("data") or []:
            for side in ("token_x", "token_y"):
                tok = pool.get(side) or {}
                if tok.get("symbol") == "SOL" and tok.get("price"):
                    usd = float(tok["price"])
                    _SOL_CACHE.update(ts=now, usd=usd)
                    return usd
    except Exception as exc:  # noqa: BLE001
        logger.warning("live SOL price unavailable: %s", exc)
    return float(_SOL_CACHE["usd"] or fallback)


def state_path(name: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    return STATE_DIR / f"jev_{safe}.json"


def write_snapshot(name: str, payload: dict[str, Any]) -> str:
    """Write JSON snapshot to state/jev_<name>.json. Returns path or ''."""
    try:
        path = state_path(name)
        body = dict(payload)
        body.setdefault("_ts", datetime.now(timezone.utc).isoformat())
        body.setdefault("_name", name)
        path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
        return str(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("jev snapshot write %s failed: %s", name, exc)
        return ""


def read_snapshot(name: str) -> dict[str, Any] | None:
    path = state_path(name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


async def persist_memory(name: str, payload: dict[str, Any], description: str = "") -> None:
    """Best-effort Condor memory write (same shape as the other routines). Never raises."""
    try:
        from mcp_servers.condor.tools import memory  # type: ignore

        await memory.manage_memory(
            action="write",
            name=f"jev_{name}",
            content=json.dumps(payload, default=str),
            description=description or f"JEV snapshot {name}",
            type="reference",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("jev memory write %s skipped: %s", name, exc)


async def save_report(
    *,
    title: str,
    source: str,
    kpis: list[tuple[str, str]] | None = None,
    sections: list[tuple[str, str, list[dict], list[str]]] | None = None,
    markdown: str | None = None,
    extra_tags: list[str] | None = None,
) -> str:
    """Build and save a Condor dashboard report. Returns report id or ''."""
    try:
        from condor.reports import ReportBuilder

        rb = ReportBuilder(title)
        rb.source("routine", source)
        rb.tags(TAGS + (extra_tags or []) + [source.split("/")[-1]])
        for label, value in kpis or []:
            rb.kpi(str(label), str(value))
        rb.kpi("Checked", utc_stamp())
        if markdown:
            rb.markdown(markdown)
        for sec_title, sec_desc, rows, cols in sections or []:
            rb.section(sec_title, sec_desc or "")
            if rows:
                rb.table(rows, cols)
            else:
                rb.table([{"note": "(no rows)"}], ["note"])
        if hasattr(rb, "manual_order"):
            rb.manual_order()
        rid = await rb.save()
        return str(rid or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("jev report %s failed: %s", source, exc)
        return ""


def report_line(rid: str) -> str:
    return f"\n📊 Report: {rid}" if rid else "\n📊 Report: (not saved — Condor ReportBuilder unavailable)"


# Demo universe so reports never render empty when APIs are dark.
DEMO_CANDIDATES: list[dict] = [
    {"pool": "So11111111111111111111111111111111111111112EPjF", "pair": "SOL-USDC", "base": "So11111111111111111111111111111111111111112",
     "quote": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "tvl": 2_400_000, "vol": 1_900_000, "fees": 760,
     "bin_step": 20, "tab": "top", "is_rwa": False, "fee_rate_pct": 0.04, "dev_balance": 0.0, "top_10": 0.2,
     "rug_noul": 1.0, "rank_pass": True, "composite": 78.0, "score": 78.0, "hide": False},
    {"pool": "BONKpoolxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "pair": "BONK-USDC", "base": "BONKmintxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
     "quote": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "tvl": 320_000, "vol": 540_000, "fees": 650,
     "bin_step": 50, "tab": "trending", "is_rwa": False, "fee_rate_pct": 0.12, "dev_balance": 0.03, "top_10": 0.35,
     "rug_noul": 0.92, "rank_pass": True, "composite": 72.0, "score": 72.0, "hide": False},
    {"pool": "WIFpoolxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "pair": "WIF-USDC", "base": "WIFmintxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
     "quote": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "tvl": 180_000, "vol": 410_000, "fees": 615,
     "bin_step": 50, "tab": "trending", "is_rwa": False, "fee_rate_pct": 0.15, "dev_balance": 0.05, "top_10": 0.40,
     "rug_noul": 0.88, "rank_pass": True, "composite": 70.0, "score": 70.0, "hide": False},
    {"pool": "JUPpoolxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "pair": "JUP-USDC", "base": "JUPmintxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
     "quote": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "tvl": 90_000, "vol": 130_000, "fees": 130,
     "bin_step": 50, "tab": "new", "is_rwa": False, "fee_rate_pct": 0.10, "dev_balance": 0.08, "top_10": 0.45,
     "rug_noul": 0.85, "rank_pass": True, "composite": 58.0, "score": 58.0, "hide": False},
    {"pool": "USDGpoolxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "pair": "USDG-USDC", "base": "USDGmintxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
     "quote": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "tvl": 1_100_000, "vol": 600_000, "fees": 120,
     "bin_step": 20, "tab": "rwa", "is_rwa": True, "fee_rate_pct": 0.02, "dev_balance": 0.0, "top_10": 0.15,
     "rug_noul": 1.0, "rank_pass": True, "composite": 64.0, "score": 64.0, "hide": False},
]


def ensure_candidates(rows: list[dict] | None, *, force_demo: bool = False) -> tuple[list[dict], bool]:
    """Return (rows, used_demo). Never returns empty list."""
    if force_demo or not rows:
        return [dict(r) for r in DEMO_CANDIDATES], True
    return [dict(r) for r in rows], False


def load_candidates(
    rows: list[dict] | None,
    *snapshot_names: str,
    allow_demo: bool = True,
) -> tuple[list[dict], str]:
    """Resolve candidates: explicit rows → prior snapshot → demo.

    Returns (candidates, source) where source is 'config' | 'snapshot:<name>' | 'demo'.
    """
    if rows:
        return [dict(r) for r in rows], "config"
    for name in snapshot_names:
        snap = read_snapshot(name)
        if not snap:
            continue
        cands = snap.get("candidates") or snap.get("top") or snap.get("picks") or []
        if cands:
            return [dict(r) for r in cands], f"snapshot:{name}"
    if allow_demo:
        return [dict(r) for r in DEMO_CANDIDATES], "demo"
    return [], "empty"


def load_report_mod():
    """Import this module by path (routines often cannot do relative imports)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve()
    spec = importlib.util.spec_from_file_location("jev__report", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod