# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "requests", "beautifulsoup4"]
# ///
"""Integration tests for scrape.py --refresh (no network: fetch is faked).

    uv run tests/test_refresh.py
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scrape  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

# minimal synthetic review block carrying every parsed field
BLOCK = """
<div id="review-{rid}"><div>
  <div class="tw-order-2"><div>
    <div aria-label="5 out of 5 stars" role="img"></div>
    <div class="tw-text-body-xs tw-text-fg-tertiary">July 7, 2026</div></div>
    <div data-truncate-review><div data-truncate-content-copy><p>Body {rid}</p></div></div>
  </div>
  <div class="tw-order-1">
    <div class="heading"><span title="Shop {rid}">Shop {rid}</span></div>
    <div>Norway</div>
  </div>
  <div data-merchant-review-reply><div> </div></div>
</div></div>
"""


def page_html(ids) -> str:
    return (
        '<html><head><title>Reviews: Example App | Store</title></head><body>'
        '<a aria-label="25 total reviews" '
        'href="/example-app/reviews?ratings%5B%5D=5"><span>25</span></a>'
        + "".join(BLOCK.format(rid=i) for i in ids)
        + "</body></html>"
    )


# 3 listing pages: two full, one short (natural end); 23 reviews total
PAGES = {1: page_html(range(1, 11)), 2: page_html(range(11, 21)),
         3: page_html(range(21, 24))}


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    scrape.ensure_schema(c)
    return c


@pytest.fixture()
def fetch_calls(monkeypatch):
    calls = []

    def fake_fetch(session, url, params):
        calls.append(dict(params))
        return PAGES[params.get("page", 1)]

    monkeypatch.setattr(scrape, "fetch", fake_fetch)
    monkeypatch.setattr(scrape.time, "sleep", lambda s: None)
    return calls


def scrape_once(conn, **kw):
    scrape.scrape_app(conn, None, "example-app", None, 0, 50, **kw)


def test_full_scrape_baseline(conn, fetch_calls):
    scrape_once(conn)
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 23
    assert conn.execute(
        "SELECT completed FROM scrape_progress "
        "WHERE app_slug='example-app' AND ratings_key='all'").fetchone()[0] == 1


def test_refresh_stops_early_on_known_pages(conn, fetch_calls, capsys):
    scrape_once(conn)
    calls_before = len(fetch_calls)
    scrape_once(conn, refresh=True)
    out = capsys.readouterr().out
    assert "stopping early" in out
    assert "+0 new, ~0 updated" in out
    # refresh fetched first_html + page 2 only (page 1 reuses first_html),
    # never page 3 — that's the whole point
    assert len(fetch_calls) - calls_before == 2
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 23
    # the baseline stays completed
    assert conn.execute(
        "SELECT completed FROM scrape_progress "
        "WHERE app_slug='example-app'").fetchone()[0] == 1


def test_refresh_counts_updates_and_restores_content(conn, fetch_calls, capsys):
    scrape_once(conn)
    conn.execute("UPDATE reviews SET body='stale text' WHERE review_id='1'")
    conn.commit()
    scrape_once(conn, refresh=True)
    assert "~1 updated" in capsys.readouterr().out
    body = conn.execute(
        "SELECT body FROM reviews WHERE review_id='1'").fetchone()[0]
    assert body == "Body 1"


def test_refresh_without_baseline_falls_back_to_full(conn, fetch_calls, capsys):
    scrape_once(conn, refresh=True)
    captured = capsys.readouterr()
    assert "falling back" in captured.err
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 23
    # the fallback ran as a full scrape and recorded a completed baseline
    assert conn.execute(
        "SELECT completed FROM scrape_progress "
        "WHERE app_slug='example-app'").fetchone()[0] == 1


def test_review_changed_mirrors_coalesce_semantics():
    old = ("1", 5, "2026-07-07", "body", None, 0)
    same = {"rating": 5, "review_date": "2026-07-07", "body": "body",
            "dev_reply": None, "edited": 0}
    assert not scrape._review_changed(old, same)
    assert scrape._review_changed(old, {**same, "body": "new"})
    assert scrape._review_changed(old, {**same, "edited": 1})
    assert scrape._review_changed(old, {**same, "dev_reply": "thanks"})
    # a None parse never overwrites (COALESCE), so it is not a change
    assert not scrape._review_changed(old, {**same, "body": None})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
