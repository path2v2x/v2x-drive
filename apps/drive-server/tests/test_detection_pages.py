"""Pagination regression tests for reconstruction and twin replay fetches."""

import pytest

from digital_twin_bridge.detection_pages import (
    DetectionPaginationError,
    DetectionPaginationLimitExceeded,
    fetch_all_detection_pages,
)


def test_fetch_all_detection_pages_follows_every_token():
    calls = []
    pages = {
        None: {"items": [{"id": 1}], "next": "page-2"},
        "page-2": {"items": [{"id": 2}], "next": "page-3"},
        "page-3": {"items": [{"id": 3}], "next": None},
    }

    def fetch(start, end, limit, *, next_token=None):
        calls.append((start, end, limit, next_token))
        return pages[next_token]

    result = fetch_all_detection_pages(fetch, "start", "end", page_size=200)

    assert [item["id"] for item in result["items"]] == [1, 2, 3]
    assert result["pages"] == 3
    assert result["next"] is None
    assert [call[3] for call in calls] == [None, "page-2", "page-3"]


def test_fetch_all_detection_pages_rejects_repeated_token():
    def fetch(start, end, limit, *, next_token=None):
        return {"items": [], "next": "same-token"}

    with pytest.raises(DetectionPaginationError, match="repeated"):
        fetch_all_detection_pages(fetch, "start", "end")


def test_fetch_all_detection_pages_does_not_silently_truncate_legacy_fetcher():
    def fetch(start, end, limit):
        return {"items": [{"id": 1}], "next": "unreachable-page"}

    with pytest.raises(DetectionPaginationError, match="does not accept"):
        fetch_all_detection_pages(fetch, "start", "end")


def test_fetch_all_detection_pages_enforces_item_bound():
    def fetch(start, end, limit, *, next_token=None):
        return {"items": [{"id": 1}, {"id": 2}], "next": None}

    with pytest.raises(DetectionPaginationError, match="exceeded 1 items"):
        fetch_all_detection_pages(fetch, "start", "end", max_items=1)


def test_fetch_all_detection_pages_bounds_raise_limit_exceeded():
    def endless(start, end, limit, *, next_token=None):
        return {"items": [], "next": f"after-{next_token}"}

    with pytest.raises(DetectionPaginationLimitExceeded, match="exceeded 3 pages"):
        fetch_all_detection_pages(endless, "start", "end", max_pages=3)

    def oversized(start, end, limit, *, next_token=None):
        return {"items": [{"id": 1}, {"id": 2}], "next": None}

    with pytest.raises(DetectionPaginationLimitExceeded, match="exceeded 1 items"):
        fetch_all_detection_pages(oversized, "start", "end", max_items=1)
    assert issubclass(DetectionPaginationLimitExceeded, DetectionPaginationError)
