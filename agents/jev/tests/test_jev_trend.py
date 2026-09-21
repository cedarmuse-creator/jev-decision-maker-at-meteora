"""Trend allowance + stable-stable guard.

Two behaviours under test:
  1. A deep book whose FEE alone cannot pay the open cost may still open when the
     asset is trending up -- a bid wall fills on a dip and sells the recovery, so
     the round-trip spread is income too. Without a trend read nothing changes.
  2. A pair quoting two stablecoins is refused outright: no directional leg, so
     it can be neither a trend ride nor a dip buy.
"""
import importlib.util
import sys
from pathlib import Path

R = Path(__file__).resolve().parents[1] / "routines"
sys.path.insert(0, str(R))
spec = importlib.util.spec_from_file_location("_jev_math", R / "_jev_math.py")
_m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_m)

SOL = _m.SOL_MINT
USDC = _m.USDC_MINT
USDT = _m.USDT_MINT

# A deep, thin-fee book: the fee share alone is well under the open cost.
DEEP = dict(role="portfolio", tvl=20_000_000.0, vol24=8_000_000.0, bin_step=4,
            dynamic_fee_pct=0.03, outside_slots=0, rug_noul=1.0,
            vol_daily_pct=2.0, sol_usd=109.03, book_usd=100.0,
            # Pinned on purpose: this test is about the TREND allowance, not the
            # ambient mode's per-pool cap. Left ambient, the prod cap (0.2) would
            # shrink the slice until the credit could no longer carry it.
            pct_max=0.5,
            base_mint=SOL, base_symbol="SOL", quote_mint=USDC, quote_symbol="USDC")


# --- trend_credit ---------------------------------------------------------

def test_no_momentum_read_is_neutral():
    assert _m.trend_credit(None) == 0.0
    assert _m.trend_credit(None, None) == 0.0


def test_flat_or_falling_asset_gets_no_credit():
    for m in (-30.0, -5.0, -0.1, 0.0, 1.0, _m.TREND_MIN_PCT - 0.1):
        assert _m.trend_credit(m) == 0.0, m


def test_credit_scales_above_the_floor_then_saturates():
    assert _m.trend_credit(_m.TREND_MIN_PCT + 0.1) > 0.0
    mid = _m.trend_credit((_m.TREND_MIN_PCT + _m.TREND_FULL_PCT) / 2)
    assert 0.0 < mid < 1.0
    assert _m.trend_credit(_m.TREND_FULL_PCT) == 1.0
    assert _m.trend_credit(500.0) == 1.0


def test_short_window_can_only_veto():
    up = _m.trend_credit(12.0, 2.0)
    assert up == _m.trend_credit(12.0)
    assert _m.trend_credit(12.0, _m.TREND_BREAK_PCT - 0.1) == 0.0


def test_spread_capture_is_half_the_span():
    assert _m.spread_capture_usd(100.0, 6.0) == 3.0     # 6% span -> ~3% of size
    assert _m.spread_capture_usd(0.0, 6.0) == 0.0
    assert _m.spread_capture_usd(100.0, 0.0) == 0.0


# --- the actual ask: deep book opens when trending -------------------------

def test_deep_book_is_refused_flat_but_opens_when_trending_up():
    flat = _m.jev_size(**DEEP)
    assert flat["worth_tier"] == "NO", flat["reason"]
    assert flat["pct"] == 0.0
    assert flat["trend_credit"] == 0.0
    assert flat["spread_credit"] == 0.0

    up = _m.jev_size(**DEEP, momentum_pct=12.0, momentum_short_pct=2.0)
    assert up["trend_credit"] > 0.0
    assert up["spread_credit"] > 0.0
    assert up["worth_tier"] != "NO", up["reason"]
    assert up["pct"] > 0.0
    assert up["income"] > up["expected_fee"]      # the credit is what carried it


def test_a_falling_deep_book_gets_no_allowance():
    # A falling knife is the case the allowance must NOT rescue: the bid wall
    # fills and stays underwater.
    out = _m.jev_size(**DEEP, momentum_pct=-25.0)
    assert out["worth_tier"] == "NO"
    assert out["pct"] == 0.0


def test_a_fee_paying_pool_is_unaffected_by_the_trend_read():
    good = dict(DEEP, tvl=200_000.0, vol24=1_500_000.0, dynamic_fee_pct=0.5)
    flat = _m.jev_size(**good)
    up = _m.jev_size(**good, momentum_pct=30.0)
    assert flat["pct"] > 0.0 and up["pct"] > 0.0
    assert flat["worth_tier"] != "NO" and up["worth_tier"] != "NO"


# --- stable-stable guard --------------------------------------------------

def test_stable_pair_needs_both_sides_stable():
    assert _m.stable_pair(base_mint=USDC, base_symbol="USDC",
                          quote_mint=USDT, quote_symbol="USDT")
    assert not _m.stable_pair(base_mint=SOL, base_symbol="SOL",
                              quote_mint=USDC, quote_symbol="USDC")
    assert not _m.stable_pair(base_mint=USDC, base_symbol="USDC")   # quote unknown


def test_stable_detected_from_the_venue_tag_not_just_a_symbol_list():
    # JupUSD appears in no hardcoded list; Jupiter tags it `stable`.
    assert _m.is_stable_token(symbol="JupUSD", name="Jupiter USD",
                              price=0.9993, tags=["stable", "verified"])
    # USD-styled name pegged near $1, no tag.
    assert _m.is_stable_token(symbol="WEIRDUSD", name="Weird USD", price=1.001)
    # Not a stable: dollar-ish name but nowhere near the peg.
    assert not _m.is_stable_token(symbol="USDOT", name="USDot", price=3.40)
    # Not a stable: no USD naming at all.
    assert not _m.is_stable_token(symbol="SOL", name="Wrapped SOL", price=109.0)


def test_row_guard_uses_symbols_and_the_scout_flag():
    assert _m.pair_is_stable({"pair": "USDC-USDT", "base_symbol": "USDC"})
    assert not _m.pair_is_stable({"pair": "SOL-USDC", "base_symbol": "SOL"})
    # Scout flag from Jupiter tags, even when the symbol looks harmless.
    assert _m.pair_is_stable({"pair": "JupUSD-USDC", "base_is_stable": True,
                              "quote_symbol": "USDC"})
    assert not _m.pair_is_stable({"pair": "JupUSD-SOL", "base_is_stable": True,
                                  "quote_symbol": "SOL"})


def test_stable_stable_is_refused_even_with_a_strong_trend():
    out = _m.jev_size(**dict(DEEP, base_mint=USDC, base_symbol="USDC",
                             quote_mint=USDT, quote_symbol="USDT"),
                      momentum_pct=40.0)
    assert out["worth_tier"] == "NO"
    assert out["pct"] == 0.0
    assert "stable" in out["reason"]


def test_select_skips_stable_stable_rows():
    rows = [
        {"pool": "P1", "base": USDC, "base_symbol": "USDC", "pair": "USDC-USDT",
         "rug_noul": 1.0, "composite": 99.0, "rank_pass": True},
        {"pool": "P2", "base": SOL, "base_symbol": "SOL", "pair": "SOL-USDC",
         "rug_noul": 1.0, "composite": 50.0, "rank_pass": True},
    ]
    out = _m.jev_select(rows, book_usd=100.0, max_positions=2, min_position_usd=12.0)
    pools = [c["pool"] for c in out["chosen"]]
    assert pools == ["P2"], pools
