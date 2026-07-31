#!/usr/bin/env python3
"""Verify paginated, trusted schema-v2 persistence for every street camera."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

PERCEPTION_DIR = Path(__file__).resolve().parents[1]
if str(PERCEPTION_DIR) not in sys.path:
    sys.path.insert(0, str(PERCEPTION_DIR))
from runtime_health import sanitize_source_error  # noqa: E402

CAMERA_IDS = ("ch1", "ch2", "ch3", "ch4")
MEDIA_RECONSTRUCTION_MAX_ERROR_MS = 5.0
DECODE_EPOCH_MAX_ERROR_MS = 5.0
DECODE_LATENCY_MAX_ERROR_MS = 5.0
MAX_INGEST_DELAY_SECONDS = 5
PERSISTENCE_TTL_SECONDS = 7 * 24 * 60 * 60


class VerificationError(RuntimeError):
    pass


def normalize_api_base_url(value):
    parts = urlsplit(str(value).strip())
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise VerificationError(
            "API base must be credential-free HTTPS without query or fragment"
        )
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def parse_utc(value, label):
    if not isinstance(value, str) or not value.strip():
        raise VerificationError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise VerificationError(f"{label} has no timezone")
    return parsed.astimezone(timezone.utc)


def iso_millis(value):
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _fetch_json(url, timeout_seconds):
    request = Request(
        url,
        headers={
            "accept": "application/json",
            "cache-control": "no-cache",
            "user-agent": "v2x-persistence-verifier/1",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(8 * 1024 * 1024 + 1)
    except HTTPError as exc:
        safe_error = sanitize_source_error(exc)
        exc.close()
        raise VerificationError(f"range request failed: {safe_error}") from None
    except Exception as exc:
        raise VerificationError(
            f"range request failed: {sanitize_source_error(exc)}"
        ) from None
    if len(body) > 8 * 1024 * 1024:
        raise VerificationError("range response exceeds bounded size")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("range response is invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise VerificationError("range response has no item list")
    return payload


def fetch_detection_window(
    api_base_url,
    start,
    end,
    page_limit=1000,
    max_pages=100,
    timeout_seconds=20.0,
):
    api_base_url = normalize_api_base_url(api_base_url)
    page_limit = int(page_limit)
    max_pages = int(max_pages)
    if not 1 <= page_limit <= 1000 or not 1 <= max_pages <= 1000:
        raise VerificationError("pagination bounds are invalid")
    common = {
        "start": iso_millis(start),
        "end": iso_millis(end),
        "limit": str(page_limit),
    }
    items = []
    next_token = None
    seen_tokens = set()
    for page_number in range(1, max_pages + 1):
        query = dict(common)
        if next_token is not None:
            query["next"] = next_token
        payload = _fetch_json(
            f"{api_base_url}/detections/range?{urlencode(query)}",
            timeout_seconds,
        )
        items.extend(payload["items"])
        raw_token = payload.get("next")
        next_token = str(raw_token) if raw_token else None
        if next_token is None:
            return items, page_number
        if next_token in seen_tokens:
            raise VerificationError("range pagination repeated a token")
        seen_tokens.add(next_token)
    raise VerificationError("range pagination exceeded maximum pages")


def camera_id_for_item(item):
    device_id = item.get("device_id")
    if not isinstance(device_id, str):
        return None
    for camera_id in CAMERA_IDS:
        if device_id.endswith(f"-{camera_id}"):
            return camera_id
    return None


def trusted_media_time(item):
    reasons = []
    schema = item.get("timestamp_schema_version")
    if schema != 2 or isinstance(schema, bool):
        reasons.append("timestamp_schema")
    if item.get("media_time_trusted") is not True:
        reasons.append("trust_flag")
    if item.get("media_clock_status") != "matched":
        reasons.append("clock_status")
    clock = item.get("media_clock")
    if not isinstance(clock, dict):
        reasons.append("clock_missing")
    else:
        if clock.get("source") != "hls_ext_x_program_date_time":
            reasons.append("clock_source")
        clock_schema = clock.get("schema_version")
        if clock_schema != 1 or isinstance(clock_schema, bool):
            reasons.append("clock_schema")
        if clock.get("evidence_method") != "exact_same_session_pts":
            reasons.append("clock_evidence_method")
    try:
        event_time = parse_utc(item.get("timestamp_utc"), "timestamp_utc")
        media_time = parse_utc(
            item.get("media_timestamp_utc"), "media_timestamp_utc"
        )
        decode_time = parse_utc(
            item.get("decode_received_at_utc"), "decode_received_at_utc"
        )
    except VerificationError:
        return None, reasons + ["timestamp_fields"]
    if event_time != media_time:
        reasons.append("event_media_mismatch")
    if isinstance(clock, dict):
        try:
            anchor_time = parse_utc(
                clock.get("anchor_program_date_time_utc"),
                "media_clock.anchor_program_date_time_utc",
            )
            position_ms = float(clock["position_milliseconds"])
            capture_position_ms = float(
                clock["capture_position_milliseconds"]
            )
            anchor_capture_position_ms = float(
                clock["anchor_capture_position_milliseconds"]
            )
            anchor_fragment_offset_ms = float(
                clock["anchor_fragment_frame_offset_milliseconds"]
            )
            source_pts = clock["source_pts"]
            time_base_numerator = clock["source_time_base_numerator"]
            time_base_denominator = clock["source_time_base_denominator"]
            source_position_ms = (
                source_pts
                * time_base_numerator
                * 1000.0
                / time_base_denominator
            )
            reconstruction_error_ms = abs(
                (
                    anchor_time.timestamp()
                    + position_ms / 1000.0
                    - media_time.timestamp()
                )
                * 1000.0
            )
            transport_values_valid = (
                all(
                    math.isfinite(value)
                    for value in (
                        position_ms,
                        capture_position_ms,
                        anchor_capture_position_ms,
                        anchor_fragment_offset_ms,
                        source_position_ms,
                    )
                )
                and position_ms >= 0.0
                and isinstance(source_pts, int)
                and not isinstance(source_pts, bool)
                and source_pts >= 0
                and isinstance(time_base_numerator, int)
                and not isinstance(time_base_numerator, bool)
                and time_base_numerator > 0
                and isinstance(time_base_denominator, int)
                and not isinstance(time_base_denominator, bool)
                and time_base_denominator > 0
                and abs(source_position_ms - capture_position_ms) <= 0.001
                and abs(anchor_capture_position_ms - capture_position_ms)
                <= 0.001
                and abs(anchor_fragment_offset_ms - position_ms) <= 0.001
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError, VerificationError):
            reconstruction_error_ms = math.inf
            transport_values_valid = False
        if not transport_values_valid:
            reasons.append("clock_transport_provenance")
        if reconstruction_error_ms > MEDIA_RECONSTRUCTION_MAX_ERROR_MS:
            reasons.append("media_reconstruction")
    latency = item.get("decode_latency_ms")
    observed_latency = (decode_time - media_time).total_seconds() * 1000.0
    if (
        not isinstance(latency, (int, float))
        or isinstance(latency, bool)
        or not math.isfinite(float(latency))
        or abs(float(latency) - observed_latency)
        > DECODE_LATENCY_MAX_ERROR_MS
    ):
        reasons.append("decode_latency")
    decode_epoch = item.get("decode_received_at_epoch")
    if (
        not isinstance(decode_epoch, (int, float))
        or isinstance(decode_epoch, bool)
        or not math.isfinite(float(decode_epoch))
        or abs(float(decode_epoch) - decode_time.timestamp()) * 1000.0
        > DECODE_EPOCH_MAX_ERROR_MS
    ):
        reasons.append("decode_epoch")
    ingested_epoch = item.get("ingested_at_epoch")
    if (
        not isinstance(ingested_epoch, int)
        or isinstance(ingested_epoch, bool)
        or not 0
        <= ingested_epoch - int(decode_time.timestamp())
        <= MAX_INGEST_DELAY_SECONDS
    ):
        reasons.append("ingestion_time")
    expires_at = item.get("expires_at")
    expiry_delta_seconds = (
        expires_at - int(media_time.timestamp())
        if isinstance(expires_at, int) and not isinstance(expires_at, bool)
        else None
    )
    if (
        not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or expiry_delta_seconds != PERSISTENCE_TTL_SECONDS
    ):
        reasons.append("expiry_time")
    return event_time, reasons


def evaluate_persistence(
    items,
    start,
    end,
    minimum_span_hours=23.0,
    max_latest_age_hours=6.0,
    minimum_trusted_per_camera=2,
    pages=None,
):
    minimum_span_hours = float(minimum_span_hours)
    max_latest_age_hours = float(max_latest_age_hours)
    minimum_trusted_per_camera = int(minimum_trusted_per_camera)
    if (
        minimum_span_hours < 0
        or max_latest_age_hours <= 0
        or minimum_trusted_per_camera < 2
    ):
        raise VerificationError("acceptance thresholds are invalid")
    grouped = {camera_id: [] for camera_id in CAMERA_IDS}
    rejected = {camera_id: 0 for camera_id in CAMERA_IDS}
    unknown_devices = 0
    invalid_event_id_indexes = set()
    event_ids_by_index = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            invalid_event_id_indexes.add(index)
            continue
        raw_event_id = item.get("event_id")
        if (
            not isinstance(raw_event_id, str)
            or not raw_event_id
            or raw_event_id.strip() != raw_event_id
        ):
            invalid_event_id_indexes.add(index)
            continue
        event_ids_by_index[index] = raw_event_id
    event_id_counts = Counter(event_ids_by_index.values())
    duplicate_event_ids = {
        event_id for event_id, count in event_id_counts.items() if count > 1
    }
    duplicate_event_id_indexes = {
        index
        for index, event_id in event_ids_by_index.items()
        if event_id in duplicate_event_ids
    }

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            unknown_devices += 1
            continue
        camera_id = camera_id_for_item(item)
        if camera_id is None:
            unknown_devices += 1
            continue
        if index in invalid_event_id_indexes or index in duplicate_event_id_indexes:
            rejected[camera_id] += 1
            continue
        timestamp, reasons = trusted_media_time(item)
        if timestamp is None or reasons or not start <= timestamp <= end:
            rejected[camera_id] += 1
            continue
        grouped[camera_id].append(timestamp)

    result = {
        "gate_passed": True,
        "window": {"start": iso_millis(start), "end": iso_millis(end)},
        "thresholds": {
            "minimum_span_hours": minimum_span_hours,
            "max_latest_age_hours": max_latest_age_hours,
            "minimum_trusted_per_camera": minimum_trusted_per_camera,
        },
        "pages": pages,
        "total_items": len(items),
        "unknown_device_items": unknown_devices,
        "invalid_event_id_items": len(invalid_event_id_indexes),
        "duplicate_event_id_items": len(duplicate_event_id_indexes),
        "duplicate_event_ids": len(duplicate_event_ids),
        "cameras": {},
        "reasons": [],
    }
    if unknown_devices:
        result["gate_passed"] = False
        result["reasons"].append(
            f"{unknown_devices} item(s) have an unknown camera device"
        )
    if invalid_event_id_indexes:
        result["gate_passed"] = False
        result["reasons"].append(
            f"{len(invalid_event_id_indexes)} item(s) have an invalid or "
            "whitespace-padded event_id"
        )
    if duplicate_event_id_indexes:
        result["gate_passed"] = False
        result["reasons"].append(
            f"{len(duplicate_event_id_indexes)} item(s) reuse "
            f"{len(duplicate_event_ids)} duplicate event_id value(s)"
        )
    for camera_id in CAMERA_IDS:
        timestamps = sorted(grouped[camera_id])
        count = len(timestamps)
        first = timestamps[0] if timestamps else None
        last = timestamps[-1] if timestamps else None
        span_hours = (
            (last - first).total_seconds() / 3600.0
            if first is not None and last is not None
            else 0.0
        )
        latest_age_hours = (
            (end - last).total_seconds() / 3600.0 if last is not None else None
        )
        camera_passed = (
            rejected[camera_id] == 0
            and count >= minimum_trusted_per_camera
            and span_hours >= minimum_span_hours
            and latest_age_hours is not None
            and -5.0 / 3600.0 <= latest_age_hours <= max_latest_age_hours
        )
        result["cameras"][camera_id] = {
            "gate_passed": camera_passed,
            "trusted_items": count,
            "rejected_items": rejected[camera_id],
            "first_timestamp": iso_millis(first) if first else None,
            "last_timestamp": iso_millis(last) if last else None,
            "span_hours": round(span_hours, 3),
            "latest_age_hours": (
                round(latest_age_hours, 3) if latest_age_hours is not None else None
            ),
        }
        if rejected[camera_id]:
            result["gate_passed"] = False
            result["reasons"].append(
                f"{camera_id} has {rejected[camera_id]} rejected item(s)"
            )
        if not (
            count >= minimum_trusted_per_camera
            and span_hours >= minimum_span_hours
            and latest_age_hours is not None
            and -5.0 / 3600.0 <= latest_age_hours <= max_latest_age_hours
        ):
            result["gate_passed"] = False
            result["reasons"].append(
                f"{camera_id} lacks required trusted persistence span or recency"
            )
    return result


def verify_detection_persistence(
    api_base_url,
    window_hours=24.0,
    minimum_span_hours=23.0,
    max_latest_age_hours=6.0,
    minimum_trusted_per_camera=2,
    page_limit=1000,
    max_pages=100,
    timeout_seconds=20.0,
    now=None,
):
    end = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    window_hours = float(window_hours)
    if window_hours <= 0 or minimum_span_hours > window_hours:
        raise VerificationError("persistence window is invalid")
    start = end - timedelta(hours=window_hours)
    items, pages = fetch_detection_window(
        api_base_url,
        start,
        end,
        page_limit=page_limit,
        max_pages=max_pages,
        timeout_seconds=timeout_seconds,
    )
    return evaluate_persistence(
        items,
        start,
        end,
        minimum_span_hours=minimum_span_hours,
        max_latest_age_hours=max_latest_age_hours,
        minimum_trusted_per_camera=minimum_trusted_per_camera,
        pages=pages,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify trusted 24-hour detection persistence for ch1-ch4."
    )
    parser.add_argument("api_base_url")
    parser.add_argument("--window-hours", type=float, default=24.0)
    parser.add_argument("--minimum-span-hours", type=float, default=23.0)
    parser.add_argument("--max-latest-age-hours", type=float, default=6.0)
    parser.add_argument("--minimum-trusted-per-camera", type=int, default=2)
    parser.add_argument("--page-limit", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    try:
        result = verify_detection_persistence(
            args.api_base_url,
            window_hours=args.window_hours,
            minimum_span_hours=args.minimum_span_hours,
            max_latest_age_hours=args.max_latest_age_hours,
            minimum_trusted_per_camera=args.minimum_trusted_per_camera,
            page_limit=args.page_limit,
            max_pages=args.max_pages,
            timeout_seconds=args.timeout,
        )
    except Exception as exc:
        print(f"verification failed: {sanitize_source_error(exc)}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
