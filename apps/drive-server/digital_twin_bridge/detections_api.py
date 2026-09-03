"""
Client for the twin server's local detection history.

`path2v2x/v2x-digital-twin` keeps 72 hours of co-perception detections in
SQLite on the same host and serves them at
``GET /detections/history?start=&end=&limit=`` as
``{"items": [{ts, camera, object_id, object_type, confidence, lat, lon}], "next": ISO|null}``.

The drive server's registry, scene reconstructor and page walker were written
against the older cloud record shape, so this module adapts each history item
to that shape at the boundary.  ``next`` is the ISO timestamp of the first
unreturned item and is passed back verbatim as ``start`` to continue.
"""

from typing import Callable

import requests

from digital_twin_bridge.config import Config

DEVICE_ID_PREFIX = "cam-001-"


def history_item_to_detection(item: dict) -> dict:
    """Map one history item to the detection record shape used internally."""
    camera = str(item.get("camera") or "")
    return {
        "object_id": item.get("object_id", ""),
        "object_type": item.get("object_type", "unknown"),
        "confidence_score": item.get("confidence", 0.0),
        "timestamp_utc": item.get("ts", ""),
        "device_id": f"{DEVICE_ID_PREFIX}{camera}" if camera else "",
        "gps_location": {
            "latitude": item.get("lat"),
            "longitude": item.get("lon"),
        },
    }


def make_history_fetcher(config: Config) -> Callable[..., dict]:
    """Create a range fetcher ``fetch(start, end, limit, *, next_token)``.

    Returns ``{"items": [...], "next": ISO | None}`` so it can be paged by
    :func:`digital_twin_bridge.detection_pages.fetch_all_detection_pages`.
    """

    def fetch(
        start: str,
        end: str,
        limit: int = 500,
        *,
        next_token: str | None = None,
    ) -> dict:
        params = {"start": next_token or start, "end": end, "limit": limit}
        resp = requests.get(
            config.DETECTIONS_HISTORY_URL,
            params=params,
            timeout=max(0.5, float(config.SCENE_FETCH_REQUEST_TIMEOUT_SECONDS)),
        )
        resp.raise_for_status()
        page = resp.json()
        raw_items = page.get("items", []) if isinstance(page, dict) else []
        return {
            "items": [history_item_to_detection(item) for item in raw_items],
            "next": page.get("next") if isinstance(page, dict) else None,
        }

    return fetch
