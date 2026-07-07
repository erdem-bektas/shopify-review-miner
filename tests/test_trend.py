# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///
"""Unit tests for the trend classification in opportunity_report.py.

    uv run tests/test_trend.py

The algorithm here is normative for the JS mirror in reviews.html: if these
expectations change, change the JS the same way.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import opportunity_report as opp  # noqa: E402

CURRENT_YEAR = 2026


def tag(year, theme="x", kind="feature_gap", rid=None):
    return {"review_id": rid or f"r{year}", "review_date": f"{year}-06-01",
            "theme": theme, "kind": kind, "churn_signal": 0}


# one tagged review per year 2019–2026 → denominator 1 review/year
APP_TAGS = [tag(y, theme="other", kind="praise") for y in range(2019, 2027)]


def classify(feat):
    label, _ = opp.trend_of(feat, APP_TAGS + feat, CURRENT_YEAR)
    return label


def test_growing_recent_surge():
    feat = [tag(2025, rid=f"a{i}") for i in range(2)] + \
           [tag(2026, rid=f"b{i}") for i in range(2)]
    assert classify(feat) == "growing"


def test_fading_old_complaints_gone_quiet():
    feat = [tag(2019, rid=f"a{i}") for i in range(2)] + \
           [tag(2020, rid=f"b{i}") for i in range(2)]
    assert classify(feat) == "fading"


def test_stable_uniform_series():
    feat = [tag(y, rid=f"u{y}") for y in range(2019, 2027)]
    assert classify(feat) == "stable"


def test_insufficient_below_min_tags():
    feat = [tag(2025, rid="a"), tag(2026, rid="b")]  # 2 < TREND_MIN_TAGS
    assert classify(feat) is None


def test_insufficient_short_history():
    # app with only 2 distinct years of data → no trend call
    app_tags = [tag(2025, theme="other", kind="praise"),
                tag(2026, theme="other", kind="praise")]
    feat = [tag(2026, rid=f"c{i}") for i in range(5)]
    label, _ = opp.trend_of(feat, app_tags + feat, CURRENT_YEAR)
    assert label is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
