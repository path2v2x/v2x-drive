"""Raw retained four-camera calibration evidence for strict-contract tests."""

import copy
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from digital_twin_bridge.reviewed_localization import (
    canonical_object_sha256,
    seal_authenticated_artifact,
)
from digital_twin_bridge.twin_camera_rig import (
    absolute_twin_model,
    heading_to_carla_yaw,
    horizontal_fov_deg,
    twin_horizontal_fov_deg,
)
from tests.test_aggregate_twin_calibration_manifests import (
    fixture as aggregate_fixture,
)
from tools.aggregate_twin_calibration_manifests import aggregate_site_manifests
from tools.build_twin_calibration_manifest import (
    build_deployment_model,
    offline_depth_pixel_to_world,
)


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_reviewed_static_calibration(
    root,
    cameras_path,
    cameras,
    map_name,
    opendrive_sha256,
    authority_key_id,
    authority_key,
):
    """Create signed evidence whose acceptance is recomputed from raw inputs."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    registry_path, manifest_paths = aggregate_fixture(root / "retained")
    cameras_raw = Path(cameras_path).read_bytes()
    cameras_sha256 = hashlib.sha256(cameras_raw).hexdigest()
    retained_cameras_path = root / "retained" / "cameras.json"
    retained_cameras_path.write_bytes(cameras_raw)
    cameras_identity = {
        "path": str(retained_cameras_path.resolve()),
        "sha256": cameras_sha256,
        "size_bytes": len(cameras_raw),
    }
    registry = json.loads(registry_path.read_text())
    registry["cameras_file_sha256"] = cameras_sha256
    camera_index = {camera["id"]: camera for camera in cameras}
    for manifest_path in manifest_paths:
        manifest = json.loads(manifest_path.read_text())
        camera = camera_index[manifest["camera_id"]]
        width = int(camera["intrinsics"]["width"])
        height = int(camera["intrinsics"]["height"])
        prior_width = float(manifest["width"])
        prior_height = float(manifest["height"])
        scale_x = width / prior_width
        scale_y = height / prior_height
        for feature in manifest["features"]:
            if feature["type"] == "point":
                feature["image"] = [
                    feature["image"][0] * scale_x,
                    feature["image"][1] * scale_y,
                ]
            else:
                feature["image_polyline"] = [
                    [pixel[0] * scale_x, pixel[1] * scale_y]
                    for pixel in feature["image_polyline"]
                ]
        manifest["width"] = width
        manifest["height"] = height
        manifest["cameras_file_sha256"] = cameras_sha256
        manifest["camera_config_sha256"] = canonical_object_sha256(camera)
        manifest["source_artifacts"]["cameras_file"] = cameras_identity

        real_identity = manifest["source_artifacts"]["real_frame"]
        real_path = Path(real_identity["path"])
        with Image.open(real_path) as source:
            resized = source.convert("RGB").resize((width, height))
            resized.save(real_path, "PNG")
        real_raw = real_path.read_bytes()
        real_identity.update(
            sha256=hashlib.sha256(real_raw).hexdigest(),
            size_bytes=len(real_raw),
        )
        manifest["source_frame_sha256"] = real_identity["sha256"]

        calibration = camera["intrinsics_calibration"]
        artifact_path = Path(calibration["artifact_path"]).resolve()
        artifact_raw = artifact_path.read_bytes()
        manifest["intrinsics_calibration"] = {
            "method": calibration["method"],
            "artifact_sha256": hashlib.sha256(artifact_raw).hexdigest(),
            "image_count": calibration["image_count"],
            "source_images_sha256": copy.deepcopy(
                calibration["source_images_sha256"]
            ),
            "rms_reprojection_error_px": calibration[
                "rms_reprojection_error_px"
            ],
            "resolution": copy.deepcopy(calibration["resolution"]),
            "camera_matrix": copy.deepcopy(calibration["camera_matrix"]),
            "distortion": copy.deepcopy(calibration["distortion"]),
        }
        manifest["source_artifacts"]["intrinsics_artifact"] = {
            "path": str(artifact_path),
            "sha256": hashlib.sha256(artifact_raw).hexdigest(),
            "size_bytes": len(artifact_raw),
        }
        source_identities = []
        for source_path_value in calibration["source_image_paths"]:
            source_path = Path(source_path_value).resolve()
            source_raw = source_path.read_bytes()
            source_identities.append({
                "path": str(source_path),
                "sha256": hashlib.sha256(source_raw).hexdigest(),
                "size_bytes": len(source_raw),
            })
        manifest["source_artifacts"][
            "intrinsics_source_images"
        ] = source_identities
        manifest["ue5_map"] = map_name
        manifest["ue5_map_opendrive_sha256"] = opendrive_sha256
        manifest["projection"]["map_name"] = map_name
        manifest["projection"]["opendrive_sha256"] = opendrive_sha256
        anchor = [0.0, 0.0, 0.0]
        base = {
            "pitch_deg": float(camera["pitch_deg"]),
            "yaw_deg": heading_to_carla_yaw(
                float(camera["heading_deg"]), float(camera["yaw_deg"])
            ),
            "roll_deg": float(camera.get("roll_deg", 0.0)),
            "fov_deg": horizontal_fov_deg(camera["intrinsics"]),
        }
        baseline = absolute_twin_model(
            anchor, base, camera.get("twin_pose") or {}
        )
        manifest["baseline"]["location"] = baseline["location"]
        manifest["baseline"]["pitch_deg"] = baseline["pitch_deg"]
        manifest["baseline"]["yaw_deg"] = baseline["yaw_deg"]
        manifest["baseline"]["roll_deg"] = baseline["roll_deg"]
        manifest["baseline"]["fov_deg"] = baseline["fov_deg"]
        manifest["baseline"]["cx"] = camera["intrinsics"]["cx"]
        manifest["baseline"]["cy"] = camera["intrinsics"]["cy"]
        manifest["deployment_model"] = build_deployment_model(
            camera,
            SimpleNamespace(
                location=SimpleNamespace(
                    x=baseline["location"][0],
                    y=baseline["location"][1],
                    z=baseline["location"][2],
                ),
                rotation=SimpleNamespace(
                    pitch=baseline["pitch_deg"],
                    yaw=baseline["yaw_deg"],
                    roll=baseline["roll_deg"],
                ),
            ),
        )
        depth_width = int(manifest["depth_frame"]["width"])
        depth_height = int(manifest["depth_frame"]["height"])
        twin_focal = (depth_width / 2.0) / math.tan(
            math.radians(twin_horizontal_fov_deg(camera)) / 2.0
        )
        fx = float(camera["intrinsics"]["fx"])
        fy = float(camera["intrinsics"]["fy"])
        cx = float(camera["intrinsics"]["cx"])
        cy = float(camera["intrinsics"]["cy"])

        def twin_pixel_for_real(pixel):
            return [
                depth_width / 2.0 + (float(pixel[0]) - cx) / fx * twin_focal,
                depth_height / 2.0 + (float(pixel[1]) - cy) / fy * twin_focal,
            ]

        for feature in manifest["features"]:
            if feature["type"] == "point":
                feature["twin"] = twin_pixel_for_real(feature["image"])
            else:
                feature["twin_polyline"] = [
                    twin_pixel_for_real(pixel)
                    for pixel in feature["image_polyline"]
                ]
        for feature in manifest["features"]:
            if feature["type"] == "point":
                projected_world = offline_depth_pixel_to_world(
                    manifest["baseline"],
                    feature["twin"][0],
                    feature["twin"][1],
                    feature["depth_neighborhood"]["center_depth_m"],
                    baseline["fov_deg"],
                    depth_width,
                    depth_height,
                )
                feature["world"] = projected_world
                feature["surveyed_world"] = projected_world
            else:
                feature["world"] = [
                    offline_depth_pixel_to_world(
                        manifest["baseline"],
                        pixel[0],
                        pixel[1],
                        neighborhood["center_depth_m"],
                        baseline["fov_deg"],
                        depth_width,
                        depth_height,
                    )
                    for pixel, neighborhood in zip(
                        feature["twin_polyline"],
                        feature["depth_neighborhoods"],
                    )
                ]

        annotation_identity = manifest["source_artifacts"]["annotations"]
        annotation_path = Path(annotation_identity["path"])
        annotation = json.loads(annotation_path.read_text())
        annotation["real_frame_sha256"] = real_identity["sha256"]
        annotation["cameras_file_sha256"] = cameras_sha256
        points = {point["id"]: point for point in annotation["points"]}
        roads = {road["id"]: road for road in annotation["roads"]}
        for feature in manifest["features"]:
            target = (
                points[feature["id"]]
                if feature["type"] == "point"
                else roads[feature["id"]]
            )
            for key in (
                (
                    "global_landmark_id", "surveyed_world", "split",
                    "provenance", "category", "description", "twin", "image",
                    "survey_record_sha256", "survey_record_path",
                )
                if feature["type"] == "point"
                else (
                    "split", "provenance", "category", "description",
                    "twin_polyline", "image_polyline",
                )
            ):
                target[key] = copy.deepcopy(feature[key])
        annotation_raw = json.dumps(annotation, sort_keys=True).encode()
        annotation_path.write_bytes(annotation_raw)
        annotation_identity.update(
            sha256=hashlib.sha256(annotation_raw).hexdigest(),
            size_bytes=len(annotation_raw),
        )
        manifest["annotation_sha256"] = annotation_identity["sha256"]
        _write_json(manifest_path, manifest)
    landmark_world = {}
    for manifest_path in manifest_paths:
        manifest = json.loads(manifest_path.read_text())
        for feature in manifest["features"]:
            if feature["type"] == "point":
                landmark_world.setdefault(
                    feature["global_landmark_id"], feature["surveyed_world"]
                )
    for landmark in registry["landmarks"]:
        landmark["surveyed_world"] = landmark_world[
            landmark["global_landmark_id"]
        ]
    _write_json(registry_path, registry)
    aggregate = aggregate_site_manifests(registry_path, manifest_paths)
    aggregate_path = root / "site-aggregation.json"
    aggregate_sha256 = _write_json(aggregate_path, aggregate)
    reprojection = {}
    for manifest_path in manifest_paths:
        manifest = json.loads(manifest_path.read_text())
        camera_id = manifest["camera_id"]
        points = []
        roads = []
        for feature in manifest["features"]:
            if feature.get("split") != "holdout":
                continue
            if feature["type"] == "point":
                points.append({
                    "feature_id": feature["id"],
                    "observed_pixel": feature["image"],
                })
            else:
                roads.append({
                    "feature_id": feature["id"],
                    "observed_polyline": feature["image_polyline"],
                })
        evidence = seal_authenticated_artifact({
            "schema": "v2x-camera-heldout-reprojection/v1",
            "camera_id": camera_id,
            "camera_config_sha256": manifest["camera_config_sha256"],
            "camera_manifest_sha256": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
            "native_resolution": [manifest["width"], manifest["height"]],
            "points": points,
            "roads": roads,
        }, authority_key_id, authority_key)
        evidence_path = root / f"{camera_id}-heldout-reprojection.json"
        evidence_sha256 = _write_json(evidence_path, evidence)
        reprojection[camera_id] = {
            "path": str(evidence_path),
            "sha256": evidence_sha256,
            "schema": "v2x-camera-heldout-reprojection/v1",
        }
    static = seal_authenticated_artifact({
        "schema": "v2x-static-camera-survey-manifest/v1",
        "source_cameras_json_sha256": cameras_sha256,
        "map": {"name": map_name, "opendrive_sha256": opendrive_sha256},
        "site_aggregation": {
            "path": str(aggregate_path),
            "sha256": aggregate_sha256,
            "schema": "v2x-site-calibration-aggregation/v1",
        },
        "heldout_reprojection": reprojection,
    }, authority_key_id, authority_key)
    static_path = root / "static-calibration.json"
    _write_json(static_path, static)
    return static_path
