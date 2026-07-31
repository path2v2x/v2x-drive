import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "latency_baseline",
    pathlib.Path(__file__).resolve().parents[1] / "tools" / "latency_baseline.py",
)
latency_baseline = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(latency_baseline)


def _record(event="2026-07-14T13:48:55.508Z", decoded=1784036940.13,
            ingested=1784036941, schema=2, trusted=True):
    return {
        "media_timestamp_utc": event,
        "decode_received_at_epoch": decoded,
        "ingested_at_epoch": ingested,
        "timestamp_schema_version": schema,
        "media_time_trusted": trusted,
    }


def test_percentile_nearest_rank():
    values = [5.0, 1.0, 3.0, 2.0, 4.0]
    assert latency_baseline.percentile(values, 0.50) == 3.0
    assert latency_baseline.percentile(values, 0.95) == 5.0
    assert latency_baseline.percentile([], 0.50) is None


def test_record_lags_trusted_schema_v2():
    lags = latency_baseline.record_lags(_record())
    assert lags is not None
    decode_lag, ingest_lag, end_to_end = lags
    assert abs(decode_lag - 4.622) < 0.01
    assert abs(ingest_lag - 0.87) < 0.01
    assert abs(end_to_end - (decode_lag + ingest_lag)) < 1e-6


def test_record_lags_rejects_untrusted_and_legacy():
    assert latency_baseline.record_lags(_record(trusted=False)) is None
    assert latency_baseline.record_lags(_record(schema=1)) is None
    assert latency_baseline.record_lags({}) is None


def test_record_lags_rejects_negative_decode_lag():
    bad = _record(decoded=1784036900.0)  # decoded before the event
    assert latency_baseline.record_lags(bad) is None


def test_iter_range_pages_follows_cursor_and_stops():
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if "next=" not in url:
            return {"items": [{"event_id": "a"}], "next": "tok=="}
        return {"items": [{"event_id": "b"}], "next": None}

    pages = list(latency_baseline.iter_range_pages(
        "https://api", "2026-07-13T17:00:00Z", "2026-07-14T14:15:00Z",
        fetch=fake_fetch,
    ))
    assert [p[0]["event_id"] for p in pages] == ["a", "b"]
    assert len(calls) == 2
    assert "next=tok%3D%3D" in calls[1]
    assert "start=2026-07-13T17%3A00%3A00Z" in calls[0]


def test_iter_range_pages_bounded_by_max_pages():
    def endless(url):
        return {"items": [], "next": "more"}

    pages = list(latency_baseline.iter_range_pages(
        "https://api", "s", "e", max_pages=3, fetch=endless,
    ))
    assert len(pages) == 3


def test_summarize_shape():
    rows = [(4.0, 1.0, 5.0), (6.0, 1.0, 7.0)]
    summary = latency_baseline.summarize(rows, [4500.0, 6500.0], 12)
    assert summary["records_used"] == 2
    assert summary["samples"] == 12
    assert summary["stages"]["decode"]["p95_seconds"] == 6.0
    assert summary["stages"]["end_to_end"]["max_seconds"] == 7.0
    assert summary["health_decode_latency_ms"]["max"] == 6500.0
    assert summary["twin_poll_interval_seconds"] == 5.0
