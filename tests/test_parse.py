# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "requests", "beautifulsoup4"]
# ///
"""Fixture tests for scrape.py's parsers.

    uv run tests/test_parse.py

The fixtures in tests/fixtures/ are synthetic copies of the app-store reviews
page structure (see docs/specs/review-scraper-design.md for the selector
inventory). If Shopify changes its DOM these tests keep passing — run
`uv run scrape.py --check <slug>` against a live page for that — but they
guarantee any change to the parsers keeps every documented case working.
"""

import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scrape  # noqa: E402  (repo root on path)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> BeautifulSoup:
    return BeautifulSoup((FIXTURES / name).read_text(encoding="utf-8"), "html.parser")


@pytest.fixture(scope="module")
def parsed():
    return scrape.parse_reviews(load("reviews_page.html"))


def by_id(parsed, rid):
    return next(r for r in parsed[0] if r["review_id"] == rid)


def test_block_counting(parsed):
    reviews, n_blocks = parsed
    # 3 parseable + 1 broken block; the nested review-reply-77 block is not a review
    assert n_blocks == 4
    assert [r["review_id"] for r in reviews] == ["1001", "1002", "1003"]


def test_plain_review(parsed):
    r = by_id(parsed, "1001")
    assert r["rating"] == 5
    assert r["review_date"] == "2026-07-07"
    assert r["edited"] == 0
    assert r["body"] == "Great app, campaigns are easy to build."
    assert r["shop_name"] == "Demo Shop One"
    assert r["country"] == "Australia"
    assert r["usage_duration"] is None
    assert r["dev_reply"] is None  # empty reply div = no reply


def test_edited_review_with_dev_reply(parsed):
    r = by_id(parsed, "1002")
    assert r["rating"] == 1
    assert r["edited"] == 1
    assert r["review_date"] == "2023-09-09"
    # paragraphs joined with newlines
    assert r["body"] == "Flows kept failing after the update.\nSupport never got back to us."
    assert r["shop_name"] == "Luna Demo Store"
    assert r["country"] == "Hong Kong SAR"
    assert r["usage_duration"] == "2 months using the app"
    assert r["dev_reply"] == "Sorry to hear that — our team will reach out."


def test_rating_only_review(parsed):
    # a review with an empty body paragraph still parses (rating carries it)
    r = by_id(parsed, "1003")
    assert r["rating"] == 4
    assert r["review_date"] == "2024-03-02"
    assert r["body"] == ""
    assert r["country"] == "Türkiye"
    assert r["usage_duration"] == "Over 2 years using the app"
    assert r["dev_reply"] is None


def test_star_distribution():
    dist = scrape.parse_star_distribution(load("reviews_page.html"))
    # 5★ has aria-label="1234 total reviews" → exact count wins over "1.2K";
    # "1,034" -> 1034; pagination / combined-ratings links ignored
    assert dist == {5: 1234, 4: 320, 3: 40, 2: 18, 1: 1034}


def test_empty_page():
    assert scrape.parse_reviews(load("empty_page.html")) == ([], 0)


def test_app_name_from_title():
    assert scrape.app_name_from_title(load("reviews_page.html")) == "Example App"


def test_plausible_listing_end():
    # page 1 empty while reviews are expected → rot, not the end
    assert not scrape.plausible_listing_end(1, 500)
    # page 51 empty with ~500 expected → believable end (50 pages ≈ 500)
    assert scrape.plausible_listing_end(51, 500)
    # far short of the expected count → rot
    assert not scrape.plausible_listing_end(20, 500)
    # nothing expected (tiny filtered scrape) → any empty page is fine
    assert scrape.plausible_listing_end(1, 0)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
