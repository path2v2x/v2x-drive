"""Helpers for consuming paginated detection-range API responses.

The history API caps each page and returns ``next``, the ISO timestamp of
the first unreturned item.  Bridge consumers must either follow that token or fail explicitly;
silently treating the first page as the whole time range reconstructs an
incomplete scene and creates gaps during twin replay.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable


class DetectionPaginationError(RuntimeError):
    """Raised when a complete detection range cannot be fetched safely."""


class DetectionPaginationCancelled(DetectionPaginationError):
    """Raised when a caller abandons an in-flight paginated read."""


def _accepts_next_token(fetch_page: Callable[..., dict]) -> bool:
    """Return whether ``fetch_page`` supports the continuation keyword."""
    try:
        parameters = inspect.signature(fetch_page).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "next_token"
        or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def fetch_all_detection_pages(
    fetch_page: Callable[..., dict],
    start: str,
    end: str,
    page_size: int = 200,
    *,
    max_pages: int = 100,
    max_items: int = 100_000,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Fetch every page in a detection time range.

    Legacy test/local fetchers that return a single response and no ``next``
    token remain supported.  If such a fetcher advertises a continuation but
    cannot accept ``next_token``, this helper raises instead of truncating.
    Repeated tokens and configured safety-bound exhaustion also raise.
    """
    if page_size < 1:
        raise ValueError("page_size must be positive")
    if max_pages < 1 or max_items < 1:
        raise ValueError("pagination safety bounds must be positive")

    accepts_next = _accepts_next_token(fetch_page)
    next_token: str | None = None
    seen_tokens: set[str] = set()
    items: list[Any] = []
    pages = 0

    while True:
        if should_stop is not None and should_stop():
            raise DetectionPaginationCancelled("detection pagination cancelled")

        if accepts_next:
            response = fetch_page(
                start,
                end,
                page_size,
                next_token=next_token,
            )
        elif next_token is None:
            response = fetch_page(start, end, page_size)
        else:
            raise DetectionPaginationError(
                "detection fetcher returned a continuation token but does not "
                "accept next_token"
            )

        # A timed-out caller may set its cancellation flag while a bounded
        # HTTP request is still completing.  Check again before retaining or
        # returning that page.  This worker performs reads only; CARLA actor
        # mutation remains on the event-loop thread.
        if should_stop is not None and should_stop():
            raise DetectionPaginationCancelled("detection pagination cancelled")

        if not isinstance(response, dict):
            raise DetectionPaginationError("detection fetcher returned a non-object response")
        page_items = response.get("items", []) or []
        if not isinstance(page_items, list):
            raise DetectionPaginationError("detection response 'items' must be a list")

        pages += 1
        items.extend(page_items)
        if len(items) > max_items:
            raise DetectionPaginationError(
                f"detection pagination exceeded {max_items} items"
            )

        raw_token = response.get("next")
        next_token = str(raw_token) if raw_token else None
        if next_token is None:
            return {
                "items": items,
                "next": None,
                "count": len(items),
                "pages": pages,
            }

        if next_token in seen_tokens:
            raise DetectionPaginationError("detection API repeated a continuation token")
        seen_tokens.add(next_token)
        if pages >= max_pages:
            raise DetectionPaginationError(
                f"detection pagination exceeded {max_pages} pages"
            )
