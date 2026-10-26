"""A detection range too busy for the page bounds is narrowed instead of failing the start."""

import pytest

from tests.conftest import SAMPLE_DETECTIONS

START = "2026-03-22T17:00:00Z"
END = "2026-03-22T18:00:00Z"


def busy_fetcher(first_pages, fits_minutes):
    """History fetcher that records the start of every range it is asked for.

    A range longer than ``fits_minutes`` keeps handing out continuation tokens,
    so it runs past the page bound; a shorter one returns one cone.
    """
    from digital_twin_bridge.drive_server import _parse_iso_utc

    tokens = iter(range(1_000_000))

    def fetch(start, end, limit=500, *, next_token=None):
        if next_token is None:
            first_pages.append(start)
        begin, finish = _parse_iso_utc(start), _parse_iso_utc(end)
        if begin is not None and (finish - begin).total_seconds() <= fits_minutes * 60:
            return {"items": [SAMPLE_DETECTIONS[0]], "next": None}
        return {"items": [], "next": f"token-{next(tokens)}"}

    return fetch


def make_session(mock_world, fetch):
    from digital_twin_bridge.drive_server import DriveSession

    return DriveSession(
        world=mock_world,
        carla_map=mock_world.get_map(),
        api_fetcher=fetch,
        scene_fetch_max_pages=2,
    )


@pytest.mark.unit
def test_iso_helpers_round_trip_javascript_times():
    from digital_twin_bridge.drive_server import _format_iso_utc, _parse_iso_utc

    assert _format_iso_utc(_parse_iso_utc("2026-10-26T19:31:14.200Z")) == "2026-10-26T19:31:14.200Z"
    assert _format_iso_utc(_parse_iso_utc("2026-10-26T12:31:14-07:00")) == "2026-10-26T19:31:14.000Z"
    assert _parse_iso_utc("") is None
    assert _parse_iso_utc(None) is None
    assert _parse_iso_utc("an hour ago") is None


@pytest.mark.unit
class TestSceneFetchFallback:
    @pytest.mark.asyncio
    async def test_busy_hour_is_narrowed_to_its_most_recent_part(self, mock_world):
        first_pages = []
        session = make_session(mock_world, busy_fetcher(first_pages, fits_minutes=15))
        ready = await session.start(START, END)
        try:
            assert first_pages == [
                START,
                "2026-03-22T17:30:00.000Z",
                "2026-03-22T17:45:00.000Z",
            ]
            assert ready["type"] == "session_ready"
            assert ready["scene_start"] == "2026-03-22T17:45:00.000Z"
            assert ready["objects_count"] == 1
            assert session.vehicle is not None
        finally:
            session.end()

    @pytest.mark.asyncio
    async def test_range_too_busy_even_at_a_minute_starts_without_scene(self, mock_world):
        first_pages = []
        session = make_session(mock_world, busy_fetcher(first_pages, fits_minutes=0))
        ready = await session.start(START, END)
        try:
            # 60, 30, 15, 7.5, 3.75 and 1.875 minutes; half of the last is under a minute
            assert first_pages == [
                START,
                "2026-03-22T17:30:00.000Z",
                "2026-03-22T17:45:00.000Z",
                "2026-03-22T17:52:30.000Z",
                "2026-03-22T17:56:15.000Z",
                "2026-03-22T17:58:07.500Z",
            ]
            assert ready["type"] == "session_ready"
            assert ready["scene_start"] is None
            assert ready["objects_count"] == 0
            assert ready["scene_actor_ids"] == []
            assert session.vehicle is not None
        finally:
            session.end()

    @pytest.mark.asyncio
    async def test_unparseable_range_is_not_narrowed(self, mock_world):
        first_pages = []
        session = make_session(mock_world, busy_fetcher(first_pages, fits_minutes=15))
        ready = await session.start("an hour ago", END)
        try:
            assert first_pages == ["an hour ago"]
            assert ready["scene_start"] is None
            assert ready["objects_count"] == 0
        finally:
            session.end()

    @pytest.mark.asyncio
    async def test_malformed_pages_still_fail_the_start(self, mock_world):
        from digital_twin_bridge.detection_pages import DetectionPaginationError

        first_pages = []

        def repeating(start, end, limit=500, *, next_token=None):
            if next_token is None:
                first_pages.append(start)
            return {"items": [], "next": "same-token"}

        session = make_session(mock_world, repeating)
        with pytest.raises(DetectionPaginationError, match="repeated"):
            await session.start(START, END)
        assert first_pages == [START]
        assert session.vehicle is None
        assert mock_world.spawned_actors == []
