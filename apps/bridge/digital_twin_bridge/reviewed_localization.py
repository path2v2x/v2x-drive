"""Versioned, fail-closed reviewed vehicle localization contract.

The live producer's GPS and bbox-bottom-centre projection remain diagnostics.
This module validates only independently reviewed, hash-bound CARLA-world actor
placements.  It intentionally has no CARLA or NumPy dependency so the same
contract can be exercised by offline tools and bridge unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Optional

from digital_twin_bridge.twin_camera_rig import (
    absolute_twin_model,
    heading_to_carla_yaw,
    horizontal_fov_deg,
)

try:
    from tools.aggregate_twin_calibration_manifests import (
        SiteManifestError,
        aggregate_site_manifests,
    )
    from tools.build_twin_calibration_manifest import (
        validate_intrinsics_artifact,
        validate_intrinsics_source_images,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script/package fallback
    from aggregate_twin_calibration_manifests import (
        SiteManifestError,
        aggregate_site_manifests,
    )
    from build_twin_calibration_manifest import (
        validate_intrinsics_artifact,
        validate_intrinsics_source_images,
    )


SCHEMA = "v2x-reviewed-vehicle-localization/v1"
TRAJECTORY_SCHEMA = "v2x-reviewed-vehicle-trajectory/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VEHICLE_FAMILIES = {
    "car": "passenger_car",
    "truck": "truck",
    "bus": "bus",
}
AUTHORITY_SCHEMA = "v2x-review-authority-keys/v1"
AUTHORITY_SCHEME = "hmac-sha256-v1"
AUTHORITY_ROLES = frozenset({
    "reviewed_contract",
    "contact_consensus",
    "factor_graph",
    "trajectory_identity",
    "independent_reference",
    "appearance_model",
    "blueprint_catalog",
    "static_calibration",
})
MAX_LOCALIZATION_UNCERTAINTY_M = 2.0
MAX_INDEPENDENT_REFERENCE_ERROR_M = 2.0
MAX_VEHICLE_SPEED_MPS = 45.0
MAX_VEHICLE_ACCELERATION_MPS2 = 8.0
MAX_TRANSIT_SECONDS = 30.0
MIN_APPEARANCE_SIMILARITY = 0.60
MAX_CONTACT_SIGMA_AT_1280_PX = 32.0
MAX_BLUEPRINT_DIMENSION_ERROR_M = 0.25


class ReviewedLocalizationError(ValueError):
    """A stable rejection reason for a reviewed localization contract."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CameraPlacementContext:
    camera_config_sha256: str
    intrinsics_artifact_sha256: str
    intrinsics_report_sha256: str
    native_resolution: tuple[int, int] = (1280, 960)


@dataclass(frozen=True)
class ReviewedPlacementContext:
    map_name: str
    opendrive_sha256: str
    cameras_json_sha256: str
    cameras: Mapping[str, CameraPlacementContext]
    static_calibration_sha256: str
    authority_keys: Mapping[str, bytes]
    authority_roles: Mapping[str, frozenset[str]]


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReviewedLocalizationError("contract_not_canonical_json") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_object_sha256(value: Any) -> str:
    """Match the producer's per-camera canonical object fingerprint."""
    try:
        payload = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReviewedLocalizationError("object_not_canonical_json") from exc
    return sha256_bytes(payload)


def contract_sha256(value: Mapping[str, Any]) -> str:
    unsigned = dict(value)
    unsigned.pop("contract_sha256", None)
    return sha256_bytes(canonical_json_bytes(unsigned))


def _authority_payload(value: Mapping[str, Any]) -> bytes:
    unsigned = dict(value)
    unsigned.pop("contract_sha256", None)
    authority = dict(_object(unsigned.get("authority"), "authority_missing"))
    authority.pop("signature", None)
    unsigned["authority"] = authority
    return canonical_json_bytes(unsigned)


def authority_signature(value: Mapping[str, Any], key: bytes) -> str:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ReviewedLocalizationError("authority_key_invalid")
    return hmac.new(key, _authority_payload(value), hashlib.sha256).hexdigest()


def seal_contract(value: Mapping[str, Any], key_id: str, key: bytes) -> dict:
    sealed = dict(value)
    sealed.pop("contract_sha256", None)
    sealed["authority"] = {
        "scheme": AUTHORITY_SCHEME,
        "key_id": _text(key_id, "authority_key_id_missing"),
    }
    sealed["authority"]["signature"] = authority_signature(sealed, key)
    sealed["contract_sha256"] = contract_sha256(sealed)
    return sealed


def seal_authenticated_artifact(
    value: Mapping[str, Any], key_id: str, key: bytes
) -> dict:
    """Attach the same keyed authority envelope to an upstream artifact."""
    sealed = dict(value)
    sealed["authority"] = {
        "scheme": AUTHORITY_SCHEME,
        "key_id": _text(key_id, "authority_key_id_missing"),
    }
    sealed["authority"]["signature"] = authority_signature(sealed, key)
    return sealed


def placement_key_sha256(global_track_id: str, blueprint_family: str) -> str:
    payload = f"{global_track_id}\0{blueprint_family}".encode("utf-8")
    return sha256_bytes(payload)


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _sha(value: Any, reason: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ReviewedLocalizationError(reason)
    return value


def _object(value: Any, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ReviewedLocalizationError(reason)
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], reason: str) -> None:
    if set(value) != expected:
        raise ReviewedLocalizationError(reason)


def _text(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewedLocalizationError(reason)
    return value


def _vector(value: Any, size: int, reason: str) -> list[float]:
    if (
        not isinstance(value, list)
        or len(value) != size
        or any(not _finite(item) for item in value)
    ):
        raise ReviewedLocalizationError(reason)
    return [float(item) for item in value]


def _matrix(value: Any, size: int, reason: str) -> list[list[float]]:
    if (
        not isinstance(value, list)
        or len(value) != size
        or any(not isinstance(row, list) or len(row) != size for row in value)
        or any(not _finite(item) for row in value for item in row)
    ):
        raise ReviewedLocalizationError(reason)
    matrix = [[float(item) for item in row] for row in value]
    for row in range(size):
        for column in range(size):
            if abs(matrix[row][column] - matrix[column][row]) > 1e-9:
                raise ReviewedLocalizationError(reason)
    if min(_symmetric_eigenvalues(matrix)) < -1e-9:
        raise ReviewedLocalizationError(reason)
    return matrix


def _symmetric_eigenvalues(matrix: list[list[float]]) -> list[float]:
    """Jacobi eigenvalues for a finite 2x2/3x3 symmetric matrix."""
    size = len(matrix)
    work = [row[:] for row in matrix]
    for _ in range(32):
        row, column = max(
            (
                (left, right)
                for left in range(size)
                for right in range(left + 1, size)
            ),
            key=lambda pair: abs(work[pair[0]][pair[1]]),
        )
        if abs(work[row][column]) <= 1e-12:
            break
        angle = 0.5 * math.atan2(
            2.0 * work[row][column],
            work[column][column] - work[row][row],
        )
        cosine, sine = math.cos(angle), math.sin(angle)
        for index in range(size):
            if index in (row, column):
                continue
            left = work[index][row]
            right = work[index][column]
            work[index][row] = work[row][index] = cosine * left - sine * right
            work[index][column] = work[column][index] = sine * left + cosine * right
        diagonal_row = work[row][row]
        diagonal_column = work[column][column]
        cross = work[row][column]
        work[row][row] = (
            cosine * cosine * diagonal_row
            - 2.0 * sine * cosine * cross
            + sine * sine * diagonal_column
        )
        work[column][column] = (
            sine * sine * diagonal_row
            + 2.0 * sine * cosine * cross
            + cosine * cosine * diagonal_column
        )
        work[row][column] = work[column][row] = 0.0
    return [work[index][index] for index in range(size)]


def largest_covariance_eigenvalue(matrix: list[list[float]]) -> float:
    return max(_symmetric_eigenvalues(matrix))


def _utc(value: Any, reason: str) -> tuple[str, float]:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReviewedLocalizationError(reason)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ReviewedLocalizationError(reason) from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ReviewedLocalizationError(reason)
    return value, parsed.timestamp()


def _camera_id_for_detection(detection: Mapping[str, Any]) -> Optional[str]:
    camera_id = detection.get("camera_id")
    if isinstance(camera_id, str) and camera_id:
        return camera_id
    device_id = detection.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        return None
    return device_id.rsplit("-", 1)[-1]


def _nested(value: Mapping[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _read_hashed_json(path_value: Any, expected_hash: Any, base: Path, reason: str):
    expected = _sha(expected_hash, f"{reason}_hash_invalid")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ReviewedLocalizationError(f"{reason}_path_missing")
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = base / path
    try:
        raw = path.resolve(strict=True).read_bytes()
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewedLocalizationError(f"{reason}_unavailable") from exc
    if sha256_bytes(raw) != expected or not isinstance(parsed, dict):
        raise ReviewedLocalizationError(f"{reason}_mismatch")
    return raw, parsed


def validate_measured_intrinsics(
    camera: Mapping[str, Any], base: Path
) -> tuple[str, str]:
    calibration = _object(
        camera.get("intrinsics_calibration"), "active_intrinsics_missing"
    )
    artifact_raw, artifact = _read_hashed_json(
        calibration.get("artifact_path"),
        calibration.get("artifact_sha256"),
        base,
        "active_intrinsics_artifact",
    )
    report_raw, report = _read_hashed_json(
        calibration.get("report_path"),
        calibration.get("report_sha256"),
        base,
        "active_intrinsics_report",
    )
    method = calibration.get("method")
    expected_report_schema = {
        "checkerboard": "v2x-checkerboard-calibration-report/v1",
        "charuco": "v2x-charuco-calibration-report/v1",
    }.get(method)
    accepted = report.get("accepted")
    holdouts = report.get("holdouts")
    metrics = report.get("holdout_metrics")
    intrinsics = _object(camera.get("intrinsics"), "active_intrinsics_model_missing")
    expected_resolution = [intrinsics.get("width"), intrinsics.get("height")]
    matrix = calibration.get("camera_matrix")
    distortion = calibration.get("distortion")
    normalized = {
        key: calibration.get(key)
        for key in (
            "method", "image_count", "source_images_sha256",
            "rms_reprojection_error_px", "resolution", "camera_matrix",
            "distortion",
        )
    }
    try:
        fit_rms = float(calibration["rms_reprojection_error_px"])
        holdout_rmse = float(metrics["rmse_px"])
        holdout_max = float(metrics["max_error_px"])
        matrix_values = [float(item) for row in matrix for item in row]
        distortion_values = [
            float(distortion[key]) for key in ("k1", "k2", "p1", "p2", "k3")
        ]
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ReviewedLocalizationError("active_intrinsics_metrics_invalid") from exc
    source_hashes = calibration.get("source_images_sha256")
    source_image_values = calibration.get("source_image_paths")
    if not isinstance(source_image_values, list):
        raise ReviewedLocalizationError("active_intrinsics_sources_missing")
    source_image_paths = []
    for path_value in source_image_values:
        if not isinstance(path_value, str) or not path_value.strip():
            raise ReviewedLocalizationError("active_intrinsics_sources_missing")
        source_path = Path(path_value).expanduser()
        if not source_path.is_absolute():
            source_path = base / source_path
        try:
            source_image_paths.append(source_path.resolve(strict=True))
        except OSError as exc:
            raise ReviewedLocalizationError(
                "active_intrinsics_source_unavailable"
            ) from exc
    report_items = (
        (accepted if isinstance(accepted, list) else [])
        + (holdouts if isinstance(holdouts, list) else [])
    )
    report_hash_list = [
        item.get("sha256") if isinstance(item, dict) else None
        for item in report_items
    ]
    report_hashes = {
        value for value in report_hash_list if isinstance(value, str)
    }
    source_hash_set = {
        value for value in source_hashes if isinstance(value, str)
    } if isinstance(source_hashes, list) else set()
    width, height = expected_resolution
    intrinsic_values = [
        intrinsics.get(key) for key in ("fx", "fy", "cx", "cy")
    ]
    if (
        artifact != normalized
        or expected_report_schema is None
        or report.get("schema") != expected_report_schema
        or not isinstance(accepted, list) or len(accepted) < 10
        or not isinstance(holdouts, list) or len(holdouts) < 2
        or not isinstance(source_hashes, list)
        or len(source_hashes) < 12
        or len(source_image_paths) != len(source_hashes)
        or len(source_hashes) != len(source_hash_set)
        or any(
            not isinstance(value, str) or SHA256_RE.fullmatch(value) is None
            for value in source_hashes
        )
        or len(report_hash_list) != len(source_hashes)
        or len(report_hash_list) != len(report_hashes)
        or any(
            not isinstance(value, str) or SHA256_RE.fullmatch(value) is None
            for value in report_hash_list
        )
        or source_hash_set != report_hashes
        or calibration.get("image_count") != len(source_hashes)
        or not isinstance(width, int) or isinstance(width, bool) or width <= 0
        or not isinstance(height, int) or isinstance(height, bool) or height <= 0
        or not all(_finite(value) for value in intrinsic_values)
        or float(intrinsics.get("fx")) <= 0.0
        or float(intrinsics.get("fy")) <= 0.0
        or not 0.0 <= float(intrinsics.get("cx")) < width
        or not 0.0 <= float(intrinsics.get("cy")) < height
        or calibration.get("resolution") != expected_resolution
        or not all(_finite(value) for value in matrix_values + distortion_values)
        or not all(_finite(value) for value in (fit_rms, holdout_rmse, holdout_max))
        or not 0.0 <= fit_rms <= 2.0
        or not 0.0 <= holdout_rmse <= 2.0
        or not 0.0 <= holdout_max <= 5.0
        or matrix != [
            [intrinsics.get("fx"), 0, intrinsics.get("cx")],
            [0, intrinsics.get("fy"), intrinsics.get("cy")],
            [0, 0, 1],
        ]
    ):
        raise ReviewedLocalizationError("active_intrinsics_gate_failed")
    try:
        validate_intrinsics_artifact(camera, artifact_raw)
        verified_source_hashes = validate_intrinsics_source_images(
            camera, source_image_paths
        )
    except (OSError, ValueError) as exc:
        raise ReviewedLocalizationError("active_intrinsics_gate_failed") from exc
    if verified_source_hashes != sorted(source_hash_set):
        raise ReviewedLocalizationError("active_intrinsics_gate_failed")
    return sha256_bytes(artifact_raw), sha256_bytes(report_raw)


def load_authority_registry(path_value: str) -> Mapping[str, dict]:
    try:
        path = Path(path_value).expanduser().resolve(strict=True)
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewedLocalizationError("authority_key_file_unavailable") from exc
    keys = value.get("keys") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema") != AUTHORITY_SCHEMA
        or not isinstance(keys, dict)
        or not keys
    ):
        raise ReviewedLocalizationError("authority_key_file_invalid")
    output: dict[str, dict] = {}
    for key_id, entry in keys.items():
        if (
            not isinstance(key_id, str)
            or not key_id
            or not isinstance(entry, dict)
            or set(entry) != {"key_hex", "roles"}
            or not isinstance(entry.get("key_hex"), str)
            or not isinstance(entry.get("roles"), list)
            or not entry["roles"]
            or any(
                not isinstance(role, str) or role not in AUTHORITY_ROLES
                for role in entry["roles"]
            )
            or len(entry["roles"]) != len(set(entry["roles"]))
        ):
            raise ReviewedLocalizationError("authority_key_file_invalid")
        try:
            key = bytes.fromhex(entry["key_hex"])
        except ValueError as exc:
            raise ReviewedLocalizationError("authority_key_file_invalid") from exc
        if len(key) < 32:
            raise ReviewedLocalizationError("authority_key_file_invalid")
        output[key_id] = {
            "key": key,
            "roles": frozenset(entry["roles"]),
        }
    return output


def load_authority_keys(path_value: str) -> Mapping[str, bytes]:
    return {
        key_id: entry["key"]
        for key_id, entry in load_authority_registry(path_value).items()
    }


def verify_authenticated_artifact(
    value: Mapping[str, Any],
    expected_schema: str,
    expected_role: str,
    registry: Mapping[str, dict],
) -> str:
    if not isinstance(value, dict) or value.get("schema") != expected_schema:
        raise ReviewedLocalizationError("artifact_schema_not_allowlisted")
    authority = _object(value.get("authority"), "artifact_authority_missing")
    _exact_keys(
        authority,
        {"scheme", "key_id", "signature"},
        "artifact_authority_fields_invalid",
    )
    if authority.get("scheme") != AUTHORITY_SCHEME:
        raise ReviewedLocalizationError("artifact_authority_scheme_invalid")
    key_id = _text(authority.get("key_id"), "artifact_authority_key_missing")
    entry = registry.get(key_id)
    signature = _sha(
        authority.get("signature"), "artifact_authority_signature_invalid"
    )
    if (
        entry is None
        or expected_role not in entry["roles"]
        or not hmac.compare_digest(
            signature, authority_signature(value, entry["key"])
        )
    ):
        raise ReviewedLocalizationError("artifact_authority_mismatch")
    return key_id


def _artifact_descriptor(
    value: Any, expected_schema: str, reason: str
) -> tuple[str, str]:
    descriptor = _object(value, reason)
    _exact_keys(descriptor, {"path", "sha256", "schema"}, reason)
    if descriptor.get("schema") != expected_schema:
        raise ReviewedLocalizationError(reason)
    return (
        _text(descriptor.get("path"), reason),
        _sha(descriptor.get("sha256"), reason),
    )


def _pixel(value: Any, width: int, height: int, reason: str) -> list[float]:
    pixel = _vector(value, 2, reason)
    if not (0.0 <= pixel[0] < width and 0.0 <= pixel[1] < height):
        raise ReviewedLocalizationError(reason)
    return pixel


def _project_surveyed_world_pixel(
    world_value: Any,
    manifest: Mapping[str, Any],
    camera: Mapping[str, Any],
) -> list[float]:
    """Project one retained CARLA-world point through the exact camera model."""
    world = _vector(world_value, 3, "static_reprojection_world_invalid")
    baseline = _object(
        manifest.get("baseline"), "static_reprojection_extrinsics_invalid"
    )
    deployment = _object(
        manifest.get("deployment_model"),
        "static_reprojection_extrinsics_invalid",
    )
    deployment_base = _object(
        deployment.get("base"), "static_reprojection_extrinsics_invalid"
    )
    location = _vector(
        baseline.get("location"), 3, "static_reprojection_extrinsics_invalid"
    )
    rotation = [
        baseline.get("pitch_deg"),
        baseline.get("yaw_deg"),
        baseline.get("roll_deg"),
    ]
    if any(not _finite(value) for value in rotation):
        raise ReviewedLocalizationError("static_reprojection_extrinsics_invalid")
    try:
        expected_base = {
            "pitch_deg": float(camera["pitch_deg"]),
            "yaw_deg": heading_to_carla_yaw(
                float(camera["heading_deg"]), float(camera["yaw_deg"])
            ),
            "roll_deg": float(camera.get("roll_deg", 0.0)),
            "fov_deg": horizontal_fov_deg(camera["intrinsics"]),
        }
        deployed = absolute_twin_model(
            deployment.get("anchor_location"),
            deployment_base,
            camera.get("twin_pose") or {},
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ReviewedLocalizationError(
            "static_reprojection_extrinsics_invalid"
        ) from exc
    deployed_location = deployed.get("location")
    deployed_rotation = [
        deployed.get("pitch_deg"),
        deployed.get("yaw_deg"),
        deployed.get("roll_deg"),
    ]
    deployed_fov = deployed.get("fov_deg")
    yaw_error = abs(
        (float(deployed_rotation[1]) - float(rotation[1]) + 180.0)
        % 360.0
        - 180.0
    ) if all(_finite(value) for value in deployed_rotation) else math.inf
    base_yaw_error = abs(
        (
            float(deployment_base.get("yaw_deg"))
            - expected_base["yaw_deg"]
            + 180.0
        )
        % 360.0
        - 180.0
    ) if _finite(deployment_base.get("yaw_deg")) else math.inf
    if (
        not isinstance(deployed_location, list)
        or len(deployed_location) != 3
        or any(not _finite(value) for value in deployed_location)
        or any(
            abs(float(deployed_location[index]) - location[index]) > 1e-6
            for index in range(3)
        )
        or abs(float(deployed_rotation[0]) - float(rotation[0])) > 1e-6
        or yaw_error > 1e-6
        or abs(float(deployed_rotation[2]) - float(rotation[2])) > 1e-6
        or not _finite(deployed_fov)
        or not _finite(baseline.get("fov_deg"))
        or abs(float(deployed_fov) - float(baseline["fov_deg"])) > 1e-6
        or not _finite(deployment_base.get("pitch_deg"))
        or abs(
            float(deployment_base["pitch_deg"])
            - expected_base["pitch_deg"]
        ) > 1e-6
        or base_yaw_error > 1e-6
        or not _finite(deployment_base.get("roll_deg"))
        or abs(
            float(deployment_base["roll_deg"])
            - expected_base["roll_deg"]
        ) > 1e-6
        or not _finite(deployment_base.get("fov_deg"))
        or abs(
            float(deployment_base["fov_deg"])
            - expected_base["fov_deg"]
        ) > 1e-6
    ):
        raise ReviewedLocalizationError("static_reprojection_extrinsics_invalid")
    intrinsics = _object(
        camera.get("intrinsics"), "static_reprojection_intrinsics_invalid"
    )
    calibration = _object(
        camera.get("intrinsics_calibration"),
        "static_reprojection_intrinsics_invalid",
    )
    distortion = _object(
        calibration.get("distortion"), "static_reprojection_intrinsics_invalid"
    )
    values = {
        key: intrinsics.get(key) for key in ("fx", "fy", "cx", "cy")
    }
    values.update({
        key: distortion.get(key) for key in ("k1", "k2", "p1", "p2", "k3")
    })
    if any(not _finite(value) for value in values.values()):
        raise ReviewedLocalizationError("static_reprojection_intrinsics_invalid")
    if float(values["fx"]) <= 0.0 or float(values["fy"]) <= 0.0:
        raise ReviewedLocalizationError("static_reprojection_intrinsics_invalid")

    pitch, yaw, roll = (math.radians(float(value)) for value in rotation)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    forward = (cp * cy, cp * sy, sp)
    zero_roll_right = (-sy, cy, 0.0)
    zero_roll_up = (-sp * cy, -sp * sy, cp)
    right = tuple(
        cr * zero_roll_right[index] - sr * zero_roll_up[index]
        for index in range(3)
    )
    up = tuple(
        sr * zero_roll_right[index] + cr * zero_roll_up[index]
        for index in range(3)
    )
    relative = [world[index] - location[index] for index in range(3)]
    camera_forward = sum(relative[index] * forward[index] for index in range(3))
    camera_right = sum(relative[index] * right[index] for index in range(3))
    camera_up = sum(relative[index] * up[index] for index in range(3))
    if not math.isfinite(camera_forward) or camera_forward <= 1e-6:
        raise ReviewedLocalizationError("static_reprojection_world_behind_camera")
    normalized_x = camera_right / camera_forward
    normalized_y = -camera_up / camera_forward
    radius2 = normalized_x * normalized_x + normalized_y * normalized_y
    radial = (
        1.0
        + float(values["k1"]) * radius2
        + float(values["k2"]) * radius2 * radius2
        + float(values["k3"]) * radius2 * radius2 * radius2
    )
    distorted_x = (
        normalized_x * radial
        + 2.0 * float(values["p1"]) * normalized_x * normalized_y
        + float(values["p2"]) * (radius2 + 2.0 * normalized_x * normalized_x)
    )
    distorted_y = (
        normalized_y * radial
        + float(values["p1"]) * (radius2 + 2.0 * normalized_y * normalized_y)
        + 2.0 * float(values["p2"]) * normalized_x * normalized_y
    )
    pixel = [
        float(values["fx"]) * distorted_x + float(values["cx"]),
        float(values["fy"]) * distorted_y + float(values["cy"]),
    ]
    if any(not math.isfinite(value) for value in pixel):
        raise ReviewedLocalizationError("static_reprojection_projection_invalid")
    return pixel


def _heldout_reprojection_metrics(
    evidence: Mapping[str, Any],
    manifest: Mapping[str, Any],
    camera_id: str,
    camera_hash: str,
    manifest_hash: str,
    camera: Mapping[str, Any],
) -> None:
    _exact_keys(
        evidence,
        {
            "schema", "camera_id", "camera_config_sha256",
            "camera_manifest_sha256", "native_resolution", "points",
            "roads", "authority",
        },
        "static_reprojection_fields_invalid",
    )
    width = manifest.get("width")
    height = manifest.get("height")
    if (
        evidence.get("schema") != "v2x-camera-heldout-reprojection/v1"
        or evidence.get("camera_id") != camera_id
        or evidence.get("camera_config_sha256") != camera_hash
        or evidence.get("camera_manifest_sha256") != manifest_hash
        or not isinstance(width, int)
        or isinstance(width, bool)
        or width <= 0
        or not isinstance(height, int)
        or isinstance(height, bool)
        or height <= 0
        or evidence.get("native_resolution") != [width, height]
        or canonical_object_sha256(camera) != camera_hash
    ):
        raise ReviewedLocalizationError("static_reprojection_binding_invalid")
    camera_intrinsics = _object(
        camera.get("intrinsics"), "static_reprojection_intrinsics_invalid"
    )
    camera_calibration = _object(
        camera.get("intrinsics_calibration"),
        "static_reprojection_intrinsics_invalid",
    )
    manifest_calibration = _object(
        manifest.get("intrinsics_calibration"),
        "static_reprojection_intrinsics_invalid",
    )
    expected_matrix = [
        [camera_intrinsics.get("fx"), 0.0, camera_intrinsics.get("cx")],
        [0.0, camera_intrinsics.get("fy"), camera_intrinsics.get("cy")],
        [0.0, 0.0, 1.0],
    ]
    if (
        manifest_calibration.get("resolution") != [width, height]
        or manifest_calibration.get("camera_matrix") != expected_matrix
        or manifest_calibration.get("distortion")
        != camera_calibration.get("distortion")
    ):
        raise ReviewedLocalizationError("static_reprojection_intrinsics_invalid")
    features = manifest.get("features")
    if not isinstance(features, list):
        raise ReviewedLocalizationError("static_reprojection_manifest_invalid")
    holdout_points = {
        item.get("id"): item
        for item in features
        if isinstance(item, dict)
        and item.get("split") == "holdout"
        and item.get("type") == "point"
    }
    holdout_roads = {
        item.get("id"): item
        for item in features
        if isinstance(item, dict)
        and item.get("split") == "holdout"
        and item.get("type") == "polyline"
    }
    points = evidence.get("points")
    roads = evidence.get("roads")
    if (
        len(holdout_points) < 4
        or len(holdout_roads) < 2
        or not isinstance(points, list)
        or not isinstance(roads, list)
    ):
        raise ReviewedLocalizationError("static_reprojection_denominator_invalid")
    point_residuals: list[float] = []
    seen_points = set()
    for item in points:
        _exact_keys(
            _object(item, "static_reprojection_point_invalid"),
            {"feature_id", "observed_pixel"},
            "static_reprojection_point_invalid",
        )
        feature_id = _text(
            item.get("feature_id"), "static_reprojection_point_invalid"
        )
        feature = holdout_points.get(feature_id)
        if feature is None or feature_id in seen_points:
            raise ReviewedLocalizationError("static_reprojection_point_invalid")
        seen_points.add(feature_id)
        observed = _pixel(
            item.get("observed_pixel"), width, height,
            "static_reprojection_point_invalid",
        )
        predicted = _project_surveyed_world_pixel(
            feature.get("surveyed_world"), manifest, camera
        )
        if observed != [float(value) for value in feature.get("image", [])]:
            raise ReviewedLocalizationError(
                "static_reprojection_observation_substituted"
            )
        point_residuals.append(math.dist(observed, predicted))
    road_residuals: list[float] = []
    seen_roads = set()
    for item in roads:
        _exact_keys(
            _object(item, "static_reprojection_road_invalid"),
            {"feature_id", "observed_polyline"},
            "static_reprojection_road_invalid",
        )
        feature_id = _text(
            item.get("feature_id"), "static_reprojection_road_invalid"
        )
        feature = holdout_roads.get(feature_id)
        observed_values = item.get("observed_polyline")
        feature_values = feature.get("image_polyline") if feature else None
        world_values = feature.get("world") if feature else None
        if (
            feature is None
            or feature_id in seen_roads
            or not isinstance(observed_values, list)
            or not isinstance(world_values, list)
            or len(observed_values) < 2
            or len(observed_values) != len(world_values)
            or observed_values != feature_values
        ):
            raise ReviewedLocalizationError("static_reprojection_road_invalid")
        seen_roads.add(feature_id)
        for observed_value, world_value in zip(observed_values, world_values):
            observed = _pixel(
                observed_value, width, height,
                "static_reprojection_road_invalid",
            )
            predicted = _project_surveyed_world_pixel(
                world_value, manifest, camera
            )
            road_residuals.append(math.dist(observed, predicted))
    if seen_points != set(holdout_points) or seen_roads != set(holdout_roads):
        raise ReviewedLocalizationError("static_reprojection_denominator_invalid")
    point_rmse = math.sqrt(
        sum(value * value for value in point_residuals) / len(point_residuals)
    )
    ordered = sorted(point_residuals)
    point_p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    point_max = max(point_residuals)
    road_rmse = math.sqrt(
        sum(value * value for value in road_residuals) / len(road_residuals)
    )
    road_max = max(road_residuals)
    scale = width / 1280.0
    if (
        point_rmse > 10.0 * scale
        or point_p95 > 16.0 * scale
        or point_max > 24.0 * scale
        or road_rmse > 6.0 * scale
        or road_max > 12.0 * scale
    ):
        raise ReviewedLocalizationError("static_reprojection_threshold_failed")


def validate_static_calibration(
    path_value: str,
    cameras_json_sha256: str,
    camera_hashes: Mapping[str, str],
    camera_resolutions: Mapping[str, tuple[int, int]],
    camera_models: Mapping[str, Mapping[str, Any]],
    map_name: str,
    opendrive_sha256: str,
    authority_registry: Mapping[str, dict],
) -> str:
    path = Path(path_value).expanduser()
    try:
        path = path.resolve(strict=True)
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewedLocalizationError("static_calibration_unavailable") from exc
    if not isinstance(value, dict):
        raise ReviewedLocalizationError("static_calibration_gate_failed")
    try:
        verify_authenticated_artifact(
            value,
            "v2x-static-camera-survey-manifest/v1",
            "static_calibration",
            authority_registry,
        )
    except ReviewedLocalizationError as exc:
        raise ReviewedLocalizationError("static_calibration_gate_failed") from exc
    _exact_keys(
        value,
        {
            "schema", "source_cameras_json_sha256", "map",
            "site_aggregation", "heldout_reprojection", "authority",
        },
        "static_calibration_gate_failed",
    )
    aggregation_path_value, aggregation_hash = _artifact_descriptor(
        value.get("site_aggregation"),
        "v2x-site-calibration-aggregation/v1",
        "static_calibration_aggregation_invalid",
    )
    aggregation_raw, aggregation = _read_hashed_json(
        aggregation_path_value,
        aggregation_hash,
        path.parent,
        "static_calibration_aggregation",
    )
    manifest_index = aggregation.get("manifests")
    registry_descriptor = aggregation.get("site_landmark_registry")
    reprojection = value.get("heldout_reprojection")
    if (
        value.get("source_cameras_json_sha256") != cameras_json_sha256
        or value.get("map") != {
            "name": map_name,
            "opendrive_sha256": opendrive_sha256,
        }
        or set(camera_hashes) != {"ch1", "ch2", "ch3", "ch4"}
        or set(camera_resolutions) != set(camera_hashes)
        or set(camera_models) != set(camera_hashes)
        or not isinstance(manifest_index, dict)
        or set(manifest_index) != set(camera_hashes)
        or not isinstance(registry_descriptor, dict)
        or registry_descriptor.get("cameras_file_sha256")
        != cameras_json_sha256
        or not isinstance(reprojection, dict)
        or set(reprojection) != set(camera_hashes)
    ):
        raise ReviewedLocalizationError("static_calibration_gate_failed")
    registry_path_value = registry_descriptor.get("path")
    manifest_paths = []
    for camera_id in sorted(camera_hashes):
        manifest_item = manifest_index[camera_id]
        if not isinstance(manifest_item, dict):
            raise ReviewedLocalizationError("static_calibration_gate_failed")
        manifest_paths.append(manifest_item.get("path"))
    try:
        recomputed = aggregate_site_manifests(
            registry_path_value, manifest_paths
        )
    except (OSError, SiteManifestError, ValueError) as exc:
        raise ReviewedLocalizationError(
            "static_calibration_raw_replay_failed"
        ) from exc
    if recomputed != aggregation:
        raise ReviewedLocalizationError("static_calibration_stale_summary")
    aggregate_map = aggregation.get("map_identity")
    if (
        not isinstance(aggregate_map, dict)
        or aggregate_map.get("ue5_map") != map_name
        or aggregate_map.get("ue5_map_opendrive_sha256") != opendrive_sha256
    ):
        raise ReviewedLocalizationError("static_calibration_gate_failed")
    for camera_id, expected_hash in camera_hashes.items():
        manifest_item = manifest_index[camera_id]
        manifest_raw, manifest = _read_hashed_json(
            manifest_item.get("path"),
            manifest_item.get("sha256"),
            path.parent,
            "static_camera_manifest",
        )
        evidence_path_value, evidence_hash = _artifact_descriptor(
            reprojection[camera_id],
            "v2x-camera-heldout-reprojection/v1",
            "static_reprojection_descriptor_invalid",
        )
        _evidence_raw, evidence = _read_hashed_json(
            evidence_path_value,
            evidence_hash,
            path.parent,
            "static_reprojection_evidence",
        )
        try:
            verify_authenticated_artifact(
                evidence,
                "v2x-camera-heldout-reprojection/v1",
                "static_calibration",
                authority_registry,
            )
        except ReviewedLocalizationError as exc:
            raise ReviewedLocalizationError(
                "static_reprojection_authentication_failed"
            ) from exc
        if (
            manifest.get("camera_config_sha256") != expected_hash
            or manifest.get("cameras_file_sha256") != cameras_json_sha256
            or manifest.get("ue5_map") != map_name
            or manifest.get("ue5_map_opendrive_sha256") != opendrive_sha256
            or [manifest.get("width"), manifest.get("height")]
            != list(camera_resolutions[camera_id])
        ):
            raise ReviewedLocalizationError("static_calibration_gate_failed")
        _heldout_reprojection_metrics(
            evidence,
            manifest,
            camera_id,
            expected_hash,
            sha256_bytes(manifest_raw),
            camera_models[camera_id],
        )
    return sha256_bytes(raw)


def build_runtime_context(
    carla_map,
    cameras_json_path: str,
    static_calibration_path: str,
    authority_key_file: str,
) -> ReviewedPlacementContext:
    """Freeze the exact active map and camera inputs for strict validation."""

    path = Path(cameras_json_path).expanduser()
    try:
        path = path.resolve(strict=True)
        cameras_raw = path.read_bytes()
        config = json.loads(cameras_raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewedLocalizationError("active_camera_config_unavailable") from exc
    cameras = config.get("cameras") if isinstance(config, dict) else None
    if not isinstance(cameras, list) or not cameras:
        raise ReviewedLocalizationError("active_camera_config_invalid")
    indexed: dict[str, CameraPlacementContext] = {}
    camera_hashes = {}
    camera_models = {}
    for camera in cameras:
        if not isinstance(camera, dict):
            raise ReviewedLocalizationError("active_camera_config_invalid")
        camera_id = _text(camera.get("id"), "active_camera_id_invalid")
        if camera_id in indexed:
            raise ReviewedLocalizationError("active_camera_id_duplicate")
        camera_hash = canonical_object_sha256(camera)
        artifact_hash, report_hash = validate_measured_intrinsics(
            camera, path.parent
        )
        indexed[camera_id] = CameraPlacementContext(
            camera_config_sha256=camera_hash,
            intrinsics_artifact_sha256=artifact_hash,
            intrinsics_report_sha256=report_hash,
            native_resolution=(
                int(camera["intrinsics"]["width"]),
                int(camera["intrinsics"]["height"]),
            ),
        )
        camera_hashes[camera_id] = camera_hash
        camera_models[camera_id] = camera
    try:
        opendrive = carla_map.to_opendrive()
    except Exception as exc:
        raise ReviewedLocalizationError("active_opendrive_unavailable") from exc
    if not isinstance(opendrive, str) or not opendrive:
        raise ReviewedLocalizationError("active_opendrive_unavailable")
    cameras_hash = sha256_bytes(cameras_raw)
    opendrive_hash = sha256_bytes(opendrive.encode("utf-8"))
    map_name = _text(getattr(carla_map, "name", None), "active_map_name_invalid")
    authority_registry = load_authority_registry(authority_key_file)
    static_hash = validate_static_calibration(
        static_calibration_path,
        cameras_hash,
        camera_hashes,
        {
            camera_id: placement.native_resolution
            for camera_id, placement in indexed.items()
        },
        camera_models,
        map_name,
        opendrive_hash,
        authority_registry,
    )
    return ReviewedPlacementContext(
        map_name=map_name,
        opendrive_sha256=opendrive_hash,
        cameras_json_sha256=cameras_hash,
        cameras=indexed,
        static_calibration_sha256=static_hash,
        authority_keys={
            key_id: entry["key"]
            for key_id, entry in authority_registry.items()
        },
        authority_roles={
            key_id: entry["roles"]
            for key_id, entry in authority_registry.items()
        },
    )


def validate_contract(
    contract: Any,
    detection: Mapping[str, Any],
    context: ReviewedPlacementContext,
) -> dict:
    """Validate and normalize one embedded reviewed localization sample."""

    value = _object(contract, "reviewed_localization_missing")
    _exact_keys(value, {
        "schema", "event_id", "camera_id", "global_track_id",
        "trajectory_id", "sample_index", "source", "contact", "timing",
        "review", "identity", "placement", "authority", "contract_sha256",
    }, "contract_fields_invalid")
    if value.get("schema") != SCHEMA:
        raise ReviewedLocalizationError("reviewed_localization_schema")
    supplied_hash = _sha(value.get("contract_sha256"), "contract_hash_missing")
    if supplied_hash != contract_sha256(value):
        raise ReviewedLocalizationError("contract_hash_mismatch")
    authority = _object(value.get("authority"), "authority_missing")
    _exact_keys(
        authority,
        {"scheme", "key_id", "signature"},
        "authority_fields_invalid",
    )
    if authority.get("scheme") != AUTHORITY_SCHEME:
        raise ReviewedLocalizationError("authority_scheme_invalid")
    authority_key_id = _text(
        authority.get("key_id"), "authority_key_id_missing"
    )
    authority_key = context.authority_keys.get(authority_key_id)
    supplied_signature = _sha(
        authority.get("signature"), "authority_signature_invalid"
    )
    if (
        authority_key is None
        or "reviewed_contract" not in context.authority_roles.get(
            authority_key_id, frozenset()
        )
        or not hmac.compare_digest(
        supplied_signature, authority_signature(value, authority_key)
        )
    ):
        raise ReviewedLocalizationError("authority_signature_mismatch")

    event_id = _text(value.get("event_id"), "contract_event_id_missing")
    if event_id != detection.get("event_id"):
        raise ReviewedLocalizationError("contract_event_id_mismatch")
    camera_id = _text(value.get("camera_id"), "contract_camera_id_missing")
    if camera_id != _camera_id_for_detection(detection):
        raise ReviewedLocalizationError("contract_camera_id_mismatch")
    camera_context = context.cameras.get(camera_id)
    if camera_context is None:
        raise ReviewedLocalizationError("contract_camera_not_active")

    global_track_id = _text(
        value.get("global_track_id"), "contract_global_track_id_missing"
    )
    if global_track_id != detection.get("object_id"):
        raise ReviewedLocalizationError("contract_global_track_id_mismatch")
    trajectory_id = _text(value.get("trajectory_id"), "trajectory_id_missing")
    sample_index = value.get("sample_index")
    if not isinstance(sample_index, int) or isinstance(sample_index, bool) or sample_index < 0:
        raise ReviewedLocalizationError("trajectory_sample_index_invalid")

    source = _object(value.get("source"), "contract_source_missing")
    _exact_keys(source, {"frame", "detector", "camera", "map"}, "source_fields_invalid")
    frame = _object(source.get("frame"), "native_frame_binding_missing")
    _exact_keys(
        frame,
        {
            "source_kind", "sha256", "mask_sha256", "native_resolution",
            "frame_number", "inference_manifest_sha256",
            "frame_pixel_sha256", "mask_pixel_sha256",
            "detector_output_sha256",
        },
        "native_frame_fields_invalid",
    )
    if frame.get("source_kind") != "persisted_native_frame_and_instance_mask":
        raise ReviewedLocalizationError("native_frame_source_invalid")
    frame_sha256 = _sha(frame.get("sha256"), "native_frame_hash_invalid")
    mask_sha256 = _sha(frame.get("mask_sha256"), "native_mask_hash_invalid")
    inference_manifest_sha256 = _sha(
        frame.get("inference_manifest_sha256"),
        "inference_manifest_hash_invalid",
    )
    frame_pixel_sha256 = _sha(
        frame.get("frame_pixel_sha256"), "native_frame_pixel_hash_invalid"
    )
    mask_pixel_sha256 = _sha(
        frame.get("mask_pixel_sha256"), "native_mask_pixel_hash_invalid"
    )
    detector_output_sha256 = _sha(
        frame.get("detector_output_sha256"), "detector_output_hash_invalid"
    )
    emitted_inference = _nested(
        detection, "raw_observation", "inference_evidence"
    )
    if (
        not isinstance(emitted_inference, dict)
        or emitted_inference.get("schema")
        != "v2x-persisted-inference-evidence/v1"
        or emitted_inference.get("acceptance_eligible") is not True
        or emitted_inference.get("reason") is not None
        or inference_manifest_sha256
        != emitted_inference.get("manifest_sha256")
        or frame_pixel_sha256 != emitted_inference.get("frame_pixel_sha256")
        or mask_pixel_sha256 != emitted_inference.get("mask_pixel_sha256")
        or detector_output_sha256
        != emitted_inference.get("detector_output_sha256")
    ):
        raise ReviewedLocalizationError("producer_inference_binding_mismatch")
    resolution = frame.get("native_resolution")
    if (
        not isinstance(resolution, list)
        or len(resolution) != 2
        or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in resolution)
    ):
        raise ReviewedLocalizationError("native_frame_resolution_invalid")
    emitted_resolution = _nested(detection, "raw_observation", "native_resolution")
    if emitted_resolution != resolution:
        raise ReviewedLocalizationError("native_frame_resolution_mismatch")
    if resolution != list(camera_context.native_resolution):
        raise ReviewedLocalizationError("active_camera_resolution_mismatch")
    frame_number = frame.get("frame_number")
    if not isinstance(frame_number, int) or isinstance(frame_number, bool) or frame_number < 0:
        raise ReviewedLocalizationError("native_frame_number_invalid")
    if frame_number != _nested(detection, "camera_data", "bifocal_metadata", "frame"):
        raise ReviewedLocalizationError("native_frame_number_mismatch")

    detector = _object(source.get("detector"), "detector_binding_missing")
    _exact_keys(detector, {"model_sha256", "config_sha256"}, "detector_fields_invalid")
    detector_model_sha256 = _sha(
        detector.get("model_sha256"), "detector_model_hash_invalid"
    )
    detector_config_sha256 = _sha(
        detector.get("config_sha256"), "detector_config_hash_invalid"
    )
    emitted_fingerprints = _nested(detection, "raw_observation", "fingerprints")
    if not isinstance(emitted_fingerprints, dict):
        raise ReviewedLocalizationError("emitted_fingerprints_missing")
    if detector_model_sha256 != emitted_fingerprints.get("detector_model_sha256"):
        raise ReviewedLocalizationError("detector_model_hash_mismatch")
    if detector_config_sha256 != emitted_fingerprints.get("detector_config_sha256"):
        raise ReviewedLocalizationError("detector_config_hash_mismatch")

    camera = _object(source.get("camera"), "camera_binding_missing")
    _exact_keys(camera, {"cameras_json_sha256", "camera_config_sha256", "intrinsics_artifact_sha256", "intrinsics_report_sha256", "static_calibration_sha256"}, "camera_fields_invalid")
    cameras_json_sha256 = _sha(
        camera.get("cameras_json_sha256"), "cameras_json_hash_invalid"
    )
    camera_config_sha256 = _sha(
        camera.get("camera_config_sha256"), "camera_config_hash_invalid"
    )
    intrinsics_artifact_sha256 = _sha(
        camera.get("intrinsics_artifact_sha256"),
        "intrinsics_artifact_hash_invalid",
    )
    intrinsics_report_sha256 = _sha(
        camera.get("intrinsics_report_sha256"),
        "intrinsics_report_hash_invalid",
    )
    static_calibration_sha256 = _sha(
        camera.get("static_calibration_sha256"),
        "static_calibration_hash_invalid",
    )
    if cameras_json_sha256 != context.cameras_json_sha256:
        raise ReviewedLocalizationError("active_cameras_json_mismatch")
    if camera_config_sha256 != camera_context.camera_config_sha256:
        raise ReviewedLocalizationError("active_camera_config_mismatch")
    if intrinsics_artifact_sha256 != camera_context.intrinsics_artifact_sha256:
        raise ReviewedLocalizationError("active_intrinsics_mismatch")
    if intrinsics_report_sha256 != camera_context.intrinsics_report_sha256:
        raise ReviewedLocalizationError("active_intrinsics_report_mismatch")
    if static_calibration_sha256 != context.static_calibration_sha256:
        raise ReviewedLocalizationError("active_static_calibration_mismatch")
    if cameras_json_sha256 != emitted_fingerprints.get("cameras_json_sha256"):
        raise ReviewedLocalizationError("emitted_cameras_json_mismatch")
    if camera_config_sha256 != emitted_fingerprints.get("camera_config_sha256"):
        raise ReviewedLocalizationError("emitted_camera_config_mismatch")

    map_binding = _object(source.get("map"), "map_binding_missing")
    _exact_keys(map_binding, {"name", "opendrive_sha256"}, "map_fields_invalid")
    if map_binding.get("name") != context.map_name:
        raise ReviewedLocalizationError("active_map_name_mismatch")
    opendrive_sha256 = _sha(
        map_binding.get("opendrive_sha256"), "opendrive_hash_invalid"
    )
    if opendrive_sha256 != context.opendrive_sha256:
        raise ReviewedLocalizationError("active_opendrive_mismatch")

    contact = _object(value.get("contact"), "reviewed_contact_missing")
    _exact_keys(contact, {"method", "left_ground_pixel", "right_ground_pixel", "footprint_midpoint_pixel", "covariance_px2"}, "contact_fields_invalid")
    if contact.get("method") != "reviewed_vehicle_footprint_midpoint":
        raise ReviewedLocalizationError("reviewed_contact_method_invalid")
    left_pixel = _vector(contact.get("left_ground_pixel"), 2, "left_contact_invalid")
    right_pixel = _vector(contact.get("right_ground_pixel"), 2, "right_contact_invalid")
    midpoint = _vector(
        contact.get("footprint_midpoint_pixel"), 2, "footprint_midpoint_invalid"
    )
    expected_midpoint = [
        (left_pixel[index] + right_pixel[index]) / 2.0 for index in range(2)
    ]
    if any(abs(midpoint[index] - expected_midpoint[index]) > 1e-6 for index in range(2)):
        raise ReviewedLocalizationError("footprint_midpoint_mismatch")
    for pixel in (left_pixel, right_pixel, midpoint):
        if not (0.0 <= pixel[0] < resolution[0] and 0.0 <= pixel[1] < resolution[1]):
            raise ReviewedLocalizationError("reviewed_contact_outside_frame")
    if (
        right_pixel[0] <= left_pixel[0]
        or right_pixel[0] - left_pixel[0] < 0.01 * resolution[0]
        or abs(left_pixel[1] - right_pixel[1]) > 0.05 * resolution[1]
    ):
        raise ReviewedLocalizationError("reviewed_contact_endpoints_invalid")
    bbox = detection.get("bbox") or _nested(
        detection, "camera_data", "bifocal_metadata", "bbox"
    )
    if not isinstance(bbox, dict) or any(
        not _finite(bbox.get(key)) for key in ("x1", "y1", "x2", "y2")
    ):
        raise ReviewedLocalizationError("reviewed_contact_bbox_missing")
    x1, y1, x2, y2 = (float(bbox[key]) for key in ("x1", "y1", "x2", "y2"))
    if x2 <= x1 or y2 <= y1:
        raise ReviewedLocalizationError("reviewed_contact_bbox_invalid")
    margin_x = 0.05 * (x2 - x1)
    for pixel in (left_pixel, right_pixel, midpoint):
        if not (
            x1 - margin_x <= pixel[0] <= x2 + margin_x
            and y1 + 0.45 * (y2 - y1) <= pixel[1] <= y2 + 0.05 * (y2 - y1)
        ):
            raise ReviewedLocalizationError("reviewed_contact_bbox_mismatch")
    covariance_px2 = _matrix(
        contact.get("covariance_px2"), 2, "contact_covariance_not_psd"
    )
    contact_sigma_limit = MAX_CONTACT_SIGMA_AT_1280_PX * resolution[0] / 1280.0
    if largest_covariance_eigenvalue(covariance_px2) > contact_sigma_limit ** 2:
        raise ReviewedLocalizationError("contact_covariance_exceeds_gate")

    timing = _object(value.get("timing"), "timing_binding_missing")
    _exact_keys(timing, {"method", "trusted", "session_id", "pts_seconds", "media_timestamp_utc", "timestamp_error_ms"}, "timing_fields_invalid")
    if timing.get("method") != "exact_same_session_pts" or timing.get("trusted") is not True:
        raise ReviewedLocalizationError("timing_not_exact_same_session_pts")
    session_id = _text(timing.get("session_id"), "timing_session_id_missing")
    pts_seconds = timing.get("pts_seconds")
    timestamp_error_ms = timing.get("timestamp_error_ms")
    if not _finite(pts_seconds) or float(pts_seconds) < 0.0:
        raise ReviewedLocalizationError("timing_pts_invalid")
    if not _finite(timestamp_error_ms) or not 0.0 <= float(timestamp_error_ms) <= 1.0:
        raise ReviewedLocalizationError("timing_error_exceeds_exact_gate")
    media_timestamp_utc, media_epoch = _utc(
        timing.get("media_timestamp_utc"), "timing_media_timestamp_invalid"
    )
    detection_media = detection.get("media_timestamp_utc")
    if media_timestamp_utc != detection_media or detection.get("timestamp_utc") != detection_media:
        raise ReviewedLocalizationError("timing_detection_timestamp_mismatch")
    if detection.get("timestamp_schema_version") != 2 or detection.get("media_time_trusted") is not True:
        raise ReviewedLocalizationError("timing_detection_not_trusted_v2")
    media_clock = detection.get("media_clock")
    if (
        not isinstance(media_clock, dict)
        or media_clock.get("schema_version") != 1
        or media_clock.get("source") != "hls_ext_x_program_date_time"
        or media_clock.get("matching_method") != "exact_same_session_pts"
        or media_clock.get("session_id") != session_id
        or not _finite(media_clock.get("position_milliseconds"))
        or abs(float(media_clock["position_milliseconds"]) - float(pts_seconds) * 1000.0) > 1.0
    ):
        raise ReviewedLocalizationError("timing_media_clock_mismatch")

    review = _object(value.get("review"), "review_provenance_missing")
    _exact_keys(review, {"decision", "reviewer", "consensus", "factor_graph", "independent_reference"}, "review_fields_invalid")
    if review.get("decision") != "accepted":
        raise ReviewedLocalizationError("review_not_accepted")
    reviewer = _object(review.get("reviewer"), "reviewer_missing")
    _exact_keys(reviewer, {"kind", "id"}, "reviewer_fields_invalid")
    if reviewer.get("kind") != "human":
        raise ReviewedLocalizationError("reviewer_not_human")
    reviewer_id = _text(reviewer.get("id"), "reviewer_id_missing")
    if reviewer_id != authority_key_id:
        raise ReviewedLocalizationError("reviewer_authority_mismatch")
    consensus = _object(review.get("consensus"), "consensus_provenance_missing")
    _exact_keys(consensus, {"method", "artifact_sha256", "reviewer_ids"}, "consensus_fields_invalid")
    if consensus.get("method") != "independent_review_consensus":
        raise ReviewedLocalizationError("consensus_method_invalid")
    consensus_sha256 = _sha(
        consensus.get("artifact_sha256"), "consensus_hash_invalid"
    )
    reviewer_ids = consensus.get("reviewer_ids")
    if (
        not isinstance(reviewer_ids, list)
        or any(not isinstance(item, str) or not item.strip() for item in reviewer_ids)
        or len(reviewer_ids) != len(set(reviewer_ids))
        or len(set(reviewer_ids)) < 2
        or reviewer_id not in reviewer_ids
    ):
        raise ReviewedLocalizationError("consensus_reviewers_invalid")
    factor_graph = _object(review.get("factor_graph"), "factor_graph_provenance_missing")
    _exact_keys(factor_graph, {"artifact_sha256", "acceptance_eligible"}, "factor_graph_fields_invalid")
    factor_graph_sha256 = _sha(
        factor_graph.get("artifact_sha256"), "factor_graph_hash_invalid"
    )
    if factor_graph.get("acceptance_eligible") is not True:
        raise ReviewedLocalizationError("factor_graph_not_acceptance_eligible")
    independent_reference = _object(
        review.get("independent_reference"),
        "independent_reference_provenance_missing",
    )
    _exact_keys(
        independent_reference,
        {"artifact_sha256", "acceptance_eligible"},
        "independent_reference_fields_invalid",
    )
    independent_reference_sha256 = _sha(
        independent_reference.get("artifact_sha256"),
        "independent_reference_hash_invalid",
    )
    if independent_reference.get("acceptance_eligible") is not True:
        raise ReviewedLocalizationError("independent_reference_not_accepted")

    identity = _object(value.get("identity"), "identity_provenance_missing")
    _exact_keys(identity, {"status", "global_track_id", "trajectory_id", "association_method", "evidence_sha256", "camera_ids", "cross_camera_transition_sha256", "transition"}, "identity_fields_invalid")
    if identity.get("status") != "unambiguous":
        raise ReviewedLocalizationError("identity_ambiguous")
    if identity.get("global_track_id") != global_track_id:
        raise ReviewedLocalizationError("identity_global_track_mismatch")
    if identity.get("trajectory_id") != trajectory_id:
        raise ReviewedLocalizationError("identity_trajectory_mismatch")
    if identity.get("association_method") != "reviewed_multicamera_trajectory":
        raise ReviewedLocalizationError("identity_association_method_invalid")
    identity_evidence_sha256 = _sha(
        identity.get("evidence_sha256"), "identity_evidence_hash_invalid"
    )
    cross_camera_transition_sha256 = _sha(
        identity.get("cross_camera_transition_sha256"),
        "identity_cross_camera_transition_hash_invalid",
    )
    camera_ids = identity.get("camera_ids")
    if (
        not isinstance(camera_ids, list)
        or any(not isinstance(item, str) or not item for item in camera_ids)
        or camera_id not in camera_ids
        or len(camera_ids) < 2
        or len(camera_ids) != len(set(camera_ids))
        or any(item not in context.cameras for item in camera_ids)
    ):
        raise ReviewedLocalizationError("identity_camera_set_invalid")
    transition = identity.get("transition")
    if sample_index == 0:
        if transition is not None:
            raise ReviewedLocalizationError("identity_first_transition_invalid")
        normalized_transition = None
    else:
        transition = _object(transition, "identity_transition_missing")
        _exact_keys(
            transition,
            {
                "previous_event_id", "accepted", "ambiguity", "appearance_similarity",
                "transit_seconds", "distance_m", "speed_mps", "acceleration_mps2",
                "trajectory_covariance_m2", "pair_evidence_sha256",
            },
            "identity_transition_fields_invalid",
        )
        previous_event_id = _text(
            transition.get("previous_event_id"),
            "identity_previous_event_missing",
        )
        pair_evidence_sha256 = _sha(
            transition.get("pair_evidence_sha256"),
            "identity_pair_hash_invalid",
        )
        numeric = {
            key: transition.get(key)
            for key in (
                "appearance_similarity", "transit_seconds", "distance_m",
                "speed_mps",
            )
        }
        if any(not _finite(value) for value in numeric.values()):
            raise ReviewedLocalizationError("identity_transition_nonfinite")
        acceleration_mps2 = transition.get("acceleration_mps2")
        if acceleration_mps2 is not None and not _finite(acceleration_mps2):
            raise ReviewedLocalizationError("identity_transition_nonfinite")
        if (
            transition.get("accepted") is not True
            or transition.get("ambiguity") is not False
            or float(numeric["appearance_similarity"]) < MIN_APPEARANCE_SIMILARITY
            or not 0.0 < float(numeric["transit_seconds"]) <= MAX_TRANSIT_SECONDS
            or float(numeric["distance_m"]) < 0.0
            or not 0.0 <= float(numeric["speed_mps"]) <= MAX_VEHICLE_SPEED_MPS
            or (
                acceleration_mps2 is not None
                and abs(float(acceleration_mps2))
                > MAX_VEHICLE_ACCELERATION_MPS2
            )
        ):
            raise ReviewedLocalizationError("identity_transition_gate_failed")
        trajectory_covariance_m2 = _matrix(
            transition.get("trajectory_covariance_m2"),
            3,
            "trajectory_covariance_not_psd",
        )
        if math.sqrt(max(
            0.0, largest_covariance_eigenvalue(trajectory_covariance_m2)
        )) > MAX_LOCALIZATION_UNCERTAINTY_M:
            raise ReviewedLocalizationError("trajectory_covariance_exceeds_gate")
        normalized_transition = {
            "previous_event_id": previous_event_id,
            "appearance_similarity": float(numeric["appearance_similarity"]),
            "transit_seconds": float(numeric["transit_seconds"]),
            "distance_m": float(numeric["distance_m"]),
            "speed_mps": float(numeric["speed_mps"]),
            "acceleration_mps2": (
                float(acceleration_mps2)
                if acceleration_mps2 is not None else None
            ),
            "trajectory_covariance_m2": trajectory_covariance_m2,
            "pair_evidence_sha256": pair_evidence_sha256,
        }

    placement = _object(value.get("placement"), "world_placement_missing")
    _exact_keys(placement, {"coordinate_frame", "position_semantics", "position_m", "covariance_m2", "uncertainty_m", "heading_deg", "dimensions_m", "blueprint_family", "independent_reference", "blueprint"}, "placement_fields_invalid")
    if placement.get("coordinate_frame") != "carla_world":
        raise ReviewedLocalizationError("placement_coordinate_frame_invalid")
    if placement.get("position_semantics") != "ue5_actor_center":
        raise ReviewedLocalizationError("placement_position_semantics_invalid")
    position = _object(placement.get("position_m"), "placement_position_missing")
    _exact_keys(position, {"x", "y", "z"}, "placement_position_fields_invalid")
    if any(not _finite(position.get(axis)) for axis in ("x", "y", "z")):
        raise ReviewedLocalizationError("placement_position_nonfinite")
    covariance_m2 = _matrix(
        placement.get("covariance_m2"), 3, "placement_covariance_not_psd"
    )
    uncertainty_m = placement.get("uncertainty_m")
    if not _finite(uncertainty_m) or not 0.0 <= float(uncertainty_m) <= MAX_LOCALIZATION_UNCERTAINTY_M:
        raise ReviewedLocalizationError("placement_uncertainty_exceeds_2m")
    covariance_sigma = math.sqrt(max(0.0, largest_covariance_eigenvalue(covariance_m2)))
    if covariance_sigma > float(uncertainty_m) + 1e-9:
        raise ReviewedLocalizationError("placement_uncertainty_understates_covariance")
    heading_deg = placement.get("heading_deg")
    if not _finite(heading_deg):
        raise ReviewedLocalizationError("placement_heading_invalid")
    dimensions = _object(placement.get("dimensions_m"), "vehicle_dimensions_missing")
    _exact_keys(dimensions, {"length", "width", "height"}, "vehicle_dimension_fields_invalid")
    normalized_dimensions = {}
    for key, upper in (("length", 30.0), ("width", 5.0), ("height", 6.0)):
        item = dimensions.get(key)
        if not _finite(item) or not 0.1 <= float(item) <= upper:
            raise ReviewedLocalizationError("vehicle_dimensions_invalid")
        normalized_dimensions[key] = float(item)
    object_type = str(detection.get("object_type") or "").lower()
    blueprint_family = placement.get("blueprint_family")
    if VEHICLE_FAMILIES.get(object_type) != blueprint_family:
        raise ReviewedLocalizationError("blueprint_family_object_type_mismatch")
    reference = _object(
        placement.get("independent_reference"),
        "independent_reference_position_missing",
    )
    _exact_keys(reference, {"position_m", "error_m"}, "independent_reference_position_fields_invalid")
    reference_position = _object(
        reference.get("position_m"), "independent_reference_position_missing"
    )
    _exact_keys(reference_position, {"x", "y", "z"}, "independent_reference_position_fields_invalid")
    if any(not _finite(reference_position.get(axis)) for axis in ("x", "y", "z")):
        raise ReviewedLocalizationError("independent_reference_position_nonfinite")
    reference_error_m = reference.get("error_m")
    computed_reference_error_m = math.sqrt(sum(
        (float(position[axis]) - float(reference_position[axis])) ** 2
        for axis in ("x", "y", "z")
    ))
    if (
        not _finite(reference_error_m)
        or abs(float(reference_error_m) - computed_reference_error_m) > 1e-6
        or computed_reference_error_m > MAX_INDEPENDENT_REFERENCE_ERROR_M
    ):
        raise ReviewedLocalizationError("independent_reference_error_invalid")
    blueprint = _object(placement.get("blueprint"), "blueprint_binding_missing")
    _exact_keys(
        blueprint,
        {
            "catalog_sha256", "pool_sha256", "selected_blueprint_id",
            "expected_dimensions_m", "dimension_tolerance_m",
        },
        "blueprint_fields_invalid",
    )
    catalog_sha256 = _sha(
        blueprint.get("catalog_sha256"), "blueprint_catalog_hash_invalid"
    )
    pool_sha256 = _sha(
        blueprint.get("pool_sha256"), "blueprint_pool_hash_invalid"
    )
    selected_blueprint_id = _text(
        blueprint.get("selected_blueprint_id"), "blueprint_id_missing"
    )
    expected_blueprint_dimensions = _object(
        blueprint.get("expected_dimensions_m"), "blueprint_dimensions_missing"
    )
    _exact_keys(expected_blueprint_dimensions, {"length", "width", "height"}, "blueprint_dimensions_invalid")
    if any(
        not _finite(expected_blueprint_dimensions.get(key))
        or abs(float(expected_blueprint_dimensions[key]) - normalized_dimensions[key]) > 1e-9
        for key in ("length", "width", "height")
    ):
        raise ReviewedLocalizationError("blueprint_dimensions_mismatch")
    dimension_tolerance_m = blueprint.get("dimension_tolerance_m")
    if (
        not _finite(dimension_tolerance_m)
        or not 0.0 <= float(dimension_tolerance_m) <= MAX_BLUEPRINT_DIMENSION_ERROR_M
    ):
        raise ReviewedLocalizationError("blueprint_dimension_tolerance_invalid")

    return {
        "schema": SCHEMA,
        "contract_sha256": supplied_hash,
        "authority_key_id": authority_key_id,
        "event_id": event_id,
        "camera_id": camera_id,
        "global_track_id": global_track_id,
        "trajectory_id": trajectory_id,
        "sample_index": sample_index,
        "media_timestamp_utc": media_timestamp_utc,
        "media_epoch": media_epoch,
        "session_id": session_id,
        "pts_seconds": float(pts_seconds),
        "frame_sha256": frame_sha256,
        "mask_sha256": mask_sha256,
        "inference_manifest_sha256": inference_manifest_sha256,
        "frame_pixel_sha256": frame_pixel_sha256,
        "mask_pixel_sha256": mask_pixel_sha256,
        "detector_output_sha256": detector_output_sha256,
        "detector_model_sha256": detector_model_sha256,
        "detector_config_sha256": detector_config_sha256,
        "cameras_json_sha256": cameras_json_sha256,
        "camera_config_sha256": camera_config_sha256,
        "intrinsics_artifact_sha256": intrinsics_artifact_sha256,
        "intrinsics_report_sha256": intrinsics_report_sha256,
        "static_calibration_sha256": static_calibration_sha256,
        "map_name": context.map_name,
        "opendrive_sha256": opendrive_sha256,
        "footprint_midpoint_pixel": midpoint,
        "contact_covariance_px2": covariance_px2,
        "consensus_sha256": consensus_sha256,
        "factor_graph_sha256": factor_graph_sha256,
        "independent_reference_sha256": independent_reference_sha256,
        "identity_evidence_sha256": identity_evidence_sha256,
        "identity_camera_ids": list(camera_ids),
        "cross_camera_transition_sha256": cross_camera_transition_sha256,
        "transition": normalized_transition,
        "position_m": {axis: float(position[axis]) for axis in ("x", "y", "z")},
        "covariance_m2": covariance_m2,
        "uncertainty_m": float(uncertainty_m),
        "covariance_sigma_m": covariance_sigma,
        "heading_deg": float(heading_deg) % 360.0,
        "dimensions_m": normalized_dimensions,
        "blueprint_family": blueprint_family,
        "independent_reference_position_m": {
            axis: float(reference_position[axis]) for axis in ("x", "y", "z")
        },
        "independent_reference_error_m": computed_reference_error_m,
        "blueprint": {
            "catalog_sha256": catalog_sha256,
            "pool_sha256": pool_sha256,
            "selected_blueprint_id": selected_blueprint_id,
            "expected_dimensions_m": {
                key: float(expected_blueprint_dimensions[key])
                for key in ("length", "width", "height")
            },
            "dimension_tolerance_m": float(dimension_tolerance_m),
        },
        "placement_key_sha256": placement_key_sha256(
            global_track_id, blueprint_family
        ),
    }
