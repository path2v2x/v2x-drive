import copy
from io import BytesIO
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from tools.aggregate_twin_calibration_manifests import (
    SiteManifestError,
    aggregate_site_manifests,
)
from tools.build_twin_calibration_manifest import (
    bind_survey_record_artifacts,
    build_deployment_model,
    canonical_camera_sha256,
    depth_neighborhood_evidence,
    offline_depth_pixel_to_world,
    validate_annotations,
    validate_intrinsics_calibration,
)


CAMERAS = ("ch1", "ch2", "ch3", "ch4")


def rewrite_annotation_from_manifest(manifest_path):
    """Keep a deliberate site-level mutation valid at the builder boundary."""
    manifest = json.loads(manifest_path.read_text())
    identity = manifest["source_artifacts"]["annotations"]
    annotation_path = Path(identity["path"])
    annotation = json.loads(annotation_path.read_text())
    by_id = {point["id"]: point for point in annotation["points"]}
    for feature in manifest["features"]:
        if feature.get("type") != "point":
            continue
        point = by_id[feature["id"]]
        for key in (
            "global_landmark_id", "surveyed_world", "split", "provenance",
            "category", "description", "twin", "image",
            "survey_record_sha256", "survey_record_path",
        ):
            point[key] = copy.deepcopy(feature[key])
    raw = json.dumps(annotation, sort_keys=True).encode()
    annotation_path.write_bytes(raw)
    identity.update(
        sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
    )
    manifest["annotation_sha256"] = identity["sha256"]
    manifest_path.write_text(json.dumps(manifest))


def rewrite_registry_from_manifests(registry, manifests):
    original = json.loads(registry.read_text())
    landmarks = {}
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text())
        for feature in manifest["features"]:
            if feature.get("type") != "point":
                continue
            landmarks.setdefault(feature["global_landmark_id"], {
                "global_landmark_id": feature["global_landmark_id"],
                "split": feature["split"],
                "surveyed_world": feature["surveyed_world"],
                "survey_record_sha256": feature["survey_record_sha256"],
                "survey_record_path": feature["survey_record_path"],
                "survey_record_size_bytes": feature["survey_record"]["size_bytes"],
            })
    original["landmarks"] = list(landmarks.values())
    registry.write_text(json.dumps(original))


def fixture(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)

    def artifact(name, payload):
        path = (tmp_path / name).resolve()
        path.write_bytes(payload)
        return {
            "path": str(path),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }

    def png(width, height, color):
        output = BytesIO()
        Image.new("RGB", (width, height), color).save(output, "PNG")
        return output.getvalue()

    real_size = (640, 480)
    twin_size = (320, 240)
    intrinsics_sources = [
        artifact(
            f"intrinsics-source-{index}.png",
            png(*real_size, (index, 20 + index, 40 + index)),
        )
        for index in range(10)
    ]
    calibration_payload = {
        "method": "charuco",
        "image_count": 10,
        "source_images_sha256": [item["sha256"] for item in intrinsics_sources],
        "rms_reprojection_error_px": 0.5,
        "resolution": list(real_size),
        "camera_matrix": [
            [320.0, 0.0, 320.0],
            [0.0, 320.0, 240.0],
            [0.0, 0.0, 1.0],
        ],
        "distortion": {
            "k1": 0.0,
            "k2": 0.0,
            "p1": 0.0,
            "p2": 0.0,
            "k3": 0.0,
        },
    }
    intrinsics_bytes = json.dumps(
        calibration_payload, sort_keys=True
    ).encode()
    intrinsics_artifact = artifact("intrinsics.json", intrinsics_bytes)
    cameras = []
    for camera_id in CAMERAS:
        cameras.append({
            "id": camera_id,
            "pitch_deg": -35.0,
            "yaw_deg": 0.0,
            "heading_deg": 180.0,
            "roll_deg": 0.0,
            "intrinsics": {
                "fx": 320.0,
                "fy": 320.0,
                "cx": 320.0,
                "cy": 240.0,
                "width": real_size[0],
                "height": real_size[1],
            },
            "intrinsics_calibration": {
                **calibration_payload,
                "artifact_sha256": intrinsics_artifact["sha256"],
            },
        })
    cameras_artifact = artifact(
        "cameras.json", json.dumps({"cameras": cameras}, sort_keys=True).encode()
    )
    cameras_hash = cameras_artifact["sha256"]
    map_name = "Carla/Maps/Richmond_Field_Station_Richmond_CA"
    opendrive_hash = "d" * 64
    landmarks = []
    for index in range(12):
        survey_record = artifact(
            f"survey-{index}.json", f"survey-{index}".encode()
        )
        landmarks.append({
            "global_landmark_id": f"rfs-landmark-{index:02d}",
            "split": "train" if index < 8 else "holdout",
            "surveyed_world": [float(index), float(index * 2), 0.5],
            "survey_record_sha256": survey_record["sha256"],
            "survey_record_path": survey_record["path"],
            "survey_record_size_bytes": survey_record["size_bytes"],
        })
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "schema": "v2x-site-landmark-registry/v1",
        "cameras_file_sha256": cameras_hash,
        "landmarks": landmarks,
    }))
    encoded = int(round(10.0 / 1000.0 * 16777215.0))
    pixel = bytes((encoded >> 16, (encoded >> 8) & 255, encoded & 255, 0))
    depth_raw = pixel * (twin_size[0] * twin_size[1])
    depth_artifact = artifact("depth.bgra", depth_raw)
    baseline = {
        "location": [0.0, 0.0, 8.0],
        "pitch_deg": -35.0,
        "yaw_deg": 90.0,
        "roll_deg": 0.0,
        "fov_deg": 90.0,
        "cx": 320.0,
        "cy": 240.0,
        "k1": 0.0,
    }
    train_real = [
        [50, 50], [590, 55], [55, 425], [585, 420],
        [320, 70], [95, 245], [545, 240], [320, 405],
    ]
    holdout_real = [[75, 90], [565, 100], [85, 390], [555, 380]]
    train_twin = [[u / 2, v / 2] for u, v in train_real]
    holdout_twin = [[u / 2, v / 2] for u, v in holdout_real]
    manifests = []
    for camera_id, camera in zip(CAMERAS, cameras):
        path = tmp_path / f"{camera_id}.json"
        real_artifact = artifact(
            f"{camera_id}-real.png", png(*real_size, (20, 40, 60))
        )
        twin_artifact = artifact(
            f"{camera_id}-twin.png", png(*twin_size, (60, 40, 20))
        )
        points = []
        for index, (landmark, image, twin) in enumerate(zip(
            landmarks, train_real + holdout_real, train_twin + holdout_twin
        )):
            points.append({
                "id": f"{camera_id}-feature-{index}",
                "global_landmark_id": landmark["global_landmark_id"],
                "surveyed_world": landmark["surveyed_world"],
                "survey_record_sha256": landmark["survey_record_sha256"],
                "survey_record_path": landmark["survey_record_path"],
                "split": landmark["split"],
                "image": image,
                "twin": twin,
                "provenance": "manually_verified_unique",
                "category": "static_landmark",
                "description": f"Unique surveyed static landmark {index}",
            })
        roads = [
            {
                "id": f"{camera_id}-road-{index}",
                "split": "train" if index < 3 else "holdout",
                "twin_polyline": [
                    [25.0, 105.0 + index * 10.0],
                    [295.0, 95.0 + index * 10.0],
                ],
                "image_polyline": [
                    [50.0, 210.0 + index * 20.0],
                    [590.0, 190.0 + index * 20.0],
                ],
                "provenance": "manually_traced_geometry",
                "category": "road_edge",
                "description": f"Unique manually traced road edge {index}",
            }
            for index in range(5)
        ]
        annotation_payload = {
            "camera_id": camera_id,
            "real_frame_sha256": real_artifact["sha256"],
            "twin_frame_sha256": twin_artifact["sha256"],
            "cameras_file_sha256": cameras_hash,
            "points": points,
            "roads": roads,
        }
        annotation_artifact = artifact(
            f"{camera_id}-annotations.json",
            json.dumps(annotation_payload, sort_keys=True).encode(),
        )
        normalized = bind_survey_record_artifacts(validate_annotations(
            annotation_payload, camera_id, real_size, twin_size
        ))
        point_depth = depth_neighborhood_evidence(
            depth_raw, *twin_size, normalized[0]["twin"][0], normalized[0]["twin"][1]
        )
        features = []
        for feature in normalized:
            resolved = copy.deepcopy(feature)
            if feature["type"] == "point":
                pixels = [feature["twin"]]
            else:
                pixels = feature["twin_polyline"]
            worlds = [
                offline_depth_pixel_to_world(
                    baseline, pixel_value[0], pixel_value[1],
                    point_depth["center_depth_m"], 90.0, *twin_size
                )
                for pixel_value in pixels
            ]
            evidence = [
                depth_neighborhood_evidence(
                    depth_raw, *twin_size, pixel_value[0], pixel_value[1]
                )
                for pixel_value in pixels
            ]
            if feature["type"] == "point":
                resolved["world"] = worlds[0]
                resolved["depth_neighborhood"] = evidence[0]
            else:
                resolved["world"] = worlds
                resolved["depth_neighborhoods"] = evidence
            features.append(resolved)
        transform = SimpleNamespace(
            location=SimpleNamespace(x=0.0, y=0.0, z=8.0),
            rotation=SimpleNamespace(pitch=-35.0, yaw=90.0, roll=0.0),
        )
        path.write_text(json.dumps({
            "schema_version": 1,
            "camera_id": camera_id,
            "width": real_size[0],
            "height": real_size[1],
            "source_frame_sha256": real_artifact["sha256"],
            "twin_frame_sha256": twin_artifact["sha256"],
            "annotation_sha256": annotation_artifact["sha256"],
            "cameras_file_sha256": cameras_hash,
            "camera_config_sha256": canonical_camera_sha256(camera),
            "source_artifacts": {
                "annotations": annotation_artifact,
                "real_frame": real_artifact,
                "twin_frame": twin_artifact,
                "cameras_file": cameras_artifact,
                "intrinsics_artifact": intrinsics_artifact,
                "intrinsics_source_images": intrinsics_sources,
            },
            "ue5_map": map_name,
            "ue5_map_opendrive_sha256": opendrive_hash,
            "projection": {
                "source": "opendrive_georeference",
                "strict": True,
                "map_origin_error_m": 0.1,
                "map_name": map_name,
                "opendrive_sha256": opendrive_hash,
                "georeference_sha256": "e" * 64,
            },
            "depth_frame": {
                "carla_frame": 123,
                "sensor_timestamp": 45.5,
                "width": twin_size[0],
                "height": twin_size[1],
                "raw_data_sha256": depth_artifact["sha256"],
                "raw_data_size": len(depth_raw),
                "path": depth_artifact["path"],
            },
            "baseline": baseline,
            "deployment_model": build_deployment_model(camera, transform),
            "intrinsics_calibration": validate_intrinsics_calibration(camera),
            "features": features,
        }))
        manifests.append(path)
    return registry, manifests


def test_four_camera_registry_allows_canonical_cross_camera_reuse(tmp_path):
    registry, manifests = fixture(tmp_path)

    report = aggregate_site_manifests(registry, list(reversed(manifests)))

    assert report["gate_passed"] is True
    assert report["acceptance_eligible"] is False
    assert set(report["manifests"]) == set(CAMERAS)
    assert all(
        landmark["cameras"] == list(CAMERAS)
        for landmark in report["landmarks"].values()
    )
    assert report["contract"]["shared_landmark_count"] == 12
    assert report["contract"]["connected_camera_count"] == 4
    assert report["contract"]["cross_camera_edge_count"] == 6
    assert report["contract"]["shared_landmarks_per_camera"] == {
        camera: 12 for camera in CAMERAS
    }
    assert report["site_landmark_registry"]["sha256"] == hashlib.sha256(
        registry.read_bytes()
    ).hexdigest()
    for camera_id, manifest in report["manifests"].items():
        path = tmp_path / f"{camera_id}.json"
        assert manifest["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_cross_camera_reuse_cannot_change_the_global_split(tmp_path):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[1].read_text())
    value["features"][0]["split"] = "holdout"
    value["features"][8]["split"] = "train"
    manifests[1].write_text(json.dumps(value))
    rewrite_annotation_from_manifest(manifests[1])

    with pytest.raises(SiteManifestError, match="canonical split"):
        aggregate_site_manifests(registry, manifests)


def test_renamed_near_duplicate_landmark_is_rejected_site_wide(tmp_path):
    registry, manifests = fixture(tmp_path)
    registry_value = json.loads(registry.read_text())
    renamed = copy.deepcopy(registry_value["landmarks"][0])
    renamed["global_landmark_id"] = "caller-renamed-landmark"
    renamed["surveyed_world"][0] += 0.01
    registry_value["landmarks"].append(renamed)
    registry.write_text(json.dumps(registry_value))
    manifest_value = json.loads(manifests[0].read_text())
    manifest_value["features"][0].update(copy.deepcopy(renamed))
    manifests[0].write_text(json.dumps(manifest_value))

    with pytest.raises(SiteManifestError, match="renamed near-duplicate"):
        aggregate_site_manifests(registry, manifests)


@pytest.mark.parametrize(
    ("separation", "accepted"),
    [(0.249, False), (0.25, True)],
)
def test_renamed_landmark_separation_boundary_is_fail_closed(
    tmp_path, separation, accepted
):
    registry, manifests = fixture(tmp_path)
    registry_value = json.loads(registry.read_text())
    registry_value["landmarks"][1]["surveyed_world"] = [
        separation,
        0.0,
        0.5,
    ]
    registry.write_text(json.dumps(registry_value))
    for path in manifests:
        value = json.loads(path.read_text())
        value["features"][1]["surveyed_world"] = [separation, 0.0, 0.5]
        path.write_text(json.dumps(value))
        rewrite_annotation_from_manifest(path)

    if accepted:
        assert aggregate_site_manifests(registry, manifests)["gate_passed"]
    else:
        with pytest.raises(SiteManifestError, match="renamed near-duplicate"):
            aggregate_site_manifests(registry, manifests)


def test_survey_world_or_record_hash_disagreement_is_rejected(tmp_path):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[2].read_text())
    value["features"][3]["surveyed_world"][0] += 1.0
    manifests[2].write_text(json.dumps(value))
    rewrite_annotation_from_manifest(manifests[2])

    with pytest.raises(SiteManifestError, match="surveyed world identity"):
        aggregate_site_manifests(registry, manifests)


@pytest.mark.parametrize("malformed", [None, [], "not-an-object"])
def test_malformed_manifest_annotation_entries_fail_controlled(tmp_path, malformed):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[0].read_text())
    value["features"].append(malformed)
    manifests[0].write_text(json.dumps(value))

    with pytest.raises(SiteManifestError, match="feature is malformed"):
        aggregate_site_manifests(registry, manifests)


def test_requires_exactly_one_manifest_per_camera(tmp_path):
    registry, manifests = fixture(tmp_path)

    with pytest.raises(SiteManifestError, match="exactly four"):
        aggregate_site_manifests(registry, manifests[:3])


def test_all_cameras_must_share_one_map_opendrive_fingerprint(tmp_path):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[3].read_text())
    value["ue5_map_opendrive_sha256"] = "f" * 64
    value["projection"]["opendrive_sha256"] = "f" * 64
    manifests[3].write_text(json.dumps(value))

    with pytest.raises(SiteManifestError, match="one map/OpenDRIVE"):
        aggregate_site_manifests(registry, manifests)


def test_same_global_landmark_requires_cross_camera_resolved_world_identity(
    tmp_path,
):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[2].read_text())
    value["features"][4]["world"][0] += 0.251
    manifests[2].write_text(json.dumps(value))

    with pytest.raises(SiteManifestError, match="world geometry mismatches"):
        aggregate_site_manifests(registry, manifests)


def test_incomplete_builder_contract_or_fallback_projection_is_rejected(tmp_path):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[0].read_text())
    value.pop("depth_frame")
    manifests[0].write_text(json.dumps(value))
    with pytest.raises(SiteManifestError, match="depth identity"):
        aggregate_site_manifests(registry, manifests)

    registry, manifests = fixture(tmp_path / "fallback")
    value = json.loads(manifests[0].read_text())
    value["projection"].update(
        source="origin_centered_fallback", strict=False
    )
    manifests[0].write_text(json.dumps(value))
    with pytest.raises(SiteManifestError, match="strict OpenDRIVE projection"):
        aggregate_site_manifests(registry, manifests)


def test_builder_counts_and_measured_intrinsics_must_be_complete(tmp_path):
    registry, manifests = fixture(tmp_path / "counts")
    value = json.loads(manifests[0].read_text())
    value["features"] = [
        feature for feature in value["features"]
        if feature["id"] != "ch1-road-4"
    ]
    manifests[0].write_text(json.dumps(value))
    with pytest.raises(SiteManifestError, match="feature counts are incomplete"):
        aggregate_site_manifests(registry, manifests)

    registry, manifests = fixture(tmp_path / "intrinsics")
    value = json.loads(manifests[0].read_text())
    value["intrinsics_calibration"]["source_images_sha256"] = ["a" * 64] * 10
    manifests[0].write_text(json.dumps(value))
    with pytest.raises(SiteManifestError, match="measured-intrinsics contract"):
        aggregate_site_manifests(registry, manifests)


def test_zero_shared_landmarks_are_rejected_even_with_four_complete_cameras(tmp_path):
    registry, manifests = fixture(tmp_path)
    for camera_index, path in enumerate(manifests):
        value = json.loads(path.read_text())
        for feature in value["features"]:
            if feature.get("type") != "point":
                continue
            feature["global_landmark_id"] = (
                f"{value['camera_id']}-{feature['global_landmark_id']}"
            )
            feature["surveyed_world"][0] += 100.0 * camera_index
        path.write_text(json.dumps(value))
        rewrite_annotation_from_manifest(path)
    rewrite_registry_from_manifests(registry, manifests)

    with pytest.raises(SiteManifestError, match="every camera must participate"):
        aggregate_site_manifests(registry, manifests)


def test_two_disconnected_shared_camera_islands_are_rejected(tmp_path):
    registry, manifests = fixture(tmp_path)
    for path in manifests[2:]:
        value = json.loads(path.read_text())
        for feature in value["features"]:
            if feature.get("type") != "point":
                continue
            feature["global_landmark_id"] = "east-" + feature["global_landmark_id"]
            feature["surveyed_world"][0] += 200.0
        path.write_text(json.dumps(value))
        rewrite_annotation_from_manifest(path)
    rewrite_registry_from_manifests(registry, manifests)

    with pytest.raises(SiteManifestError, match="disconnected camera islands"):
        aggregate_site_manifests(registry, manifests)


@pytest.mark.parametrize(
    "artifact_kind",
    ["annotations", "real_frame", "twin_frame", "intrinsics", "depth", "survey"],
)
def test_missing_or_tampered_retained_artifact_cannot_pass_by_hash_string(
    tmp_path, artifact_kind
):
    registry, manifests = fixture(tmp_path)
    manifest = json.loads(manifests[0].read_text())
    if artifact_kind in {"annotations", "real_frame", "twin_frame"}:
        path = Path(manifest["source_artifacts"][artifact_kind]["path"])
    elif artifact_kind == "intrinsics":
        path = Path(
            manifest["source_artifacts"]["intrinsics_source_images"][0]["path"]
        )
    elif artifact_kind == "depth":
        path = Path(manifest["depth_frame"]["path"])
    else:
        path = Path(manifest["features"][0]["survey_record_path"])
    path.write_bytes(path.read_bytes() + b"tampered")

    with pytest.raises(SiteManifestError, match="artifact"):
        aggregate_site_manifests(registry, manifests)


def test_nonexistent_artifact_path_is_rejected_not_accepted_from_declared_hash(tmp_path):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[0].read_text())
    value["source_artifacts"]["annotations"]["path"] = str(
        (tmp_path / "does-not-exist.json").resolve()
    )
    manifests[0].write_text(json.dumps(value))

    with pytest.raises(SiteManifestError, match="artifact is missing"):
        aggregate_site_manifests(registry, manifests)


@pytest.mark.parametrize(
    ("camera_index", "attack"),
    [(1, "annotation"), (2, "real_dimensions"), (3, "camera_config")],
)
def test_every_non_target_camera_is_semantically_reverified(
    tmp_path, camera_index, attack
):
    registry, manifests = fixture(tmp_path)
    manifest_path = manifests[camera_index]
    manifest = json.loads(manifest_path.read_text())
    if attack == "annotation":
        identity = manifest["source_artifacts"]["annotations"]
        path = Path(identity["path"])
        annotation = json.loads(path.read_text())
        annotation["points"][0]["description"] += " forged"
        raw = json.dumps(annotation, sort_keys=True).encode()
        path.write_bytes(raw)
        identity.update(
            sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
        )
        manifest["annotation_sha256"] = identity["sha256"]
    elif attack == "real_dimensions":
        real_identity = manifest["source_artifacts"]["real_frame"]
        real_path = Path(real_identity["path"])
        raw = BytesIO()
        Image.new("RGB", (320, 240), (1, 2, 3)).save(raw, "PNG")
        real_bytes = raw.getvalue()
        real_path.write_bytes(real_bytes)
        real_identity.update(
            sha256=hashlib.sha256(real_bytes).hexdigest(),
            size_bytes=len(real_bytes),
        )
        manifest["source_frame_sha256"] = real_identity["sha256"]
        annotation_identity = manifest["source_artifacts"]["annotations"]
        annotation_path = Path(annotation_identity["path"])
        annotation = json.loads(annotation_path.read_text())
        annotation["real_frame_sha256"] = real_identity["sha256"]
        annotation_bytes = json.dumps(annotation, sort_keys=True).encode()
        annotation_path.write_bytes(annotation_bytes)
        annotation_identity.update(
            sha256=hashlib.sha256(annotation_bytes).hexdigest(),
            size_bytes=len(annotation_bytes),
        )
        manifest["annotation_sha256"] = annotation_identity["sha256"]
    else:
        manifest["camera_config_sha256"] = "a" * 64
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(SiteManifestError, match="semantic binding failed"):
        aggregate_site_manifests(registry, manifests)
