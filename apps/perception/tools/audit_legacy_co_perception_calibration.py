#!/usr/bin/env python3
"""Audit legacy co-perception calibration inputs without importing its code.

The legacy repository is valuable provenance, but its Python files are not an
acceptance artifact.  This tool parses the active numeric inputs with ``ast``,
reproduces the pitch/yaw fit, and reports data geometry, runtime/config drift,
and missing evidence.  It never changes either repository or a live service.
"""

import argparse
import ast
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import re

import numpy as np
from scipy.optimize import minimize


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json_exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def assigned_expression(tree, name):
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            matches.append(node.value)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one assignment to {name}, found {len(matches)}")
    return matches[0]


def parse_legacy_calibration(path):
    tree = ast.parse(Path(path).read_text())
    points = ast.literal_eval(assigned_expression(tree, "calibration_points"))
    matrix_expression = assigned_expression(tree, "K")
    if not (
        isinstance(matrix_expression, ast.Call)
        and isinstance(matrix_expression.func, ast.Attribute)
        and matrix_expression.func.attr == "array"
        and matrix_expression.args
    ):
        raise ValueError("K is not a literal np.array call")
    matrix = np.asarray(ast.literal_eval(matrix_expression.args[0]), dtype=float)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise ValueError("legacy K must be a finite 3x3 matrix")
    if matrix[0, 0] <= 0 or matrix[1, 1] <= 0:
        raise ValueError("legacy K must have positive fx/fy")
    if not np.allclose(matrix[2], [0.0, 0.0, 1.0], atol=1e-12):
        raise ValueError("legacy K has an invalid homogeneous row")
    required = {"u", "v", "true_X", "true_Z"}
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError("legacy calibration_points requires at least two rows")
    if any(not isinstance(point, dict) or set(point) != required for point in points):
        raise ValueError("legacy calibration points do not have the exact expected fields")
    values = np.asarray([
        [point["u"], point["v"], point["true_X"], point["true_Z"]]
        for point in points
    ], dtype=float)
    if not np.all(np.isfinite(values)):
        raise ValueError("legacy calibration points contain non-finite values")
    return matrix, points


def literal_argument(call, index):
    try:
        return ast.literal_eval(call.args[index])
    except (IndexError, ValueError):
        return None


def parse_runtime_camera_calls(path):
    tree = ast.parse(Path(path).read_text())
    cameras = {}
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        function_name = (
            call.func.id if isinstance(call.func, ast.Name)
            else call.func.attr if isinstance(call.func, ast.Attribute)
            else None
        )
        if function_name != "VideoObjectDetector" or len(call.args) < 9:
            continue
        device_id = literal_argument(call, 8)
        if not isinstance(device_id, str) or "-ch" not in device_id:
            continue
        camera_id = device_id.rsplit("-", 1)[-1]
        values = {
            "height_m": literal_argument(call, 4),
            "pitch_deg": literal_argument(call, 5),
            "yaw_deg": literal_argument(call, 6),
            "heading_deg": literal_argument(call, 7),
            "device_id": device_id,
        }
        if not all(isinstance(values[key], (int, float)) for key in (
            "height_m", "pitch_deg", "yaw_deg", "heading_deg"
        )):
            raise ValueError(f"{camera_id} runtime parameters are not numeric literals")
        if camera_id in cameras:
            raise ValueError(f"duplicate runtime camera definition for {camera_id}")
        if float(values["height_m"]) <= 0:
            raise ValueError(f"{camera_id} runtime height must be positive")
        cameras[camera_id] = values
    if not cameras:
        raise ValueError("no legacy VideoObjectDetector camera definitions found")
    return cameras


def parse_calibration_csv(path, image_width, image_height):
    required = {
        "Point_ID", "u_pixel", "v_pixel", "True_X_m", "True_Z_m",
        "Pred_X_m", "Pred_Z_m", "Error_X_m", "Error_Z_m", "Total_Error_m",
    }
    with Path(path).open(newline="") as stream:
        reader = csv.DictReader(stream)
        if set(reader.fieldnames or ()) != required:
            raise ValueError(f"{path}: calibration CSV columns are unsupported")
        rows = list(reader)
    if len(rows) < 3:
        raise ValueError(f"{path}: calibration CSV has fewer than three rows")
    points, stored = [], []
    for index, row in enumerate(rows, start=1):
        if int(row["Point_ID"]) != index:
            raise ValueError(f"{path}: Point_ID sequence is not contiguous")
        values = {key: float(row[key]) for key in required - {"Point_ID"}}
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError(f"{path}: calibration CSV contains non-finite values")
        if not 0 <= values["u_pixel"] < image_width or not 0 <= values["v_pixel"] < image_height:
            raise ValueError(f"{path}: calibration pixel falls outside the nominal image")
        recomputed = math.hypot(
            values["Pred_X_m"] - values["True_X_m"],
            values["Pred_Z_m"] - values["True_Z_m"],
        )
        if not math.isclose(recomputed, values["Total_Error_m"], abs_tol=0.002):
            raise ValueError(f"{path}: stored total error is internally inconsistent")
        points.append({
            "u": values["u_pixel"],
            "v": values["v_pixel"],
            "true_X": values["True_X_m"],
            "true_Z": values["True_Z_m"],
        })
        stored.append(values["Total_Error_m"])
    return points, stored


def normalized_angle_delta(left, right):
    return (float(left) - float(right) + 180.0) % 360.0 - 180.0


def rounded_csv_matches_script_points(csv_points, script_points):
    if len(csv_points) != len(script_points):
        return False
    fields = ("u", "v", "true_X", "true_Z")
    return all(
        all(
            math.isclose(
                float(csv_point[field]), float(script_point[field]),
                abs_tol=0.011 if field in {"u", "v"} else 0.00051,
            )
            for field in fields
        )
        for csv_point, script_point in zip(csv_points, script_points)
    )


def predict_local(matrix, point, height_m, pitch_deg, yaw_deg):
    fx, fy = matrix[0, 0], matrix[1, 1]
    cx, cy = matrix[0, 2], matrix[1, 2]
    pitch, yaw = np.radians([pitch_deg, yaw_deg])
    rx = np.asarray([
        [1, 0, 0],
        [0, math.cos(pitch), -math.sin(pitch)],
        [0, math.sin(pitch), math.cos(pitch)],
    ])
    ry = np.asarray([
        [math.cos(yaw), 0, math.sin(yaw)],
        [0, 1, 0],
        [-math.sin(yaw), 0, math.cos(yaw)],
    ])
    ray = ry @ rx @ np.asarray([
        (point["u"] - cx) / fx,
        (point["v"] - cy) / fy,
        1.0,
    ])
    if ray[1] <= 1e-6:
        return None
    scale = height_m / ray[1]
    return np.asarray([scale * ray[0], scale * ray[2]])


def audit_csv_dataset(matrix, points, stored_errors, height_m, runtime_camera):
    fitted = fit_pitch_yaw(matrix, points, height_m)
    holdout_errors, parameters = [], []
    for index, point in enumerate(points):
        training = points[:index] + points[index + 1:]
        fold = fit_pitch_yaw(matrix, training, height_m)
        prediction = predict_local(
            matrix, point, height_m, fold["pitch_deg"], fold["yaw_deg"]
        )
        if prediction is None:
            holdout_errors.append(1_000_000.0)
        else:
            holdout_errors.append(float(np.linalg.norm(
                prediction - np.asarray([point["true_X"], point["true_Z"]])
            )))
        parameters.append([fold["pitch_deg"], fold["yaw_deg"]])
    parameters = np.asarray(parameters)
    yaw_center = fitted["yaw_deg"]
    normalized_yaw = np.asarray([
        yaw_center + normalized_angle_delta(value, yaw_center)
        for value in parameters[:, 1]
    ])
    holdout_errors = np.asarray(holdout_errors)
    return {
        "point_count": len(points),
        "data_geometry": data_geometry(points, matrix[0, 2] * 2, matrix[1, 2] * 2),
        "stored_error_m": {
            "mean": float(np.mean(stored_errors)),
            "rmse": float(math.sqrt(np.mean(np.asarray(stored_errors) ** 2))),
            "max": float(np.max(stored_errors)),
        },
        "reproduced_fit": fitted,
        "leave_one_out_error_m": {
            "median": float(np.median(holdout_errors)),
            "rmse": float(math.sqrt(np.mean(holdout_errors**2))),
            "max": float(np.max(holdout_errors)),
        },
        "leave_one_out_parameter_range_deg": {
            "pitch": float(np.ptp(parameters[:, 0])),
            "yaw_normalized": float(np.ptp(normalized_yaw)),
        },
        "fit_minus_same_label_runtime_deg": {
            "pitch": fitted["pitch_deg"] - float(runtime_camera["pitch_deg"]),
            "yaw": normalized_angle_delta(fitted["yaw_deg"], runtime_camera["yaw_deg"]),
        },
        "acceptance_eligible": False,
        "limitations": [
            "no_source_frame_hash",
            "no_global_landmark_ids",
            "camera_local_coordinates_only",
            "no_survey_method_or_units_artifact_beyond_column_names",
            "csv_channel_label_not_bound_to_image_or_script_revision",
        ],
    }


def data_geometry(points, image_width, image_height):
    pixels = np.asarray([[point["u"], point["v"]] for point in points], dtype=float)
    local = np.asarray([
        [point["true_X"], point["true_Z"]] for point in points
    ], dtype=float)

    def summarize(values, normalization):
        centered = values - values.mean(axis=0)
        singular = np.linalg.svd(centered, compute_uv=False)
        ratio = float(singular[1] / singular[0]) if singular[0] > 0 else 0.0
        span = np.ptp(values, axis=0)
        return {
            "singular_values": singular.tolist(),
            "secondary_to_primary_ratio": ratio,
            "collinear_at_0_05_ratio": ratio < 0.05,
            "span": span.tolist(),
            "normalized_span": (span / np.asarray(normalization)).tolist(),
        }

    return {
        "pixels": summarize(pixels, (image_width, image_height)),
        "local_xz_m": summarize(local, (1.0, 1.0)),
    }


def fit_pitch_yaw(matrix, points, height_m):
    fx, fy = matrix[0, 0], matrix[1, 1]
    cx, cy = matrix[0, 2], matrix[1, 2]

    def objective(angles):
        pitch, yaw = np.radians(angles)
        rx = np.asarray([
            [1, 0, 0],
            [0, math.cos(pitch), -math.sin(pitch)],
            [0, math.sin(pitch), math.cos(pitch)],
        ])
        ry = np.asarray([
            [math.cos(yaw), 0, math.sin(yaw)],
            [0, 1, 0],
            [-math.sin(yaw), 0, math.cos(yaw)],
        ])
        rotation = ry @ rx
        errors = []
        for point in points:
            ray = rotation @ np.asarray([
                (point["u"] - cx) / fx,
                (point["v"] - cy) / fy,
                1.0,
            ])
            if ray[1] <= 1e-6:
                return 999999.0
            scale = height_m / ray[1]
            errors.append(math.hypot(
                scale * ray[0] - point["true_X"],
                scale * ray[2] - point["true_Z"],
            ))
        return float(np.mean(errors))

    result = minimize(objective, [-40.0, -30.0], method="Nelder-Mead")
    optimum = result.x
    pitch, yaw = np.radians(optimum)
    rx = np.asarray([
        [1, 0, 0],
        [0, math.cos(pitch), -math.sin(pitch)],
        [0, math.sin(pitch), math.cos(pitch)],
    ])
    ry = np.asarray([
        [math.cos(yaw), 0, math.sin(yaw)],
        [0, 1, 0],
        [-math.sin(yaw), 0, math.cos(yaw)],
    ])
    rotation = ry @ rx
    residuals = []
    for point in points:
        ray = rotation @ np.asarray([
            (point["u"] - cx) / fx,
            (point["v"] - cy) / fy,
            1.0,
        ])
        scale = height_m / ray[1]
        residuals.append(math.hypot(
            scale * ray[0] - point["true_X"],
            scale * ray[2] - point["true_Z"],
        ))
    residuals = np.asarray(residuals)
    return {
        "success": bool(result.success),
        "pitch_deg": float(optimum[0]),
        "yaw_deg": float(optimum[1]),
        "in_sample_reproduction_loss_m": {
            "mean": float(np.mean(residuals)),
            "rmse": float(math.sqrt(np.mean(residuals**2))),
            "median": float(np.median(residuals)),
            "p95": float(np.quantile(residuals, 0.95)),
            "max": float(np.max(residuals)),
        },
        "optimizer": "scipy.optimize.minimize/Nelder-Mead",
        "initial_guess_deg": [-40.0, -30.0],
        "parameter_bounds": None,
        "warning": "optimized on unverified training points; not physical calibration accuracy",
    }


def git_head(repository):
    return subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()


def git_output(repository, *args):
    return subprocess.check_output(
        ["git", "-C", str(repository), *args], text=True
    ).strip()


def bind_repository_input(repository, path):
    repository = Path(repository).resolve()
    path = Path(path).resolve()
    try:
        relative = path.relative_to(repository)
    except ValueError as exc:
        raise ValueError(f"{path} is outside repository {repository}") from exc
    relative_text = relative.as_posix()
    try:
        tracked = git_output(repository, "ls-files", "--error-unmatch", "--", relative_text)
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"{relative_text} is not tracked by the repository") from exc
    if tracked != relative_text:
        raise ValueError(f"unexpected tracked path response for {relative_text}")
    head_blob = git_output(repository, "rev-parse", f"HEAD:{relative_text}")
    working_blob = git_output(repository, "hash-object", "--", relative_text)
    dirty_status = git_output(repository, "status", "--porcelain=v1", "--", relative_text)
    if dirty_status or head_blob != working_blob:
        raise ValueError(f"{relative_text} differs from the reported repository HEAD")
    return {
        "relative_path": relative_text,
        "git_blob_at_head": head_blob,
        "git_blob_working_tree": working_blob,
        "clean_at_head": True,
    }


def build_report(
    calibration_script,
    runtime_script,
    camera_config,
    repository,
    camera_config_repository,
    calibration_csvs=(),
):
    repository = Path(repository).resolve()
    calibration_binding = bind_repository_input(repository, calibration_script)
    runtime_binding = bind_repository_input(repository, runtime_script)
    camera_config_repository = Path(camera_config_repository).resolve()
    config_binding = bind_repository_input(camera_config_repository, camera_config)
    matrix, points = parse_legacy_calibration(calibration_script)
    runtime = parse_runtime_camera_calls(runtime_script)
    config = json.loads(Path(camera_config).read_text())
    configured = {camera["id"]: camera for camera in config.get("cameras", [])}
    if "ch4" not in runtime or "ch4" not in configured:
        raise ValueError("both legacy runtime and current config must define ch4")
    image_width = float(configured["ch4"]["intrinsics"]["width"])
    image_height = float(configured["ch4"]["intrinsics"]["height"])
    current_intrinsics = configured["ch4"]["intrinsics"]
    if image_width <= 0 or image_height <= 0:
        raise ValueError("current image dimensions must be positive")
    if float(current_intrinsics["fx"]) <= 0 or float(current_intrinsics["fy"]) <= 0:
        raise ValueError("current camera fx/fy must be positive")
    if any(
        not (0 <= float(point["u"]) < image_width)
        or not (0 <= float(point["v"]) < image_height)
        for point in points
    ):
        raise ValueError("legacy calibration pixels fall outside current image bounds")
    geometry = data_geometry(points, image_width, image_height)
    fit = fit_pitch_yaw(matrix, points, float(runtime["ch4"]["height_m"]))
    csv_reports = {}
    active_csv_matches = []
    for csv_path in calibration_csvs:
        csv_path = Path(csv_path).resolve()
        match = re.fullmatch(r"(ch[1-4])_calibration_errors\.csv", csv_path.name)
        if not match:
            raise ValueError(f"unexpected calibration CSV name: {csv_path.name}")
        camera_id = match.group(1)
        csv_points, stored_errors = parse_calibration_csv(
            csv_path, image_width, image_height
        )
        csv_reports[camera_id] = {
            "path": str(csv_path),
            "sha256": sha256(csv_path),
            **audit_csv_dataset(
                matrix, csv_points, stored_errors,
                float(runtime[camera_id]["height_m"]), runtime[camera_id],
            ),
        }
        if rounded_csv_matches_script_points(csv_points, points):
            active_csv_matches.append(camera_id)
    declared_match = re.search(
        r"Use these numbers for Channel\s+([1-4])!",
        Path(calibration_script).read_text(),
    )
    declared_script_channel = (
        f"ch{declared_match.group(1)}" if declared_match else None
    )
    expected_matrix = np.asarray([
        [current_intrinsics["fx"], 0.0, current_intrinsics["cx"]],
        [0.0, current_intrinsics["fy"], current_intrinsics["cy"]],
        [0.0, 0.0, 1.0],
    ])
    failures = []
    if len(points) < 12:
        failures.append("fewer_than_12_correspondences")
    failures.append("no_declared_fit_holdout_partition")
    if geometry["local_xz_m"]["collinear_at_0_05_ratio"]:
        failures.append("world_points_are_collinear")
    failures.append("intrinsics_are_not_bound_to_a_measured_calibration_artifact")
    failures.extend([
        "camera_local_coordinates_are_not_global_landmark_truth",
        "no_global_landmark_ids",
        "no_source_frame_hashes",
        "no_survey_provenance",
        "camera_local_point_generation_may_be_circular",
    ])
    if csv_reports and not active_csv_matches:
        failures.append("active_script_points_match_no_preserved_channel_csv")
    if (
        declared_script_channel
        and active_csv_matches
        and declared_script_channel not in active_csv_matches
    ):
        failures.append("active_script_channel_comment_disagrees_with_matching_csv")
    if abs(fit["pitch_deg"] - runtime["ch4"]["pitch_deg"]) > 1.0:
        failures.append("reproduced_pitch_disagrees_with_runtime")
    if abs(fit["yaw_deg"] - runtime["ch4"]["yaw_deg"]) > 1.0:
        failures.append("reproduced_yaw_disagrees_with_runtime")
    return {
        "schema": "v2x-legacy-co-perception-calibration-audit/v1",
        "generated_at": utc_now(),
        "acceptance_eligible": False,
        "repository": {
            "path": str(repository),
            "commit": git_head(repository),
            "working_tree_clean": not bool(git_output(repository, "status", "--porcelain=v1")),
        },
        "inputs": {
            "calibration_script": str(Path(calibration_script).resolve()),
            "calibration_script_sha256": sha256(calibration_script),
            "calibration_script_git_binding": calibration_binding,
            "runtime_script": str(Path(runtime_script).resolve()),
            "runtime_script_sha256": sha256(runtime_script),
            "runtime_script_git_binding": runtime_binding,
            "current_camera_config": str(Path(camera_config).resolve()),
            "current_camera_config_sha256": sha256(camera_config),
            "current_camera_config_git_binding": config_binding,
            "current_camera_config_repository": {
                "path": str(camera_config_repository),
                "commit": git_head(camera_config_repository),
            },
        },
        "legacy_intrinsic_matrix": matrix.tolist(),
        "matches_current_ch4_nominal_intrinsics": bool(np.array_equal(matrix, expected_matrix)),
        "active_point_count": len(points),
        "declared_holdout_count": 0,
        "data_geometry": geometry,
        "reproduced_ch4_fit": fit,
        "legacy_runtime_cameras": runtime,
        "preserved_channel_csvs": csv_reports,
        "active_script_points_match_csv_labels": active_csv_matches,
        "active_script_declared_channel_comment": declared_script_channel,
        "current_ch4": {
            key: configured["ch4"][key]
            for key in ("height_m", "pitch_deg", "yaw_deg", "heading_deg", "intrinsics")
        },
        "ch4_fit_minus_legacy_runtime_deg": {
            "pitch": fit["pitch_deg"] - float(runtime["ch4"]["pitch_deg"]),
            "yaw": fit["yaw_deg"] - float(runtime["ch4"]["yaw_deg"]),
        },
        "acceptance_failures": failures,
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--calibration-script", type=Path, required=True)
    parser.add_argument("--runtime-script", type=Path, required=True)
    parser.add_argument("--camera-config", type=Path, required=True)
    parser.add_argument("--camera-config-repository", type=Path, required=True)
    parser.add_argument("--calibration-csv", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    report = build_report(
        args.calibration_script,
        args.runtime_script,
        args.camera_config,
        args.repository,
        args.camera_config_repository,
        args.calibration_csv,
    )
    write_json_exclusive(args.output, report)
    print(json.dumps({
        "output": str(args.output),
        "repository_commit": report["repository"]["commit"],
        "active_point_count": report["active_point_count"],
        "acceptance_failure_count": len(report["acceptance_failures"]),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
