"""Scan schema tests — the parser must match the LIVE Meteora datapi payload.

Regression context: the scan read flat ``volume24h`` / ``fees24h`` / ``bin_step``
keys and ``tokenX`` mints. The live API nests tokens as ``token_x``/``token_y``
dicts, sends windowed ``volume``/``fees`` dicts, and puts ``bin_step`` inside
``pool_config``. Every row was therefore dropped, the scan reported 0 candidates
and silently fell back to DEMO pools — the desk LOOKED alive while reading
fixtures. These tests pin the real shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROUTINES = Path(__file__).resolve().parents[1] / "routines"
sys.path.insert(0, str(ROUTINES))

from jev_scan import (  # noqa: E402
    USDC, TAB_PARAMS, _row, _side, _window, _pool_cfg, _fee_pct,
    _display_pair, _symbol,
)


def test_pair_is_readable_never_a_truncated_mint():
    x = LIVE_POOL["token_x"]["address"]
    # The datapi names the pair -- that is what the reports should show.
    assert _display_pair(LIVE_POOL, x, USDC) == "ANTFUN-USDC"
    assert _symbol(LIVE_POOL, x) == "ANTFUN"
    # No name field: fall back to the two token symbols.
    unnamed = {k: v for k, v in LIVE_POOL.items() if k != "name"}
    assert _display_pair(unnamed, x, USDC) == "ANTFUN-USDC"
    # Nothing readable at all: last-resort mints, so it still is not blank.
    bare = {"token_x": LIVE_POOL["token_x"], "token_y": LIVE_POOL["token_y"]}
    assert _display_pair(bare, "ABCDEFGHIJKLMNOP", USDC) == "ABCDEF-EPjF"


def test_live_row_carries_the_readable_pair():
    row = _row(LIVE_POOL, USDC, "top")
    assert row["pair"] == "ANTFUN-USDC"
    assert row["base_symbol"] == "ANTFUN"
    assert "EPjF" not in row["pair"]          # no truncated-mint label anywhere

# One real pool from https://dlmm.datapi.meteora.ag/pools (trimmed, USDC-quoted).
LIVE_POOL = {
    "address": "54Vp27uLaw4wNLo5n7r4fcC6zLamoQc28xBARjss4EUJ",
    "name": "ANTFUN-USDC",
    "token_x": {"address": "CWZ6BsdnjkDVTGkmL6bGbJXXig6ceef12KvyGQW14cMt",
                "name": "AntFun", "symbol": "ANTFUN", "decimals": 6},
    "token_y": {"address": USDC, "name": "USDC", "symbol": "USDC", "decimals": 6},
    "reserve_x": "GS5Cjmp2WnifFHo6ckJmT2P5pQHXxDTHxN3HTGmJyQhW",
    "reserve_y": "FBuV6Bt7gygU4oiRECAA7B2x7yBwRkfo5pDvXTaik6zo",
    "created_at": 1783657634000,
    "pool_config": {"bin_step": 16, "base_fee_pct": 0.03, "protocol_fee_pct": 10.0},
    "dynamic_fee_pct": 0.0,
    "tvl": 60024769.642333426,
    "current_price": 0.08179021565480554,
    "volume": {"30m": 150457.588, "1h": 251708.250, "24h": 8_400_000.0},
    "fees": {"30m": 41.162, "1h": 68.803, "24h": 25_000.0},
    "is_blacklisted": False,
    "launchpad": "",
    "tags": [],
}


def test_side_reads_nested_token_dicts():
    # The live shape.
    assert _side(LIVE_POOL, "x") == LIVE_POOL["token_x"]["address"]
    assert _side(LIVE_POOL, "y") == USDC
    # Legacy flat shapes must still work.
    assert _side({"tokenX": "AAA"}, "x") == "AAA"
    assert _side({"mint_x": "BBB"}, "x") == "BBB"
    assert _side({}, "x") == ""


def test_window_reads_the_24h_bucket():
    assert _window(LIVE_POOL, "volume") == 8_400_000.0
    assert _window(LIVE_POOL, "fees") == 25_000.0
    assert _window({"volume24h": 5.0}, "volume") == 0.0   # flat key handled by _row


def test_pool_config_and_fee_rate():
    assert _pool_cfg(LIVE_POOL)["bin_step"] == 16
    # Live pools report dynamic_fee_pct 0.0 alongside a real base fee. Reading
    # the volatility field alone reports a 0% fee -> $0 expected fees -> the desk
    # SITs on every pool. The effective rate is base + volatility.
    eff, base = _fee_pct(LIVE_POOL)
    assert base == 0.03
    assert eff == 0.03, "a real base fee must survive a 0.0 dynamic fee"
    eff2, _ = _fee_pct({"pool_config": {"base_fee_pct": 0.2}, "dynamic_fee_pct": 0.001})
    assert abs(eff2 - 0.201) < 1e-9
    assert _fee_pct({}) == (0.0, 0.0)


def test_live_row_opens_at_the_100_book():
    # End-to-end guard for the fee plumbing: a lively live-shaped pool must
    # produce a non-zero expected fee and reach OPEN at a $100 book with a $20
    # floor. Before the base-fee fix this was NO/SIT for every pool.
    from _jev_math import jev_size
    live = dict(LIVE_POOL)
    live["tvl"] = 192_533.0
    live["volume"] = {"24h": 2_755_440.0}
    live["fees"] = {"24h": 5_500.0}
    live["pool_config"] = {"bin_step": 20, "base_fee_pct": 0.2}
    live["dynamic_fee_pct"] = 0.0
    row = _row(live, USDC, "trending")
    # `rug_noul` is stamped by the rank step, not the parser.
    out = jev_size(role="portfolio", tvl=row["tvl"], vol24=row["vol"],
                   bin_step=row["bin_step"], dynamic_fee_pct=row["dynamic_fee_pct"],
                   outside_slots=0, rug_noul=1.0, vol_daily_pct=3.0,
                   sol_usd=150.0, book_usd=100.0)
    assert out["expected_fee"] > 0.0
    assert out["pct"] > 0.0
    assert out["worth_tier"] in ("GO", "MARGINAL")
    assert out["size_usd"] >= 20.0


def test_live_pool_is_parsed_not_dropped():
    row = _row(LIVE_POOL, USDC, "top")
    assert row is not None, "a real live pool was dropped by the parser"
    assert row["pool"] == LIVE_POOL["address"]
    assert row["base"] == LIVE_POOL["token_x"]["address"]
    assert row["tvl"] == 60_024_769.642333426
    assert row["vol"] == 8_400_000.0          # from volume["24h"], not a flat key
    assert row["fees"] == 25_000.0            # from fees["24h"]
    assert row["bin_step"] == 16              # from pool_config
    assert row["dynamic_fee_pct"] == 0.03      # effective rate, not the raw 0.0
    assert row["base_fee_pct"] == 0.03
    assert row["fee_rate_pct"] > 0            # realized fee rate: fees / vol
    assert row["age_hours"] is not None


def test_live_pool_survives_the_candidate_filters():
    # The seat filters that emptied the scan: tvl >= min_tvl, vol >= min_vol,
    # 0 < bin_step <= max_bin_step. All three used to read 0 from a live row.
    row = _row(LIVE_POOL, USDC, "top")
    assert row["tvl"] >= 20_000.0
    assert row["vol"] >= 5_000.0
    assert 0 < row["bin_step"] <= 400.0


def test_row_still_rejects_a_foreign_quote():
    assert _row(LIVE_POOL, "So11111111111111111111111111111111111111112", "top") is None


def test_tab_params_use_the_api_field_order_form():
    # A separate `order=` param (or a bare `fees`/`volume`) is HTTP 400 on every
    # tab, which is what silently emptied the scan.
    for tab, params in TAB_PARAMS.items():
        assert "order" not in params, f"{tab} still sends the invalid order param"
        assert "filter_by" not in params, f"{tab} still sends an invalid filter_by"
        assert ":" in params["sort_by"], f"{tab} sort_by must be field:order"
        assert params["sort_by"].endswith(":desc")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print(f"ALL {len(tests)} PASSED")
