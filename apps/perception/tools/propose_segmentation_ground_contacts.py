#!/usr/bin/env python3
"""Propose vehicle road-contact midpoints from exact-frame instance masks.

The bbox centre is not a ground point and the bbox bottom can be extended by a
shadow, trailer, or detector padding.  This tool keeps the full segmentation
mask and estimates the midpoint of its lowest, spatially distributed support
envelope.  Results are hash-bound diagnostic proposals; they do not replace an
independent contact review or static camera calibration.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import weakref

import cv2
import numpy as np

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from redetect_selected_capture_frames import (
    VEHICLE_LABELS,
    choose_event_match,
    decode_bound_frame,
    load_capture_report,
    sha256,
    sha256_bytes,
    touches_boundary,
    write_bytes_exclusive,
    write_json_exclusive,
)
from capture_static_kvs_window import atomic_publish_directory, StaticCaptureError


SCHEMA = "v2x-segmentation-ground-contact-proposals/v1"


class ContactProposalError(RuntimeError):
    pass


class StagedAssetDirectory:
    """Hide all segmentation artifacts until one atomic no-replace publish."""

    def __init__(self, destination):
        self.destination = Path(destination).resolve()
        if self.destination.exists():
            raise ContactProposalError("segmentation output directory already exists")
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(
            prefix=f".{self.destination.name}.tmp-", dir=self.destination.parent
        ))
        self._cleanup = weakref.finalize(self, shutil.rmtree, self.path, True)

    def publish(self):
        try:
            atomic_publish_directory(self.path, self.destination)
        except StaticCaptureError as exc:
            raise ContactProposalError(
                "atomic segmentation artifact publication failed"
            ) from exc
        self._cleanup.detach()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def validate_bbox(value, width, height):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or not all(math.isfinite(float(item)) for item in value)
    ):
        raise ContactProposalError("segmentation bbox is invalid")
    x1, y1, x2, y2 = map(float, value)
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise ContactProposalError("segmentation bbox is outside the frame")
    return np.asarray([x1, y1, x2, y2], dtype=float)


def has_visibility_margin(bbox, width, height, fraction=0.01):
    x1, y1, x2, y2 = map(float, bbox)
    return (
        x1 >= fraction * width
        and y1 >= fraction * height
        and x2 <= (1.0 - fraction) * width
        and y2 <= (1.0 - fraction) * height
    )


def largest_component(mask):
    binary = np.asarray(mask, dtype=np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    if count <= 1:
        return np.zeros_like(binary)
    index = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return (labels == index).astype(np.uint8)


def support_runs(columns):
    runs = []
    for value in map(int, columns):
        if not runs or value > runs[-1][-1] + 1:
            runs.append([value])
        else:
            runs[-1].append(value)
    return runs


def estimate_contact(mask, bbox, *, support_quantile=0.80):
    """Return a robust visible-footprint midpoint from one full-frame mask."""
    if not 0.5 <= float(support_quantile) <= 0.95:
        raise ContactProposalError("support quantile is outside [0.5, 0.95]")
    mask = np.asarray(mask)
    if mask.ndim != 2 or mask.size == 0:
        raise ContactProposalError("segmentation mask must be a non-empty plane")
    height, width = mask.shape
    x1, y1, x2, y2 = validate_bbox(bbox, width, height)
    ix1, iy1 = max(0, int(math.floor(x1))), max(0, int(math.floor(y1)))
    ix2, iy2 = min(width, int(math.ceil(x2))), min(height, int(math.ceil(y2)))
    component = largest_component((mask[iy1:iy2, ix1:ix2] > 0).astype(np.uint8))
    box_area = max(1.0, (x2 - x1) * (y2 - y1))
    mask_area = int(component.sum())
    if mask_area < max(32, int(0.04 * box_area)):
        raise ContactProposalError("segmentation mask is too small for its bbox")

    bottom_by_x = []
    for local_x in range(component.shape[1]):
        ys = np.flatnonzero(component[:, local_x])
        if ys.size:
            bottom_by_x.append((ix1 + local_x, iy1 + int(ys[-1])))
    if len(bottom_by_x) < max(8, int(0.08 * (x2 - x1))):
        raise ContactProposalError("segmentation support is too sparse")

    bottoms = np.asarray([item[1] for item in bottom_by_x], dtype=float)
    threshold = max(
        float(np.quantile(bottoms, float(support_quantile))),
        float(bottoms.max()) - max(2.0, 0.10 * (y2 - y1)),
    )
    selected = [(x, y) for x, y in bottom_by_x if y >= threshold]
    minimum_run = max(2, int(math.ceil(0.01 * (x2 - x1))))
    retained_columns = []
    for run in support_runs(x for x, _ in selected):
        if len(run) >= minimum_run:
            retained_columns.extend(run)
    retained_column_set = set(retained_columns)
    support = [(x, y) for x, y in selected if x in retained_column_set]
    if len(support) < max(4, minimum_run * 2):
        raise ContactProposalError("segmentation has no distributed road support")

    support_array = np.asarray(support, dtype=float)
    left_x, right_x = float(support_array[:, 0].min()), float(support_array[:, 0].max())
    support_span = right_x - left_x
    if support_span < 0.12 * (x2 - x1):
        raise ContactProposalError("segmentation road support is too narrow")
    left_y = float(np.median(support_array[support_array[:, 0] <= left_x + 1, 1]))
    right_y = float(np.median(support_array[support_array[:, 0] >= right_x - 1, 1]))
    midpoint = [(left_x + right_x) / 2.0, (left_y + right_y) / 2.0]
    median_y = float(np.median(support_array[:, 1]))
    mad_y = float(np.median(np.abs(support_array[:, 1] - median_y)))
    gaps = np.diff(np.unique(support_array[:, 0]))
    sigma_x = max(2.0, float(gaps.max(initial=1.0)) / 2.0)
    sigma_y = max(2.0, 1.4826 * mad_y)
    return {
        "method": "segmentation_visible_support_midpoint_proposal",
        "pixel": midpoint,
        "covariance_px2": [[sigma_x**2, 0.0], [0.0, sigma_y**2]],
        "support_endpoints": [[left_x, left_y], [right_x, right_y]],
        "support_span_fraction_of_bbox": support_span / (x2 - x1),
        "support_column_count": len(support),
        "mask_area_px": mask_area,
        "mask_fraction_of_bbox": mask_area / box_area,
        "support_quantile": float(support_quantile),
        "reviewed": False,
    }


def model_instances(model, image, confidence, iou_threshold, image_size, device):
    result = model.predict(
        source=image,
        conf=confidence,
        iou=iou_threshold,
        imgsz=image_size,
        device=device,
        retina_masks=True,
        verbose=False,
    )[0]
    if result.boxes is None or len(result.boxes) == 0:
        return []
    if result.masks is None:
        raise ContactProposalError("model did not return instance masks")
    masks = result.masks.data.detach().cpu().numpy()
    height, width = image.shape[:2]
    instances = []
    for index, box in enumerate(result.boxes):
        class_id = int(box.cls[0].item())
        label = str(result.names[class_id])
        if label not in VEHICLE_LABELS:
            continue
        mask = masks[index]
        if mask.shape != (height, width):
            mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
        instances.append({
            "label": label,
            "confidence": float(box.conf[0].item()),
            "bbox_xyxy": [float(value) for value in box.xyxy[0].tolist()],
            "mask": mask >= 0.5,
        })
    return sorted(
        instances,
        key=lambda item: (-item["confidence"], item["label"], item["bbox_xyxy"]),
    )


def draw_overlay(image, bbox, mask, proposal):
    output = image.copy()
    tint = np.zeros_like(output)
    tint[:, :, 1] = 255
    selected = mask.astype(bool)
    output[selected] = cv2.addWeighted(output[selected], 0.45, tint[selected], 0.55, 0)
    x1, y1, x2, y2 = np.rint(bbox).astype(int)
    cv2.rectangle(output, (x1, y1), (x2, y2), (0, 220, 255), 4)
    endpoints = np.rint(proposal["support_endpoints"]).astype(int)
    cv2.line(output, tuple(endpoints[0]), tuple(endpoints[1]), (255, 0, 255), 5)
    point = tuple(np.rint(proposal["pixel"]).astype(int))
    cv2.drawMarker(output, point, (0, 0, 255), cv2.MARKER_CROSS, 30, 4)
    ok, encoded = cv2.imencode(".jpg", output, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise ContactProposalError("failed to encode contact overlay")
    return encoded.tobytes()


def propose(capture_report, model_path, output_dir, *, confidence=0.25,
            iou_threshold=0.7, image_size=1280, device="cpu"):
    report_path, report_raw, report = load_capture_report(capture_report)
    model_path = Path(model_path).resolve()
    if not model_path.is_file():
        raise ContactProposalError("segmentation model is unavailable")
    try:
        import torch
        import ultralytics
        from ultralytics import YOLO
    except ImportError as exc:
        raise ContactProposalError("pinned segmentation runtime is unavailable") from exc
    model = YOLO(str(model_path))
    staged_assets = StagedAssetDirectory(output_dir)
    final_output_dir = staged_assets.destination
    output_dir = staged_assets.path
    events = []
    for event in report["events"]:
        frame_path, image = decode_bound_frame(event)
        height, width = image.shape[:2]
        instances = model_instances(
            model, image, confidence, iou_threshold, image_size, device
        )
        serializable = [
            {key: value for key, value in item.items() if key != "mask"}
            for item in instances
        ]
        match = choose_event_match(serializable, event.get("bbox_xyxy"), width, height)
        reasons = []
        proposal = None
        mask_descriptor = None
        overlay_descriptor = None
        if match is None:
            reasons.append("no_vehicle_instance_matches_event_hint")
        else:
            instance = instances[match["detection_index"]]
            bbox = instance["bbox_xyxy"]
            if (
                touches_boundary(bbox, width, height)
                or not has_visibility_margin(bbox, width, height)
            ):
                reasons.append("vehicle_instance_touches_frame_boundary")
            else:
                try:
                    proposal = estimate_contact(instance["mask"], bbox)
                except ContactProposalError as exc:
                    reasons.append(str(exc).replace(" ", "_"))
            if proposal is not None:
                event_id = event["event_id"]
                mask_path = output_dir / "masks" / f"{event_id}.png"
                ok, encoded_mask = cv2.imencode(
                    ".png", instance["mask"].astype(np.uint8) * 255,
                    [cv2.IMWRITE_PNG_COMPRESSION, 4],
                )
                if not ok:
                    raise ContactProposalError("failed to encode segmentation mask")
                write_bytes_exclusive(mask_path, encoded_mask.tobytes())
                mask_descriptor = {"path": str(mask_path), "sha256": sha256(mask_path)}
                overlay_path = output_dir / "overlays" / f"{event_id}.jpg"
                write_bytes_exclusive(
                    overlay_path,
                    draw_overlay(image, bbox, instance["mask"], proposal),
                )
                overlay_descriptor = {
                    "path": str(overlay_path), "sha256": sha256(overlay_path)
                }
        events.append({
            "event_id": event.get("event_id"),
            "camera_id": event.get("camera_id"),
            "selected_frame_timestamp_utc": event.get("selected_frame_timestamp_utc"),
            "frame": {
                "path": str(frame_path),
                "encoded_jpeg_sha256": event["frame"]["sha256"],
                "width": width,
                "height": height,
            },
            "event_bbox_matching_hint": event.get("bbox_xyxy"),
            "event_match_proposal": match,
            "matched_instance": (
                None if match is None else serializable[match["detection_index"]]
            ),
            "ground_contact_proposal": proposal,
            "mask": mask_descriptor,
            "overlay": overlay_descriptor,
            "rejection_reasons": reasons,
            "acceptance_eligible": False,
        })
    result = {
        "schema": SCHEMA,
        "generated_at_utc": utc_now(),
        "acceptance_eligible": False,
        "capture_report": {"path": str(report_path), "sha256": sha256_bytes(report_raw)},
        "model": {"path": str(model_path), "sha256": sha256(model_path)},
        "runtime": {
            "device": device,
            "image_size": image_size,
            "confidence": confidence,
            "nms_iou": iou_threshold,
            "ultralytics_version": ultralytics.__version__,
            "torch_version": torch.__version__,
            "opencv_version": cv2.__version__,
            "python_version": platform.python_version(),
        },
        "events": events,
        "summary": {
            "event_count": len(events),
            "proposal_count": sum(item["ground_contact_proposal"] is not None for item in events),
            "rejected_count": sum(item["ground_contact_proposal"] is None for item in events),
        },
        "acceptance_failures": [
            "segmentation_is_a_model_proposal_not_independent_contact_truth",
            "single_frame_support_does_not_supply_world_position_or_identity",
            "static_camera_calibration_must_pass_before_backprojection",
        ],
    }
    staged_assets.publish()
    for event in result["events"]:
        for name in ("mask", "overlay"):
            descriptor = event.get(name)
            if descriptor is not None:
                relative = Path(descriptor["path"]).relative_to(output_dir)
                descriptor["path"] = str(final_output_dir / relative)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-report", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument("--nms-iou", type=float, default=0.7)
    parser.add_argument("--image-size", type=int, default=1280)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    result = propose(
        args.capture_report,
        args.model,
        args.output_directory,
        confidence=args.confidence,
        iou_threshold=args.nms_iou,
        image_size=args.image_size,
        device=args.device,
    )
    write_json_exclusive(args.output, result)
    print(json.dumps({**result["summary"], "output": str(Path(args.output).resolve())}))


if __name__ == "__main__":
    main()
