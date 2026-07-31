#!/usr/bin/env python3
"""Atomically freeze a sanitized, hash-bound V2X detection window.

The exporter is deliberately read-only with respect to the public API. It
retains every range page plus a canonical NDJSON union, rejects pagination or
event-ID ambiguity, and reconciles the result with the timeline endpoint. URL
query strings are never written to disk.
"""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import uuid

PERCEPTION_DIR = Path(__file__).resolve().parents[1]
if str(PERCEPTION_DIR) not in sys.path:
    sys.path.insert(0, str(PERCEPTION_DIR))
from runtime_health import sanitize_source_error  # noqa: E402

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
VEHICLE_TYPES = frozenset({"car", "truck", "bus", "vehicle"})


class ExportError(RuntimeError):
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
        raise ExportError(
            "API base must be credential-free HTTPS without query or fragment"
        )
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def parse_utc(value, label="timestamp"):
    if not isinstance(value, str) or not value.strip():
        raise ExportError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExportError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ExportError(f"{label} has no timezone")
    return parsed.astimezone(timezone.utc)


def iso_millis(value):
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def canonical_json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sanitize_url(value):
    if not isinstance(value, str) or not value:
        return value
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https", "ws", "wss"} or not parts.hostname:
        return value
    try:
        port = parts.port
    except ValueError:
        return "[redacted-invalid-url]"
    hostname = parts.hostname
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def sanitize_tree(value, key=""):
    if isinstance(value, dict):
        return {str(k): sanitize_tree(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_tree(item, key) for item in value]
    if isinstance(value, str):
        # Signed resources are not guaranteed to use a field name containing
        # "url". Sanitize every HTTP(S)/WS(S) value; non-URLs are unchanged.
        return sanitize_url(value)
    return value


def _fetch_json_bytes(url, timeout_seconds):
    request = Request(
        url,
        headers={
            "accept": "application/json",
            "cache-control": "no-cache",
            "user-agent": "v2x-detection-corpus-exporter/1",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        safe_error = sanitize_source_error(exc)
        exc.close()
        raise ExportError(f"API request failed: {safe_error}") from None
    except Exception as exc:
        raise ExportError(
            f"API request failed: {sanitize_source_error(exc)}"
        ) from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise ExportError("API response exceeds bounded size")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError("API response is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ExportError("API response is not an object")
    return payload, body


def fetch_window(
    api_base_url,
    start,
    end,
    *,
    page_limit=1000,
    max_pages=100,
    timeout_seconds=20.0,
):
    api_base_url = normalize_api_base_url(api_base_url)
    page_limit = int(page_limit)
    max_pages = int(max_pages)
    if not 1 <= page_limit <= 1000 or not 1 <= max_pages <= 1000:
        raise ExportError("pagination bounds are invalid")
    common = {
        "start": iso_millis(start),
        "end": iso_millis(end),
        "limit": str(page_limit),
    }
    items = []
    pages = []
    next_token = None
    seen_tokens = set()
    for page_number in range(1, max_pages + 1):
        query = dict(common)
        if next_token is not None:
            query["next"] = next_token
        payload, raw_body = _fetch_json_bytes(
            f"{api_base_url}/detections/range?{urlencode(query)}",
            timeout_seconds,
        )
        if not isinstance(payload.get("items"), list):
            raise ExportError("range response has no item list")
        sanitized = sanitize_tree(payload)
        pages.append(
            {
                "number": page_number,
                "raw_sha256": sha256_bytes(raw_body),
                "sanitized_sha256": sha256_bytes(canonical_json_bytes(sanitized)),
                "payload": sanitized,
            }
        )
        items.extend(sanitized["items"])
        raw_token = payload.get("next")
        next_token = str(raw_token) if raw_token else None
        if next_token is None:
            return items, pages
        if next_token in seen_tokens:
            raise ExportError("range pagination repeated a token")
        seen_tokens.add(next_token)
    raise ExportError("range pagination exceeded maximum pages")


def fetch_timeline(api_base_url, start, end, timeout_seconds=20.0):
    api_base_url = normalize_api_base_url(api_base_url)
    query = urlencode(
        {"start": iso_millis(start), "end": iso_millis(end), "bucket": "60"}
    )
    payload, raw_body = _fetch_json_bytes(
        f"{api_base_url}/detections/timeline?{query}", timeout_seconds
    )
    if payload.get("truncated") is True:
        raise ExportError("timeline response is truncated")
    if (
        payload.get("start") != iso_millis(start)
        or payload.get("end") != iso_millis(end)
        or payload.get("bucketSeconds") != 60
    ):
        raise ExportError("timeline response window/bucket does not match request")
    total = payload.get("totalDetections")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ExportError("timeline response has no valid totalDetections")
    sanitized = sanitize_tree(payload)
    return sanitized, {
        "raw_sha256": sha256_bytes(raw_body),
        "sanitized_sha256": sha256_bytes(canonical_json_bytes(sanitized)),
    }


def camera_id(item):
    device = item.get("device_id") if isinstance(item, dict) else None
    if not isinstance(device, str):
        return "unknown"
    return device.rsplit("-", 1)[-1]


def is_trusted_v2(item):
    return (
        isinstance(item, dict)
        and item.get("timestamp_schema_version") == 2
        and not isinstance(item.get("timestamp_schema_version"), bool)
        and item.get("media_time_trusted") is True
        and item.get("media_clock_status") == "matched"
        and isinstance(item.get("media_clock"), dict)
        and item["media_clock"].get("source") == "hls_ext_x_program_date_time"
        and item["media_clock"].get("schema_version") == 1
        and not isinstance(item["media_clock"].get("schema_version"), bool)
        and item.get("timestamp_utc") == item.get("media_timestamp_utc")
    )


def validate_items(items, start, end):
    seen = set()
    missing_ids = []
    duplicate_ids = []
    outside_window = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ExportError(f"range item {index} is not an object")
        event_id = item.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            missing_ids.append(index)
        elif event_id in seen:
            duplicate_ids.append(event_id)
        else:
            seen.add(event_id)
        timestamp = parse_utc(item.get("timestamp_utc"), f"item {index} timestamp")
        if not start <= timestamp <= end:
            outside_window.append(event_id or index)
    if missing_ids:
        raise ExportError("range contains items without event IDs")
    if duplicate_ids:
        raise ExportError("range contains duplicate event IDs")
    if outside_window:
        raise ExportError("range contains items outside the requested window")


def build_manifest(api_base_url, start, end, items, pages, timeline, timeline_hashes):
    timeline_total = timeline["totalDetections"]
    if timeline_total != len(items):
        raise ExportError(
            f"range/timeline count mismatch ({len(items)} != {timeline_total})"
        )
    trusted = [item for item in items if is_trusted_v2(item)]
    vehicles = [
        item for item in trusted
        if str(item.get("object_type", "")).lower() in VEHICLE_TYPES
    ]
    return {
        "schema": "v2x-detection-corpus-snapshot/v1",
        "created_at": iso_millis(datetime.now(timezone.utc)),
        "api_base_url": normalize_api_base_url(api_base_url),
        "window": {"start": iso_millis(start), "end": iso_millis(end)},
        "pages": [
            {
                "number": page["number"],
                "file": f"pages/{page['number']:04d}.json",
                "items": len(page["payload"]["items"]),
                "raw_sha256": page["raw_sha256"],
                "sanitized_sha256": page["sanitized_sha256"],
            }
            for page in pages
        ],
        "timeline": {
            "file": "timeline.json",
            "total_detections": timeline_total,
            **timeline_hashes,
        },
        "counts": {
            "items": len(items),
            "trusted_schema_v2": len(trusted),
            "trusted_vehicles": len(vehicles),
            "items_by_camera": dict(sorted(Counter(camera_id(i) for i in items).items())),
            "trusted_vehicles_by_camera": dict(
                sorted(Counter(camera_id(i) for i in vehicles).items())
            ),
        },
        "privacy": {
            "url_query_strings_retained": False,
            "source_frames_included": False,
        },
        "acceptance_eligible": False,
        "acceptance_note": (
            "This is a sanitized API snapshot. Derived GPS/CARLA positions are "
            "diagnostic and must not be optimizer truth."
        ),
    }


def write_snapshot(output_root, manifest, pages, timeline, items, snapshot_id):
    requested_root = Path(output_root).expanduser()
    if requested_root.is_symlink():
        raise ExportError("output root must not be a symlink")
    requested_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if requested_root.is_symlink():
        raise ExportError("output root must not be a symlink")
    output_root = requested_root.resolve()
    final_dir = output_root / snapshot_id
    if final_dir.exists():
        raise ExportError("snapshot output already exists")
    temp_dir = output_root / f".{snapshot_id}.tmp-{uuid.uuid4().hex}"
    previous_umask = os.umask(0o077)
    try:
        (temp_dir / "pages").mkdir(parents=True, mode=0o700)
        for page in pages:
            path = temp_dir / "pages" / f"{page['number']:04d}.json"
            path.write_bytes(canonical_json_bytes(page["payload"]))
        (temp_dir / "timeline.json").write_bytes(canonical_json_bytes(timeline))
        ordered = sorted(
            items,
            key=lambda item: (
                str(item.get("timestamp_utc", "")),
                str(item.get("event_id", "")),
            ),
        )
        ndjson = b"".join(canonical_json_bytes(item) for item in ordered)
        (temp_dir / "detections.ndjson").write_bytes(ndjson)
        manifest = dict(manifest)
        manifest["artifacts"] = {
            "detections.ndjson": sha256_bytes(ndjson),
            "timeline.json": sha256_bytes((temp_dir / "timeline.json").read_bytes()),
        }
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
        (temp_dir / "manifest.json").write_bytes(manifest_bytes)
        sums = []
        for path in sorted(p for p in temp_dir.rglob("*") if p.is_file()):
            relative = path.relative_to(temp_dir).as_posix()
            sums.append(f"{sha256_bytes(path.read_bytes())}  {relative}\n")
        (temp_dir / "SHA256SUMS").write_text("".join(sums), encoding="utf-8")
        os.rename(temp_dir, final_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        os.umask(previous_umask)
    return final_dir


def prune_snapshots(output_root, retention_count):
    """Retain a bounded number of canonical rolling snapshot directories."""
    if retention_count is None:
        return []
    retention_count = int(retention_count)
    if not 2 <= retention_count <= 720:
        raise ExportError("retention count must be between 2 and 720")
    output_root = Path(output_root).expanduser().resolve()
    snapshots = []
    for path in output_root.iterdir():
        if (
            not path.is_dir()
            or path.is_symlink()
            or re.fullmatch(r"\d{8}T\d{6}Z", path.name) is None
        ):
            continue
        try:
            manifest = json.loads((path / "manifest.json").read_bytes())
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("schema") == "v2x-detection-corpus-snapshot/v1":
            snapshots.append(path)
    removed = []
    for path in sorted(snapshots, key=lambda value: value.name)[:-retention_count]:
        shutil.rmtree(path)
        removed.append(path.name)
    return removed


def export_detection_corpus(
    api_base_url,
    output_root,
    *,
    window_hours=24.0,
    page_limit=1000,
    max_pages=100,
    timeout_seconds=20.0,
    retention_count=None,
    minimum_free_bytes=0,
    now=None,
):
    requested_root = Path(output_root).expanduser()
    if requested_root.is_symlink():
        raise ExportError("output root must not be a symlink")
    minimum_free_bytes = int(minimum_free_bytes)
    if minimum_free_bytes < 0:
        raise ExportError("minimum free bytes cannot be negative")
    disk_probe = requested_root
    while not disk_probe.exists() and disk_probe != disk_probe.parent:
        disk_probe = disk_probe.parent
    if shutil.disk_usage(disk_probe).free < minimum_free_bytes:
        raise ExportError("insufficient free space for detection corpus export")
    end = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    window_hours = float(window_hours)
    if not 0 < window_hours <= 168:
        raise ExportError("window hours must be between 0 and 168")
    start = end - timedelta(hours=window_hours)
    items, pages = fetch_window(
        api_base_url,
        start,
        end,
        page_limit=page_limit,
        max_pages=max_pages,
        timeout_seconds=timeout_seconds,
    )
    validate_items(items, start, end)
    timeline, timeline_hashes = fetch_timeline(
        api_base_url, start, end, timeout_seconds=timeout_seconds
    )
    manifest = build_manifest(
        api_base_url, start, end, items, pages, timeline, timeline_hashes
    )
    snapshot_id = end.strftime("%Y%m%dT%H%M%SZ")
    output = write_snapshot(
        output_root, manifest, pages, timeline, items, snapshot_id
    )
    prune_snapshots(output_root, retention_count)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("api_base_url")
    parser.add_argument("output_root")
    parser.add_argument("--window-hours", type=float, default=24.0)
    parser.add_argument("--page-limit", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--retention-count",
        type=int,
        default=72,
        help="retain this many hourly snapshots (2-720)",
    )
    parser.add_argument(
        "--minimum-free-bytes",
        type=int,
        default=2_147_483_648,
        help="refuse export below this free-space floor",
    )
    args = parser.parse_args(argv)
    try:
        output = export_detection_corpus(
            args.api_base_url,
            args.output_root,
            window_hours=args.window_hours,
            page_limit=args.page_limit,
            max_pages=args.max_pages,
            timeout_seconds=args.timeout,
            retention_count=args.retention_count,
            minimum_free_bytes=args.minimum_free_bytes,
        )
    except Exception as exc:
        print(f"export failed: {sanitize_source_error(exc)}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
