"""
Offline tests for parsing, math, and bucket logic.
These don't hit the network - they verify the logic that has historically
broken on edge cases (regex parsing, edge buckets, Kelly clamping).
"""

import math

import pytest

from weatherbot.common import (
    parse_temp_range,
    in_bucket,
    bucket_prob,
    calc_ev,
    calc_kelly,
    norm_cdf,
    parse_outcome_prices,
    hours_until,
)


# ---------------------------------------------------------------------------
# parse_temp_range
# ---------------------------------------------------------------------------

class TestParseTempRange:
    def test_between_range(self):
        q = "Will the highest temperature in NYC be between 70-72°F on May 5, 2025?"
        assert parse_temp_range(q) == (70.0, 72.0)

    def test_or_below(self):
        q = "Will the highest temperature in NYC be 50°F or below on May 5, 2025?"
        assert parse_temp_range(q) == (-999.0, 50.0)

    def test_or_higher(self):
        q = "Will the highest temperature in NYC be 100°F or higher on May 5, 2025?"
        assert parse_temp_range(q) == (100.0, 999.0)

    def test_exact_be(self):
        q = "Will the highest temperature be 75°F on May 5, 2025?"
        assert parse_temp_range(q) == (75.0, 75.0)

    def test_celsius(self):
        q = "Will the highest temperature in London be between 18-20°C on May 5, 2025?"
        assert parse_temp_range(q) == (18.0, 20.0)

    def test_no_degree_symbol(self):
        q = "Will the highest temperature be between 70-72F on May 5"
        assert parse_temp_range(q) == (70.0, 72.0)

    def test_negative_temps(self):
        q = "Will the highest temperature be -5°C or below on January 5"
        assert parse_temp_range(q) == (-999.0, -5.0)

    def test_garbage(self):
        assert parse_temp_range("") is None
        assert parse_temp_range("Will Bitcoin hit $100k?") is None
        assert parse_temp_range(None) is None


# ---------------------------------------------------------------------------
# in_bucket
# ---------------------------------------------------------------------------

class TestInBucket:
    def test_inside(self):
        assert in_bucket(71, 70, 72) is True

    def test_lower_edge(self):
        assert in_bucket(70, 70, 72) is True

    def test_upper_edge(self):
        assert in_bucket(72, 70, 72) is True

    def test_outside_low(self):
        assert in_bucket(69, 70, 72) is False

    def test_outside_high(self):
        assert in_bucket(73, 70, 72) is False

    def test_exact_match_bucket(self):
        # When low == high, should round forecast and compare
        assert in_bucket(75.4, 75, 75) is True
        assert in_bucket(75.6, 75, 75) is False  # rounds to 76


# ---------------------------------------------------------------------------
# bucket_prob - edge buckets use normal dist
# ---------------------------------------------------------------------------

class TestBucketProb:
    def test_regular_in(self):
        assert bucket_prob(71, 70, 72) == 1.0

    def test_regular_out(self):
        assert bucket_prob(73, 70, 72) == 0.0

    def test_or_below_high_prob(self):
        # forecast 40, bucket "50 or below", sigma=2 → very high prob
        p = bucket_prob(40, -999, 50, sigma=2.0)
        assert p > 0.99

    def test_or_below_low_prob(self):
        # forecast 60, bucket "50 or below" → very low prob
        p = bucket_prob(60, -999, 50, sigma=2.0)
        assert p < 0.01

    def test_or_higher_at_threshold(self):
        # forecast == threshold, "100 or higher" → 50/50
        p = bucket_prob(100, 100, 999, sigma=2.0)
        assert abs(p - 0.5) < 0.001

    def test_sigma_zero_safety(self):
        # sigma=0 should not divide by zero
        p = bucket_prob(50, -999, 50, sigma=0.0)
        assert 0.0 <= p <= 1.0


# ---------------------------------------------------------------------------
# calc_ev
# ---------------------------------------------------------------------------

class TestEV:
    def test_positive_ev(self):
        # 50% true prob, 25c price → big edge
        ev = calc_ev(0.5, 0.25)
        assert ev > 0

    def test_zero_ev(self):
        # 25% true prob, 25c price → break even
        ev = calc_ev(0.25, 0.25)
        assert abs(ev) < 0.001

    def test_negative_ev(self):
        ev = calc_ev(0.10, 0.50)
        assert ev < 0

    def test_invalid_price(self):
        assert calc_ev(0.5, 0) == 0.0
        assert calc_ev(0.5, 1) == 0.0
        assert calc_ev(0.5, -0.1) == 0.0


# ---------------------------------------------------------------------------
# calc_kelly
# ---------------------------------------------------------------------------

class TestKelly:
    def test_no_edge_returns_zero(self):
        # p == price → no edge → no bet
        assert calc_kelly(0.25, 0.25) == 0.0

    def test_full_edge(self):
        # massive edge - but never returns more than 1.0
        k = calc_kelly(0.99, 0.01)
        assert 0 < k <= 1.0

    def test_negative_edge_returns_zero(self):
        assert calc_kelly(0.10, 0.50) == 0.0

    def test_fractional_kelly(self):
        full = calc_kelly(0.5, 0.25, fraction=1.0)
        quarter = calc_kelly(0.5, 0.25, fraction=0.25)
        assert abs(quarter - full * 0.25) < 0.01

    def test_invalid_price(self):
        assert calc_kelly(0.5, 0) == 0.0
        assert calc_kelly(0.5, 1) == 0.0


# ---------------------------------------------------------------------------
# norm_cdf - sanity vs scipy values
# ---------------------------------------------------------------------------

class TestNormCDF:
    def test_zero(self):
        assert abs(norm_cdf(0) - 0.5) < 1e-6

    def test_one_sigma(self):
        # P(Z < 1) ≈ 0.8413
        assert abs(norm_cdf(1) - 0.8413) < 1e-3

    def test_two_sigma(self):
        # P(Z < 2) ≈ 0.9772
        assert abs(norm_cdf(2) - 0.9772) < 1e-3

    def test_negative(self):
        # P(Z < -1) ≈ 0.1587
        assert abs(norm_cdf(-1) - 0.1587) < 1e-3


# ---------------------------------------------------------------------------
# parse_outcome_prices - Polymarket returns JSON-encoded strings
# ---------------------------------------------------------------------------

class TestParseOutcomePrices:
    def test_json_string(self):
        m = {"outcomePrices": "[0.3, 0.7]"}
        assert parse_outcome_prices(m) == (0.3, 0.7)

    def test_actual_list(self):
        m = {"outcomePrices": [0.3, 0.7]}
        assert parse_outcome_prices(m) == (0.3, 0.7)

    def test_missing(self):
        assert parse_outcome_prices({}) == (0.5, 0.5)

    def test_malformed(self):
        assert parse_outcome_prices({"outcomePrices": "not json"}) == (0.5, 0.5)

    def test_single_outcome(self):
        m = {"outcomePrices": "[0.3]"}
        yes, no = parse_outcome_prices(m)
        assert yes == 0.3
        assert abs(no - 0.7) < 0.001


# ---------------------------------------------------------------------------
# hours_until - must handle Polymarket's Z-suffix and bad input
# ---------------------------------------------------------------------------

class TestHoursUntil:
    def test_none(self):
        assert hours_until(None) == 999.0

    def test_empty(self):
        assert hours_until("") == 999.0

    def test_garbage(self):
        assert hours_until("not a date") == 999.0

    def test_past(self):
        # 2020 is in the past → 0
        assert hours_until("2020-01-01T00:00:00Z") == 0.0

    def test_z_suffix_handled(self):
        # must not raise - common Polymarket format
        result = hours_until("2099-01-01T00:00:00Z")
        assert result > 0
