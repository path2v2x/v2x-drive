"""Synthetic recovery and fail-closed tests for the road-geometry optimizer."""

import copy
import hashlib
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

TOOL = Path(__file__).resolve().parents[2] / "bridge" / "tools" / "optimize_twin_road_geometry.py"
SPEC = importlib.util.spec_from_file_location("optimize_twin_road_geometry", TOOL)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

from build_twin_calibration_manifest import (  # noqa: E402
    canonical_camera_sha256,
    depth_neighborhood_evidence,
    offline_depth_pixel_to_world,
)


class RoadGeometryOptimizerTests(unittest.TestCase):
    def optimize(self, manifest, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            manifest_bytes, report_bytes, _paths = self.site_bundle(
                manifest, Path(directory)
            )
            return MODULE.optimize_manifest(
                manifest,
                manifest_bytes=manifest_bytes,
                site_aggregation_report_bytes=report_bytes,
                external_evidence_verified=True,
                **kwargs,
            )

    @staticmethod
    def materialize_manifest_artifacts(manifest, directory, survey_directory=None):
        directory.mkdir(parents=True, exist_ok=True)
        survey_directory = survey_directory or (directory / "surveys")
        survey_directory.mkdir(parents=True, exist_ok=True)

        def artifact(name, payload):
            path = (directory / name).resolve()
            path.write_bytes(payload)
            return {
                "path": str(path),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }

        def image_bytes(size, color):
            output = BytesIO()
            Image.new("RGB", size, color).save(output, format="PNG")
            return output.getvalue()

        source_images = [
            artifact(f"intrinsics-{index}.png", payload)
            for index, payload in enumerate(
                RoadGeometryOptimizerTests.intrinsics_source_images()
            )
        ]
        manifest["intrinsics_calibration"]["source_images_sha256"] = [
            item["sha256"] for item in source_images
        ]
        manifest["intrinsics_calibration"]["image_count"] = len(source_images)
        calibration_payload = {
            key: value
            for key, value in manifest["intrinsics_calibration"].items()
            if key != "artifact_sha256"
        }
        intrinsics = artifact(
            "intrinsics.json",
            json.dumps(calibration_payload, sort_keys=True).encode(),
        )
        manifest["intrinsics_calibration"]["artifact_sha256"] = intrinsics["sha256"]
        camera_template = {
            "pitch_deg": -33.0,
            "yaw_deg": 0.0,
            "heading_deg": 178.0,
            "roll_deg": 0.0,
            "intrinsics": {
                "fx": 640.0,
                "fy": 640.0,
                "cx": 640.0,
                "cy": 480.0,
                "width": manifest["width"],
                "height": manifest["height"],
            },
            "twin_pose": {"fov_offset_deg": 2.0},
            "intrinsics_calibration": manifest["intrinsics_calibration"],
        }
        cameras_payload = {
            "cameras": [
                {"id": camera_id, **copy.deepcopy(camera_template)}
                for camera_id in ("ch1", "ch2", "ch3", "ch4")
            ]
        }
        cameras = artifact(
            "cameras.json", json.dumps(cameras_payload, sort_keys=True).encode()
        )
        camera = next(
            item for item in cameras_payload["cameras"]
            if item["id"] == manifest["camera_id"]
        )
        manifest["camera_config_sha256"] = canonical_camera_sha256(camera)

        for index, feature in enumerate(manifest["features"]):
            if feature.get("type") != "point":
                continue
            survey_path = (survey_directory / f"survey-{index}.json").resolve()
            survey_payload = json.dumps({
                "global_landmark_id": feature["global_landmark_id"],
                "surveyed_world": feature["surveyed_world"],
            }, sort_keys=True).encode()
            survey_path.write_bytes(survey_payload)
            survey = {
                "path": str(survey_path),
                "sha256": hashlib.sha256(survey_payload).hexdigest(),
                "size_bytes": len(survey_payload),
            }
            feature.update({
                "survey_record_sha256": survey["sha256"],
                "survey_record_path": survey["path"],
                "survey_record": survey,
            })

        depth_width, depth_height = 1280, 960
        baseline = manifest["baseline"]
        twin_params = np.array([
            *baseline["location"], baseline["pitch_deg"], baseline["yaw_deg"],
            baseline["roll_deg"], baseline["fov_deg"],
            depth_width / 2.0, depth_height / 2.0, 0.0,
        ])
        depth_values = np.full((depth_height, depth_width), 50.0, dtype=float)
        projected = {}
        for feature in manifest["features"]:
            worlds = (
                [feature["world"]]
                if feature["type"] == "point"
                else feature["world"]
            )
            pixels, depths = MODULE.project_calibration_points(
                worlds, twin_params, depth_width, depth_height
            )
            for pixel_value, depth_value in zip(pixels, depths):
                u, v = (int(round(pixel_value[0])), int(round(pixel_value[1])))
                if not (1 <= u < depth_width - 1 and 1 <= v < depth_height - 1):
                    raise AssertionError("synthetic twin evidence is outside depth frame")
                for dv in (-1, 0, 1):
                    for du in (-1, 0, 1):
                        existing = projected.get((u + du, v + dv))
                        if existing is not None and abs(existing - depth_value) > 1e-6:
                            raise AssertionError("synthetic depth neighborhoods conflict")
                        projected[(u + du, v + dv)] = float(depth_value)
            if feature["type"] == "point":
                feature["twin"] = pixels[0].tolist()
            else:
                feature["twin_polyline"] = pixels.tolist()
        for (u, v), depth_value in projected.items():
            depth_values[v, u] = depth_value

        normalized = np.rint(depth_values / 1000.0 * 16777215.0).astype(np.uint32)
        depth_array = np.zeros((depth_height, depth_width, 4), dtype=np.uint8)
        depth_array[:, :, 0] = ((normalized >> 16) & 255).astype(np.uint8)
        depth_array[:, :, 1] = ((normalized >> 8) & 255).astype(np.uint8)
        depth_array[:, :, 2] = (normalized & 255).astype(np.uint8)
        depth_payload = depth_array.tobytes()
        depth = artifact("depth.bgra", depth_payload)
        for feature in manifest["features"]:
            pixels = (
                [feature["twin"]]
                if feature["type"] == "point"
                else feature["twin_polyline"]
            )
            evidence = [
                depth_neighborhood_evidence(
                    depth_payload, depth_width, depth_height, *pixel_value
                )
                for pixel_value in pixels
            ]
            worlds = [
                offline_depth_pixel_to_world(
                    baseline, pixel_value[0], pixel_value[1],
                    depth_evidence["center_depth_m"], baseline["fov_deg"],
                    depth_width, depth_height,
                )
                for pixel_value, depth_evidence in zip(pixels, evidence)
            ]
            if feature["type"] == "point":
                feature["world"] = worlds[0]
                feature["depth_neighborhood"] = evidence[0]
            else:
                feature["world"] = worlds
                feature["depth_neighborhoods"] = evidence

        real_frame = artifact(
            "real-frame.png",
            image_bytes((manifest["width"], manifest["height"]), (10, 20, 30)),
        )
        twin_frame = artifact(
            "twin-frame.png", image_bytes((depth_width, depth_height), (30, 20, 10))
        )
        annotations_payload = {
            "camera_id": manifest["camera_id"],
            "real_frame_sha256": real_frame["sha256"],
            "twin_frame_sha256": twin_frame["sha256"],
            "cameras_file_sha256": cameras["sha256"],
            "points": [],
            "roads": [],
        }
        for feature in manifest["features"]:
            keys = (
                (
                    "id", "global_landmark_id", "surveyed_world",
                    "survey_record_sha256", "survey_record_path", "split",
                    "provenance", "category", "description", "twin", "image",
                )
                if feature["type"] == "point"
                else (
                    "id", "split", "provenance", "category", "description",
                    "twin_polyline", "image_polyline",
                )
            )
            target = (
                annotations_payload["points"]
                if feature["type"] == "point"
                else annotations_payload["roads"]
            )
            target.append({key: copy.deepcopy(feature[key]) for key in keys})
        annotations = artifact(
            "annotations.json",
            json.dumps(annotations_payload, sort_keys=True).encode(),
        )
        manifest.update({
            "annotation_sha256": annotations["sha256"],
            "source_frame_sha256": real_frame["sha256"],
            "twin_frame_sha256": twin_frame["sha256"],
            "cameras_file_sha256": cameras["sha256"],
            "source_artifacts": {
                "annotations": annotations,
                "real_frame": real_frame,
                "twin_frame": twin_frame,
                "cameras_file": cameras,
                "intrinsics_artifact": intrinsics,
                "intrinsics_source_images": source_images,
            },
        })
        manifest["depth_frame"].update({
            "width": depth_width,
            "height": depth_height,
            "path": depth["path"],
            "raw_data_sha256": depth["sha256"],
            "raw_data_size": depth["size_bytes"],
        })
        return manifest

    @staticmethod
    def site_bundle(manifest, directory):
        directory.mkdir(parents=True, exist_ok=True)
        selected_camera = manifest["camera_id"]
        survey_directory = directory / "surveys"
        manifest_paths = []
        manifest_bytes = None
        selected_manifest = None
        for camera_id in ("ch1", "ch2", "ch3", "ch4"):
            camera_manifest = copy.deepcopy(manifest)
            camera_manifest["camera_id"] = camera_id
            RoadGeometryOptimizerTests.materialize_manifest_artifacts(
                camera_manifest,
                directory / "artifacts" / camera_id,
                survey_directory,
            )
            raw = json.dumps(camera_manifest, sort_keys=True).encode()
            path = directory / f"{camera_id}.json"
            path.write_bytes(raw)
            manifest_paths.append(path)
            if camera_id == selected_camera:
                manifest_bytes = raw
                selected_manifest = camera_manifest
        manifest.clear()
        manifest.update(copy.deepcopy(selected_manifest))
        point_features = [
            feature for feature in manifest["features"]
            if feature.get("type") == "point"
        ]
        registry = directory / "registry.json"
        registry.write_text(json.dumps({
            "schema": "v2x-site-landmark-registry/v1",
            "cameras_file_sha256": manifest["cameras_file_sha256"],
            "landmarks": [
                {
                    "global_landmark_id": feature["global_landmark_id"],
                    "split": feature["split"],
                    "surveyed_world": feature["surveyed_world"],
                    "survey_record_sha256": feature["survey_record_sha256"],
                    "survey_record_path": feature["survey_record_path"],
                    "survey_record_size_bytes": feature["survey_record"][
                        "size_bytes"
                    ],
                }
                for feature in point_features
            ],
        }, sort_keys=True))
        report = MODULE.aggregate_site_manifests(registry, manifest_paths)
        report_bytes = json.dumps(report, sort_keys=True).encode()
        return manifest_bytes, report_bytes, {
            "registry": registry,
            "manifests": manifest_paths,
        }

    @staticmethod
    def intrinsics_source_images():
        payloads = []
        for index in range(24):
            output = BytesIO()
            Image.new("RGB", (1280, 960), (index, 0, 0)).save(output, format="PNG")
            payloads.append(output.getvalue())
        return payloads

    def synthetic_manifest(self):
        width, height = 1280, 960
        truth = [-35.0, 90.0, 2.0, 90.0, 640.0, 480.0, 0.0]
        location = [0.0, 0.0, 8.0]
        worlds = [
            [-10, 12, 0], [10, 12, 0], [-10, 50, 0], [10, 50, 0],
            [-8, 20, 0], [8, 20, 0], [-8, 35, 0], [8, 35, 0],
            [-12, 12, 0], [12, 12, 0], [-12, 50, 0], [12, 50, 0],
        ]
        pixels, depth = MODULE.project_world_points(worlds, location, truth, width, height)
        self.assertTrue(np.all(depth > 0.1))
        features = []
        for index, (world, pixel) in enumerate(zip(worlds[:12], pixels[:12])):
            features.append({
                "id": f"point-{index}", "type": "point",
                "global_landmark_id": f"rfs-landmark-{index:02d}",
                "surveyed_world": [float(index), float(index * 2), 0.5],
                "survey_record_sha256": hashlib.sha256(
                    f"survey-{index}".encode()
                ).hexdigest(),
                "split": "train" if index < 8 else "holdout",
                "world": world, "image": pixel.tolist(),
                "twin": pixel.tolist(),
                "category": "static_landmark",
                "description": f"Unique synthetic landmark number {index}",
                "provenance": "manually_verified_unique",
                "depth_neighborhood": {"center_depth_m": float(depth[index])},
            })
        line_specs = [
            ("a", [[-10, 12, 0], [0, 12, 0], [10, 12, 0]]),
            ("b", [[-10, 12, 0], [-10, 30, 0], [-10, 50, 0]]),
            ("c", [[-10, 12, 0], [0, 31, 0], [10, 50, 0]]),
            ("d", [[-10, 50, 0], [0, 50, 0], [10, 50, 0]]),
            ("e", [[10, 12, 0], [10, 30, 0], [10, 50, 0]]),
        ]
        for index, (name, world) in enumerate(line_specs):
            projected, _ = MODULE.project_world_points(world, location, truth, width, height)
            features.append({
                "id": f"line-{name}", "type": "polyline",
                "split": "train" if index < 3 else "holdout",
                "world": world,
                "twin_polyline": projected.tolist(),
                "image_polyline": projected.tolist(),
                "category": "road_edge",
                "description": f"Unique synthetic road edge named {name}",
                "provenance": "manually_traced_geometry",
                "depth_neighborhoods": [
                    {"center_depth_m": 10.0} for _point in world
                ],
            })
        return {
            "schema_version": 1,
            "camera_id": "ch1", "width": width, "height": height,
            "source_frame_sha256": hashlib.sha256(b"frame").hexdigest(),
            "twin_frame_sha256": hashlib.sha256(b"twin").hexdigest(),
            "annotation_sha256": hashlib.sha256(b"annotations").hexdigest(),
            "cameras_file_sha256": hashlib.sha256(b"cameras").hexdigest(),
            "camera_config_sha256": hashlib.sha256(b"camera").hexdigest(),
            "ue5_map": "Carla/Maps/Richmond_Field_Station_Richmond_CA",
            "ue5_map_opendrive_sha256": hashlib.sha256(b"opendrive").hexdigest(),
            "projection": {
                "source": "opendrive_georeference",
                "strict": True,
                "map_origin_error_m": 0.1,
                "map_name": "Carla/Maps/Richmond_Field_Station_Richmond_CA",
                "opendrive_sha256": hashlib.sha256(b"opendrive").hexdigest(),
                "georeference_sha256": hashlib.sha256(b"georeference").hexdigest(),
            },
            "depth_frame": {
                "carla_frame": 123,
                "sensor_timestamp": 45.5,
                "width": 1280,
                "height": 960,
                "raw_data_sha256": hashlib.sha256(b"depth-frame").hexdigest(),
                "raw_data_size": 1280 * 960 * 4,
            },
            "baseline": {
                "location": location, "pitch_deg": -33.0, "yaw_deg": 88.0,
                "roll_deg": 0.0, "fov_deg": 92.0, "cx": 640.0,
                "cy": 480.0, "k1": 0.0,
            },
            "deployment_model": {
                "type": "twin_camera_rig_v1",
                "anchor_location": location,
                "base": {
                    "pitch_deg": -33.0,
                    "yaw_deg": 88.0,
                    "roll_deg": 0.0,
                    "fov_deg": 90.0,
                },
                "lens": {
                    "lens_k": -1.0,
                    "lens_kcube": 0.0,
                    "lens_circle_falloff": 5.0,
                    "lens_circle_multiplier": 0.0,
                    "lens_x_size": 0.08,
                    "lens_y_size": 0.08,
                },
            },
            "intrinsics_calibration": {
                "method": "charuco",
                "artifact_sha256": hashlib.sha256(b"intrinsics").hexdigest(),
                "image_count": 24,
                "source_images_sha256": [
                    hashlib.sha256(payload).hexdigest()
                    for payload in self.intrinsics_source_images()
                ],
                "rms_reprojection_error_px": 0.3,
                "resolution": [width, height],
                "camera_matrix": [
                    [640.0, 0.0, 640.0],
                    [0.0, 640.0, 480.0],
                    [0.0, 0.0, 1.0],
                ],
                "distortion": {
                    "k1": 0.0,
                    "k2": 0.0,
                    "p1": 0.0,
                    "p2": 0.0,
                    "k3": 0.0,
                },
            },
            "features": features,
        }

    def test_recovers_synthetic_camera_and_passes_holdout(self):
        report = self.optimize(self.synthetic_manifest())
        self.assertTrue(report["passed"], report)
        self.assertLess(report["heldout"]["points"]["rmse_px"], 1.0)
        self.assertLess(report["heldout"]["lines"]["rmse_px"], 1.0)
        self.assertTrue(report["deployability"]["passed"], report)
        self.assertTrue(report["identifiability"]["passed"], report)
        self.assertLessEqual(
            report["deployability"]["optical_roundtrip_max_px"], 0.25
        )

    def test_recovers_translated_camera_and_roundtrips_to_twin_pose(self):
        manifest = self.synthetic_manifest()
        truth = np.array([0.7, -0.4, 8.3, -35.0, 90.0, 2.0, 90.0, 640.0, 480.0, 0.0])
        for feature in manifest["features"]:
            world = [feature["world"]] if feature["type"] == "point" else feature["world"]
            pixels, depth = MODULE.project_calibration_points(
                world, truth, manifest["width"], manifest["height"]
            )
            self.assertTrue(np.all(depth > 0.1))
            if feature["type"] == "point":
                feature["image"] = pixels[0].tolist()
            else:
                feature["image_polyline"] = pixels.tolist()
        report = self.optimize(manifest)
        self.assertTrue(report["passed"], report)
        self.assertLess(report["deployability"]["transform_roundtrip_max"], 1e-6)
        fitted = report["parameters"]
        self.assertAlmostEqual(fitted["location_x"], truth[0], delta=0.1)
        self.assertAlmostEqual(fitted["location_y"], truth[1], delta=0.1)
        self.assertAlmostEqual(fitted["location_z"], truth[2], delta=0.1)

    def test_good_fit_with_unrepresentable_optics_cannot_pass_deployment(self):
        manifest = self.synthetic_manifest()
        truth = [-35.0, 90.0, 2.0, 90.0, 700.0, 430.0, -0.25]
        location = manifest["baseline"]["location"]
        for feature in manifest["features"]:
            world = [feature["world"]] if feature["type"] == "point" else feature["world"]
            pixels, _ = MODULE.project_world_points(
                world, location, truth, manifest["width"], manifest["height"]
            )
            if feature["type"] == "point":
                feature["image"] = pixels[0].tolist()
            else:
                feature["image_polyline"] = pixels.tolist()
        report = self.optimize(manifest)
        self.assertFalse(report["passed"], report)
        self.assertFalse(report["deployability"]["passed"])
        self.assertIn(
            "measured_physical_optics_not_representable_in_ue5",
            report["deployability"]["reasons"],
        )
        unconstrained = report["unconstrained_diagnostic"]
        self.assertFalse(unconstrained["deployability"]["passed"])
        self.assertIn(
            "unrepresentable_principal_point_or_radial_distortion",
            unconstrained["deployability"]["reasons"],
        )
        self.assertLess(unconstrained["heldout"]["points"]["rmse_px"], 5.0)
        self.assertTrue(any(reason.startswith("heldout_") for reason in report["reasons"]))

    def test_rejects_missing_independent_evidence(self):
        manifest = self.synthetic_manifest()
        manifest["features"] = manifest["features"][:4]
        gate = MODULE.manifest_gate(manifest)
        self.assertFalse(gate["passed"])
        self.assertIn("insufficient_train_points", gate["reasons"])

    def test_rejects_manifest_without_frozen_deployment_model(self):
        manifest = self.synthetic_manifest()
        manifest.pop("deployment_model")
        gate = MODULE.manifest_gate(manifest)
        self.assertFalse(gate["passed"])
        self.assertIn("missing_deployment_model", gate["reasons"])

    def test_rejects_manifest_without_measured_intrinsics(self):
        manifest = self.synthetic_manifest()
        manifest.pop("intrinsics_calibration")
        gate = MODULE.manifest_gate(manifest)
        self.assertFalse(gate["passed"])
        self.assertIn(
            "missing_measured_intrinsics_calibration",
            gate["reasons"],
        )

    def test_rejects_untraceable_intrinsics_source_images(self):
        manifest = self.synthetic_manifest()
        manifest["intrinsics_calibration"]["source_images_sha256"] = ["a" * 64] * 24
        gate = MODULE.manifest_gate(manifest)
        self.assertFalse(gate["passed"])
        self.assertIn(
            "invalid_measured_intrinsics_calibration",
            gate["reasons"],
        )

    def test_measured_distortion_blocks_otherwise_deployable_fit(self):
        manifest = self.synthetic_manifest()
        manifest["intrinsics_calibration"]["distortion"]["k1"] = -0.1
        report = self.optimize(manifest)
        self.assertFalse(report["passed"], report)
        self.assertIn(
            "measured_physical_optics_not_representable_in_ue5",
            report["reasons"],
        )

    def test_identifiability_rejects_rank_deficient_geometry(self):
        manifest = self.synthetic_manifest()
        report = self.optimize(manifest)
        params = np.array([report["parameters"][key] for key in MODULE.PARAMETER_NAMES])
        for feature in manifest["features"]:
            if feature["split"] != "train":
                continue
            if feature["type"] == "point":
                feature["world"] = [0.0, 25.0, 0.0]
            else:
                feature["world"] = [[-1.0, 25.0, 0.0], [1.0, 25.0, 0.0]]
        metrics = MODULE.deployment_identifiability(
            manifest, params, np.array([1, 1, 1, 5, 5, 3, 8, 30, 30, 0.1])
        )
        self.assertFalse(metrics["passed"])
        self.assertLess(metrics["rank"], metrics["required_rank"])

    def test_rejects_hand_authored_manifest_without_builder_fingerprints(self):
        manifest = self.synthetic_manifest()
        manifest.pop("annotation_sha256")
        manifest.pop("depth_frame")
        gate = MODULE.manifest_gate(manifest)
        self.assertFalse(gate["passed"])
        self.assertIn(
            "missing_annotation_sha256", gate["reasons"]
        )
        self.assertIn(
            "missing_depth_frame_identity", gate["reasons"]
        )

    def test_optimizer_manifest_gate_rejects_missing_malformed_and_fallback_projection(self):
        projections = (
            None,
            {"source": "opendrive_georeference", "strict": True},
            {**self.synthetic_manifest()["projection"], "source": "origin_centered_fallback", "strict": False},
        )
        for projection in projections:
            with self.subTest(projection=projection):
                manifest = self.synthetic_manifest()
                manifest["projection"] = projection
                gate = MODULE.manifest_gate(manifest)
                self.assertFalse(gate["passed"])
                self.assertIn(
                    "invalid_strict_opendrive_projection_provenance",
                    gate["reasons"],
                )

    def test_polyline_distance_follows_segments_not_infinite_extension(self):
        points = np.array([[0.5, 1.0], [3.0, 0.0]])
        polyline = np.array([[0.0, 0.0], [1.0, 0.0]])
        distances = MODULE.point_to_polyline_distances(points, polyline)
        self.assertTrue(np.allclose(distances, [1.0, 2.0]))

    def test_heldout_polyline_behind_camera_fails_closed(self):
        manifest = self.synthetic_manifest()
        report = self.optimize(manifest)
        self.assertTrue(report["passed"], report)
        heldout = next(
            feature for feature in manifest["features"]
            if feature["type"] == "polyline" and feature["split"] == "holdout"
        )
        heldout["world"] = [
            [0.0, -10.0, 8.0],
            [0.5, -10.0, 8.0],
            [1.0, -10.0, 8.0],
        ]
        params = np.array([
            report["parameters"][key] for key in MODULE.PARAMETER_NAMES
        ])
        metrics = MODULE.evaluate_split(manifest, params, "holdout")
        self.assertGreaterEqual(metrics["lines"]["max_px"], 5000.0)

    def test_nonfinite_metrics_are_replaced_by_fail_closed_sentinel(self):
        metrics = MODULE.point_metrics([1.0, float("nan")])
        self.assertEqual(metrics["nonfinite_count"], 1)
        self.assertEqual(metrics["max_px"], 5000.0)

    def test_infinite_line_manifest_is_rejected(self):
        manifest = self.synthetic_manifest()
        road = next(feature for feature in manifest["features"] if feature["type"] == "polyline")
        road["type"] = "line"
        road["image_line"] = [*road.pop("image_polyline")[0], *[0.0, 0.0]]
        gate = MODULE.manifest_gate(manifest)
        self.assertFalse(gate["passed"])
        self.assertIn("infinite_line_evidence_not_allowed", gate["reasons"])

    def test_optimizer_refuses_unbound_direct_manifest(self):
        report = MODULE.optimize_manifest(self.synthetic_manifest())
        self.assertFalse(report["passed"])
        self.assertEqual(report["reason"], "external_evidence_not_verified")

    def test_optimizer_refuses_missing_site_aggregation_even_if_external_bound(self):
        manifest = self.synthetic_manifest()
        manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
        report = MODULE.optimize_manifest(
            manifest,
            manifest_bytes=manifest_bytes,
            external_evidence_verified=True,
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["reason"], "site_aggregation_gate")

    def test_optimizer_refuses_stale_registry_map_and_resolved_world_bundles(self):
        mutations = ("registry", "map", "resolved_world")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw:
                manifest = self.synthetic_manifest()
                manifest_bytes, report_bytes, paths = self.site_bundle(
                    manifest, Path(raw)
                )
                if mutation == "registry":
                    registry = json.loads(paths["registry"].read_text())
                    registry["landmarks"][0]["survey_record_sha256"] = "f" * 64
                    paths["registry"].write_text(json.dumps(registry))
                else:
                    other = paths["manifests"][1]
                    payload = json.loads(other.read_text())
                    if mutation == "map":
                        payload["ue5_map_opendrive_sha256"] = "f" * 64
                        payload["projection"]["opendrive_sha256"] = "f" * 64
                    else:
                        payload["features"][0]["world"][0] += 0.251
                    other.write_text(json.dumps(payload))
                report = MODULE.optimize_manifest(
                    manifest,
                    manifest_bytes=manifest_bytes,
                    site_aggregation_report_bytes=report_bytes,
                    external_evidence_verified=True,
                )
                self.assertFalse(report["passed"], report)
                self.assertEqual(report["reason"], "site_aggregation_gate")

    def test_optimizer_refuses_relabelled_landmark_after_aggregation(self):
        with tempfile.TemporaryDirectory() as raw:
            original = self.synthetic_manifest()
            _manifest_bytes, report_bytes, _paths = self.site_bundle(
                original, Path(raw)
            )
            relabelled = json.loads(json.dumps(original))
            relabelled["features"][0]["global_landmark_id"] = (
                relabelled["features"][1]["global_landmark_id"]
            )
            relabelled_bytes = json.dumps(relabelled, sort_keys=True).encode()
            report = MODULE.optimize_manifest(
                relabelled,
                manifest_bytes=relabelled_bytes,
                site_aggregation_report_bytes=report_bytes,
                external_evidence_verified=True,
            )
            self.assertFalse(report["passed"], report)
            self.assertEqual(report["reason"], "site_aggregation_gate")

    def test_optimizer_independently_rehashes_retained_manifest_artifacts(self):
        with tempfile.TemporaryDirectory() as raw:
            manifest = self.synthetic_manifest()
            manifest_bytes, report_bytes, _paths = self.site_bundle(
                manifest, Path(raw)
            )
            annotation_path = Path(
                manifest["source_artifacts"]["annotations"]["path"]
            )
            annotation_path.write_bytes(annotation_path.read_bytes() + b"tampered")

            report = MODULE.optimize_manifest(
                manifest,
                manifest_bytes=manifest_bytes,
                site_aggregation_report_bytes=report_bytes,
                external_evidence_verified=True,
            )

            self.assertFalse(report["passed"], report)
            self.assertEqual(report["reason"], "site_aggregation_gate")
            self.assertTrue(any(
                reason.startswith("retained_artifact_identity_mismatch:annotations")
                for reason in report["reasons"]
            ))

    def test_external_evidence_rebinds_every_retained_artifact(self):
        manifest = self.synthetic_manifest()
        retained_directory = tempfile.TemporaryDirectory()
        self.addCleanup(retained_directory.cleanup)
        self.materialize_manifest_artifacts(
            manifest, Path(retained_directory.name)
        )
        annotations_payload = {"points": [], "roads": []}
        for feature in manifest["features"]:
            if feature["type"] == "point":
                annotations_payload["points"].append({
                    key: feature[key]
                    for key in (
                        "id", "global_landmark_id", "surveyed_world",
                        "survey_record_sha256", "survey_record_path",
                        "split", "provenance",
                        "category", "description", "twin", "image",
                    )
                })
            else:
                annotations_payload["roads"].append({
                    key: feature[key]
                    for key in (
                        "id", "split", "provenance", "category",
                        "description",
                        "twin_polyline", "image_polyline",
                    )
                })
        annotations = json.dumps(annotations_payload).encode()
        real_frame = b"real-frame"
        twin_frame = b"twin-frame"
        artifact_payload = {
            key: value
            for key, value in manifest["intrinsics_calibration"].items()
            if key != "artifact_sha256"
        }
        artifact = json.dumps(artifact_payload).encode()
        manifest["intrinsics_calibration"]["artifact_sha256"] = hashlib.sha256(
            artifact
        ).hexdigest()
        camera = {
            "id": manifest["camera_id"],
            "pitch_deg": -33.0,
            "yaw_deg": 0.0,
            "heading_deg": 178.0,
            "roll_deg": 0.0,
            "intrinsics": {
                "fx": 640.0,
                "fy": 640.0,
                "cx": 640.0,
                "cy": 480.0,
                "width": 1280,
                "height": 960,
            },
            "twin_pose": {"fov_offset_deg": 2.0},
            "intrinsics_calibration": manifest["intrinsics_calibration"],
        }
        cameras = json.dumps({"cameras": [camera]}).encode()
        depth_frame = b"\0" * manifest["depth_frame"]["raw_data_size"]
        Path(manifest["depth_frame"]["path"]).write_bytes(depth_frame)
        manifest["depth_frame"].update(
            raw_data_sha256=hashlib.sha256(depth_frame).hexdigest(),
            raw_data_size=len(depth_frame),
        )
        manifest.update({
            "annotation_sha256": hashlib.sha256(annotations).hexdigest(),
            "source_frame_sha256": hashlib.sha256(real_frame).hexdigest(),
            "twin_frame_sha256": hashlib.sha256(twin_frame).hexdigest(),
            "cameras_file_sha256": hashlib.sha256(cameras).hexdigest(),
            "camera_config_sha256": hashlib.sha256(json.dumps(
                camera, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode()).hexdigest(),
        })

        def rewrite_source(key, payload):
            identity = manifest["source_artifacts"][key]
            path = Path(identity["path"])
            path.write_bytes(payload)
            identity.update(
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
            )

        rewrite_source("annotations", annotations)
        rewrite_source("real_frame", real_frame)
        rewrite_source("twin_frame", twin_frame)
        rewrite_source("cameras_file", cameras)
        rewrite_source("intrinsics_artifact", artifact)
        kwargs = {
            "annotations_bytes": annotations,
            "real_frame_bytes": real_frame,
            "twin_frame_bytes": twin_frame,
            "cameras_bytes": cameras,
            "intrinsics_artifact_bytes": artifact,
            "intrinsics_source_image_bytes": self.intrinsics_source_images(),
            "depth_frame_bytes": depth_frame,
            "runtime_evidence": {
                "ue5_map": manifest["ue5_map"],
                "ue5_map_opendrive_sha256": manifest[
                    "ue5_map_opendrive_sha256"
                ],
                "projection": manifest["projection"],
                "endpoint": {"host": "127.0.0.1", "port": 2000},
                "fresh_depth_frame": {
                    "carla_frame": 124,
                    "sensor_timestamp": 46.0,
                    "raw_data_sha256": hashlib.sha256(b"fresh").hexdigest(),
                },
                "baseline": manifest["baseline"],
                "deployment_model": manifest["deployment_model"],
                "feature_worlds": {
                    feature["id"]: feature["world"]
                    for feature in manifest["features"]
                },
            },
            "external_evidence_paths": {
                "annotations": manifest["source_artifacts"]["annotations"]["path"],
                "real_frame": manifest["source_artifacts"]["real_frame"]["path"],
                "twin_frame": manifest["source_artifacts"]["twin_frame"]["path"],
                "cameras_file": manifest["source_artifacts"]["cameras_file"]["path"],
                "intrinsics_artifact": manifest["source_artifacts"][
                    "intrinsics_artifact"
                ]["path"],
                "intrinsics_source_images": [
                    item["path"] for item in manifest["source_artifacts"][
                        "intrinsics_source_images"
                    ]
                ],
                "depth_frame": manifest["depth_frame"]["path"],
            },
        }
        valid_gate = MODULE.verify_external_evidence(manifest, **kwargs)
        self.assertTrue(valid_gate["passed"], valid_gate)
        alternate_annotations = Path(retained_directory.name) / "same-bytes-new-path.json"
        alternate_annotations.write_bytes(annotations)
        wrong_path_kwargs = copy.deepcopy(kwargs)
        wrong_path_kwargs["external_evidence_paths"]["annotations"] = str(
            alternate_annotations.resolve()
        )
        gate = MODULE.verify_external_evidence(manifest, **wrong_path_kwargs)
        self.assertFalse(gate["passed"])
        self.assertIn(
            "external_evidence_path_mismatch:annotations", gate["reasons"]
        )
        unsafe_camera = json.loads(json.dumps(camera))
        unsafe_camera["twin_lens"] = {"lens_k": -1.0}
        unsafe_cameras = json.dumps({"cameras": [unsafe_camera]}).encode()
        unsafe_manifest = json.loads(json.dumps(manifest))
        unsafe_manifest["cameras_file_sha256"] = hashlib.sha256(
            unsafe_cameras
        ).hexdigest()
        unsafe_manifest["camera_config_sha256"] = hashlib.sha256(json.dumps(
            unsafe_camera,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()).hexdigest()
        unsafe_kwargs = dict(kwargs)
        unsafe_kwargs["cameras_bytes"] = unsafe_cameras
        gate = MODULE.verify_external_evidence(unsafe_manifest, **unsafe_kwargs)
        self.assertFalse(gate["passed"])
        self.assertIn("camera_lens_override_safety_hold", gate["reasons"])
        tampered = json.loads(json.dumps(manifest))
        tampered["intrinsics_calibration"]["distortion"]["k1"] = 0.123
        gate = MODULE.verify_external_evidence(tampered, **kwargs)
        self.assertFalse(gate["passed"])
        self.assertIn("intrinsics_calibration_config_mismatch", gate["reasons"])
        tampered_feature = json.loads(json.dumps(manifest))
        tampered_feature["features"][0]["image"][0] += 20.0
        gate = MODULE.verify_external_evidence(tampered_feature, **kwargs)
        self.assertFalse(gate["passed"])
        self.assertIn("manifest_features_annotation_mismatch", gate["reasons"])
        relabelled_feature = json.loads(json.dumps(manifest))
        relabelled_feature["features"][0]["global_landmark_id"] = (
            relabelled_feature["features"][1]["global_landmark_id"]
        )
        gate = MODULE.verify_external_evidence(relabelled_feature, **kwargs)
        self.assertFalse(gate["passed"])
        self.assertIn("manifest_features_annotation_mismatch", gate["reasons"])
        tampered_world = json.loads(json.dumps(manifest))
        tampered_world["features"][0]["world"][0] += 1.0
        gate = MODULE.verify_external_evidence(tampered_world, **kwargs)
        self.assertFalse(gate["passed"])
        self.assertTrue(any(
            reason.startswith("runtime_feature_world_mismatch:")
            for reason in gate["reasons"]
        ))
        bad_runtime_kwargs = dict(kwargs)
        bad_runtime_kwargs["runtime_evidence"] = json.loads(json.dumps(
            kwargs["runtime_evidence"]
        ))
        bad_runtime_kwargs["runtime_evidence"]["ue5_map_opendrive_sha256"] = "f" * 64
        gate = MODULE.verify_external_evidence(manifest, **bad_runtime_kwargs)
        self.assertFalse(gate["passed"])
        self.assertIn("runtime_ue5_map_content_mismatch", gate["reasons"])
        bad_depth_kwargs = dict(kwargs)
        bad_depth_kwargs["depth_frame_bytes"] = kwargs["depth_frame_bytes"][:-4]
        gate = MODULE.verify_external_evidence(manifest, **bad_depth_kwargs)
        self.assertFalse(gate["passed"])
        self.assertIn("depth_frame_sha256_mismatch", gate["reasons"])


if __name__ == "__main__":
    unittest.main()
