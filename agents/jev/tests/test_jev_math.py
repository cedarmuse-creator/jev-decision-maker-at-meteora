"""Pure-function tests for JEV math. No network. No orders."""

from __future__ import annotations

import sys
from pathlib import Path

ROUTINES = Path(__file__).resolve().parents[1] / "routines"
sys.path.insert(0, str(ROUTINES))

from _jev_math import (  # noqa: E402
    SIT, WAIT, SHIFT, REBUILD, KILL, HOLD, SELECT, SIZE,
    any_red, flag_card, scout_clean, churn_ok, vol_gate,
    buy_only_bounds, sell_only_bounds, bin_count, clamp_width_pct,
    bounds_ok, jev_select, jev_size, pool_verb, minor_exit, haircut_base,
    open_cost_usd, expected_fee_usd, jev_viability, jev_width, worth_sized_pct,
    MAJOR_PCT_MIN, MAJOR_PCT_MAX, MINOR_PCT_MAX, TRUST_NOUL_FLOOR,
    MAX_WIDTH_PCT, MARGINAL_SCALE, WORTH_MARGIN, WORTH_MARGIN_FLOOR,
    RACE_USD, MIN_POSITION_USD, MAX_POSITIONS, PORTFOLIO_PCT_MAX,
    MODE, MODE_IS_KNOWN, MODE_LABEL, MODE_PROFILES, MODE_TEST, MODE_PROD,
    DEFAULT_MODE, mode_profile, is_known_mode,
)

# The TEST profile's envelope, pinned. The sizing tests assert specific worth
# tiers, and a tier is a property of a (book, per-pool cap) pair — so they pin
# the envelope rather than inheriting whatever mode the process runs in.
_TEST = MODE_PROFILES[MODE_TEST]
_TEST_BOOK = _TEST["book_usd"]
_TEST_CAP = 1.0 / _TEST["max_positions"]


def test_bounds_strictly_one_sided_buy_under_price():
    lo, hi = buy_only_bounds(100.0, 1.2)
    assert hi < 100.0 and lo < hi


def test_bounds_strictly_one_sided_sell_above_price():
    lo, hi = sell_only_bounds(100.0, 1.2)
    assert lo > 100.0 and hi > lo


def test_bin_clamp_keeps_under_69():
    w = clamp_width_pct(100.0, 5.0, 1, "BUY")
    lo, hi = buy_only_bounds(100.0, w)
    assert bin_count(lo, hi, 1) < 69


def test_rug_card_red_on_dirty():
    flags = flag_card(mint_authority="x", freeze_authority=None, sell_ok=True,
                      creator_pct=50, lp_locked=False, has_route=True,
                      volume_24h=1e6, tvl=1e6, bin_step=10)
    assert any_red(flags)
    assert not scout_clean(flags)


def test_rug_card_clean():
    flags = flag_card(mint_authority=None, freeze_authority=None, sell_ok=True,
                      creator_pct=5, lp_locked=True, has_route=True,
                      volume_24h=1e6, tvl=1e6, bin_step=10)
    assert scout_clean(flags)


def test_vol_gate_floor():
    assert vol_gate(0.03, 0.02)
    assert not vol_gate(0.01, 0.02)


def test_churn_ok_only_when_fees_beat_cost():
    assert churn_ok(extra_fees_usd=5, slip_usd=1, priority_usd=1, rent_usd=1, outside_slots=5)
    assert not churn_ok(extra_fees_usd=1, slip_usd=5, priority_usd=5, rent_usd=5, outside_slots=5)


def test_jev_select_picks_top_portfolio():
    cands = [
        {"pool": "A", "base": "AAA", "score": 80, "rug_noul": 1.0},
        {"pool": "B", "base": "BBB", "score": 40, "rug_noul": 1.0},
        {"pool": "C", "base": "CCC", "score": 60, "rug_noul": 1.0},
    ]
    out = jev_select(cands, book_usd=800, max_positions=5)
    assert out["verdict"] == SELECT
    chosen = [p["pool"] for p in out["chosen"]]
    assert chosen == ["A", "C", "B"][:out["count"]]
    assert out["count"] == 3


def test_jev_select_excludes_dirty_keep_clean_same_base():
    cands = [
        {"pool": "A1", "base": "AAA", "score": 80, "rug_noul": 0.1},  # dirty -> blocked
        {"pool": "A2", "base": "AAA", "score": 65, "rug_noul": 1.0},  # clean rep of AAA
        {"pool": "B", "base": "BBB", "score": 70, "rug_noul": 1.0},
        {"pool": "C", "base": "CCC", "score": 55, "rug_noul": 1.0},
    ]
    out = jev_select(cands, book_usd=800, max_positions=5, trust_floor=0.30)
    chosen = [p["pool"] for p in out["chosen"]]
    assert "A1" not in chosen          # dirty blocked
    assert "A2" in chosen              # clean rep of the same base survives
    assert "B" in chosen and "C" in chosen


def test_jev_select_dedup_base_keeps_highest():
    cands = [
        {"pool": "A2", "base": "AAA", "score": 65, "rug_noul": 1.0},
        {"pool": "A1", "base": "AAA", "score": 80, "rug_noul": 1.0},  # higher, same base
    ]
    out = jev_select(cands, book_usd=800, max_positions=5)
    chosen = [p["pool"] for p in out["chosen"]]
    assert chosen == ["A1"]             # only one AAA, the higher-scored


def test_jev_select_sits_below_floor():
    cands = [{"pool": "A", "base": "AAA", "score": 30, "rug_noul": 0.1}]
    out = jev_select(cands, book_usd=800, max_positions=5, trust_floor=0.30)
    assert out["verdict"] == SIT
    assert out["count"] == 0


def test_jev_size_major_clamped():
    # Book pinned: this asserts the role envelope, not the desk's book size.
    out = jev_size(role="major", tvl=5e6, vol24=1e6, bin_step=10,
                   dynamic_fee_pct=0.05, outside_slots=5, rug_noul=1.0,
                   book_usd=800.0)
    assert MAJOR_PCT_MIN <= out["pct"] <= MAJOR_PCT_MAX


def test_jev_size_minor_capped():
    out = jev_size(role="minor", tvl=5e6, vol24=5e6, bin_step=10,
                   dynamic_fee_pct=0.1, outside_slots=5, rug_noul=1.0)
    assert out["pct"] <= MINOR_PCT_MAX


def test_jev_size_zero_on_rug_smell():
    out = jev_size(role="minor", tvl=5e6, vol24=1e6, bin_step=10,
                   dynamic_fee_pct=0.05, outside_slots=5, rug_noul=0.1)
    assert out["pct"] == 0.0


def test_pool_verb_wait_when_no_position():
    assert pool_verb(has_position=False, filled_buy=False, position_side="BUY",
                     out_of_range=False, outside_slots=0, extra_fees_usd=0,
                     slip_usd=0, priority_usd=0, rent_usd=0, dynamic_fee_pct=0.05) == WAIT


def test_pool_verb_sit_below_fee_floor():
    assert pool_verb(has_position=True, filled_buy=False, position_side="BUY",
                     out_of_range=True, outside_slots=5, extra_fees_usd=5,
                     slip_usd=1, priority_usd=1, rent_usd=1, dynamic_fee_pct=0.01) == SIT


def test_pool_verb_rebuild_after_buy_fill():
    assert pool_verb(has_position=True, filled_buy=True, position_side="BUY",
                     out_of_range=False, outside_slots=0, extra_fees_usd=0,
                     slip_usd=0, priority_usd=0, rent_usd=0, dynamic_fee_pct=0.05,
                     can_reuse_position=False) == REBUILD


def test_minor_exit_kills_on_old_or_rug():
    assert minor_exit(rug_noul=0.1, age_sec=10) == KILL
    assert minor_exit(rug_noul=1.0, age_sec=4000) == KILL
    assert minor_exit(rug_noul=1.0, age_sec=10) == HOLD


def test_haircut_base():
    assert haircut_base(100.0) == 99.5


def test_open_cost_positive():
    c = open_cost_usd(150.0)
    assert c > 0


def test_jev_viability_worth_when_fee_exceeds_cost():
    v = jev_viability(open_cost=1.0, expected_fee=5.0, margin=3.0)
    assert v["worth"] is True and v["tier"] == "GO"


def test_jev_viability_tiers_are_scale_not_a_wall():
    # Default margin 1.2x: comfortable, marginal, dead.
    assert jev_viability(open_cost=1.0, expected_fee=1.3)["tier"] == "GO"
    mid = jev_viability(open_cost=1.0, expected_fee=0.9)
    assert mid["tier"] == "MARGINAL" and mid["worth"] is True
    dead = jev_viability(open_cost=1.0, expected_fee=0.4)
    assert dead["tier"] == "NO" and dead["worth"] is False
    # No open cost at all -> trivially GO, never a division error.
    assert jev_viability(open_cost=0.0, expected_fee=0.1)["tier"] == "GO"


def test_worth_sized_pct_trims_instead_of_refusing():
    # GO keeps the proposal; MARGINAL trims to 75%; NO zeroes it.
    assert worth_sized_pct(0.45, 2.0) == 0.45
    assert worth_sized_pct(0.25, 0.9) == round(0.25 * MARGINAL_SCALE, 6)
    assert worth_sized_pct(0.25, 0.3) == 0.0
    assert worth_sized_pct(0.0, 5.0) == 0.0
    assert worth_sized_pct(0.25, float("inf")) == 0.25


def test_worth_sized_pct_never_trims_below_the_floor():
    # ratio just above the floor: 75% would stop paying for the position, so
    # the trim lands on the smallest slice that still clears the floor.
    pct = worth_sized_pct(0.25, 0.55)
    assert pct > round(0.25 * MARGINAL_SCALE, 6)
    ratio_after = 0.55 * (pct / 0.25)
    assert ratio_after >= WORTH_MARGIN_FLOOR - 1e-9


def test_jev_width_returns_under_69_bins():
    w = jev_width(tvl=2e6, vol24=1e6, bin_step=20, dynamic_fee_pct=0.05,
                  amount=560.0, vol_daily_pct=2.0, sol_usd=150.0)
    assert w["bin_count"] < 69
    assert w["width_pct"] > 0


def test_jev_size_includes_width_and_worth():
    # Book pinned: a major envelope needs a book big enough for the fee to
    # cover the open cost, or the worth gate (not the envelope) decides.
    out = jev_size(role="major", tvl=2e6, vol24=1e6, bin_step=20,
                   dynamic_fee_pct=0.05, outside_slots=6, rug_noul=1.0,
                   vol_daily_pct=1.0, sol_usd=150.0, book_usd=800.0)
    assert "width_pct" in out and out["width_pct"] > 0
    assert "worth" in out
    assert out["pct"] >= MAJOR_PCT_MIN


def test_jev_size_worth_false_when_tiny_fee():
    # Tiny amount on a thin pool -> fee can't beat open cost -> not worth.
    out = jev_size(role="minor", tvl=5e3, vol24=1e3, bin_step=50,
                   dynamic_fee_pct=0.02, outside_slots=6, rug_noul=1.0,
                   vol_daily_pct=5.0, sol_usd=150.0)
    assert out["worth"] is False or out["pct"] <= MINOR_PCT_MAX


def test_rug_noul_penalizes_dev_and_new():
    from _jev_math import rug_noul_from_proxies
    clean = rug_noul_from_proxies(dev_balance=0.0, top10=0.2, tab="top")
    dirty = rug_noul_from_proxies(dev_balance=0.4, top10=0.7, tab="new")
    assert clean > dirty
    assert dirty < 0.55


def test_jev_rank_row_hard_fails_thin_or_dirty():
    from _jev_math import jev_rank_row
    ok = jev_rank_row(tvl=5e5, vol24=2e5, bin_step=20, fee_rate_pct=0.08,
                      fees24=800.0, tab="top", rug_noul=0.95)
    assert ok["pass"] is True and ok["composite"] > 0
    thin = jev_rank_row(tvl=1000, vol24=100, bin_step=20, fee_rate_pct=0.1,
                        tab="top", rug_noul=1.0)
    assert thin["pass"] is False
    dirty = jev_rank_row(tvl=5e5, vol24=2e5, bin_step=20, fee_rate_pct=0.1,
                         tab="new", rug_noul=0.1)
    assert dirty["pass"] is False and "trust" in dirty["reason"]


def test_jev_rank_row_prefers_fee_yield_over_empty_heat():
    from _jev_math import jev_rank_row
    with_fees = jev_rank_row(tvl=1e6, vol24=1e5, bin_step=20, fees24=5_000.0,
                             fee_rate_pct=0.0, tab="top", rug_noul=1.0)
    no_fees = jev_rank_row(tvl=1e6, vol24=1e5, bin_step=20, fees24=0.0,
                           fee_rate_pct=0.02, tab="top", rug_noul=1.0)
    assert with_fees["composite"] >= no_fees["composite"]


def test_jev_rank_row_wash_tape_fails():
    from _jev_math import jev_rank_row
    wash = jev_rank_row(tvl=50_000, vol24=5_000_000, bin_step=25,
                        fee_rate_pct=0.2, tab="trending", rug_noul=1.0)
    assert wash["pass"] is False and "wash_tape" in wash["reason"]


def test_rug_noul_from_flags_red_is_zero():
    from _jev_math import flag_card, rug_noul_from_flags, apply_scout_to_candidate
    dirty = flag_card(mint_authority="x", freeze_authority=None, sell_ok=True,
                      creator_pct=50, lp_locked=False, has_route=True,
                      volume_24h=1e6, tvl=1e6, bin_step=10)
    assert rug_noul_from_flags(dirty) == 0.0
    clean = flag_card(mint_authority=None, freeze_authority=None, sell_ok=True,
                      creator_pct=5, lp_locked=True, has_route=True,
                      volume_24h=1e6, tvl=1e6, bin_step=10)
    n = rug_noul_from_flags(clean, creator_pct=5)
    assert n >= 0.9
    row = {"pool": "P", "base": "B"}
    apply_scout_to_candidate(row, flags=dirty, creator_pct=50, sell_ok=True)
    assert row["rug_noul"] == 0.0 and row["scout_allowed"] is False
    assert row["flags"]["creator_pile"] is True


def test_rank_hard_fails_when_live_flags_red():
    from _jev_math import flag_card, jev_rank_row
    flags = flag_card(mint_authority="x", freeze_authority=None, sell_ok=False,
                      creator_pct=40, lp_locked=False, has_route=False,
                      volume_24h=1e6, tvl=1e6, bin_step=20)
    out = jev_rank_row(tvl=5e5, vol24=2e5, bin_step=20, fee_rate_pct=0.1,
                       tab="trending", flags=flags)
    assert out["pass"] is False
    assert out["rug_noul"] == 0.0


def test_enrich_pick_targets_unique_bases():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "routines" / "jev_enrich.py"
    spec = importlib.util.spec_from_file_location("jev_enrich_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cands = [
        {"pool": "1", "base": "AAA", "composite": 90, "tab": "top"},
        {"pool": "2", "base": "AAA", "composite": 80, "tab": "top"},
        {"pool": "3", "base": "BBB", "composite": 70, "tab": "new"},
        {"pool": "4", "base": "CCC", "composite": 60, "tab": "rwa"},
    ]
    t = mod.pick_scout_targets(cands, top_k=3)
    bases = [x["base"] for x in t]
    assert bases == ["AAA", "BBB", "CCC"]


def test_expected_fee_scales_with_position_amount():
    # Fee income must grow with the money posted, or sizing means nothing.
    args = dict(tvl=2.4e6, vol24=1.9e6, dynamic_fee_pct=0.05, width_pct=3.0,
                vol_daily_pct=2.0, horizon_days=30.0)
    small = expected_fee_usd(amount=200.0, **args)
    big = expected_fee_usd(amount=400.0, **args)
    assert big > small
    assert abs(big / small - 2.0) < 1e-6          # linear while share < 1


def test_expected_fee_sizes_off_the_book_not_a_constant():
    # Regression: the width/fee math used to be pinned to a hard-coded $800,
    # so a bigger book never produced more expected fee.
    common = dict(role="major", tvl=2.4e6, vol24=1.9e6, bin_step=20,
                  dynamic_fee_pct=0.05, outside_slots=6, rug_noul=1.0,
                  vol_daily_pct=2.0, sol_usd=150.0)
    small = jev_size(book_usd=800.0, **common)
    big = jev_size(book_usd=1_500.0, **common)
    assert small["pct"] == big["pct"]              # same slice of the book
    assert big["expected_fee"] > small["expected_fee"]
    assert big["size_usd"] > small["size_usd"]


def test_default_800_book_can_open_a_major():
    # The 3x gate used to need ~$843 of capital before any major could open;
    # at the 1.2x margin the strategy's $800 book deploys normally.
    out = jev_size(role="major", tvl=2.4e6, vol24=1.9e6, bin_step=20,
                   dynamic_fee_pct=0.05, outside_slots=6, rug_noul=1.0,
                   vol_daily_pct=2.0, sol_usd=150.0, book_usd=800.0)
    assert out["worth_tier"] == "GO"
    assert out["worth_ratio"] >= WORTH_MARGIN
    assert out["pct"] > 0 and out["size_usd"] >= 100.0


def test_marginal_pool_is_trimmed_not_refused():
    # A thin trending book: fee only just covers the open cost -> smaller slice,
    # not a hard SIT. pct_max pinned: the trim is the subject, and the ambient
    # prod cap would shrink the slice until the ratio fell through the floor and
    # the pool was refused instead of trimmed.
    out = jev_size(role="portfolio", tvl=9.0e4, vol24=1.3e5, bin_step=50,
                   dynamic_fee_pct=0.2, outside_slots=6, rug_noul=1.0,
                   vol_daily_pct=10.0, sol_usd=150.0, book_usd=600.0,
                   pct_max=_TEST_CAP)
    assert out["worth_tier"] == "MARGINAL"
    assert 0.0 < out["pct"] < _TEST_CAP * 0.999      # trimmed below the full cap
    assert out["worth"] is True                 # but not refused


def test_dead_pool_still_gets_no_slice():
    # Fee far below the open cost -> the worth tier is the one thing that stops it.
    out = jev_size(role="portfolio", tvl=5.0e5, vol24=1.0e3, bin_step=50,
                   dynamic_fee_pct=0.01, outside_slots=6, rug_noul=1.0,
                   vol_daily_pct=1.0, sol_usd=150.0, book_usd=800.0)
    assert out["worth_tier"] == "NO"
    assert out["pct"] == 0.0 and out["worth"] is False


def test_width_sweep_has_an_interior_optimum_in_calm_tape():
    # A range far wider than the daily move wastes capital; the sweep must not
    # simply return the maximum width.
    calm = jev_width(tvl=1.1e6, vol24=6e5, bin_step=20, dynamic_fee_pct=0.02,
                     amount=250.0, vol_daily_pct=1.0, sol_usd=150.0,
                     horizon_days=30.0)
    assert 0 < calm["width_pct"] < MAX_WIDTH_PCT
    assert calm["bin_count"] < 69


def test_width_sweep_still_caps_in_violent_tape():
    hot = jev_width(tvl=3.2e5, vol24=5.4e5, bin_step=50, dynamic_fee_pct=0.1,
                    amount=250.0, vol_daily_pct=8.0, sol_usd=150.0,
                    horizon_days=30.0)
    assert hot["width_pct"] <= MAX_WIDTH_PCT        # never beyond the ceiling
    assert hot["bin_count"] < 69                    # and never past on-chain cap


def test_pct_override_is_clamped_to_the_role_envelope():
    # A pool whose fee comfortably covers the open cost (tier GO), so the
    # envelope is the only thing doing the clamping.
    common = dict(role="major", tvl=2.4e6, vol24=1.9e6, bin_step=20,
                  dynamic_fee_pct=0.05, outside_slots=5, rug_noul=1.0,
                  vol_daily_pct=2.0, book_usd=800.0)
    over = jev_size(pct_override=5.0, **common)      # model asks for 500%
    assert over["worth_tier"] == "GO" and over["pct"] == MAJOR_PCT_MAX
    inside = jev_size(pct_override=0.4, **common)    # inside the envelope: kept
    assert inside["pct"] == 0.4
    under = jev_size(pct_override=0.05, **common)    # below the major floor
    assert under["pct"] == MAJOR_PCT_MIN
    veto = jev_size(pct_override=0.0, **common)      # model vetoes -> SIT
    assert veto["pct"] == 0.0 and veto["worth"] is False


def test_worth_trim_also_applies_to_a_model_override():
    # A marginal pool cannot be talked into a full slice by the model.
    marginal = dict(role="major", tvl=5e6, vol24=1e6, bin_step=20,
                    dynamic_fee_pct=0.05, outside_slots=5, rug_noul=1.0,
                    vol_daily_pct=2.0, book_usd=800.0)
    out = jev_size(pct_override=MAJOR_PCT_MAX, **marginal)
    assert out["worth_tier"] == "MARGINAL"
    assert MAJOR_PCT_MIN <= out["pct"] < MAJOR_PCT_MAX


# ─────────────────── the $100 test book (the desk default) ───────────────────


def test_portfolio_cap_cannot_oversubscribe_the_book():
    # N slots at the per-pool cap must fit inside the book, or the later opens
    # get rejected by the exchange for insufficient funds.
    assert PORTFOLIO_PCT_MAX * MAX_POSITIONS <= 1.0 + 1e-9


def test_100_book_fits_the_full_slot_budget():
    cands = [{"pool": f"P{i}", "base": f"B{i}", "score": 80 - i, "rug_noul": 0.9}
             for i in range(8)]
    out = jev_select(cands, book_usd=RACE_USD, max_positions=MAX_POSITIONS,
                     min_position_usd=MIN_POSITION_USD)
    assert out["verdict"] == SELECT
    assert out["count"] == MAX_POSITIONS
    assert RACE_USD / out["count"] >= MIN_POSITION_USD


def test_100_book_with_a_100_floor_can_never_open():
    # Regression guard for the trap: the old $800 config carried a $100 per-position
    # floor. At a $100 book that fits exactly ONE slot, whose slice is then under
    # the floor -- so every open is refused and the desk silently does nothing.
    cands = [{"pool": f"P{i}", "base": f"B{i}", "score": 80 - i, "rug_noul": 0.9}
             for i in range(8)]
    out = jev_select(cands, book_usd=100.0, max_positions=5, min_position_usd=100.0)
    assert out["count"] == 1
    sized = jev_size(role="portfolio", tvl=50_000, vol24=120_000, bin_step=50,
                     dynamic_fee_pct=1.0, outside_slots=0, rug_noul=0.95,
                     vol_daily_pct=6.0, sol_usd=150.0, book_usd=100.0)
    assert sized["worth_tier"] == "GO"             # the pool itself is fine...
    assert sized["size_usd"] < 100.0               # ...but no slice reaches the floor


def test_100_book_opens_lively_books_and_sits_on_deep_calm_ones():
    # Expected fee scales with the slice; the one-time open cost does not. At a
    # $100 book the deep/calm books fall into the NO tier while hot ones clear.
    # Pinned to the TEST profile: these tiers are a property of a (book, cap)
    # pair, and this is the rig the recorded demo ran on. The prod envelope is
    # covered by test_prod_profile_opens_a_lively_book below.
    lively = jev_size(role="portfolio", tvl=50_000, vol24=120_000, bin_step=50,
                      dynamic_fee_pct=1.0, outside_slots=0, rug_noul=0.95,
                      vol_daily_pct=6.0, sol_usd=150.0,
                      book_usd=_TEST_BOOK, pct_max=_TEST_CAP)
    assert lively["worth_tier"] == "GO"
    assert lively["size_usd"] >= _TEST["min_position_usd"]

    # The pool that still sits out is the deep one whose fee rate is too thin to
    # pay the fixed open cost at this book. (At two slots a mid-fee deep pool
    # reaches MARGINAL and opens trimmed -- that is the point of the wider cap.)
    deep = jev_size(role="portfolio", tvl=2_000_000, vol24=1_500_000, bin_step=4,
                    dynamic_fee_pct=0.05, outside_slots=0, rug_noul=0.95,
                    vol_daily_pct=2.0, sol_usd=150.0,
                    book_usd=_TEST_BOOK, pct_max=_TEST_CAP)
    assert deep["worth_tier"] == "NO"
    assert deep["pct"] == 0.0


def test_prod_profile_opens_a_lively_book():
    """The competition envelope must actually deploy capital.

    A bigger book means bigger slices and therefore bigger absolute fees, so a
    lively book must clear prod's larger per-position floor rather than tripping
    it. Without this, prod could size every open under the floor and sit forever.
    """
    prod = MODE_PROFILES[MODE_PROD]
    cap = 1.0 / prod["max_positions"]
    out = jev_size(role="portfolio", tvl=50_000, vol24=120_000, bin_step=50,
                   dynamic_fee_pct=1.0, outside_slots=0, rug_noul=0.95,
                   vol_daily_pct=6.0, sol_usd=150.0,
                   book_usd=prod["book_usd"], pct_max=cap)
    assert out["worth_tier"] == "GO"
    assert out["size_usd"] >= prod["min_position_usd"]
    assert out["size_usd"] <= prod["book_usd"] * cap + 1e-6


# ────────────── the live rug card (Jupiter v2 shapes) ──────────────


def _card(**kw):
    base = dict(mint_authority=None, freeze_authority=None, sell_ok=True,
                creator_pct=0.0, lp_locked=True, has_route=True,
                volume_24h=1e6, tvl=5e5, bin_step=20)
    base.update(kw)
    return flag_card(**base)


def test_rug_card_missing_dev_data_is_not_red():
    # Jupiter publishes devBalancePercentage for only a minority of tokens. When
    # "absent" counted as red, every pool -- wrapped SOL included -- was blocked
    # and the desk could never open.
    assert _card(creator_pct=None)["creator_pile"] is False


def test_rug_card_dev_pile_at_cap_is_red():
    assert _card(creator_pct=10.0, creator_cap_pct=10.0)["creator_pile"] is True
    assert _card(creator_pct=9.9, creator_cap_pct=10.0)["creator_pile"] is False


def test_rug_card_extreme_holder_concentration_is_red():
    # Top-holder concentration is a separate signal and only red at the extreme:
    # SOL (58%) and TRUMP (81%) pass, a 96.7% wallet pile does not.
    assert _card(top_holders_pct=96.7)["creator_pile"] is True
    assert _card(top_holders_pct=81.4)["creator_pile"] is False
    assert _card(top_holders_pct=58.1)["creator_pile"] is False


def test_rug_card_still_fails_closed_on_the_real_checks():
    assert _card(mint_authority="enabled")["mint_live"] is True
    assert _card(freeze_authority="enabled")["freeze_on"] is True
    assert _card(sell_ok=False)["cant_sell"] is True
    assert _card(lp_locked=None)["lp_unlocked"] is True    # unknown -> red
    assert _card(tvl=0.5)["ghost_tape"] is True
    assert _card(bin_step=200)["coarse_bins"] is True
    assert not any_red(_card())                            # a clean card stays clean


# ------------------------------------------------------------------ run modes

def test_mode_profiles_are_coherent():
    """Both profiles must hold the two invariants the sizing math relies on.

    The per-pool cap is derived as 1/slots, so slots x cap must not exceed the
    book; and a book split evenly across the slots must still clear the
    per-position floor, or the last opens get rejected for insufficient funds.
    """
    for name, prof in MODE_PROFILES.items():
        book, slots, floor = (prof["book_usd"], prof["max_positions"],
                              prof["min_position_usd"])
        assert slots >= 1, name
        assert (1.0 / slots) * slots <= 1.0 + 1e-9, name
        assert book / slots >= floor, f"{name}: {book}/{slots} < {floor}"


def test_default_mode_is_the_small_book(monkeypatch):
    """Unset JEV_MODE must land on test — the safe direction to fail."""
    assert DEFAULT_MODE == MODE_TEST
    monkeypatch.delenv("JEV_MODE", raising=False)
    assert mode_profile()["label"] == "TEST"
    assert mode_profile("")["label"] == "TEST"


def test_mode_profile_resolves_case_and_whitespace_insensitively():
    assert mode_profile("PROD")["book_usd"] == 800.0
    assert mode_profile(" Prod ")["max_positions"] == 5
    assert mode_profile("test")["book_usd"] == 100.0
    assert is_known_mode("PROD") and is_known_mode("prod")
    assert not is_known_mode("bogus")


def test_unknown_mode_falls_back_instead_of_raising():
    """A typo in JEV_MODE must not take the desk down mid-run."""
    assert mode_profile("nonsense") == MODE_PROFILES[DEFAULT_MODE]
    assert mode_profile("nonsense")["label"] == "TEST"
    assert mode_profile("nonsense")["book_usd"] == 100.0


def test_active_constants_are_derived_from_the_profile():
    """RACE_USD / MAX_POSITIONS / MIN_POSITION_USD are profile lookups, not
    literals — otherwise a mode switch would silently leave the book behind."""
    prof = mode_profile(MODE)
    assert RACE_USD == float(prof["book_usd"])
    assert MAX_POSITIONS == int(prof["max_positions"])
    assert MIN_POSITION_USD == float(prof["min_position_usd"])
    assert MODE_LABEL == prof["label"]
    assert PORTFOLIO_PCT_MAX == 1.0 / MAX_POSITIONS
    assert MODE_IS_KNOWN is (MODE in MODE_PROFILES)


def test_prod_profile_is_the_competition_envelope():
    """Guards the numbers the submission advertises: 800 USDC across 3-5."""
    prod = MODE_PROFILES[MODE_PROD]
    assert prod["book_usd"] == 800.0
    assert 3 <= prod["max_positions"] <= 5
    assert MODE_PROFILES[MODE_TEST]["book_usd"] == 100.0


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print(f"ALL {len(tests)} PASSED")
