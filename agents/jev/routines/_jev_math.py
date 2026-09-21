"""Pure JEV desk math. No network. No orders.

Decision Maker at Meteora: the model proposes (portfolio select + size from
risk/market data); this module is the math that DISPOSES — rug card, rank
hard-gate, bin clamp, one-sided bounds, fee floor, worth tier (size trim),
churn gate. Kept pure so it is unit-testable without a key.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Iterable

# ------------------------------------------------------------------ run modes
# Two ways to run JEV. The book, the slot budget and the per-position floor move
# TOGETHER: a 2-slot budget on an 800 USDC book would open $400 walls, and a
# 5-slot budget on a 100 USDC book would open dust under the fee-vs-cost floor.
#
#   test — small book, for organizers and capital-constrained testing
#   prod — the 48-hour competition envelope
#
# Selected by the `JEV_MODE` env var (read at import) or the strategy's `mode`
# key. Everything downstream follows from here: the jev_select / jev_size /
# jev_gate config defaults, and the dashboard's book and slot count.
MODE_TEST = "test"
MODE_PROD = "prod"
DEFAULT_MODE = MODE_TEST

MODE_PROFILES: dict[str, dict] = {
    MODE_TEST: {
        "label": "TEST",
        "book_usd": 100.0,
        "max_positions": 2,
        "min_position_usd": 12.0,
    },
    MODE_PROD: {
        "label": "PROD",
        "book_usd": 800.0,
        "max_positions": 5,
        # Must sit at or below the model's SMALLEST non-zero Score level, or
        # that answer can never clear the floor and the desk silently loses an
        # option. Level 1 = 0.32 x cap x book = 0.32 x (800/5) = $51.20 here
        # ($16.00 in test), so $50 keeps all three levels live.
        "min_position_usd": 50.0,
    },
}


_STRATEGY_MODE: str | None = None


def strategy_mode() -> str:
    """The `mode:` key from the desk strategy file, or "" if it is unreadable.

    This file is the operator's single switch — set `mode: prod` and restart.
    Only the YAML frontmatter is read: the body mentions modes in prose, and a
    stray `mode:` there must not silently pick the profile.
    """
    global _STRATEGY_MODE
    if _STRATEGY_MODE is None:
        path = (Path(__file__).resolve().parents[1]
                / "strategies" / "jev_desk" / "strategy.md")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        parts = text.split("---")
        front = parts[1] if len(parts) >= 3 else ""
        m = re.search(r"^\s*mode:\s*[\"']?([A-Za-z_-]+)", front, re.MULTILINE)
        _STRATEGY_MODE = m.group(1).strip().lower() if m else ""
    return _STRATEGY_MODE


def _mode_key(mode: str | None = None) -> str:
    """Resolve the active mode name.

    Precedence: an explicit argument, then $JEV_MODE (so organizers can pin a
    profile per process), then the strategy file's `mode:` key, then the default.
    The file fallback is what makes switching a single edit.
    """
    if mode and str(mode).strip():
        return str(mode).strip().lower()
    env = os.environ.get("JEV_MODE")
    if env and env.strip():
        return env.strip().lower()
    return strategy_mode() or DEFAULT_MODE


def is_known_mode(mode: str | None = None) -> bool:
    """True if `mode` (or $JEV_MODE) names a real profile."""
    return _mode_key(mode) in MODE_PROFILES


def mode_profile(mode: str | None = None) -> dict:
    """Resolve a run mode to its book / slot / floor profile.

    An unknown or blank name falls back to `DEFAULT_MODE` instead of raising: a
    typo in `JEV_MODE` must not take the desk down mid-run, and the mode label
    printed in every report makes the substitution visible.
    """
    return MODE_PROFILES.get(_mode_key(mode), MODE_PROFILES[DEFAULT_MODE])


MODE = _mode_key()
MODE_IS_KNOWN = MODE in MODE_PROFILES
_PROFILE = mode_profile(MODE)

# Model-sizing envelope (JEV Score -> % of book). Overridden live by jev_size.
# RACE_USD is the desk's book, taken from the active run mode. Code default =
# the desk's live book, so a config-less run sizes slices the wallet can fund.
# The book is also set in strategies/jev_desk/strategy.md (total_amount_quote).
RACE_USD = float(_PROFILE["book_usd"])
MAX_POSITIONS = int(_PROFILE["max_positions"])   # slot budget the cap derives from
MIN_POSITION_USD = float(_PROFILE["min_position_usd"])
MODE_LABEL = str(_PROFILE["label"])
MAJOR_PCT_MIN = 0.30
MAJOR_PCT_MAX = 0.45
MINOR_PCT_MAX = 0.20
# Per-pool cap for the 3-5 pool portfolio, derived from the slot budget: N slots
# at the cap must not exceed the book, or the last opens get rejected for
# insufficient funds (5 x 25% = 125% of the book).
PORTFOLIO_PCT_MAX = 1.0 / MAX_POSITIONS
TRUST_NOUL_FLOOR = 0.30       # rug noul below this -> size 0, SIT
SELECT_CONF_FLOOR = 0.55      # JEV Choice confidence floor for portfolio picks
MINOR_MAX_SEC = 3600

MIN_OUTSIDE_SLOTS = 3
MAX_BINS = 69
BUY_GAP = 0.0005
SELL_GAP = 0.0005
FEE_FLOOR_PCT = 0.02  # min Meteora dynamic (volatility) fee % to justify a re-site

# Worth gate — a size trim, not a wall. `WORTH_MARGIN` is the fee/open-cost
# ratio a full-size slice should clear; between the margin and the floor the
# position is trimmed instead of refused (the rug card, fee floor and the
# model's read are the other criteria the gate must not outrank); below the
# floor the fee does not pay for the position at all.
WORTH_MARGIN = 1.2
WORTH_MARGIN_FLOOR = 0.5
MARGINAL_SCALE = 0.75
# Headroom over the floor for a MARGINAL trim. The caller recomputes fee and
# width on the trimmed amount, and a slice tuned to land exactly on the floor
# re-evaluates a hair under it -- flipping MARGINAL to NO and killing a position
# that should open trimmed.
TRIM_HEADROOM = 1.02

# === Trend allowance: the deep-book / thin-fee case ===
# JEV opens a ONE-SIDED BID WALL under price, so a position's income is not only
# fees: when the wall fills it buys base at a discount, and the re-sited ask sells
# that base back higher. Fee-only viability under-prices that round trip, which is
# why a deep book (thin fee share) was refused even though the trade pays.
#
# The spread is only realised if the dip that fills the wall is bought back --
# which is exactly what an uptrend buys you: an appreciating asset recovers its
# dips, a falling knife does not. So spread capture is credited at
# `trend_credit`, which is 0 with no positive trend read -- a caller holding no
# momentum data keeps the existing fee-only behaviour, unchanged.
TREND_MIN_PCT = 3.0       # 24h change (%) that counts as an uptrend
TREND_FULL_PCT = 15.0     # at/above this the trend credit is full
TREND_BREAK_PCT = -5.0    # short-window rollover this bad voids the trend
# A bid wall parked under price over a multi-week horizon is touched often, so
# the round trip is given a flat, deliberately conservative fill probability.
# (`in_range_fraction` measures TIME IN RANGE for a position straddling price and
# badly understates the odds a one-sided wall below price gets hit at all.)
TREND_FILL_PROB = 0.5

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

STABLE_MINTS = {USDC_MINT, USDT_MINT}
BLUECHIP_MINTS = {
    SOL_MINT,
    USDC_MINT,
    USDT_MINT,
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # jitoSOL
}

FLAG_KEYS = (
    "mint_live",
    "freeze_on",
    "cant_sell",
    "creator_pile",
    "lp_unlocked",
    "no_route",
    "ghost_tape",
    "coarse_bins",
)

SIT = "SIT"
WAIT = "WAIT"
TAKE = "TAKE"
SHIFT = "SHIFT"
REBUILD = "REBUILD"
SELECT = "SELECT"
SIZE = "SIZE"
KILL = "KILL"
LEARN = "LEARN"
HOLD = "HOLD"


def any_red(flags: dict) -> bool:
    """True if any known red flag is set."""
    for key in FLAG_KEYS:
        if bool(flags.get(key)):
            return True
    return False


def flag_card(
    *,
    mint_authority: str | None,
    freeze_authority: str | None,
    sell_ok: bool,
    creator_pct: float | None,
    lp_locked: bool | None,
    has_route: bool,
    volume_24h: float,
    tvl: float,
    bin_step: float | None,
    creator_cap_pct: float = 30.0,
    ghost_vol_tvl: float = 0.05,
    max_bin_step: float = 80.0,
    min_tvl: float = 1.0,
    top_holders_pct: float | None = None,
    whale_cap_pct: float = 90.0,
) -> dict[str, bool]:
    """Build the eight-flag rug card.

    Fail-closed on the checks whose data source exists. ``creator_pct`` is the
    exception: Jupiter publishes ``devBalancePercentage`` for only a minority of
    tokens, so treating "absent" as red blocked every pool including verified
    majors. The flag is red on *evidence* instead — a dev pile at/above the cap,
    or extreme top-holder concentration (``whale_cap_pct``). Unknown is not red,
    and the other seven checks stay fail-closed.
    """
    mint_live = bool(mint_authority)
    freeze_on = bool(freeze_authority)
    cant_sell = not bool(sell_ok)
    dev_pile = creator_pct is not None and creator_pct >= creator_cap_pct
    whale_pile = top_holders_pct is not None and top_holders_pct >= whale_cap_pct
    creator_pile = dev_pile or whale_pile
    lp_unlocked = lp_locked is not True
    no_route = not bool(has_route)
    ghost_tape = tvl < min_tvl or (tvl > 0 and volume_24h / tvl < ghost_vol_tvl)
    coarse_bins = bin_step is None or bin_step <= 0 or bin_step > max_bin_step
    return {
        "mint_live": mint_live,
        "freeze_on": freeze_on,
        "cant_sell": cant_sell,
        "creator_pile": creator_pile,
        "lp_unlocked": lp_unlocked,
        "no_route": no_route,
        "ghost_tape": ghost_tape,
        "coarse_bins": coarse_bins,
    }


def scout_clean(flags: dict) -> bool:
    """Pool entry requires a fully clean rug card."""
    return not any_red(flags)


def scout_allowed(flags: dict) -> bool:
    """Alias used by scout routines — clean card only."""
    return scout_clean(flags)


def rug_noul_from_flags(flags: dict, creator_pct: float | None = None) -> float:
    """Map an eight-flag card (+ optional creator %) to a 0..1 trust noul.

    Any red flag → 0. Clean card → high trust, haircut slightly if creator
    concentration is elevated but still under the hard cap.
    """
    if any_red(flags):
        return 0.0
    noul = 1.0
    if creator_pct is not None:
        c = float(creator_pct)
        if c > 1.5:  # percent 0-100
            c = c / 100.0
        c = max(0.0, min(1.0, c))
        if c > 0.15:
            noul -= 0.15 * (c - 0.15) / 0.85
    return round(max(0.0, min(1.0, noul)), 3)


def apply_scout_to_candidate(row: dict, *, flags: dict, creator_pct: float | None = None,
                             sell_ok: bool | None = None, scout_source: str = "live") -> dict:
    """Write live scout fields onto a candidate row (mutates + returns)."""
    noul = rug_noul_from_flags(flags, creator_pct=creator_pct)
    row["flags"] = dict(flags)
    row["rug_noul"] = noul
    row["scout_allowed"] = scout_allowed(flags)
    row["scout_source"] = scout_source
    row["scout_red"] = red_flag_names(flags)
    if creator_pct is not None:
        row["creator_pct"] = creator_pct
        # Keep rank proxies in sync with the card.
        c = float(creator_pct)
        row["dev_balance"] = (c / 100.0) if c > 1.5 else c
    if sell_ok is not None:
        row["sell_ok"] = bool(sell_ok)
    return row


def rug_noul_from_proxies(
    *,
    dev_balance: float = 0.0,
    top10: float = 0.0,
    age_hours: float | None = None,
    flags: dict | None = None,
    tab: str = "top",
    is_rwa: bool = False,
) -> float:
    """0..1 trust proxy before a full scout card runs.

    Starts near-clean and is cut by concentration, youth, and any red flags.
    Missing age is neutral. New-tab names without age are treated as young.
    """
    if flags is not None and any_red(flags):
        return 0.0
    noul = 1.0
    dev = max(0.0, min(1.0, float(dev_balance or 0.0)))
    top = max(0.0, min(1.0, float(top10 or 0.0)))
    # Dev pile and top-10 concentration are the biggest pre-card smells.
    noul -= 0.55 * dev
    noul -= 0.35 * max(0.0, top - 0.35) / 0.65
    age = age_hours
    if age is None and (tab or "").lower() == "new":
        age = 12.0  # unknown new → treat as young
    if age is not None:
        if age < 6:
            noul -= 0.35
        elif age < 24:
            noul -= 0.20
        elif age < 72:
            noul -= 0.08
    if is_rwa:
        noul = min(1.0, noul + 0.05)
    return round(max(0.0, min(1.0, noul)), 3)


def fee_yield_score(tvl: float, vol24: float, fees24: float = 0.0,
                    fee_rate_pct: float = 0.0) -> float:
    """0..1 economic attractiveness: realized fees if present, else vol×fee.

    Prefer fees/TVL (what LPs actually earned) over raw fee % or raw volume.
    """
    if tvl <= 0:
        return 0.0
    if fees24 > 0:
        # 1% of TVL in daily fees is excellent; log-compress.
        y = fees24 / tvl
        return max(0.0, min(1.0, math.log10(1.0 + y * 200.0) / 2.0))
    # Fallback: expected fee pool share texture from vol and fee rate.
    heat = min(vol24 / tvl, 4.0) / 4.0
    take = max(0.0, min(1.0, fee_rate_pct / 0.5))  # 0.5% fee → full
    return round(0.65 * heat + 0.35 * take, 4)


def jev_rank_row(
    *,
    tvl: float,
    vol24: float,
    bin_step: float,
    fee_rate_pct: float = 0.0,
    fees24: float = 0.0,
    tab: str = "top",
    is_rwa: bool = False,
    dev_balance: float = 0.0,
    top10: float = 0.0,
    age_hours: float | None = None,
    flags: dict | None = None,
    rug_noul: float | None = None,
    min_tvl: float = 20_000.0,
    min_vol: float = 5_000.0,
    max_bin_step: float = 400.0,
    trust_floor: float = TRUST_NOUL_FLOOR,
    wash_vol_tvl: float = 25.0,
) -> dict:
    """Full pre-select score: yield engine + safety. Math baseline for rank.

    Hard fail (pass=False, composite=0) when depth/tape/bins/trust are unsafe.
    Soft score blends depth, fee yield, cleanliness, tab. TVL/vol/fee remain
    the economic core; they do not override a failed safety gate.
    """
    tab_k = (tab or "top").lower()
    noul = (float(rug_noul) if rug_noul is not None
            else rug_noul_from_proxies(
                dev_balance=dev_balance, top10=top10, age_hours=age_hours,
                flags=flags, tab=tab_k, is_rwa=is_rwa))
    reasons: list[str] = []
    if tvl < min_tvl:
        reasons.append("tvl_floor")
    if vol24 < min_vol:
        reasons.append("vol_floor")
    if bin_step <= 0 or bin_step > max_bin_step:
        reasons.append("bin_step")
    if noul < trust_floor:
        reasons.append("trust")
    if tvl > 0 and (vol24 / tvl) > wash_vol_tvl:
        reasons.append("wash_tape")  # volume ≫ TVL often bot/wash
    if flags is not None and any_red(flags):
        reasons.append("rug_flags")

    if reasons:
        return {
            "pass": False, "composite": 0.0, "rug_noul": noul,
            "yield": 0.0, "depth": 0.0, "clean": noul,
            "tab_mult": 1.0, "reasons": reasons,
            "reason": "hard_fail:" + ",".join(reasons),
        }

    depth = math.log10(max(tvl, 1.0)) / 7.0          # ~0..1 to $10M
    depth = max(0.0, min(1.0, depth))
    yld = fee_yield_score(tvl, vol24, fees24=fees24, fee_rate_pct=fee_rate_pct)
    clean = noul
    # Tab: trending boosted, new penalized until age/trust carries it, rwa steady.
    tab_mult = {"top": 1.0, "trending": 1.12, "new": 0.88, "rwa": 1.06}.get(tab_k, 1.0)
    if is_rwa and tab_k != "rwa":
        tab_mult = max(tab_mult, 1.06)
    # Weights: economy first, then trust, then depth texture.
    base = 100.0 * (0.40 * yld + 0.30 * depth + 0.30 * clean)
    composite = round(base * tab_mult, 2)
    return {
        "pass": True, "composite": composite, "rug_noul": noul,
        "yield": round(yld, 4), "depth": round(depth, 4), "clean": round(clean, 4),
        "tab_mult": tab_mult, "reasons": [],
        "reason": f"y{yld:.2f} d{depth:.2f} c{clean:.2f} ×{tab_mult:.2f}",
    }


def churn_ok(
    extra_fees_usd: float,
    slip_usd: float,
    priority_usd: float,
    rent_usd: float,
    outside_slots: int,
    min_slots: int = MIN_OUTSIDE_SLOTS,
) -> bool:
    """Move only if extra fees beat cost AND price lived outside long enough."""
    cost = max(0.0, slip_usd) + max(0.0, priority_usd) + max(0.0, rent_usd)
    if extra_fees_usd <= cost:
        return False
    if outside_slots < min_slots:
        return False
    return True


def vol_gate(dynamic_fee_pct: float, fee_floor_pct: float) -> bool:
    """True if the pool's dynamic fee is at/above the move floor.

    Meteora adjusts DLMM fees to volatility (getDynamicFee). Below the
    floor the fee is too thin to pay for a re-site — sit cash instead.
    """
    if dynamic_fee_pct < 0 or fee_floor_pct < 0:
        return False
    return dynamic_fee_pct >= fee_floor_pct


def buy_only_bounds(price: float, width_pct: float, gap: float = BUY_GAP) -> tuple[float, float]:
    """Cash wall strictly under price."""
    if price <= 0 or width_pct <= 0:
        return 0.0, 0.0
    w = width_pct / 100.0
    hi = price * (1.0 - gap)
    lo = price * (1.0 - w)
    if lo >= hi:
        lo = hi * 0.99
    return round(lo, 10), round(hi, 10)


def sell_only_bounds(price: float, width_pct: float, gap: float = SELL_GAP) -> tuple[float, float]:
    """Sell wall strictly above price."""
    if price <= 0 or width_pct <= 0:
        return 0.0, 0.0
    w = width_pct / 100.0
    lo = price * (1.0 + gap)
    hi = price * (1.0 + w)
    if hi <= lo:
        hi = lo * 1.01
    return round(lo, 10), round(hi, 10)


def bin_count(lower: float, upper: float, bin_step: float) -> float:
    if lower <= 0 or upper <= lower or bin_step <= 0:
        return math.inf
    step = math.log(1.0 + bin_step / 10000.0)
    if step <= 0:
        return math.inf
    return math.log(upper / lower) / step


def clamp_width_pct(price: float, width_pct: float, bin_step: float, side: str) -> float:
    """Shrink width until bin count < 69."""
    w = max(0.05, float(width_pct))
    for _ in range(24):
        if side == "SELL":
            lo, hi = sell_only_bounds(price, w)
        else:
            lo, hi = buy_only_bounds(price, w)
        if bin_count(lo, hi, bin_step) < MAX_BINS:
            return round(w, 4)
        w *= 0.8
    return round(w, 4)


def bounds_ok(lo: float, hi: float, price: float, side: str) -> bool:
    if lo <= 0 or hi <= lo or price <= 0:
        return False
    if side == "BUY":
        return hi < price
    if side == "SELL":
        return lo > price
    return lo < price < hi

# === JEV decision logic: model proposes, math disposes ===


def _role_clamp(role: str, pct: float, pct_max: float | None = None) -> float:
    """Hold a slice inside its role envelope (major floor/ceiling, caps for the rest).

    `pct_max` overrides the portfolio per-pool cap, which is derived from the
    active run mode's slot budget. Injectable so a caller can pin the envelope it
    means rather than inheriting whatever mode the process happens to be in.
    """
    try:
        pct = max(0.0, float(pct))
    except (TypeError, ValueError):
        return 0.0
    cap = PORTFOLIO_PCT_MAX if pct_max is None else float(pct_max)
    if role == "major":
        if pct > 0:
            pct = max(MAJOR_PCT_MIN, min(MAJOR_PCT_MAX, pct))
    elif role == "portfolio":
        pct = min(cap, pct)                        # modest per-pool cap
    else:  # minor
        pct = min(MINOR_PCT_MAX, pct)
    return pct


def jev_select(candidates: list[dict], book_usd: float = RACE_USD,
               max_positions: int = MAX_POSITIONS,
               min_position_usd: float = MIN_POSITION_USD,
               trust_floor: float = TRUST_NOUL_FLOOR) -> dict:
    """Model Choice: pick the portfolio — the top N distinct-base pools.

    candidates: list of dicts with keys
        pool, base, score (0..100 market score), rug_noul (0..1)
    The math gate disposes: a pool below the trust floor is blocked, and a
    pool whose sized amount would fall under `min_position_usd` is skipped.
    Returns a dict the agent journals verbatim:
        verdict: SELECT | SIT
        chosen:  list of pool dicts (sorted by score desc), no dup bases
        count:   len(chosen)
        reason:  short string
    """
    clean = []
    for c in candidates:
        if pair_is_stable(c):
            continue                       # both sides stable: no directional edge
        noul = c.get("rug_noul")
        if noul is None:
            noul = rug_noul_from_proxies(
                dev_balance=float(c.get("dev_balance", 0.0) or 0.0),
                top10=float(c.get("top_10", c.get("top_10_holders", 0.0)) or 0.0),
                age_hours=c.get("age_hours"),
                flags=c.get("flags"),
                tab=str(c.get("tab", "top") or "top"),
                is_rwa=bool(c.get("is_rwa")),
            )
            c = {**c, "rug_noul": noul}
        if c.get("rank_pass") is False:
            continue
        if float(noul) < trust_floor:
            continue
        clean.append(c)
    clean.sort(key=lambda c: float(c.get("score", c.get("composite", 0.0)) or 0.0), reverse=True)
    chosen, bases = [], set()
    for c in clean:
        base = c.get("base")
        if base in bases:
            continue                       # one position per base
        bases.add(base)
        chosen.append(c)
        if len(chosen) >= max_positions:
            break
    if not chosen:
        return {"verdict": "SIT", "chosen": [], "count": 0,
                "reason": "no clean candidate clears trust floor"}
    # Reserve the floor: if the book can't fund max_positions at the floor,
    # trim to what fits (largest first).
    fit = max_positions
    while fit > 0 and book_usd / fit < min_position_usd:
        fit -= 1
    chosen = chosen[:max(fit, 0)]
    if not chosen:
        return {"verdict": "SIT", "chosen": [], "count": 0,
                "reason": f"book {book_usd:.0f} < floor {min_position_usd:.0f} per slot"}
    return {"verdict": "SELECT", "chosen": chosen, "count": len(chosen),
            "reason": f"portfolio of {len(chosen)} distinct-base pools"}


def jev_size(*, role: str, tvl: float, vol24: float, bin_step: float,
             dynamic_fee_pct: float, outside_slots: int, rug_noul: float,
             vol_daily_pct: float = 2.0, sol_usd: float = 150.0,
             trust_floor: float = TRUST_NOUL_FLOOR, book_usd: float = RACE_USD,
             pct_max: float | None = None,
             pct_override: float | None = None,
             worth_margin: float = WORTH_MARGIN,
             worth_floor: float = WORTH_MARGIN_FLOOR,
             momentum_pct: float | None = None,
             momentum_short_pct: float | None = None,
             base_mint: str = "", base_symbol: str = "",
             quote_mint: str = USDC_MINT, quote_symbol: str = "USDC") -> dict:
    """Model Score: position % of the book + the range width JEV chooses.

    Calm, deep, clean book -> larger, wider. Thin hot book -> smaller.
    Rug smell below the trust floor -> size 0 (SIT). Returns pct (0..1)
    already clamped to the role envelope, plus the chosen width_pct, the
    open-cost vs expected-fee viability, and the raw score for the log.

    `book_usd` is the book the pct is a slice of, and it also sets the amount
    the fee/viability math is computed on. `pct_override` replaces the
    score-derived pct with a model-provided one (still clamped to the role
    envelope, so the model can propose but not escape the risk limits); width,
    open cost and the worth gate are then recomputed on that amount.

    The worth gate does not refuse a position on its own: a slice whose fee
    only marginally covers the open cost is trimmed (`worth_sized_pct`) so the
    other criteria — rug card, fee floor, the model's read — stay decisive.
    """
    if stable_pair(base_mint=base_mint, base_symbol=base_symbol,
                   quote_mint=quote_mint, quote_symbol=quote_symbol):
        return {"pct": 0.0, "score": 0.0, "width_pct": 0.0, "bin_count": 0,
                "worth": False, "worth_tier": "NO", "worth_ratio": 0.0,
                "open_cost": 0.0, "expected_fee": 0.0, "size_usd": 0.0,
                "spread_credit": 0.0, "income": 0.0, "trend_credit": 0.0,
                "reason": "stable-stable pair -> no directional edge, SIT"}
    if rug_noul < trust_floor:
        return {"pct": 0.0, "score": 0.0, "width_pct": 0.0, "bin_count": 0,
                "worth": False, "worth_tier": "NO", "worth_ratio": 0.0,
                "open_cost": 0.0, "expected_fee": 0.0, "size_usd": 0.0,
                "reason": "rug_noul below trust floor -> SIT"}
    if tvl <= 0:
        return {"pct": 0.0, "score": 0.0, "width_pct": 0.0, "bin_count": 0,
                "worth": False, "worth_tier": "NO", "worth_ratio": 0.0,
                "open_cost": 0.0, "expected_fee": 0.0, "size_usd": 0.0,
                "reason": "no tvl -> SIT"}

    # Depth factor: log-scaled so a $5M book isn't 100x a $50k book.
    depth = math.log10(max(tvl, 1.0)) / 7.0          # ~0..1 across $1..$10M
    # Heat factor: volume/tvl tape texture, capped.
    heat = min(vol24 / tvl, 3.0) / 3.0 if tvl else 0.0
    # Cleanliness: rug noul closeness to 1.
    clean = max(0.0, min(1.0, rug_noul))
    score = round(100.0 * (0.5 * depth + 0.3 * heat + 0.2 * clean), 2)

    # Base pct from score, then clamp by role. A model-provided pct replaces
    # the score-derived one but is clamped by the same envelope (math disposes).
    if pct_override is None:
        pct = max(0.0, min(1.0, score / 100.0))
    else:
        try:
            pct = max(0.0, float(pct_override))
        except (TypeError, ValueError):
            pct = 0.0
    pct = _role_clamp(role, pct, pct_max)

    # Horizon follows the role: a major / portfolio position is held long-term
    # (open cost amortizes to near-zero, so it is worth opening); a minor has a
    # ~1h leash, so the open cost genuinely gates whether a NEW minor position
    # is worth it.
    horizon = 30.0 if role in ("major", "portfolio") else (3600.0 / 86400.0)
    width = jev_width(
        tvl=tvl, vol24=vol24, bin_step=bin_step,
        dynamic_fee_pct=dynamic_fee_pct, amount=max(pct * book_usd, 1.0),
        vol_daily_pct=vol_daily_pct, sol_usd=sol_usd, horizon_days=horizon,
        margin=worth_margin, worth_floor=worth_floor,
        momentum_pct=momentum_pct, momentum_short_pct=momentum_short_pct,
    )

    # Worth trims the slice instead of refusing it; re-run width/cost on the
    # trimmed amount so the reported fee, ratio and bin count stay consistent.
    sized = worth_sized_pct(pct, width["worth_ratio"], margin=worth_margin,
                            floor=worth_floor)
    if sized != pct:
        pct = _role_clamp(role, sized, pct_max)
        if pct > 0:
            width = jev_width(
                tvl=tvl, vol24=vol24, bin_step=bin_step,
                dynamic_fee_pct=dynamic_fee_pct, amount=max(pct * book_usd, 1.0),
                vol_daily_pct=vol_daily_pct, sol_usd=sol_usd, horizon_days=horizon,
                margin=worth_margin, worth_floor=worth_floor,
                momentum_pct=momentum_pct, momentum_short_pct=momentum_short_pct,
            )

    return {"pct": round(pct, 3), "score": score, "width_pct": width["width_pct"],
            "bin_count": width["bin_count"], "worth": width["worth"],
            "worth_tier": width.get("worth_tier"), "worth_ratio": width.get("worth_ratio"),
            "open_cost": width["open_cost"], "expected_fee": width["expected_fee"],
            "spread_credit": width.get("spread_credit", 0.0),
            "income": width.get("income", width["expected_fee"]),
            "trend_credit": width.get("trend_credit", 0.0),
            "momentum_pct": momentum_pct,
            "size_usd": round(pct * book_usd, 2),
            "reason": f"depth {depth:.2f} heat {heat:.2f} clean {clean:.2f} | {width['reason']}"}


def pool_verb(*, has_position: bool, filled_buy: bool, position_side: str,
              out_of_range: bool, outside_slots: int, extra_fees_usd: float,
              slip_usd: float, priority_usd: float, rent_usd: float,
              dynamic_fee_pct: float, fee_floor_pct: float = FEE_FLOOR_PCT,
              can_reuse_position: bool = False,
              min_slots: int = MIN_OUTSIDE_SLOTS) -> str:
    """One verb for one pool (major or minor).

    WAIT / SHIFT / REBUILD / SIT gated on the pool's dynamic (volatility)
    fee: below the floor the fee is too thin to pay for a re-site, sit cash.
    SHIFT reuses the position account only when that path is proven live;
    otherwise the honest path is REBUILD (stop keep_position=True, open SELL).
    """
    if not has_position:
        return WAIT
    if filled_buy and position_side == "BUY":
        return SHIFT if can_reuse_position else REBUILD
    if out_of_range:
        if not vol_gate(dynamic_fee_pct, fee_floor_pct):
            return SIT
        if not churn_ok(extra_fees_usd, slip_usd, priority_usd, rent_usd,
                        outside_slots, min_slots):
            return SIT
        return SHIFT if can_reuse_position else REBUILD
    return SIT


def minor_exit(*, rug_noul: float, age_sec: float, flag_after: bool = False,
               kill_pct: float = 10.0, max_sec: int = MINOR_MAX_SEC) -> str:
    """First minor-pool leash that fires. HOLD means stay."""
    if flag_after or rug_noul < TRUST_NOUL_FLOOR:
        return KILL
    if age_sec >= max_sec:
        return KILL
    return HOLD


def haircut_base(executed_base: float, haircut: float = 0.995) -> float:
    if executed_base <= 0:
        return 0.0
    return round(executed_base * haircut, 9)


def red_flag_names(flags: dict) -> list[str]:
    return [k for k in FLAG_KEYS if flags.get(k)]


def fmt_flags(flags: dict) -> str:
    names = red_flag_names(flags)
    return ",".join(names) if names else "clean"


def iter_flag_keys() -> Iterable[str]:
    return FLAG_KEYS


# === Position cost / fee viability and bin-width decision ===
# NOTE: Meteora DLMM bin_step is FIXED per pool on-chain — JEV cannot change
# it. The real lever is the RANGE WIDTH (price span) JEV chooses, which sets
# how many bins the position covers. Wider = more bins, lower concentration,
# fewer re-sites. Narrower = fewer bins, higher fee concentration, more churn.

OPEN_PRIORITY_SOL = 0.004     # create-tx priority fee
OPEN_RENT_SOL = 0.0015        # new position account rent (~2872 bytes)
MIN_WIDTH_PCT = 0.2
MAX_WIDTH_PCT = 6.0


def open_cost_usd(sol_usd: float, slip_usd: float = 0.0,
                  priority_sol: float = OPEN_PRIORITY_SOL,
                  rent_sol: float = OPEN_RENT_SOL) -> float:
    """Estimated one-time USD cost to OPEN a brand-new lp_executor position."""
    return (priority_sol + rent_sol) * max(sol_usd, 0.0) + max(slip_usd, 0.0)


# Stablecoins. A pair quoting two of them has no directional leg at all -- the
# only P&L is fee dust -- so it can be neither a trend ride nor a dip buy.
STABLE_SYMBOLS = {
    "USDC", "USDT", "USDS", "USDE", "SUSDE", "PYUSD", "FDUSD", "USDG", "USD1",
    "DAI", "TUSD", "USDD", "USDP", "GUSD", "USDL", "USDY", "BUCK", "USDX",
    "JUPUSD", "USDF", "USD0", "USR", "SUSD", "EURC", "EURT", "EURS", "JEUR",
}


def _fnum(v, default=None):
    """Lenient float reader -- a bad value falls back to `default`, not 0.0."""
    if v is None or isinstance(v, bool):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def is_stable(mint: str = "", symbol: str = "") -> bool:
    """True if a mint address or a symbol names a known stablecoin."""
    if (mint or "").strip() in STABLE_MINTS:
        return True
    return (symbol or "").strip().upper() in STABLE_SYMBOLS


def is_stable_token(*, mint: str = "", symbol: str = "", name: str = "",
                    price=None, tags=None) -> bool:
    """Stablecoin test for one side, using every signal the feed offers.

    A hardcoded symbol list rots -- JupUSD ("Jupiter USD") is tagged `stable` by
    Jupiter and trades at $1.00 but appears in no list. So: known mint or symbol,
    OR the venue's own `stable` tag, OR a $1-ish peg with USD-styled naming.
    """
    if is_stable(mint, symbol):
        return True
    if any(str(t).strip().lower() == "stable" for t in (tags or [])):
        return True
    blob = f"{symbol or ''} {name or ''}".upper()
    if "USD" in blob:
        p = _fnum(price, None)
        if p is not None and 0.90 <= p <= 1.10:
            return True
    return False


def stable_pair(*, base_mint: str = "", base_symbol: str = "",
                quote_mint: str = "", quote_symbol: str = "",
                base: dict | None = None, quote: dict | None = None) -> bool:
    """True when BOTH sides are stablecoins -> no directional edge, only fee dust.

    Such a pair cannot pay for itself: there is no trend to ride and no dip to buy
    back, so the position earns a fraction of an already-thin fee share. Refuse it
    outright rather than let it consume a slot.

    `base`/`quote` accept a whole token block (address/symbol/name/price/tags) when
    the caller has one; explicit keyword args win over the block.
    """
    b = base if isinstance(base, dict) else {}
    q = quote if isinstance(quote, dict) else {}
    return (
        is_stable_token(mint=(base_mint or str(b.get("address") or "")),
                        symbol=(base_symbol or str(b.get("symbol") or "")),
                        name=str(b.get("name") or ""),
                        price=b.get("price"), tags=b.get("tags"))
        and
        is_stable_token(mint=(quote_mint or str(q.get("address") or "")),
                        symbol=(quote_symbol or str(q.get("symbol") or "")),
                        name=str(q.get("name") or ""),
                        price=q.get("price"), tags=q.get("tags"))
    )


def pair_is_stable(row: dict) -> bool:
    """Stable-pair test for a candidate/pick ROW (scan, rank or select shape).

    Honours a scout-set `base_is_stable` flag first (Jupiter tags are the most
    authoritative read available), then falls back to symbols; the pair label
    supplies the quote side (``CARDS-USDC`` -> USDC).
    """
    if row.get("base_is_stable") is True:
        pair = str(row.get("pair") or "")
        quote_sym = str(row.get("quote_symbol") or "")
        if not quote_sym and "-" in pair:
            quote_sym = pair.rsplit("-", 1)[-1]
        if is_stable(str(row.get("quote") or ""), quote_sym):
            return True
    pair = str(row.get("pair") or "")
    quote_sym = str(row.get("quote_symbol") or "")
    if not quote_sym and "-" in pair:
        quote_sym = pair.rsplit("-", 1)[-1]
    return stable_pair(
        base_mint=str(row.get("base") or ""),
        base_symbol=str(row.get("base_symbol") or ""),
        quote_mint=str(row.get("quote") or ""),
        quote_symbol=quote_sym,
        base=row.get("base_token") if isinstance(row.get("base_token"), dict) else None,
        quote=row.get("quote_token") if isinstance(row.get("quote_token"), dict) else None,
    )


def trend_credit(momentum_pct: float | None, short_pct: float | None = None) -> float:
    """0..1 appetite for riding a dip-buy whose fee alone does not pay.

    Absent momentum is neutral (0.0), so a caller with no trend data behaves
    exactly as before. A flat or falling asset earns nothing: its bid wall would
    fill and stay underwater. `short_pct` can only veto, never add.
    """
    m = _fnum(momentum_pct, None)
    if m is None or m < TREND_MIN_PCT:
        return 0.0
    s = _fnum(short_pct, None)
    if s is not None and s <= TREND_BREAK_PCT:
        return 0.0
    span = max(TREND_FULL_PCT - TREND_MIN_PCT, 1e-9)
    return round(min(1.0, (m - TREND_MIN_PCT) / span), 4)


def in_range_fraction(width_pct: float, vol_daily_pct: float) -> float:
    """Share of the time a band of `width_pct` keeps price inside it.

    Soft decay: a tighter band spends more time out of range in volatile tape,
    but never a hard cliff -- a wide band still earns on a large daily move.
    """
    half = width_pct / 2.0
    return max(0.0, min(1.0, math.exp(-vol_daily_pct / max(half, 1e-6))))


def spread_capture_usd(amount: float, width_pct: float) -> float:
    """Gross round-trip edge of a one-sided wall, before fill probability.

    The wall spans `width_pct` under price, so the average fill sits about half a
    span below it and selling that base back at the mid is worth ~half a span.
    """
    if amount <= 0 or width_pct <= 0:
        return 0.0
    return amount * (width_pct / 200.0)


def credited_spread_usd(amount: float, width_pct: float, credit: float,
                        fill_prob: float = TREND_FILL_PROB) -> float:
    """Spread capture weighted by the odds the wall fills AND the dip recovers.

    `credit` (the trend read) is the term that carries the risk judgement: it
    scales the odds the dip is bought back rather than extended.
    """
    if credit <= 0:
        return 0.0
    prob = max(0.0, min(1.0, _fnum(fill_prob, TREND_FILL_PROB) or 0.0))
    return spread_capture_usd(amount, width_pct) * prob * credit


def expected_fee_usd(*, tvl: float, vol24: float, dynamic_fee_pct: float,
                     amount: float, width_pct: float, vol_daily_pct: float,
                     horizon_days: float = 7.0,
                     ref_width_pct: float = MAX_WIDTH_PCT) -> float:
    """Heuristic expected fees for a position over a horizon.

    - daily fee pool = vol24 * fee%
    - liquidity share = amount / tvl, scaled by concentration: the same money
      spread over a narrower band is a larger share of the bins it covers
      (`ref_width_pct / width_pct`), capped at 100% of the pool.
    - in-range fraction falls as width tightens vs the daily vol move
      (narrow range goes out-of-range more often in volatile tape).
    """
    if tvl <= 0 or amount <= 0 or width_pct <= 0:
        return 0.0
    daily_fee_pool = vol24 * (dynamic_fee_pct / 100.0)
    concentration = max(1.0, ref_width_pct / width_pct) if ref_width_pct > 0 else 1.0
    share = min(1.0, (amount / tvl) * concentration)
    in_range = in_range_fraction(width_pct, vol_daily_pct)
    return daily_fee_pool * share * in_range * horizon_days


def jev_viability(*, open_cost: float, expected_fee: float,
                  margin: float = WORTH_MARGIN,
                  floor: float = WORTH_MARGIN_FLOOR) -> dict:
    """How well the expected fee covers the one-time open cost.

    Returns a tier, not a boolean verdict:
      GO       ratio >= `margin`  -> the full proposed slice is justified
      MARGINAL floor <= ratio < margin -> trim the slice (see `worth_sized_pct`)
      NO       ratio < `floor`    -> the fee does not pay for the position
    """
    ratio = (expected_fee / open_cost) if open_cost > 0 else float("inf")
    if ratio >= margin:
        tier = "GO"
    elif ratio >= floor:
        tier = "MARGINAL"
    else:
        tier = "NO"
    return {"worth": tier != "NO", "tier": tier,
            "open_cost": round(open_cost, 2), "expected_fee": round(expected_fee, 2),
            "ratio": round(ratio, 2) if ratio != float("inf") else float("inf"),
            "reason": (f"fee {expected_fee:.2f} vs open {open_cost:.2f} "
                       f"(ratio {ratio:.2f}, GO at {margin}x, floor {floor}x) -> {tier}")}


def worth_sized_pct(pct: float, ratio: float, *, margin: float = WORTH_MARGIN,
                    floor: float = WORTH_MARGIN_FLOOR,
                    marginal_scale: float = MARGINAL_SCALE) -> float:
    """Trim a proposed slice when the fee only marginally beats the open cost.

    GO       -> keep the proposed slice
    MARGINAL -> keep `marginal_scale` of it, but never below the smallest slice
                that still clears `floor` (so a trimmed position still pays)
    NO       -> 0.0
    """
    try:
        pct = max(0.0, float(pct))
        ratio = float(ratio)
    except (TypeError, ValueError):
        return 0.0
    if pct <= 0:
        return 0.0
    if ratio >= margin:
        return pct
    if ratio < floor:
        return 0.0
    smallest_viable = pct * (floor * TRIM_HEADROOM) / ratio
    return round(max(smallest_viable, pct * marginal_scale), 6)


def jev_width(*, tvl: float, vol24: float, bin_step: float, dynamic_fee_pct: float,
              amount: float, vol_daily_pct: float, sol_usd: float,
              open_cost: float | None = None, horizon_days: float = 7.0,
              min_width_pct: float = MIN_WIDTH_PCT,
              max_width_pct: float = MAX_WIDTH_PCT,
              margin: float = WORTH_MARGIN,
              worth_floor: float = WORTH_MARGIN_FLOOR,
              momentum_pct: float | None = None,
              momentum_short_pct: float | None = None) -> dict:
    """Pick the range width (-> bin count) that maximizes per-day net income.

    Income is fees PLUS the trend-credited spread of the round trip (see
    `trend_credit`). With no momentum read the spread term is 0 and this reduces
    exactly to the fee-only behaviour.

    Tight width = high concentration but more out-of-range time; wide = the
    opposite. We sweep widths and keep the one with the best fee minus the
    sunk open cost, then clamp so bin_count < 69 (Meteora's on-chain cap).
    Returns width_pct, bin_count, expected_fee, open_cost, worth (+ tier/ratio).
    """
    if open_cost is None:
        open_cost = open_cost_usd(sol_usd)
    credit = trend_credit(momentum_pct, momentum_short_pct)
    best_w, best_net, best_fee = min_width_pct, -1e9, 0.0
    w = min_width_pct
    while w <= max_width_pct + 1e-9:
        fee = expected_fee_usd(tvl=tvl, vol24=vol24, dynamic_fee_pct=dynamic_fee_pct,
                               amount=amount, width_pct=w, vol_daily_pct=vol_daily_pct,
                               horizon_days=horizon_days)
        net = fee + credited_spread_usd(amount, w, credit) - open_cost
        if net > best_net:
            best_net, best_w, best_fee = net, w, fee
        w += 0.1
    bins = (best_w / (bin_step / 100.0)) if bin_step > 0 else 1.0
    bins = max(1.0, bins)
    clamped_w = best_w
    if bins >= MAX_BINS and bin_step > 0:
        while bins >= MAX_BINS and clamped_w > min_width_pct:
            clamped_w *= 0.9
            bins = max(1.0, clamped_w / (bin_step / 100.0))
    # Report the spread at the width actually traded (the sweep pick can be
    # clamped under the bin cap), and gate on fees + credits together.
    spread = credited_spread_usd(amount, clamped_w, credit)
    income = best_fee + spread
    viab = jev_viability(open_cost=open_cost, expected_fee=income,
                         margin=margin, floor=worth_floor)
    fee_ratio = (round(best_fee / open_cost, 2) if open_cost > 0 else float("inf"))
    reason = viab["reason"]
    if credit > 0:
        reason = (f"trend {float(momentum_pct):.1f}% credit {credit:.2f} "
                  f"+spread {spread:.2f} | {reason}")
    return {"width_pct": round(clamped_w, 3), "bin_count": int(round(bins)),
            "expected_fee": round(best_fee, 2), "open_cost": round(open_cost, 2),
            "spread_credit": round(spread, 2), "income": round(income, 2),
            "trend_credit": credit, "momentum_pct": momentum_pct,
            "worth": viab["worth"], "worth_tier": viab["tier"],
            "worth_ratio": viab["ratio"], "fee_ratio": fee_ratio,
            "reason": reason}
