#!/usr/bin/env python3
"""Build read-only evidence for one archived-video/detection correlation.

The verifier requests a short ON_DEMAND HLS session from the public read API,
keeps every signed URL in memory, selects the fMP4 fragment whose
EXT-X-PROGRAM-DATE-TIME contains the persisted media timestamp, and extracts
the nearest encoded video frame.  It then validates the persisted bounding box
against that frame and saves a crop.  A local YOLO model can optionally provide
semantic visual corroboration.

This tool never controls CARLA, the Drive bridge, a live service, or an AWS
resource.  It only performs HTTP GETs and writes local evidence artifacts.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


CAMERA_IDS = ("ch1", "ch2", "ch3", "ch4")
TRUSTED_MEDIA_CLOCK_SOURCE = "hls_ext_x_program_date_time"
TRUSTED_MEDIA_CLOCK_SCHEMA_VERSION = 1
TRUSTED_DETECTION_TIMESTAMP_SCHEMA_VERSION = 2
MEDIA_CLOCK_CONSISTENCY_TOLERANCE_MS = 5.0
MEDIA_TIMESTAMP_FIELDS = (
    "media_timestamp_utc",
    "source_timestamp_utc",
    "video_timestamp_utc",
)
PLAYLIST_LIMIT = 8 * 1024 * 1024
INIT_FRAGMENT_LIMIT = 32 * 1024 * 1024
MEDIA_FRAGMENT_LIMIT = 192 * 1024 * 1024
JSON_LIMIT = 2 * 1024 * 1024
SNAPSHOT_MANIFEST_LIMIT = 2 * 1024 * 1024
SNAPSHOT_DETECTIONS_LIMIT = 128 * 1024 * 1024
FFPROBE_OUTPUT_LIMIT = 16 * 1024 * 1024
SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


class VerificationError(RuntimeError):
    """Expected, safely reportable verification failure."""


@dataclass(frozen=True)
class OnDemandSession:
    camera_id: str
    hls_url: str
    playback_mode: str
    expires_in: int | None
    requested_start: datetime
    requested_end: datetime


@dataclass(frozen=True)
class MediaSegment:
    sequence: int
    uri: str
    map_uri: str
    program_date_time: datetime
    duration_seconds: float

    @property
    def end_time(self) -> datetime:
        return self.program_date_time + timedelta(seconds=self.duration_seconds)


@dataclass(frozen=True)
class ParsedMediaPlaylist:
    sha256: str
    segments: tuple[MediaSegment, ...]


def parse_utc_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise VerificationError(f"{label} is missing")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationError(f"{label} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise VerificationError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def iso_millis(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def normalize_api_base_url(value: object) -> str:
    """Accept a credential-free API origin/path and never echo rejected input."""
    try:
        parts = urlsplit(str(value).strip())
        port = parts.port
    except (TypeError, ValueError) as exc:
        raise VerificationError("API base URL is invalid") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise VerificationError(
            "API base URL must be credential-free HTTP(S) without query or fragment"
        )
    hostname = parts.hostname.lower()
    if parts.scheme == "http" and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise VerificationError("remote API base URL must use HTTPS")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc = f"[{hostname}]:{port}" if ":" in hostname else f"{hostname}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def _validated_internal_url(value: object, *, allow_local_http: bool = True) -> str:
    """Validate a signed URL for internal use without formatting it in errors."""
    try:
        parts = urlsplit(str(value))
        _ = parts.port
    except (TypeError, ValueError) as exc:
        raise VerificationError("HLS resource URL is invalid") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise VerificationError("HLS resource URL is invalid")
    if parts.scheme == "http" and (
        not allow_local_http
        or parts.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}
    ):
        raise VerificationError("remote HLS resources must use HTTPS")
    return str(value)


def _origin(value: str) -> tuple[str, str, int | None]:
    parts = urlsplit(value)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


def resolve_hls_uri(base_url: str, reference: str) -> str:
    """Resolve a playlist child and reject cross-origin signed URL forwarding."""
    child = _validated_internal_url(urljoin(base_url, reference.strip()))
    if _origin(child) != _origin(base_url):
        raise VerificationError("HLS playlist attempted a cross-origin resource")
    return child


def _network_error(label: str, exc: BaseException) -> VerificationError:
    if isinstance(exc, HTTPError):
        return VerificationError(f"{label} request failed with HTTP {exc.code}")
    if isinstance(exc, URLError):
        reason_name = type(exc.reason).__name__
        return VerificationError(f"{label} request failed ({reason_name})")
    return VerificationError(f"{label} request failed ({type(exc).__name__})")


def fetch_bytes(url: str, *, limit: int, timeout_seconds: float, label: str) -> bytes:
    """Bounded GET whose errors cannot contain a signed query string."""
    _validated_internal_url(url)
    request = Request(
        url,
        headers={
            "accept": "application/json, application/vnd.apple.mpegurl, video/mp4, */*",
            "cache-control": "no-store",
            "user-agent": "v2x-historical-correlation-verifier/1",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(limit + 1)
    except HTTPError as exc:
        error = _network_error(label, exc)
        exc.close()
        raise error from None
    except Exception as exc:
        error = _network_error(label, exc)
        close = getattr(exc, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        raise error from None
    if len(body) > limit:
        raise VerificationError(f"{label} response exceeds its bounded size limit")
    return body


def request_on_demand_session(
    api_base_url: str,
    camera_id: str,
    media_timestamp: datetime,
    *,
    window_seconds: float = 60.0,
    timeout_seconds: float = 20.0,
) -> OnDemandSession:
    """Obtain a signed ON_DEMAND URL without printing or persisting it."""
    api_base_url = normalize_api_base_url(api_base_url)
    if camera_id not in CAMERA_IDS:
        raise VerificationError("camera must be one of ch1, ch2, ch3, or ch4")
    window_seconds = float(window_seconds)
    if not 4.0 <= window_seconds <= 3600.0:
        raise VerificationError("archive window must be between 4 and 3600 seconds")
    half_window = window_seconds / 2.0
    start = media_timestamp - timedelta(seconds=half_window)
    end = media_timestamp + timedelta(seconds=half_window)
    query = urlencode({"start": iso_millis(start), "end": iso_millis(end)})
    endpoint = (
        f"{api_base_url}/video/session/{quote(camera_id, safe='')}?{query}"
    )
    payload_bytes = fetch_bytes(
        endpoint,
        limit=JSON_LIMIT,
        timeout_seconds=timeout_seconds,
        label="video session",
    )
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("video session response is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise VerificationError("video session response is not an object")
    if payload.get("cameraId") != camera_id:
        raise VerificationError("video session camera does not match the request")
    if payload.get("playbackMode") != "ON_DEMAND":
        raise VerificationError("video session is not ON_DEMAND")
    hls_url = _validated_internal_url(payload.get("hlsUrl"))
    expires = payload.get("expiresIn")
    if not isinstance(expires, int) or isinstance(expires, bool) or expires <= 0:
        expires = None
    return OnDemandSession(
        camera_id=camera_id,
        hls_url=hls_url,
        playback_mode="ON_DEMAND",
        expires_in=expires,
        requested_start=start,
        requested_end=end,
    )


def _decode_playlist(body: bytes, label: str) -> str:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise VerificationError(f"{label} is not UTF-8") from exc
    if not text.lstrip().startswith("#EXTM3U"):
        raise VerificationError(f"{label} is not an HLS playlist")
    return text


def _master_variant_reference(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        for candidate in lines[index + 1 :]:
            if not candidate or candidate.startswith("#"):
                continue
            return candidate
    return None


def fetch_media_playlist(
    session_url: str, *, timeout_seconds: float = 20.0
) -> tuple[str, str]:
    """Return media playlist text + URL internally; callers must not serialize URL."""
    master_body = fetch_bytes(
        session_url,
        limit=PLAYLIST_LIMIT,
        timeout_seconds=timeout_seconds,
        label="HLS playlist",
    )
    master_text = _decode_playlist(master_body, "HLS playlist")
    if "#EXTINF:" in master_text:
        return master_text, session_url
    variant = _master_variant_reference(master_text)
    if variant is None:
        raise VerificationError("HLS master playlist has no media variant")
    media_url = resolve_hls_uri(session_url, variant)
    media_body = fetch_bytes(
        media_url,
        limit=PLAYLIST_LIMIT,
        timeout_seconds=timeout_seconds,
        label="HLS media playlist",
    )
    return _decode_playlist(media_body, "HLS media playlist"), media_url


def _map_reference(line: str) -> str:
    match = re.search(r'(?:^|,)URI=(?:"([^"]+)"|([^,]+))', line)
    if not match:
        raise VerificationError("HLS fMP4 map is missing its URI")
    return (match.group(1) or match.group(2)).strip()


def parse_media_playlist(text: str, media_url: str) -> ParsedMediaPlaylist:
    """Parse fMP4 map, PDT, duration, and same-origin media references."""
    if not text.lstrip().startswith("#EXTM3U"):
        raise VerificationError("HLS media playlist is invalid")
    if "#EXT-X-BYTERANGE" in text:
        raise VerificationError("byte-range HLS fragments are not supported")

    current_map: str | None = None
    next_pdt: datetime | None = None
    pending_duration: float | None = None
    segments: list[MediaSegment] = []

    for line in (raw.strip() for raw in text.splitlines()):
        if not line:
            continue
        if line.startswith("#EXT-X-MAP:"):
            if "BYTERANGE=" in line:
                raise VerificationError("byte-range fMP4 maps are not supported")
            current_map = resolve_hls_uri(
                media_url, _map_reference(line.split(":", 1)[1])
            )
            continue
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            next_pdt = parse_utc_timestamp(
                line.split(":", 1)[1], "HLS program date time"
            )
            continue
        if line.startswith("#EXTINF:"):
            duration_text = line.split(":", 1)[1].split(",", 1)[0]
            try:
                pending_duration = float(duration_text)
            except ValueError as exc:
                raise VerificationError("HLS fragment duration is invalid") from exc
            if not math.isfinite(pending_duration) or pending_duration <= 0:
                raise VerificationError("HLS fragment duration must be positive")
            continue
        if line.startswith("#"):
            continue
        if pending_duration is None:
            continue
        if current_map is None:
            raise VerificationError("HLS media fragment has no fMP4 EXT-X-MAP")
        if next_pdt is None:
            raise VerificationError("HLS media fragment has no program date time")
        segment = MediaSegment(
            sequence=len(segments),
            uri=resolve_hls_uri(media_url, line),
            map_uri=current_map,
            program_date_time=next_pdt,
            duration_seconds=pending_duration,
        )
        segments.append(segment)
        next_pdt = segment.end_time
        pending_duration = None

    if pending_duration is not None:
        raise VerificationError("HLS media playlist ends before a fragment URI")
    if not segments:
        raise VerificationError("HLS media playlist contains no fMP4 fragments")
    return ParsedMediaPlaylist(
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        segments=tuple(segments),
    )


def select_segment(
    playlist: ParsedMediaPlaylist, media_timestamp: datetime
) -> tuple[MediaSegment, float]:
    """Select only a fragment that actually contains the target PDT."""
    target = media_timestamp.astimezone(timezone.utc)
    for segment in playlist.segments:
        if segment.program_date_time <= target < segment.end_time:
            return segment, (target - segment.program_date_time).total_seconds()
    raise VerificationError(
        "persisted media timestamp is outside HLS fragment coverage"
    )


def _run_local_command(
    command: list[str], *, timeout_seconds: float, output_limit: int
) -> bytes:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise VerificationError(
            f"required local program is missing: {command[0]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VerificationError(f"{command[0]} exceeded its time bound") from exc
    if len(result.stdout) > output_limit or len(result.stderr) > output_limit:
        raise VerificationError(f"{command[0]} output exceeded its size bound")
    if result.returncode != 0:
        raise VerificationError(
            f"{command[0]} failed with exit status {result.returncode}"
        )
    return result.stdout


def probe_video_frames(
    fragment_path: Path, *, timeout_seconds: float = 30.0
) -> list[dict[str, float | int]]:
    output = _run_local_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time,pts_time,pkt_duration_time",
            "-of",
            "json",
            str(fragment_path),
        ],
        timeout_seconds=timeout_seconds,
        output_limit=FFPROBE_OUTPUT_LIMIT,
    )
    try:
        payload = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("ffprobe returned invalid frame metadata") from exc
    frames = payload.get("frames") if isinstance(payload, dict) else None
    if not isinstance(frames, list):
        raise VerificationError("fMP4 fragment has no video frame metadata")

    parsed: list[dict[str, float | int]] = []
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            continue
        raw_pts = frame.get("best_effort_timestamp_time", frame.get("pts_time"))
        try:
            pts = float(raw_pts)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(pts):
            continue
        try:
            duration = float(frame.get("pkt_duration_time"))
        except (TypeError, ValueError):
            duration = 0.0
        parsed.append({"index": index, "pts": pts, "duration": max(0.0, duration)})
    if not parsed:
        raise VerificationError("fMP4 fragment contains no timestamped video frames")
    first_pts = float(parsed[0]["pts"])
    for frame in parsed:
        frame["relative_seconds"] = float(frame["pts"]) - first_pts
    return parsed


def choose_nearest_frame(
    frames: list[dict[str, float | int]], target_offset_seconds: float
) -> dict[str, float | int]:
    if target_offset_seconds < 0 or not math.isfinite(target_offset_seconds):
        raise VerificationError("target fragment offset is invalid")
    return min(
        frames,
        key=lambda frame: abs(
            float(frame["relative_seconds"]) - target_offset_seconds
        ),
    )


def extract_nearest_frame(
    init_fragment: bytes,
    media_fragment: bytes,
    target_offset_seconds: float,
    output_path: Path,
    *,
    timeout_seconds: float = 45.0,
    overwrite: bool = False,
) -> dict[str, object]:
    """Decode the nearest actual frame from local init+media fMP4 bytes."""
    if output_path.exists() and not overwrite:
        raise VerificationError(
            "frame output already exists (use --overwrite to replace it)"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="v2x-correlation-") as temp_dir:
        combined = Path(temp_dir) / "fragment.mp4"
        with combined.open("wb") as handle:
            handle.write(init_fragment)
            handle.write(media_fragment)
        frames = probe_video_frames(combined, timeout_seconds=timeout_seconds)
        selected = choose_nearest_frame(frames, target_offset_seconds)
        selected_index = int(selected["index"])
        _run_local_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(combined),
                "-vf",
                f"select=eq(n\\,{selected_index})",
                "-vsync",
                "0",
                "-frames:v",
                "1",
                "-q:v",
                "2",
                "-y",
                str(output_path),
            ],
            timeout_seconds=timeout_seconds,
            output_limit=2 * 1024 * 1024,
        )
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise VerificationError("ffmpeg did not create the requested frame")
    selected_relative = float(selected["relative_seconds"])
    return {
        "selected_frame_index": selected_index,
        "selected_relative_seconds": round(selected_relative, 6),
        "target_relative_seconds": round(target_offset_seconds, 6),
        "absolute_error_ms": round(
            abs(selected_relative - target_offset_seconds) * 1000.0, 3
        ),
        "probed_frame_count": len(frames),
    }


def jpeg_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise VerificationError("extracted frame is not JPEG")
    position = 2
    while position + 4 <= len(data):
        while position < len(data) and data[position] != 0xFF:
            position += 1
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in {0x01, 0xD8, 0xD9}:
            continue
        if position + 2 > len(data):
            break
        length = int.from_bytes(data[position : position + 2], "big")
        if length < 2 or position + length > len(data):
            break
        if marker in SOF_MARKERS:
            payload = data[position + 2 : position + length]
            if len(payload) < 5:
                break
            height = int.from_bytes(payload[1:3], "big")
            width = int.from_bytes(payload[3:5], "big")
            if width > 0 and height > 0:
                return width, height
        position += length
    raise VerificationError("JPEG dimensions are unavailable")


def normalize_bbox(value: object) -> tuple[float, float, float, float]:
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 4:
            raise VerificationError("bbox must contain x1,y1,x2,y2")
        raw_values: object = parts
    elif isinstance(value, dict):
        raw_values = [value.get(key) for key in ("x1", "y1", "x2", "y2")]
    else:
        raw_values = value
    if not isinstance(raw_values, (list, tuple)) or len(raw_values) != 4:
        raise VerificationError("bbox must contain x1,y1,x2,y2")
    try:
        bbox = tuple(float(item) for item in raw_values)
    except (TypeError, ValueError) as exc:
        raise VerificationError("bbox coordinates must be numeric") from exc
    if not all(math.isfinite(item) for item in bbox):
        raise VerificationError("bbox coordinates must be finite")
    x1, y1, x2, y2 = bbox
    if x2 <= x1 or y2 <= y1:
        raise VerificationError("bbox must have positive width and height")
    return bbox


def bbox_geometry(
    bbox: tuple[float, float, float, float],
    frame_width: int,
    frame_height: int,
) -> dict[str, object]:
    x1, y1, x2, y2 = bbox
    clipped = (
        max(0.0, min(float(frame_width), x1)),
        max(0.0, min(float(frame_height), y1)),
        max(0.0, min(float(frame_width), x2)),
        max(0.0, min(float(frame_height), y2)),
    )
    cx1, cy1, cx2, cy2 = clipped
    saved_area = (x2 - x1) * (y2 - y1)
    clipped_area = max(0.0, cx2 - cx1) * max(0.0, cy2 - cy1)
    if clipped_area <= 0:
        raise VerificationError("saved bbox does not intersect the extracted frame")
    return {
        "saved": [round(item, 3) for item in bbox],
        "clipped": [round(item, 3) for item in clipped],
        "in_frame_area_ratio": round(clipped_area / saved_area, 6),
        "frame_area_ratio": round(clipped_area / (frame_width * frame_height), 6),
        "center": [round((x1 + x2) / 2.0, 3), round((y1 + y2) / 2.0, 3)],
    }


def _crop_bounds(geometry: dict[str, object]) -> tuple[int, int, int, int]:
    clipped = geometry["clipped"]
    assert isinstance(clipped, list)
    x1 = int(math.floor(float(clipped[0])))
    y1 = int(math.floor(float(clipped[1])))
    x2 = int(math.ceil(float(clipped[2])))
    y2 = int(math.ceil(float(clipped[3])))
    return x1, y1, max(1, x2 - x1), max(1, y2 - y1)


def create_bbox_crop(
    frame_path: Path,
    crop_path: Path,
    geometry: dict[str, object],
    *,
    timeout_seconds: float = 30.0,
    overwrite: bool = False,
) -> None:
    if crop_path.exists() and not overwrite:
        raise VerificationError(
            "bbox crop already exists (use --overwrite to replace it)"
        )
    crop_path.parent.mkdir(parents=True, exist_ok=True)
    x, y, width, height = _crop_bounds(geometry)
    _run_local_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(frame_path),
            "-vf",
            f"crop={width}:{height}:{x}:{y}",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-y",
            str(crop_path),
        ],
        timeout_seconds=timeout_seconds,
        output_limit=2 * 1024 * 1024,
    )
    if not crop_path.is_file() or crop_path.stat().st_size == 0:
        raise VerificationError("ffmpeg did not create the bbox crop")


def analyze_crop_signal(crop_path: Path) -> dict[str, object]:
    width, height = jpeg_dimensions(crop_path)
    scale = min(1.0, 256.0 / max(width, height))
    analysis_width = max(1, int(round(width * scale)))
    analysis_height = max(1, int(round(height * scale)))
    raw = _run_local_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(crop_path),
            "-vf",
            f"scale={analysis_width}:{analysis_height}:flags=area",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        timeout_seconds=30.0,
        output_limit=256 * 256 * 3 + 1024,
    )
    expected = analysis_width * analysis_height * 3
    if len(raw) != expected:
        raise VerificationError("decoded bbox crop has an unexpected byte size")

    luma = [
        (77 * raw[index] + 150 * raw[index + 1] + 29 * raw[index + 2]) >> 8
        for index in range(0, len(raw), 3)
    ]
    mean = sum(luma) / len(luma)
    variance = sum((sample - mean) ** 2 for sample in luma) / len(luma)
    histogram = [0] * 16
    for sample in luma:
        histogram[min(15, sample // 16)] += 1
    entropy = 0.0
    for count in histogram:
        if count:
            probability = count / len(luma)
            entropy -= probability * math.log2(probability)

    edges: list[int] = []
    for row in range(analysis_height):
        start = row * analysis_width
        edges.extend(
            abs(luma[start + column] - luma[start + column - 1])
            for column in range(1, analysis_width)
        )
    for row in range(1, analysis_height):
        current = row * analysis_width
        previous = current - analysis_width
        edges.extend(
            abs(luma[current + column] - luma[previous + column])
            for column in range(analysis_width)
        )
    mean_edge = sum(edges) / len(edges) if edges else 0.0
    dynamic_range = max(luma) - min(luma)
    stddev = math.sqrt(variance)
    signal_present = dynamic_range >= 12 and stddev >= 2.0 and entropy >= 0.35
    return {
        "analysis_dimensions": [analysis_width, analysis_height],
        "luma_mean": round(mean, 3),
        "luma_stddev": round(stddev, 3),
        "luma_dynamic_range": dynamic_range,
        "luma_entropy_bits": round(entropy, 4),
        "mean_edge_delta": round(mean_edge, 3),
        "non_blank_signal_present": signal_present,
    }


def _bbox_iou(first: list[float], second: list[float]) -> tuple[float, float]:
    x1 = max(first[0], second[0])
    y1 = max(first[1], second[1])
    x2 = min(first[2], second[2])
    y2 = min(first[3], second[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    return (
        intersection / union if union > 0 else 0.0,
        intersection / first_area if first_area > 0 else 0.0,
    )


def _expected_yolo_labels(object_type: str | None) -> set[str] | None:
    normalized = (object_type or "").strip().lower()
    if normalized in {"vehicle", "car", "truck", "bus", "motorcycle", "motorbike"}:
        if normalized == "vehicle":
            return {"car", "truck", "bus", "motorcycle", "motorbike"}
        return {normalized}
    if normalized in {"person", "pedestrian", "walker"}:
        return {"person"}
    if normalized in {"bicycle", "bike", "cyclist"}:
        return {"bicycle", "bike"}
    return None


def run_local_yolo(
    frame_path: Path,
    saved_bbox: tuple[float, float, float, float],
    object_type: str | None,
    model_path: Path,
    *,
    confidence: float = 0.25,
    minimum_iou: float = 0.10,
    device: str = "cpu",
) -> dict[str, object]:
    """Corroborate locally; model_path must exist so no model is downloaded."""
    if not model_path.is_file():
        raise VerificationError("YOLO model must be an existing local file")
    try:
        from ultralytics import YOLO  # type: ignore
    except ImportError as exc:
        raise VerificationError(
            "ultralytics is unavailable in this Python environment"
        ) from exc
    try:
        model = YOLO(str(model_path))
        results = model.predict(
            source=str(frame_path),
            conf=float(confidence),
            device=device,
            verbose=False,
        )
    except Exception as exc:
        error_type = type(exc).__name__
        raise VerificationError(
            f"local YOLO inference failed ({error_type})"
        ) from None
    if not results:
        raise VerificationError("local YOLO inference returned no result")

    result = results[0]
    names = getattr(result, "names", {})
    boxes = getattr(result, "boxes", None)
    expected_labels = _expected_yolo_labels(object_type)
    candidates: list[dict[str, object]] = []
    compatible = False
    if boxes is not None:
        coordinates = boxes.xyxy.cpu().tolist()
        confidences = boxes.conf.cpu().tolist()
        classes = boxes.cls.cpu().tolist()
        for coordinates_raw, score_raw, class_raw in zip(
            coordinates, confidences, classes
        ):
            candidate_box = [float(value) for value in coordinates_raw]
            class_index = int(class_raw)
            if isinstance(names, dict):
                label = str(names.get(class_index, class_index)).lower()
            elif isinstance(names, list) and 0 <= class_index < len(names):
                label = str(names[class_index]).lower()
            else:
                label = str(class_index)
            iou, saved_coverage = _bbox_iou(list(saved_bbox), candidate_box)
            class_compatible = expected_labels is None or label in expected_labels
            overlap_compatible = iou >= minimum_iou or saved_coverage >= 0.25
            is_compatible = class_compatible and overlap_compatible
            compatible = compatible or is_compatible
            candidates.append(
                {
                    "label": label,
                    "confidence": round(float(score_raw), 4),
                    "bbox": [round(value, 2) for value in candidate_box],
                    "iou_with_saved_bbox": round(iou, 4),
                    "saved_bbox_coverage": round(saved_coverage, 4),
                    "class_compatible": class_compatible,
                    "compatible": is_compatible,
                }
            )
    candidates.sort(
        key=lambda item: (
            bool(item["compatible"]),
            float(item["iou_with_saved_bbox"]),
            float(item["confidence"]),
        ),
        reverse=True,
    )
    return {
        "method": "local_yolo",
        "model_sha256": sha256_file(model_path),
        "confidence_threshold": float(confidence),
        "minimum_iou": float(minimum_iou),
        "expected_labels": sorted(expected_labels) if expected_labels else None,
        "candidate_count": len(candidates),
        "compatible_detection_present": compatible,
        "candidates": candidates[:20],
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_evidence(
    path: Path,
    payload: dict[str, object],
    *,
    overwrite: bool = False,
) -> None:
    """Atomically persist a credential-free verifier report."""
    if path.exists() and not overwrite:
        raise VerificationError(
            "JSON evidence already exists (use --overwrite to replace it)"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _nested_bbox(payload: dict[str, object]) -> object | None:
    if payload.get("bbox") is not None:
        return payload["bbox"]
    camera_data = payload.get("camera_data")
    if isinstance(camera_data, dict):
        metadata = camera_data.get("bifocal_metadata")
        if isinstance(metadata, dict):
            return metadata.get("bbox")
    return None


def _persisted_camera_id(detection: dict[str, object]) -> str | None:
    """Resolve ch1-ch4 from persisted camera/device identifiers only."""
    resolved: list[str] = []
    for field in ("camera_id", "device_id"):
        value = detection.get(field)
        if value is None:
            continue
        text = str(value).strip().lower()
        if text in CAMERA_IDS:
            resolved.append(text)
            continue
        match = re.search(r"(?:^|[-_])(ch[1-4])$", text)
        if match:
            resolved.append(match.group(1))
            continue
        raise VerificationError(f"persisted {field} does not identify ch1 through ch4")
    if not resolved:
        return None
    if len(set(resolved)) != 1:
        raise VerificationError("persisted camera_id and device_id disagree")
    return resolved[0]


def load_detection(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    if not path.is_file():
        raise VerificationError("detection JSON file does not exist")
    if path.stat().st_size > JSON_LIMIT:
        raise VerificationError("detection JSON exceeds its bounded size limit")
    try:
        payload = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("detection JSON is invalid") from exc
    if isinstance(payload, list):
        if len(payload) != 1 or not isinstance(payload[0], dict):
            raise VerificationError(
                "detection JSON list must contain exactly one object"
            )
        payload = payload[0]
    if not isinstance(payload, dict):
        raise VerificationError("detection JSON must contain one object")
    return payload


def load_detection_from_snapshot(
    snapshot_dir: Path,
    event_id: str,
) -> dict[str, object]:
    """Load one event from a hash-bound, immutable corpus snapshot."""
    if not snapshot_dir.is_dir():
        raise VerificationError("detection snapshot directory does not exist")
    manifest_path = snapshot_dir / "manifest.json"
    detections_path = snapshot_dir / "detections.ndjson"
    if not manifest_path.is_file() or not detections_path.is_file():
        raise VerificationError("detection snapshot is incomplete")
    if manifest_path.stat().st_size > SNAPSHOT_MANIFEST_LIMIT:
        raise VerificationError("detection snapshot manifest exceeds its size limit")
    if detections_path.stat().st_size > SNAPSHOT_DETECTIONS_LIMIT:
        raise VerificationError("detection snapshot data exceeds its size limit")
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("detection snapshot manifest is invalid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != (
        "v2x-detection-corpus-snapshot/v1"
    ):
        raise VerificationError("detection snapshot schema is unsupported")
    artifacts = manifest.get("artifacts")
    expected_sha256 = (
        artifacts.get("detections.ndjson") if isinstance(artifacts, dict) else None
    )
    if (
        not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or sha256_file(detections_path) != expected_sha256
    ):
        raise VerificationError("detection snapshot data hash does not match manifest")
    normalized_event_id = event_id.strip()
    if not normalized_event_id:
        raise VerificationError("snapshot event id is empty")
    matched: dict[str, object] | None = None
    try:
        with detections_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise VerificationError(
                        f"detection snapshot row {line_number} is not an object"
                    )
                if str(payload.get("event_id", "")).strip() != normalized_event_id:
                    continue
                if matched is not None:
                    raise VerificationError("snapshot event id is not unique")
                matched = payload
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("detection snapshot data is invalid NDJSON") from exc
    if matched is None:
        raise VerificationError("snapshot event id was not found")
    return matched


def validate_media_timestamp_trust(
    detection: dict[str, object],
    timestamp_field: str,
    media_timestamp: datetime,
) -> dict[str, object]:
    """Validate the persisted HLS clock provenance required for acceptance.

    Merely naming a field ``media_timestamp_utc`` is insufficient.  Version 1
    requires a persisted ``media_clock`` object whose source, schema version,
    anchor, and relative position reconstruct that timestamp.  Explicit CLI
    timestamps remain useful for diagnostics but are intentionally untrusted.
    """
    result: dict[str, object] = {
        "trusted": False,
        "expected_source": TRUSTED_MEDIA_CLOCK_SOURCE,
        "expected_media_clock_schema_version": TRUSTED_MEDIA_CLOCK_SCHEMA_VERSION,
        "expected_timestamp_schema_version": (
            TRUSTED_DETECTION_TIMESTAMP_SCHEMA_VERSION
        ),
        "consistency_tolerance_ms": MEDIA_CLOCK_CONSISTENCY_TOLERANCE_MS,
    }
    if timestamp_field != "media_timestamp_utc":
        result["reason"] = "timestamp is not the persisted media_timestamp_utc field"
        return result

    timestamp_utc = detection.get("timestamp_utc")
    persisted_media_timestamp = detection.get("media_timestamp_utc")
    if (
        not isinstance(timestamp_utc, str)
        or not timestamp_utc.strip()
        or not isinstance(persisted_media_timestamp, str)
        or not persisted_media_timestamp.strip()
        or timestamp_utc.strip() != persisted_media_timestamp.strip()
    ):
        result["reason"] = (
            "persisted timestamp_utc does not equal media_timestamp_utc"
        )
        return result
    try:
        timestamp_utc_parsed = parse_utc_timestamp(
            timestamp_utc, "persisted timestamp_utc"
        )
    except VerificationError:
        result["reason"] = "persisted timestamp_utc is invalid"
        return result
    if timestamp_utc_parsed != media_timestamp:
        result["reason"] = (
            "persisted timestamp_utc does not equal media_timestamp_utc"
        )
        return result

    timestamp_schema_version = detection.get("timestamp_schema_version")
    timestamp_schema_valid = (
        isinstance(timestamp_schema_version, (int, float))
        and not isinstance(timestamp_schema_version, bool)
        and math.isfinite(float(timestamp_schema_version))
        and float(timestamp_schema_version).is_integer()
    )
    result["timestamp_schema_version"] = (
        timestamp_schema_version if timestamp_schema_valid else None
    )
    result["producer_trust_flag"] = detection.get("media_time_trusted") is True
    if (
        not timestamp_schema_valid
        or timestamp_schema_version != TRUSTED_DETECTION_TIMESTAMP_SCHEMA_VERSION
    ):
        result["reason"] = "detection timestamp schema version is not trusted"
        return result
    if detection.get("media_time_trusted") is not True:
        result["reason"] = "persisted producer media-time trust flag is not true"
        return result

    media_clock = detection.get("media_clock")
    if not isinstance(media_clock, dict):
        result["reason"] = "persisted media_clock provenance is missing"
        return result
    source = media_clock.get("source")
    schema_version = media_clock.get("schema_version")
    schema_version_valid = (
        isinstance(schema_version, (int, float))
        and not isinstance(schema_version, bool)
        and math.isfinite(float(schema_version))
        and float(schema_version).is_integer()
    )
    result["source"] = source if isinstance(source, str) else None
    result["media_clock_schema_version"] = (
        schema_version if schema_version_valid else None
    )
    if source != TRUSTED_MEDIA_CLOCK_SOURCE:
        result["reason"] = "media clock source is not trusted"
        return result
    if (
        not schema_version_valid
        or schema_version != TRUSTED_MEDIA_CLOCK_SCHEMA_VERSION
    ):
        result["reason"] = "media clock schema version is not trusted"
        return result

    try:
        anchor = parse_utc_timestamp(
            media_clock.get("anchor_program_date_time_utc"),
            "media clock anchor",
        )
        raw_position = media_clock.get("position_milliseconds")
        if (
            isinstance(raw_position, bool)
            or not isinstance(raw_position, (int, float))
        ):
            raise TypeError("media position must be numeric")
        position_milliseconds = float(raw_position)
    except (TypeError, ValueError, VerificationError):
        result["reason"] = "media clock anchor or position is invalid"
        return result
    if not math.isfinite(position_milliseconds) or position_milliseconds < 0:
        result["reason"] = "media clock position is invalid"
        return result

    reconstructed = anchor + timedelta(milliseconds=position_milliseconds)
    delta_ms = abs((reconstructed - media_timestamp).total_seconds()) * 1000.0
    result["reconstructed_media_timestamp"] = iso_millis(reconstructed)
    result["timestamp_consistency_error_ms"] = round(delta_ms, 3)
    if delta_ms > MEDIA_CLOCK_CONSISTENCY_TOLERANCE_MS:
        result["reason"] = "media timestamp is inconsistent with its persisted clock"
        return result
    result["trusted"] = True
    result["reason"] = None
    return result


def resolve_inputs(args: argparse.Namespace) -> dict[str, object]:
    detection_json = getattr(args, "detection_json", None)
    snapshot_dir = getattr(args, "snapshot", None)
    snapshot_event_id = getattr(args, "event_id", None)
    if detection_json is not None and snapshot_dir is not None:
        raise VerificationError("use either --detection-json or --snapshot, not both")
    if snapshot_dir is None and snapshot_event_id is not None:
        raise VerificationError("--event-id requires --snapshot")
    if snapshot_dir is not None and snapshot_event_id is None:
        raise VerificationError("--snapshot requires --event-id")
    detection = (
        load_detection_from_snapshot(snapshot_dir, snapshot_event_id)
        if snapshot_dir is not None
        else load_detection(detection_json)
    )
    if detection_json is not None or snapshot_dir is not None:
        override_flags = {
            "camera": "--camera",
            "media_timestamp": "--media-timestamp",
            "bbox": "--bbox",
            "object_id": "--object-id",
            "object_type": "--object-type",
            "confidence": "--confidence",
        }
        supplied = [
            flag
            for attribute, flag in override_flags.items()
            if getattr(args, attribute, None) is not None
        ]
        if supplied:
            raise VerificationError(
                "detection JSON fields cannot be overridden from the command line: "
                + ", ".join(supplied)
            )
    camera_id = args.camera or _persisted_camera_id(detection)
    if camera_id not in CAMERA_IDS:
        raise VerificationError("camera is missing or is not one of ch1 through ch4")

    timestamp_value = args.media_timestamp
    timestamp_field = "command_line"
    if timestamp_value is None:
        for field in MEDIA_TIMESTAMP_FIELDS:
            if detection.get(field):
                timestamp_value = detection[field]
                timestamp_field = field
                break
    if timestamp_value is None:
        if detection.get("timestamp_utc"):
            raise VerificationError(
                "detection has only receipt timestamp_utc; an explicit persisted "
                "media timestamp is required"
            )
        raise VerificationError("persisted media timestamp is missing")
    media_timestamp = parse_utc_timestamp(timestamp_value, "persisted media timestamp")

    bbox_value = args.bbox if args.bbox is not None else _nested_bbox(detection)
    if bbox_value is None:
        raise VerificationError("saved detection bbox is missing")
    bbox = normalize_bbox(bbox_value)

    confidence = args.confidence
    if confidence is None:
        confidence = detection.get("confidence_score")
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise VerificationError("saved confidence must be numeric") from exc
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise VerificationError("saved confidence must be between zero and one")

    return {
        "camera_id": camera_id,
        "media_timestamp": media_timestamp,
        "timestamp_field": timestamp_field,
        "timestamp_trust": validate_media_timestamp_trust(
            detection, timestamp_field, media_timestamp
        ),
        "bbox": bbox,
        "event_id": detection.get("event_id"),
        "object_id": args.object_id or detection.get("object_id"),
        "object_type": args.object_type or detection.get("object_type"),
        "confidence": confidence,
    }


def evaluate_acceptance(
    *,
    timestamp_trusted: bool,
    frame_timing_error_ms: float,
    maximum_frame_timing_error_ms: float,
    structured_passed: bool,
    visual_corroborated: bool | None,
    require_yolo: bool,
) -> dict[str, object]:
    """Apply one fail-closed gate shared by real and negative-control tests."""
    timing_passed = (
        math.isfinite(frame_timing_error_ms)
        and frame_timing_error_ms <= maximum_frame_timing_error_ms
    )
    if not timestamp_trusted:
        verdict = "UNTRUSTED_MEDIA_TIMESTAMP"
    elif not timing_passed:
        verdict = "FRAME_TIMING_MISMATCH"
    elif not structured_passed:
        verdict = "STRUCTURED_MISMATCH"
    elif visual_corroborated is True:
        verdict = "VISUALLY_CORROBORATED"
    elif visual_corroborated is False:
        verdict = "VISUAL_MISMATCH"
    else:
        verdict = "STRUCTURED_EVIDENCE_ONLY"
    gate_passed = (
        timestamp_trusted
        and timing_passed
        and structured_passed
        and visual_corroborated is not False
        and (not require_yolo or visual_corroborated is True)
    )
    return {
        "verdict": verdict,
        "gate_passed": gate_passed,
        "trusted_media_timestamp": timestamp_trusted,
        "frame_timing_check_passed": timing_passed,
        "maximum_nearest_frame_error_ms": maximum_frame_timing_error_ms,
        "visual_corroborated": visual_corroborated,
    }


def verify_historical_correlation(
    *,
    api_base_url: str,
    camera_id: str,
    media_timestamp: datetime,
    timestamp_field: str,
    timestamp_trust: dict[str, object],
    bbox: tuple[float, float, float, float],
    event_id: object = None,
    object_id: object = None,
    object_type: object = None,
    confidence: object = None,
    output_path: Path,
    crop_path: Path | None = None,
    window_seconds: float = 60.0,
    timeout_seconds: float = 30.0,
    minimum_in_frame_ratio: float = 0.95,
    maximum_frame_error_ms: float = 100.0,
    yolo_model: Path | None = None,
    yolo_device: str = "cpu",
    yolo_confidence: float = 0.25,
    minimum_iou: float = 0.10,
    require_yolo: bool = False,
    overwrite: bool = False,
) -> dict[str, object]:
    if not 0 < minimum_in_frame_ratio <= 1:
        raise VerificationError("minimum in-frame ratio must be in (0, 1]")
    maximum_frame_error_ms = float(maximum_frame_error_ms)
    if (
        not math.isfinite(maximum_frame_error_ms)
        or maximum_frame_error_ms <= 0
        or maximum_frame_error_ms > 5000
    ):
        raise VerificationError(
            "maximum frame error must be greater than zero and at most 5000 ms"
        )
    if require_yolo and yolo_model is None:
        raise VerificationError("--require-yolo requires --yolo-model")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise VerificationError("ffmpeg and ffprobe are required")

    session = request_on_demand_session(
        api_base_url,
        camera_id,
        media_timestamp,
        window_seconds=window_seconds,
        timeout_seconds=timeout_seconds,
    )
    playlist_text, media_url = fetch_media_playlist(
        session.hls_url, timeout_seconds=timeout_seconds
    )
    playlist = parse_media_playlist(playlist_text, media_url)
    segment, target_offset = select_segment(playlist, media_timestamp)
    init_fragment = fetch_bytes(
        segment.map_uri,
        limit=INIT_FRAGMENT_LIMIT,
        timeout_seconds=timeout_seconds,
        label="fMP4 initialization fragment",
    )
    media_fragment = fetch_bytes(
        segment.uri,
        limit=MEDIA_FRAGMENT_LIMIT,
        timeout_seconds=timeout_seconds,
        label="fMP4 media fragment",
    )
    extraction = extract_nearest_frame(
        init_fragment,
        media_fragment,
        target_offset,
        output_path,
        timeout_seconds=max(30.0, timeout_seconds),
        overwrite=overwrite,
    )
    width, height = jpeg_dimensions(output_path)
    geometry = bbox_geometry(bbox, width, height)
    in_frame_passed = float(geometry["in_frame_area_ratio"]) >= minimum_in_frame_ratio

    crop_path = crop_path or output_path.with_name(
        f"{output_path.stem}.bbox{output_path.suffix or '.jpg'}"
    )
    create_bbox_crop(
        output_path,
        crop_path,
        geometry,
        timeout_seconds=max(30.0, timeout_seconds),
        overwrite=overwrite,
    )
    crop_signal = analyze_crop_signal(crop_path)
    structured_passed = in_frame_passed and bool(
        crop_signal["non_blank_signal_present"]
    )

    yolo_evidence: dict[str, object] | None = None
    visual_corroborated: bool | None = None
    if yolo_model is not None:
        yolo_evidence = run_local_yolo(
            output_path,
            bbox,
            str(object_type) if object_type is not None else None,
            yolo_model,
            confidence=yolo_confidence,
            minimum_iou=minimum_iou,
            device=yolo_device,
        )
        visual_corroborated = bool(
            yolo_evidence["compatible_detection_present"]
        )

    result = evaluate_acceptance(
        timestamp_trusted=timestamp_trust.get("trusted") is True,
        frame_timing_error_ms=float(extraction["absolute_error_ms"]),
        maximum_frame_timing_error_ms=maximum_frame_error_ms,
        structured_passed=structured_passed,
        visual_corroborated=visual_corroborated,
        require_yolo=require_yolo,
    )

    selected_media_time = segment.program_date_time + timedelta(
        seconds=float(extraction["selected_relative_seconds"])
    )
    frame_bytes = output_path.stat().st_size
    crop_bytes = crop_path.stat().st_size
    return {
        "schema_version": 1,
        "verifier": "historical_video_detection_correlation",
        "generated_at": iso_millis(datetime.now(timezone.utc)),
        "safety": {
            "mode": "read_only_remote_local_artifacts_only",
            "signed_urls_emitted": False,
            "carla_or_bridge_mutation": False,
            "aws_mutation": False,
        },
        "detection": {
            "camera_id": camera_id,
            "event_id": str(event_id) if event_id is not None else None,
            "object_id": str(object_id) if object_id is not None else None,
            "object_type": str(object_type) if object_type is not None else None,
            "confidence": confidence,
            "persisted_media_timestamp": iso_millis(media_timestamp),
            "media_timestamp_field": timestamp_field,
            "media_timestamp_trust": timestamp_trust,
            "saved_bbox": [round(value, 3) for value in bbox],
        },
        "session": {
            "playback_mode": session.playback_mode,
            "expires_in_seconds": session.expires_in,
            "requested_start": iso_millis(session.requested_start),
            "requested_end": iso_millis(session.requested_end),
        },
        "archive": {
            "playlist_sha256": playlist.sha256,
            "fragment_count": len(playlist.segments),
            "selected_fragment_sequence": segment.sequence,
            "selected_fragment_pdt": iso_millis(segment.program_date_time),
            "selected_fragment_end": iso_millis(segment.end_time),
            "selected_fragment_duration_seconds": segment.duration_seconds,
            "target_offset_seconds": round(target_offset, 6),
            "init_fragment_sha256": hashlib.sha256(init_fragment).hexdigest(),
            "media_fragment_sha256": hashlib.sha256(media_fragment).hexdigest(),
            "media_fragment_bytes": len(media_fragment),
        },
        "frame": {
            **extraction,
            "maximum_nearest_frame_error_ms": maximum_frame_error_ms,
            "timing_error_check_passed": result["frame_timing_check_passed"],
            "selected_media_timestamp": iso_millis(selected_media_time),
            "dimensions": [width, height],
            "path": str(output_path.resolve()),
            "sha256": sha256_file(output_path),
            "bytes": frame_bytes,
        },
        "bbox_evidence": {
            **geometry,
            "minimum_in_frame_area_ratio": minimum_in_frame_ratio,
            "in_frame_check_passed": in_frame_passed,
            "crop": {
                "path": str(crop_path.resolve()),
                "sha256": sha256_file(crop_path),
                "bytes": crop_bytes,
                **crop_signal,
            },
            "structured_check_passed": structured_passed,
        },
        "visual_evidence": yolo_evidence
        or {
            "method": "not_run",
            "compatible_detection_present": None,
            "limitation": (
                "bbox geometry and non-blank crop are not semantic proof that the "
                "saved object is visible; rerun with a local YOLO model or inspect "
                "artifacts"
            ),
        },
        "result": result,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract an exact archived HLS frame for a persisted media timestamp "
            "and compare it with a saved detection bbox."
        )
    )
    parser.add_argument("api_base_url", help="credential-free public read API base")
    parser.add_argument(
        "--camera",
        choices=CAMERA_IDS,
        help="diagnostic input; cannot be combined with --detection-json",
    )
    parser.add_argument(
        "--media-timestamp",
        help="untrusted diagnostic input; cannot be combined with --detection-json",
    )
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("--detection-json", type=Path)
    input_group.add_argument(
        "--snapshot",
        type=Path,
        help="hash-bound v2x-detection-corpus-snapshot/v1 directory",
    )
    parser.add_argument(
        "--event-id",
        help="persisted event id to load from --snapshot",
    )
    parser.add_argument(
        "--bbox",
        help="diagnostic x1,y1,x2,y2; cannot be combined with --detection-json",
    )
    parser.add_argument(
        "--object-id",
        help="diagnostic input; cannot be combined with --detection-json",
    )
    parser.add_argument(
        "--object-type",
        help="diagnostic input; cannot be combined with --detection-json",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        help="diagnostic input; cannot be combined with --detection-json",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--report-output",
        type=Path,
        help="optional atomic JSON evidence path; signed URLs are never included",
    )
    parser.add_argument("--crop-output", type=Path)
    parser.add_argument("--window-seconds", type=float, default=60.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--minimum-in-frame-ratio", type=float, default=0.95)
    parser.add_argument("--maximum-frame-error-ms", type=float, default=100.0)
    parser.add_argument("--yolo-model", type=Path)
    parser.add_argument("--yolo-device", default="cpu")
    parser.add_argument("--yolo-confidence", type=float, default=0.25)
    parser.add_argument("--minimum-iou", type=float, default=0.10)
    parser.add_argument("--require-yolo", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def _safe_failure_message(exc: BaseException) -> str:
    message = str(exc) if isinstance(exc, VerificationError) else type(exc).__name__
    message = re.sub(
        r"https?://[^\s]+",
        "[redacted-url]",
        message,
        flags=re.IGNORECASE,
    )
    message = re.sub(
        r"(?i)(sessiontoken|x-amz-[a-z0-9-]+|signature|credential)=[^&\s]+",
        r"\1=[redacted]",
        message,
    )
    return message


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        inputs = resolve_inputs(args)
        evidence = verify_historical_correlation(
            api_base_url=args.api_base_url,
            **inputs,
            output_path=args.output,
            crop_path=args.crop_output,
            window_seconds=args.window_seconds,
            timeout_seconds=args.timeout,
            minimum_in_frame_ratio=args.minimum_in_frame_ratio,
            maximum_frame_error_ms=args.maximum_frame_error_ms,
            yolo_model=args.yolo_model,
            yolo_device=args.yolo_device,
            yolo_confidence=args.yolo_confidence,
            minimum_iou=args.minimum_iou,
            require_yolo=args.require_yolo,
            overwrite=args.overwrite,
        )
    except Exception as exc:
        print(f"verification failed: {_safe_failure_message(exc)}", file=sys.stderr)
        return 1
    if args.report_output is not None:
        try:
            write_json_evidence(
                args.report_output,
                evidence,
                overwrite=args.overwrite,
            )
        except Exception as exc:
            print(f"verification failed: {_safe_failure_message(exc)}", file=sys.stderr)
            return 1
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence["result"]["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
