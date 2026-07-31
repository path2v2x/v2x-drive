#!/usr/bin/env python3
"""Build hash-bound, proposal-only temporal static calibration targets.

The command reads retained capture reports and their exact JPEGs.  It never
changes those sources.  Capture windows are atomic split groups so correlated
frames from one window cannot leak between fit, development, and holdout.

Event detection boxes are conservatively expanded proposal-only exclusion
masks.  The resulting masked median, MAD image, validity, and stability masks
are diagnostic inputs for later reviewed static-geometry annotation.  They are
not measured camera intrinsics, surveyed geometry, annotation truth, or
acceptance proof.
"""

import argparse
import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import uuid
import warnings

import cv2
import numpy as np


EVENT_SCHEMAS = {
    "v2x-detection-event-frame-capture/v1",
    "v2x-detection-event-frame-capture/v2",
}
DENSE_SCHEMAS = {
    "v2x-dense-kvs-window/v1",
    "v2x-static-kvs-window-proposal/v1",
}
OUTPUT_SCHEMA = "v2x-temporal-static-targets/v2"
CAMERAS = {"ch1", "ch2", "ch3", "ch4"}
SPLITS = ("fit", "dev", "holdout")


class StaticTargetError(ValueError):
    pass


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def parse_timestamp(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise StaticTargetError(f"{label} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise StaticTargetError(f"{label} is invalid") from exc
    return parsed.astimezone(timezone.utc)


def _frame_path(report_path, value):
    if not isinstance(value, str) or not value:
        raise StaticTargetError("frame path is missing")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = report_path.parent / path
    return path.resolve()


def _validate_frame(report_path, binding, timestamp, identity):
    if not isinstance(binding, dict):
        raise StaticTargetError(f"{identity} frame binding is invalid")
    expected_sha = binding.get("sha256")
    width = binding.get("width")
    height = binding.get("height")
    if (
        not isinstance(expected_sha, str)
        or len(expected_sha) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha)
        or not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or width <= 0
        or height <= 0
    ):
        raise StaticTargetError(f"{identity} hash or reported dimensions are invalid")
    path = _frame_path(report_path, binding.get("path"))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise StaticTargetError(f"{identity} frame is unreadable") from exc
    actual_sha = sha256_bytes(raw)
    if actual_sha != expected_sha:
        raise StaticTargetError(f"{identity} frame sha256 does not match")
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise StaticTargetError(f"{identity} frame is not a decodable image")
    actual_height, actual_width = image.shape[:2]
    if (actual_width, actual_height) != (width, height):
        raise StaticTargetError(f"{identity} decoded dimensions do not match report")
    parsed_timestamp = parse_timestamp(timestamp, f"{identity} timestamp")
    return {
        "identity": identity,
        "path": str(path),
        "sha256": expected_sha,
        "width": width,
        "height": height,
        "timestamp_utc": timestamp,
        "timestamp": parsed_timestamp,
        "image": image,
    }


def _dynamic_exclusion_box(value, width, height, expansion_fraction, identity):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise StaticTargetError(f"{identity} dynamic bbox is invalid")
    try:
        x1, y1, x2, y2 = (float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise StaticTargetError(f"{identity} dynamic bbox is invalid") from exc
    if not all(math.isfinite(item) for item in (x1, y1, x2, y2)) or x2 <= x1 or y2 <= y1:
        raise StaticTargetError(f"{identity} dynamic bbox is invalid")
    pad_x = (x2 - x1) * expansion_fraction
    pad_y = (y2 - y1) * expansion_fraction
    expanded = [
        max(0, min(width, math.floor(x1 - pad_x))),
        max(0, min(height, math.floor(y1 - pad_y))),
        max(0, min(width, math.ceil(x2 + pad_x))),
        max(0, min(height, math.ceil(y2 + pad_y))),
    ]
    if expanded[2] <= expanded[0] or expanded[3] <= expanded[1]:
        raise StaticTargetError(f"{identity} dynamic bbox does not intersect frame")
    return {
        "source_identity": identity,
        "provenance": "persisted_detection_bbox_proposal_only",
        "acceptance_eligible": False,
        "reported_bbox_xyxy": [x1, y1, x2, y2],
        "expanded_clamped_bbox_xyxy": expanded,
        "expansion_fraction_per_side": float(expansion_fraction),
    }


def load_window(report_value, camera_id, dynamic_bbox_expansion_fraction=0.15):
    report_path = Path(report_value).expanduser().resolve()
    try:
        raw = report_path.read_bytes()
        report = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise StaticTargetError(f"capture report is unreadable: {report_path}") from exc
    if not isinstance(report, dict):
        raise StaticTargetError("capture report root must be an object")
    schema = report.get("schema")
    selected = []
    if schema in EVENT_SCHEMAS:
        events = report.get("events")
        if not isinstance(events, list):
            raise StaticTargetError("event capture report has no event list")
        seen_ids = set()
        for event in events:
            if not isinstance(event, dict) or event.get("camera_id") != camera_id:
                continue
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or not event_id or event_id in seen_ids:
                raise StaticTargetError("selected event IDs are missing or duplicated")
            seen_ids.add(event_id)
            frame = _validate_frame(
                report_path,
                event.get("frame"),
                event.get("selected_frame_timestamp_utc"),
                event_id,
            )
            box = _dynamic_exclusion_box(
                event.get("bbox_xyxy"),
                frame["width"],
                frame["height"],
                dynamic_bbox_expansion_fraction,
                event_id,
            )
            frame["dynamic_exclusion_boxes"] = [] if box is None else [box]
            selected.append(frame)
    elif schema in DENSE_SCHEMAS:
        if report.get("camera_id") != camera_id:
            raise StaticTargetError("dense report does not belong to requested camera")
        frames = report.get("frames")
        if not isinstance(frames, list):
            raise StaticTargetError("dense capture report has no frame list")
        if report.get("frame_count") not in (None, len(frames)):
            raise StaticTargetError("dense report frame count does not match frame list")
        seen_indices = set()
        for frame in frames:
            index = frame.get("index") if isinstance(frame, dict) else None
            if not isinstance(index, int) or isinstance(index, bool) or index in seen_indices:
                raise StaticTargetError("dense frame indices are invalid or duplicated")
            seen_indices.add(index)
            selected_frame = _validate_frame(
                report_path,
                frame,
                frame.get("producer_timestamp_utc"),
                f"frame-{index}",
            )
            selected_frame["dynamic_exclusion_boxes"] = []
            selected.append(selected_frame)
        declared_resolution = report.get("resolution")
        if declared_resolution is not None and (
            not selected
            or declared_resolution
            != [selected[0]["width"], selected[0]["height"]]
        ):
            raise StaticTargetError("dense report resolution does not match frames")
    else:
        raise StaticTargetError(f"unsupported capture report schema: {schema!r}")
    if not selected:
        raise StaticTargetError(f"capture report has no {camera_id} frames")
    # One capture report can contain multiple detections bound to the exact same
    # archived JPEG. Count those pixels once while retaining every source
    # identity. Repetition across different windows is still rejected below,
    # because that would leak identical pixels across splits.
    unique_frames = {}
    for frame in selected:
        existing = unique_frames.get(frame["sha256"])
        if existing is None:
            frame["identities"] = [frame["identity"]]
            frame["source_paths"] = [frame["path"]]
            unique_frames[frame["sha256"]] = frame
            continue
        if any(
            existing[key] != frame[key]
            for key in ("width", "height", "timestamp_utc")
        ):
            raise StaticTargetError("duplicate frame hash has conflicting bindings")
        existing["identities"].append(frame["identity"])
        existing["source_paths"].append(frame["path"])
        existing["dynamic_exclusion_boxes"].extend(
            frame["dynamic_exclusion_boxes"]
        )
    selected = list(unique_frames.values())
    selected.sort(key=lambda item: (item["timestamp"], item["sha256"]))
    report_sha = sha256_bytes(raw)
    window_id = sha256_bytes(
        canonical_json(
            {
                "camera_id": camera_id,
                "report_sha256": report_sha,
            }
        )
    )
    timestamps = [item["timestamp"] for item in selected]
    return {
        "window_id": window_id,
        "report_path": str(report_path),
        "source_directory": str(report_path.parent),
        "report_sha256": report_sha,
        "schema": schema,
        "camera_id": camera_id,
        "first_timestamp": min(timestamps),
        "last_timestamp": max(timestamps),
        "frames": selected,
    }


def deterministic_split(windows, seed):
    """Reserve one middle-time window for dev and one late window for holdout.

    The windows are first stratified into early, middle, and late chronological
    thirds.  A stable seed-bound digest selects within the middle and late
    strata; all remaining complete windows are fit inputs.
    """
    if len(windows) < 3:
        raise StaticTargetError("at least three capture windows are required for split output")
    ordered = sorted(
        windows,
        key=lambda value: (
            value["first_timestamp"],
            value["last_timestamp"],
            value["window_id"],
        ),
    )
    strata = [list(values) for values in np.array_split(np.asarray(ordered, dtype=object), 3)]

    def select(values, label):
        return min(
            values,
            key=lambda value: sha256_bytes(
                f"{seed}\0{label}\0{value['window_id']}".encode("utf-8")
            ),
        )

    dev = select(strata[1], "dev")
    holdout = select(strata[2], "holdout")
    assignment = {value["window_id"]: "fit" for value in ordered}
    assignment[dev["window_id"]] = "dev"
    assignment[holdout["window_id"]] = "holdout"
    return assignment, {
        "mode": "chronological_three_strata",
        "seed": seed,
        "ordered_window_ids": [value["window_id"] for value in ordered],
        "strata": [
            {
                "name": name,
                "window_ids": [value["window_id"] for value in values],
            }
            for name, values in zip(("early", "middle", "late"), strata)
        ],
        "selection": {
            "dev_window_id": dev["window_id"],
            "holdout_window_id": holdout["window_id"],
        },
        "whole_capture_window_atomic": True,
    }


def _write_png(path, image):
    if not cv2.imwrite(str(path), image, [cv2.IMWRITE_PNG_COMPRESSION, 9]):
        raise StaticTargetError(f"failed to encode output image: {path.name}")


def _atomic_rename_noreplace(source, destination):
    """Atomically publish one directory without replacing an existing path."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        if destination.exists():
            raise StaticTargetError("output directory already exists; refusing overwrite")
        os.rename(source, destination)
        return
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise StaticTargetError("output directory already exists; refusing overwrite")
    raise OSError(error, os.strerror(error), str(destination))


def _frame_dynamic_mask(frame):
    mask = np.zeros((frame["height"], frame["width"]), dtype=bool)
    for value in frame["dynamic_exclusion_boxes"]:
        x1, y1, x2, y2 = value["expanded_clamped_bbox_xyxy"]
        mask[y1:y2, x1:x2] = True
    return mask


def _composite(
    frames,
    threshold,
    minimum_valid_samples,
    minimum_valid_fraction,
    directory,
):
    if len(frames) < int(minimum_valid_samples):
        raise StaticTargetError(
            "split has fewer frames than the minimum valid sample requirement"
        )
    if len(frames) > np.iinfo(np.uint16).max:
        raise StaticTargetError("split has too many frames for exact valid-count output")
    stack = np.stack([value["image"] for value in frames], axis=0).astype(np.float32)
    dynamic_masks = np.stack([_frame_dynamic_mask(value) for value in frames], axis=0)
    valid_count = np.sum(~dynamic_masks, axis=0).astype(np.uint16)
    masked = np.where(~dynamic_masks[..., None], stack, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        static_median = np.nanmedian(masked, axis=0)
        mad = np.nanmedian(np.abs(masked - static_median[None, ...]), axis=0)
    raw_median = np.median(stack, axis=0)
    required_valid_samples = max(
        int(minimum_valid_samples),
        int(math.ceil(float(minimum_valid_fraction) * len(frames))),
    )
    validity_bool = valid_count >= required_valid_samples
    finite_mad = np.all(np.isfinite(mad), axis=2)
    stability_bool = (
        validity_bool
        & finite_mad
        & (np.nanmax(np.where(np.isfinite(mad), mad, np.inf), axis=2) <= float(threshold))
    )
    canonical_median = np.where(validity_bool[..., None], static_median, np.nan)
    canonical_mad = np.where(validity_bool[..., None], mad, np.nan)
    median_u8 = np.rint(
        np.clip(np.nan_to_num(canonical_median, nan=0.0), 0, 255)
    ).astype(np.uint8)
    raw_median_u8 = np.rint(np.clip(raw_median, 0, 255)).astype(np.uint8)
    mad_u8 = np.rint(
        np.clip(np.nan_to_num(canonical_mad, nan=0.0), 0, 255)
    ).astype(np.uint8)
    validity = validity_bool.astype(np.uint8) * 255
    stability = stability_bool.astype(np.uint8) * 255
    directory.mkdir(parents=True)
    paths = {
        "median_rgb": directory / "median-rgb.png",
        "raw_median_rgb_diagnostic": directory / "raw-median-rgb-diagnostic.png",
        "mad_rgb": directory / "mad-rgb.png",
        "valid_sample_count": directory / "valid-sample-count.png",
        "validity_mask": directory / "validity-mask.png",
        "stability_mask": directory / "stability-mask.png",
    }
    _write_png(paths["median_rgb"], median_u8)
    _write_png(paths["raw_median_rgb_diagnostic"], raw_median_u8)
    _write_png(paths["mad_rgb"], mad_u8)
    _write_png(paths["valid_sample_count"], valid_count)
    _write_png(paths["validity_mask"], validity)
    _write_png(paths["stability_mask"], stability)
    valid_pixels = int(np.count_nonzero(validity_bool))
    return {
        "frame_count": len(frames),
        "canonical_static_target_artifact": "median_rgb",
        "required_valid_samples_per_pixel": required_valid_samples,
        "dynamic_exclusion": {
            "method": "union_of_expanded_persisted_detection_bbox_proposals",
            "proposal_only": True,
            "masked_frame_pixel_fraction": float(np.mean(dynamic_masks)),
        },
        "valid_pixel_fraction": float(np.mean(validity_bool)),
        "stable_pixel_fraction": float(np.mean(stability == 255)),
        "stable_fraction_of_valid_pixels": (
            0.0
            if valid_pixels == 0
            else float(np.count_nonzero(stability_bool) / valid_pixels)
        ),
        "artifacts": {
            name: {
                "path": str(path.relative_to(directory.parent)),
                "sha256": sha256_file(path),
                "width": int(static_median.shape[1]),
                "height": int(static_median.shape[0]),
            }
            for name, path in paths.items()
        },
    }


def build_targets(
    capture_reports,
    camera_id,
    output_dir,
    *,
    seed="v2x-temporal-static-targets/v1",
    stability_mad_threshold=12.0,
    dynamic_bbox_expansion_fraction=0.15,
    minimum_valid_samples=3,
    minimum_valid_fraction=0.5,
    proposal_only_no_split=False,
):
    if camera_id not in CAMERAS:
        raise StaticTargetError("camera must be ch1 through ch4")
    if not isinstance(seed, str) or not seed:
        raise StaticTargetError("split seed must be nonempty")
    if (
        not isinstance(stability_mad_threshold, (int, float))
        or isinstance(stability_mad_threshold, bool)
        or not math.isfinite(float(stability_mad_threshold))
        or not 0 <= float(stability_mad_threshold) <= 255
    ):
        raise StaticTargetError("stability MAD threshold must be between 0 and 255")
    if (
        not isinstance(dynamic_bbox_expansion_fraction, (int, float))
        or isinstance(dynamic_bbox_expansion_fraction, bool)
        or not math.isfinite(float(dynamic_bbox_expansion_fraction))
        or not 0 <= float(dynamic_bbox_expansion_fraction) <= 1
    ):
        raise StaticTargetError("dynamic bbox expansion fraction must be between 0 and 1")
    if (
        not isinstance(minimum_valid_samples, int)
        or isinstance(minimum_valid_samples, bool)
        or not 1 <= minimum_valid_samples <= np.iinfo(np.uint16).max
    ):
        raise StaticTargetError("minimum valid samples must be 1 through 65535")
    if (
        not isinstance(minimum_valid_fraction, (int, float))
        or isinstance(minimum_valid_fraction, bool)
        or not math.isfinite(float(minimum_valid_fraction))
        or not 0 < float(minimum_valid_fraction) <= 1
    ):
        raise StaticTargetError("minimum valid fraction must be greater than 0 and at most 1")
    reports = list(capture_reports)
    if not reports:
        raise StaticTargetError("at least one capture report is required")
    windows = [
        load_window(value, camera_id, float(dynamic_bbox_expansion_fraction))
        for value in reports
    ]
    if len({value["report_path"] for value in windows}) != len(windows):
        raise StaticTargetError("capture reports must be unique")
    dimensions = {
        (frame["width"], frame["height"])
        for window in windows
        for frame in window["frames"]
    }
    if len(dimensions) != 1:
        raise StaticTargetError("all selected frames must have identical dimensions")
    frame_hashes = [
        frame["sha256"] for window in windows for frame in window["frames"]
    ]
    if len(set(frame_hashes)) != len(frame_hashes):
        raise StaticTargetError("frame sha256 values must be unique across capture windows")
    if proposal_only_no_split:
        assignment = {value["window_id"]: "proposal" for value in windows}
        split_strategy = {
            "mode": "explicit_no_split_proposal_only",
            "seed": seed,
            "whole_capture_window_atomic": True,
        }
        split_names = ("proposal",)
    else:
        assignment, split_strategy = deterministic_split(windows, seed)
        split_names = SPLITS

    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir.exists():
        raise StaticTargetError("output directory already exists; refusing overwrite")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_dir.parent / f".{output_dir.name}.tmp-{uuid.uuid4().hex}"
    if temporary.exists():
        raise StaticTargetError("temporary output path unexpectedly exists")
    temporary.mkdir()
    try:
        composites = {}
        for split in split_names:
            frames = [
                frame
                for window in windows
                if assignment[window["window_id"]] == split
                for frame in window["frames"]
            ]
            if not frames:
                raise StaticTargetError(f"{split} split contains no frames")
            composites[split] = _composite(
                frames,
                float(stability_mad_threshold),
                minimum_valid_samples,
                float(minimum_valid_fraction),
                temporary / split,
            )
        width, height = next(iter(dimensions))
        manifest = {
            "schema": OUTPUT_SCHEMA,
            "acceptance_eligible": False,
            "acceptance_failures": [
                "proposal_only_not_annotation_truth",
                "not_measured_camera_intrinsics",
                "not_surveyed_static_geometry",
                "not_independent_acceptance_evidence",
                "dynamic_exclusion_masks_are_detection_proposals_not_truth",
                "masked_static_target_requires_independent_review",
            ],
            "purpose": (
                "Proposal/input for reviewed static calibration target creation only; "
                "not measured intrinsics, annotation truth, or acceptance proof."
            ),
            "camera_id": camera_id,
            "resolution": [width, height],
            "stability": {
                "method": "masked_per_channel_median_absolute_deviation",
                "stable_when_max_channel_mad_lte": float(stability_mad_threshold),
                "minimum_unmasked_samples": minimum_valid_samples,
                "minimum_unmasked_fraction": float(minimum_valid_fraction),
            },
            "dynamic_exclusion": {
                "source": "event_bbox_xyxy",
                "provenance": "persisted_detection_bbox_proposal_only",
                "acceptance_eligible": False,
                "expansion_fraction_per_side": float(
                    dynamic_bbox_expansion_fraction
                ),
                "duplicate_frame_boxes_combined_by_union": True,
                "dense_frames_have_dynamic_exclusion_masks": False,
            },
            "split_strategy": split_strategy,
            "windows": [
                {
                    "window_id": window["window_id"],
                    "split": assignment[window["window_id"]],
                    "source_directory": window["source_directory"],
                    "capture_report": {
                        "path": window["report_path"],
                        "sha256": window["report_sha256"],
                        "schema": window["schema"],
                    },
                    "first_timestamp_utc": window["frames"][0]["timestamp_utc"],
                    "last_timestamp_utc": window["frames"][-1]["timestamp_utc"],
                    "frames": [
                        {
                            "identity": frame["identity"],
                            "source_identities": sorted(frame["identities"]),
                            "path": frame["path"],
                            "source_paths": sorted(set(frame["source_paths"])),
                            "sha256": frame["sha256"],
                            "width": frame["width"],
                            "height": frame["height"],
                            "timestamp_utc": frame["timestamp_utc"],
                            "dynamic_exclusion_boxes": frame[
                                "dynamic_exclusion_boxes"
                            ],
                        }
                        for frame in window["frames"]
                    ],
                }
                for window in sorted(windows, key=lambda value: value["window_id"])
            ],
            "composites": composites,
            "safety": {
                "source_reports_mutated": False,
                "source_frames_mutated": False,
                "whole_capture_window_split_leakage": False,
                "dynamic_exclusion_used_as_calibration_truth": False,
                "masked_pixels_can_be_marked_stable": False,
                "output_refuses_overwrite": True,
                "output_staged_then_renamed": True,
            },
        }
        (temporary / "manifest.json").write_bytes(
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        )
        _atomic_rename_noreplace(temporary, output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output_dir / "manifest.json"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-report", action="append", required=True, type=Path)
    parser.add_argument("--camera", required=True, choices=sorted(CAMERAS))
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seed", default="v2x-temporal-static-targets/v1")
    parser.add_argument("--stability-mad-threshold", type=float, default=12.0)
    parser.add_argument("--dynamic-bbox-expansion-fraction", type=float, default=0.15)
    parser.add_argument("--minimum-valid-samples", type=int, default=3)
    parser.add_argument("--minimum-valid-fraction", type=float, default=0.5)
    parser.add_argument(
        "--proposal-only-no-split",
        action="store_true",
        help="allow one proposal composite without fit/dev/holdout isolation",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    manifest = build_targets(
        args.capture_report,
        args.camera,
        args.output_dir,
        seed=args.seed,
        stability_mad_threshold=args.stability_mad_threshold,
        dynamic_bbox_expansion_fraction=args.dynamic_bbox_expansion_fraction,
        minimum_valid_samples=args.minimum_valid_samples,
        minimum_valid_fraction=args.minimum_valid_fraction,
        proposal_only_no_split=args.proposal_only_no_split,
    )
    print(manifest)


if __name__ == "__main__":
    main()
