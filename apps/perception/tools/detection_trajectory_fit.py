#!/usr/bin/env python3
"""Detection-assisted camera-pose diagnostic around a surveyed static solution.

This module deliberately does not estimate intrinsics or a site transform.  A
ground-plane vehicle state is analytically eliminated by intersecting each
reviewed contact ray with z=0 in the surveyed ENU frame.  Reviewed lane paths,
constant-velocity motion, and reviewed simultaneous cross-camera pairs then
constrain small, prior-bounded changes to the independently fitted camera pose.

The result is always diagnostic.  It cannot become deployment eligible without
the separate immutable static-geometry, locked holdout, bootstrap, RTK, and UE5
production-representability gates.
"""

from __future__ import annotations

from collections import defaultdict
import math
import re


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TrajectoryFitError(ValueError):
    pass


def _finite_vector(value, size, label):
    if not isinstance(value, list) or len(value) != size:
        raise TrajectoryFitError(f"{label} must contain {size} values")
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise TrajectoryFitError(f"{label} must be finite")
        item = float(item)
        if not math.isfinite(item):
            raise TrajectoryFitError(f"{label} must be finite")
        result.append(item)
    return result


def _camera_optics(camera):
    import numpy as np

    calibration = camera["intrinsics_calibration"]
    matrix = np.asarray(calibration["camera_matrix"], dtype=float)
    distortion = calibration["distortion"]
    coefficients = np.asarray(
        [distortion[key] for key in ("k1", "k2", "p1", "p2", "k3")],
        dtype=float,
    )
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all() or not np.isfinite(coefficients).all():
        raise TrajectoryFitError("camera optics are invalid")
    return matrix, coefficients


def _ground_intersection(pixel, matrix, distortion, rvec, tvec):
    import cv2
    import numpy as np

    point = np.asarray(pixel, dtype=float).reshape(1, 1, 2)
    normalized = cv2.undistortPoints(point, matrix, distortion).reshape(2)
    rotation, _ = cv2.Rodrigues(np.asarray(rvec, dtype=float))
    translation = np.asarray(tvec, dtype=float)
    centre = -rotation.T @ translation
    ray = rotation.T @ np.asarray([normalized[0], normalized[1], 1.0])
    if not np.isfinite(ray).all() or abs(float(ray[2])) < 1e-9:
        return None
    scale = -float(centre[2]) / float(ray[2])
    if not math.isfinite(scale) or scale <= 0.0:
        return None
    world = centre + scale * ray
    return world[:2]


def _point_polyline_projection(point, polyline):
    import numpy as np

    point = np.asarray(point, dtype=float)
    best = math.inf
    best_projection = None
    best_normal = None
    for left, right in zip(polyline, polyline[1:]):
        left = np.asarray(left, dtype=float)
        delta = np.asarray(right, dtype=float) - left
        denominator = float(delta @ delta)
        if denominator <= 1e-12:
            continue
        fraction = 0.0 if denominator <= 1e-12 else float((point - left) @ delta) / denominator
        projection = left + min(1.0, max(0.0, fraction)) * delta
        distance = float(np.linalg.norm(point - projection))
        if distance < best:
            best = distance
            best_projection = projection
            best_normal = np.asarray([-delta[1], delta[0]]) / math.sqrt(denominator)
    if best_projection is None:
        raise TrajectoryFitError("lane polyline contains no nonzero segment")
    return best_projection, best_normal


def validate_static_solution(value, camera_ids, cameras_json_sha256):
    if not isinstance(value, dict) or value.get("schema") != "v2x-static-camera-solution/v1":
        raise TrajectoryFitError("static solution schema is unsupported")
    if value.get("source_cameras_json_sha256") != cameras_json_sha256:
        raise TrajectoryFitError("static solution camera-config hash mismatch")
    if value.get("site_frame") != "surveyed_enu_z_up":
        raise TrajectoryFitError("static solution must use surveyed ENU z-up")
    truth = value.get("truth")
    if (
        not isinstance(truth, dict)
        or truth.get("kind") not in {"surveyed_static_geometry", "surveyed_control_points"}
        or truth.get("heldout_gate_passed") is not True
        or not isinstance(truth.get("manifest_sha256"), str)
        or SHA256_RE.fullmatch(truth["manifest_sha256"]) is None
    ):
        raise TrajectoryFitError("static solution lacks passing independent truth")
    transform = value.get("site_to_map_transform")
    if (
        not isinstance(transform, dict)
        or transform.get("frozen") is not True
        or transform.get("model") != "se2_fixed_scale"
        or not isinstance(transform.get("artifact_sha256"), str)
        or SHA256_RE.fullmatch(transform["artifact_sha256"]) is None
    ):
        raise TrajectoryFitError("site-to-map transform is not frozen")
    solutions = value.get("cameras")
    if not isinstance(solutions, dict) or set(solutions) != set(camera_ids):
        raise TrajectoryFitError("static solution camera set mismatch")
    normalized = {}
    for camera_id in camera_ids:
        camera = solutions[camera_id]
        if not isinstance(camera, dict):
            raise TrajectoryFitError(f"{camera_id} static solution is invalid")
        rvec = _finite_vector(camera.get("world_to_camera_rvec"), 3, f"{camera_id} rvec")
        tvec = _finite_vector(camera.get("world_to_camera_tvec_m"), 3, f"{camera_id} tvec")
        prior = camera.get("pose_prior_sigma")
        bounds = camera.get("diagnostic_bounds")
        if not isinstance(prior, dict) or not isinstance(bounds, dict):
            raise TrajectoryFitError(f"{camera_id} prior/bounds are missing")
        translation_sigma = float(prior.get("translation_m", math.nan))
        rotation_sigma_deg = float(prior.get("rotation_deg", math.nan))
        translation_bound = float(bounds.get("translation_m", math.nan))
        rotation_bound_deg = float(bounds.get("rotation_deg", math.nan))
        if not (
            0.0 < translation_sigma <= 0.50
            and 0.0 < rotation_sigma_deg <= 1.0
            and translation_sigma <= translation_bound <= 1.0
            and rotation_sigma_deg <= rotation_bound_deg <= 3.0
        ):
            raise TrajectoryFitError(f"{camera_id} prior/bounds are unsafe")
        normalized[camera_id] = {
            "rvec": rvec,
            "tvec": tvec,
            "translation_sigma": translation_sigma,
            "rotation_sigma_rad": math.radians(rotation_sigma_deg),
            "translation_bound": translation_bound,
            "rotation_bound_rad": math.radians(rotation_bound_deg),
        }
    return normalized


def validate_lane_map(value, required_lane_ids):
    if not isinstance(value, dict) or value.get("schema") != "v2x-surveyed-lane-map/v1":
        raise TrajectoryFitError("lane-map schema is unsupported")
    if value.get("site_frame") != "surveyed_enu_z_up" or value.get("independent_of_detections") is not True:
        raise TrajectoryFitError("lane map is not independent surveyed ENU evidence")
    if (
        not isinstance(value.get("survey_manifest_sha256"), str)
        or SHA256_RE.fullmatch(value["survey_manifest_sha256"]) is None
    ):
        raise TrajectoryFitError("lane map lacks a valid survey manifest hash")
    accuracy = value.get("survey_accuracy_m")
    if isinstance(accuracy, bool) or not isinstance(accuracy, (int, float)) or not 0.0 < float(accuracy) <= 0.25:
        raise TrajectoryFitError("lane-map survey accuracy is invalid")
    raw_paths = value.get("lane_paths")
    if not isinstance(raw_paths, dict):
        raise TrajectoryFitError("lane-map paths are missing")
    paths = {}
    for lane_id in required_lane_ids:
        polyline = raw_paths.get(lane_id)
        if not isinstance(polyline, list) or len(polyline) < 2:
            raise TrajectoryFitError(f"lane path {lane_id} is missing")
        paths[lane_id] = [_finite_vector(point, 2, f"lane path {lane_id}") for point in polyline]
    return paths


def fit_detection_constraints(
    *,
    cameras,
    static_solution,
    lane_map,
    tracks,
    synchronized_pairs,
    cameras_json_sha256,
    multistarts=5,
):
    """Run a deterministic, robust, prior-bounded diagnostic fit."""
    import numpy as np
    from scipy.optimize import least_squares

    if not isinstance(multistarts, int) or not 1 <= multistarts <= 20:
        raise TrajectoryFitError("multistarts must be between 1 and 20")
    camera_ids = tuple(sorted(cameras))
    if not camera_ids or not isinstance(tracks, list) or not tracks:
        raise TrajectoryFitError("fit requires cameras and tracks")
    static = validate_static_solution(static_solution, camera_ids, cameras_json_sha256)
    required_lanes = {track["lane_path_id"] for track in tracks}
    lanes = validate_lane_map(lane_map, required_lanes)
    optics = {camera_id: _camera_optics(cameras[camera_id]) for camera_id in camera_ids}

    event_index = {}
    event_tracks = {}
    normalized_tracks = []
    for track in tracks:
        camera_id = track.get("camera_id")
        event_ids = track.get("event_ids")
        pixels = track.get("pixels")
        times = track.get("times_epoch")
        lane_id = track.get("lane_path_id")
        covariances = track.get("covariances_px2")
        split = track.get("split", "fit")
        direction_deg = track.get("motion_direction_deg")
        if (
            camera_id not in cameras
            or not isinstance(event_ids, list)
            or len(event_ids) < 3
            or not isinstance(pixels, list)
            or not isinstance(times, list)
            or len(pixels) != len(event_ids)
            or len(times) != len(event_ids)
            or not isinstance(covariances, list)
            or len(covariances) != len(event_ids)
            or lane_id not in lanes
            or split not in {"fit", "validation", "holdout"}
            or isinstance(direction_deg, bool)
            or not isinstance(direction_deg, (int, float))
            or not math.isfinite(float(direction_deg))
        ):
            raise TrajectoryFitError("track structure is invalid")
        normalized_pixels = [_finite_vector(pixel, 2, "track pixel") for pixel in pixels]
        normalized_times = _finite_vector(times, len(times), "track times")
        if any(right <= left for left, right in zip(normalized_times, normalized_times[1:])):
            raise TrajectoryFitError("track timestamps must be strictly increasing")
        normalized_covariances = []
        for covariance in covariances:
            import numpy as np
            value = np.asarray(covariance, dtype=float)
            if (
                value.shape != (2, 2)
                or not np.isfinite(value).all()
                or not np.allclose(value, value.T, rtol=0.0, atol=1e-9)
                or np.linalg.eigvalsh(value).min() <= 0.0
            ):
                raise TrajectoryFitError("track contact covariance is invalid")
            normalized_covariances.append(value)
        row = {
            "tracklet_id": track.get("tracklet_id"),
            "camera_id": camera_id,
            "event_ids": event_ids,
            "pixels": normalized_pixels,
            "times": normalized_times,
            "covariances": normalized_covariances,
            "lane_path_id": lane_id,
            "motion_direction_deg": float(direction_deg),
            "includes_turn": track.get("includes_turn") is True,
            "split": split,
        }
        normalized_tracks.append(row)
        for event_id, pixel, epoch in zip(event_ids, normalized_pixels, normalized_times):
            if event_id in event_index:
                raise TrajectoryFitError("fit event is reused")
            event_index[event_id] = (camera_id, pixel, epoch)
            event_tracks[event_id] = row

    pairs = []
    pair_keys = set()
    clock_cameras = set()
    for pair in synchronized_pairs or []:
        ids = pair.get("event_ids") if isinstance(pair, dict) else None
        sigma = pair.get("time_sigma_s") if isinstance(pair, dict) else None
        if (
            not isinstance(ids, list)
            or len(ids) != 2
            or ids[0] not in event_index
            or ids[1] not in event_index
            or event_index[ids[0]][0] == event_index[ids[1]][0]
            or isinstance(sigma, bool)
            or not isinstance(sigma, (int, float))
            or not 0.0 < float(sigma) <= 0.10
            or pair.get("reviewed") is not True
        ):
            raise TrajectoryFitError("synchronized cross-camera pair is invalid")
        pair_key = tuple(sorted(ids))
        if pair_key in pair_keys:
            raise TrajectoryFitError("synchronized cross-camera pair is duplicated")
        pair_keys.add(pair_key)
        estimate_clock = pair.get("estimate_clock_offset") is True
        pairs.append((ids[0], ids[1], float(sigma), estimate_clock))
        if estimate_clock:
            clock_cameras.update(
                (event_index[ids[0]][0], event_index[ids[1]][0])
            )

    # One clock is the gauge reference. Cameras without synchronized evidence
    # remain fixed at zero rather than being weakly estimated.
    clock_reference = min(clock_cameras) if clock_cameras else None
    fitted_clock_ids = tuple(sorted(clock_cameras - {clock_reference}))
    pose_slices = {}
    values = []
    lower = []
    upper = []
    scales = []
    for camera_id in camera_ids:
        start = len(values)
        source = static[camera_id]
        values.extend(source["rvec"] + source["tvec"])
        pose_slices[camera_id] = slice(start, start + 6)
        lower.extend(
            [value - source["rotation_bound_rad"] for value in source["rvec"]]
            + [value - source["translation_bound"] for value in source["tvec"]]
        )
        upper.extend(
            [value + source["rotation_bound_rad"] for value in source["rvec"]]
            + [value + source["translation_bound"] for value in source["tvec"]]
        )
        scales.extend([source["rotation_sigma_rad"]] * 3 + [source["translation_sigma"]] * 3)
    clock_indexes = {}
    for camera_id in fitted_clock_ids:
        clock_indexes[camera_id] = len(values)
        values.append(0.0)
        lower.append(-0.50)
        upper.append(0.50)
        scales.append(0.05)
    initial = np.asarray(values, dtype=float)
    lower = np.asarray(lower, dtype=float)
    upper = np.asarray(upper, dtype=float)
    scales = np.asarray(scales, dtype=float)

    lane_sigma = max(0.25, float(lane_map["survey_accuracy_m"]) * 2.0)
    acceleration_sigma = 3.0
    position_pair_sigma = 0.75

    def camera_parameters(parameters, camera_id):
        block = parameters[pose_slices[camera_id]]
        return block[:3], block[3:6]

    def clock_offset(parameters, camera_id):
        return float(parameters[clock_indexes[camera_id]]) if camera_id in clock_indexes else 0.0

    def positions(parameters):
        result = {}
        for event_id, (camera_id, pixel, _epoch) in event_index.items():
            rvec, tvec = camera_parameters(parameters, camera_id)
            matrix, distortion = optics[camera_id]
            result[event_id] = _ground_intersection(pixel, matrix, distortion, rvec, tvec)
        return result

    def ground_jacobian(parameters, camera_id, pixel, baseline):
        matrix, distortion = optics[camera_id]
        rvec, tvec = camera_parameters(parameters, camera_id)
        columns = []
        for axis in range(2):
            shifted = list(pixel)
            shifted[axis] += 0.5
            projected = _ground_intersection(
                shifted, matrix, distortion, rvec, tvec
            )
            if projected is None:
                return None
            columns.append((projected - baseline) / 0.5)
        return np.column_stack(columns)

    def trajectory_position(track, ground, parameters, target_epoch):
        points = [ground[event_id] for event_id in track["event_ids"]]
        if any(point is None for point in points):
            return None
        times = [
            epoch + clock_offset(parameters, track["camera_id"])
            for epoch in track["times"]
        ]
        if target_epoch <= times[0]:
            index = 0
        elif target_epoch >= times[-1]:
            index = len(times) - 2
        else:
            index = next(
                value
                for value in range(len(times) - 1)
                if times[value] <= target_epoch <= times[value + 1]
            )
        fraction = (target_epoch - times[index]) / (times[index + 1] - times[index])
        return points[index] + fraction * (points[index + 1] - points[index])

    def residuals(parameters, include_priors=True,
                  partitions=frozenset({"fit"}), camera_filter=None):
        result = []
        ground = positions(parameters)
        invalid_penalty = 1.0e3
        for track in normalized_tracks:
            if (
                track["split"] not in partitions
                or camera_filter is not None
                and track["camera_id"] != camera_filter
            ):
                continue
            points = [ground[event_id] for event_id in track["event_ids"]]
            if any(point is None for point in points):
                result.extend(
                    [invalid_penalty]
                    * (len(points) + 2 * (len(points) - 2) + 1)
                )
                continue
            lane = lanes[track["lane_path_id"]]
            for event_id, point, covariance in zip(
                track["event_ids"], points, track["covariances"]
            ):
                camera_id, pixel, _epoch = event_index[event_id]
                jacobian = ground_jacobian(
                    parameters, camera_id, pixel, point
                )
                if jacobian is None:
                    result.append(invalid_penalty)
                    continue
                projection, normal = _point_polyline_projection(point, lane)
                world_covariance = (
                    jacobian @ covariance @ jacobian.T
                    + np.eye(2) * float(lane_map["survey_accuracy_m"]) ** 2
                )
                variance = float(normal @ world_covariance @ normal)
                if not math.isfinite(variance) or variance <= 0.0:
                    result.append(invalid_penalty)
                    continue
                signed_distance = float((point - projection) @ normal)
                result.append(signed_distance / math.sqrt(variance))
            adjusted_times = [epoch + clock_offset(parameters, track["camera_id"]) for epoch in track["times"]]
            for index in range(1, len(points) - 1):
                before = (points[index] - points[index - 1]) / (adjusted_times[index] - adjusted_times[index - 1])
                after = (points[index + 1] - points[index]) / (adjusted_times[index + 1] - adjusted_times[index])
                result.extend(((after - before) / acceleration_sigma).tolist())
            displacement = points[-1] - points[0]
            displacement_norm = float(np.linalg.norm(displacement))
            if displacement_norm <= 1e-9:
                result.append(invalid_penalty)
            else:
                expected_heading = math.radians(track["motion_direction_deg"])
                expected = np.asarray(
                    [math.sin(expected_heading), math.cos(expected_heading)]
                )
                observed = displacement / displacement_norm
                cross = float(expected[0] * observed[1] - expected[1] * observed[0])
                dot = float(np.clip(expected @ observed, -1.0, 1.0))
                result.append(math.atan2(cross, dot) / math.radians(15.0))
        for left_id, right_id, time_sigma, estimate_clock in pairs:
            if camera_filter is not None:
                continue
            if event_tracks[left_id]["split"] not in partitions:
                continue
            left_camera, _pixel, left_epoch = event_index[left_id]
            right_camera, _pixel, right_epoch = event_index[right_id]
            corrected_left = left_epoch + clock_offset(parameters, left_camera)
            corrected_right = right_epoch + clock_offset(parameters, right_camera)
            common_epoch = (corrected_left + corrected_right) / 2.0
            left = trajectory_position(
                event_tracks[left_id], ground, parameters, common_epoch
            )
            right = trajectory_position(
                event_tracks[right_id], ground, parameters, common_epoch
            )
            if left is None or right is None:
                result.extend(
                    [invalid_penalty] * (3 if estimate_clock else 2)
                )
                continue
            result.extend(((left - right) / position_pair_sigma).tolist())
            corrected_delta = corrected_left - corrected_right
            if estimate_clock:
                result.append(corrected_delta / time_sigma)
        if include_priors:
            for camera_id in camera_ids:
                source = static[camera_id]
                block = parameters[pose_slices[camera_id]]
                result.extend(((block[:3] - source["rvec"]) / source["rotation_sigma_rad"]).tolist())
                result.extend(((block[3:6] - source["tvec"]) / source["translation_sigma"]).tolist())
            result.extend(parameters[index] / 0.05 for index in clock_indexes.values())
        return np.asarray(result, dtype=float)

    rng = np.random.default_rng(0x563258)
    starts = [initial]
    for _ in range(multistarts - 1):
        candidate = initial + rng.normal(0.0, scales * 0.25)
        starts.append(np.minimum(upper - 1e-9, np.maximum(lower + 1e-9, candidate)))
    solutions = []
    for start in starts:
        solution = least_squares(
            residuals,
            start,
            bounds=(lower, upper),
            loss="soft_l1",
            f_scale=1.0,
            x_scale=scales,
            max_nfev=1000,
        )
        solutions.append(solution)
    best = min(solutions, key=lambda value: float(np.sum(residuals(value.x) ** 2)))
    fitted = best.x

    def data_rms(parameters, partition="fit", camera_id=None):
        values = residuals(
            parameters,
            include_priors=False,
            partitions=frozenset({partition}),
            camera_filter=camera_id,
        )
        return float(math.sqrt(float(np.mean(values * values)))) if len(values) else math.inf

    # Numerical data-only Jacobian prevents priors from manufacturing apparent
    # observability.  Scale columns into comparable one-sigma units.
    baseline_data = residuals(fitted, include_priors=False)
    columns = []
    for index, scale in enumerate(scales):
        step = max(abs(float(scale)) * 1e-4, 1e-7)
        perturbed = fitted.copy()
        perturbed[index] = min(float(upper[index]) - 1e-10, float(perturbed[index]) + step)
        actual_step = float(perturbed[index] - fitted[index])
        columns.append((residuals(perturbed, include_priors=False) - baseline_data) / actual_step * scale)
    jacobian = np.column_stack(columns)
    singular = np.linalg.svd(jacobian, compute_uv=False)
    tolerance = max(jacobian.shape) * np.finfo(float).eps * singular[0] if len(singular) else math.inf
    rank = int(np.sum(singular > tolerance)) if len(singular) else 0
    condition = float(singular[0] / singular[-1]) if len(singular) and singular[-1] > tolerance else math.inf
    at_bound = bool(np.any((fitted - lower) <= (upper - lower) * 0.01) or np.any((upper - fitted) <= (upper - lower) * 0.01))

    camera_results = {}
    for camera_id in camera_ids:
        source = static[camera_id]
        block = fitted[pose_slices[camera_id]]
        camera_results[camera_id] = {
            "world_to_camera_rvec": block[:3].tolist(),
            "world_to_camera_tvec_m": block[3:6].tolist(),
            "delta_rotation_vector_deg": np.degrees(block[:3] - np.asarray(source["rvec"])).tolist(),
            "delta_translation_m": (block[3:6] - np.asarray(source["tvec"])).tolist(),
            "clock_offset_s": clock_offset(fitted, camera_id),
            "clock_status": "reference" if camera_id == clock_reference else "estimated" if camera_id in clock_indexes else "fixed_zero_unobservable",
        }
    split_metrics = {}
    for partition in ("fit", "validation", "holdout"):
        partition_tracks = [
            track for track in normalized_tracks if track["split"] == partition
        ]
        if not partition_tracks:
            continue
        per_camera = {}
        for camera_id in camera_ids:
            if not any(track["camera_id"] == camera_id for track in partition_tracks):
                continue
            per_camera[camera_id] = {
                "initial_data_rms_normalized": data_rms(
                    initial, partition, camera_id
                ),
                "final_data_rms_normalized": data_rms(
                    fitted, partition, camera_id
                ),
            }
        split_metrics[partition] = {
            "initial_data_rms_normalized": data_rms(initial, partition),
            "final_data_rms_normalized": data_rms(fitted, partition),
            "cameras": per_camera,
        }
    no_frozen_split_degraded = all(
        camera_metrics["final_data_rms_normalized"]
        <= camera_metrics["initial_data_rms_normalized"] + 1e-9
        for partition, metrics in split_metrics.items()
        if partition != "fit"
        for camera_metrics in metrics["cameras"].values()
    )
    numerical_passed = (
        bool(best.success)
        and np.isfinite(fitted).all()
        and rank == len(fitted)
        and condition <= 1.0e8
        and not at_bound
        and data_rms(fitted) < data_rms(initial)
        and no_frozen_split_degraded
    )
    reasons = []
    if not best.success:
        reasons.append("least_squares_failed")
    if rank != len(fitted):
        reasons.append("data_jacobian_rank_deficient")
    if condition > 1.0e8:
        reasons.append("data_jacobian_ill_conditioned")
    if at_bound:
        reasons.append("parameter_at_bound")
    if not data_rms(fitted) < data_rms(initial):
        reasons.append("no_data_residual_improvement")
    if not no_frozen_split_degraded:
        reasons.append("validation_or_holdout_degraded")
    return {
        "schema": "v2x-detection-trajectory-fit/v1",
        "fit_completed": bool(best.success) and np.isfinite(fitted).all(),
        "numerical_gate_passed": numerical_passed,
        "acceptance_eligible": False,
        "acceptance_blockers": [
            "requires_locked_independent_static_holdout_evaluation",
            "requires_whole-track_bootstrap_pose_spread",
            "requires_independent_rtk_vehicle_holdout",
            "requires_production_representability_and_ue5_visual_gate",
        ],
        "reasons": reasons,
        "objective": {
            "initial_data_rms_normalized": data_rms(initial),
            "final_data_rms_normalized": data_rms(fitted),
            "robust_loss": "soft_l1",
            "multistarts": multistarts,
            "splits": split_metrics,
        },
        "observability": {
            "data_only": True,
            "rank": rank,
            "parameters": len(fitted),
            "normalized_condition": condition,
            "condition_limit": 1.0e8,
            "parameter_at_one_percent_bound": at_bound,
        },
        "counts": {
            "tracks": len(normalized_tracks),
            "observations": len(event_index),
            "synchronized_pairs": len(pairs),
            "clock_estimating_pairs": sum(pair[3] for pair in pairs),
            "tracks_by_split": {
                partition: sum(
                    track["split"] == partition for track in normalized_tracks
                )
                for partition in ("fit", "validation", "holdout")
            },
        },
        "cameras": camera_results,
        "contract": {
            "derived_gps_parsed": False,
            "intrinsics_fixed": True,
            "site_to_map_transform_frozen": True,
            "lane_snapping_used": False,
            "lane_distance_is_weak_residual": True,
            "reviewed_motion_direction_used": True,
            "contact_covariance_propagated_to_lane_normal": True,
            "pose_priors_excluded_from_observability": True,
        },
    }
