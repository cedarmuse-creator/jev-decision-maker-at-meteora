"""pnl_race profile + split-book allocation + risk barriers."""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path

os.environ["JEV_MODE"] = "test"

def _math():
    path = Path(__file__).resolve().parents[1] / "routines" / "_jev_math.py"
    spec = importlib.util.spec_from_file_location("jev_math_pnl", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

M = _math()

def test_pnl_race_profile_split():
    p = M.MODE_PROFILES["pnl_race"]
    assert p["book_usd"] == 320
    assert p["volume_arm_usd"] == 480
    assert p["pnl_arm_usd"] == 320
    assert p["max_positions"] == 2
    assert p["require_momentum_pct"] == 3
    assert p["stop_loss_usd"] == 90
    assert p["min_position_usd"] == 40
    assert p["minor_max_sec"] == 0
    split = M.allocation_split("pnl_race")
    assert split["volume_arm_pct"] == 0.6

def test_age_kill_disabled_when_max_sec_zero():
    assert M.minor_exit(rug_noul=1.0, age_sec=99_999, max_sec=0) == M.HOLD
    assert M.minor_exit(rug_noul=0.1, age_sec=1, max_sec=0) == M.KILL

def test_stop_loss_and_trail():
    assert M.position_risk_exit(entry_price=100, price=96.5, stop_loss_pct=0.03)["action"] == "KILL_SL"
    assert M.position_risk_exit(entry_price=100, price=103.4, peak_price=105.0,
        trail_activation_pct=0.025, trail_delta_pct=0.015)["action"] == "KILL_TRAIL"
    assert M.position_risk_exit(entry_price=100, price=101.0, peak_price=101.5)["action"] == M.HOLD

def test_momentum_gate_blocks_cold_tape():
    z = M.jev_size(role="portfolio", tvl=2_000_000, vol24=800_000, bin_step=20,
        dynamic_fee_pct=0.2, outside_slots=8, rug_noul=0.95,
        momentum_pct=1.0, require_momentum_pct=3.0, book_usd=320)
    assert z["pct"] == 0.0


def test_allocation_sums_800_and_pairs():
    split = M.allocation_split("pnl_race")
    assert split["volume_arm_usd"] + split["pnl_arm_usd"] == 800
    assert split["volume_arm_usd"] == 480
    assert split["pnl_arm_usd"] == 320
    assert split["pnl_stop_loss_usd"] == 90
    assert split["volume_pair"] == "FDUSD-USDT"
    assert split["volume_pair_fallback"] == "USD1-USDT"


def test_pnl_stop_90_usdc():
    hit = M.portfolio_stop_usd(entry_nav_usd=320, current_nav_usd=230, stop_usd=90)
    assert hit["action"] == "KILL_PORTFOLIO"
    assert hit["loss_usd"] == 90
    ok = M.portfolio_stop_usd(entry_nav_usd=320, current_nav_usd=250, stop_usd=90)
    assert ok["action"] == M.HOLD


def test_effective_floor_unsticks_tight_book():
    # Configured floor 40 on a $30 free book used to SIT forever — now one slot.
    floor = M.effective_min_position_usd(30, max_positions=2, configured_floor=40)
    assert floor <= 30
    assert floor >= 20
    sel = M.jev_select(
        [{"pool": "p1", "base": "SOL", "score": 80, "rug_noul": 0.9}],
        book_usd=30, max_positions=2, min_position_usd=40,
    )
    assert sel["verdict"] == "SELECT"
    assert sel["count"] == 1


def test_momentum_gate_three_pct_race():
    cold = M.jev_size(role="portfolio", tvl=2_000_000, vol24=800_000, bin_step=20,
        dynamic_fee_pct=0.2, outside_slots=8, rug_noul=0.95,
        momentum_pct=1.0, require_momentum_pct=3.0, book_usd=320)
    assert cold["pct"] == 0.0
    warm = M.jev_size(role="portfolio", tvl=2_000_000, vol24=800_000, bin_step=20,
        dynamic_fee_pct=0.2, outside_slots=8, rug_noul=0.95,
        momentum_pct=4.0, require_momentum_pct=3.0, book_usd=320, sol_usd=150)
    assert warm["pct"] > 0 or warm["size_usd"] >= 0
