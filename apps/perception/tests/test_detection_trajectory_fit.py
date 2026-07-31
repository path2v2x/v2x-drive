import hashlib
import math
from pathlib import Path
import sys
import unittest

import cv2
import numpy as np


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from detection_trajectory_fit import (  # noqa: E402
    TrajectoryFitError,
    fit_detection_constraints,
)


CAMERAS = ("ch1", "ch2", "ch3", "ch4")


def world_to_camera(position, target):
    position = np.asarray(position, dtype=float)
    forward = np.asarray(target, dtype=float) - position
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, np.asarray([0.0, 0.0, 1.0]))
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    rotation = np.vstack([right, down, forward])
    rvec, _ = cv2.Rodrigues(rotation)
    return rvec.reshape(3), -rotation @ position


def project(point, rvec, tvec, matrix):
    pixels, _ = cv2.projectPoints(
        np.asarray([[point[0], point[1], 0.0]], dtype=float),
        rvec,
        tvec,
        matrix,
        np.zeros(5),
    )
    return pixels.reshape(2).tolist()


class DetectionTrajectoryFitTests(unittest.TestCase):
    def fixture(self):
        matrix = np.asarray([[900.0, 0.0, 500.0], [0.0, 900.0, 400.0], [0.0, 0.0, 1.0]])
        camera_positions = {
            "ch1": [-18.0, -18.0, 12.0],
            "ch2": [18.0, -18.0, 12.0],
            "ch3": [-18.0, 18.0, 12.0],
            "ch4": [18.0, 18.0, 12.0],
        }
        cameras = {}
        static_cameras = {}
        truth = {}
        for index, camera_id in enumerate(CAMERAS):
            rvec, tvec = world_to_camera(camera_positions[camera_id], [0.0, 5.0, 0.0])
            truth[camera_id] = (rvec, tvec)
            cameras[camera_id] = {
                "intrinsics_calibration": {
                    "camera_matrix": matrix.tolist(),
                    "distortion": {"k1": 0.0, "k2": 0.0, "p1": 0.0, "p2": 0.0, "k3": 0.0},
                }
            }
            # Start inside the conservative diagnostic bounds but measurably
            # wrong in both rotation and translation.
            static_cameras[camera_id] = {
                "world_to_camera_rvec": (rvec + np.asarray([0.006, -0.004, 0.005])).tolist(),
                "world_to_camera_tvec_m": (tvec + np.asarray([0.10, -0.08, 0.12])).tolist(),
                "pose_prior_sigma": {"translation_m": 0.25, "rotation_deg": 0.5},
                "diagnostic_bounds": {"translation_m": 0.75, "rotation_deg": 2.0},
            }
        config_hash = hashlib.sha256(b"measured-cameras").hexdigest()
        static_solution = {
            "schema": "v2x-static-camera-solution/v1",
            "source_cameras_json_sha256": config_hash,
            "site_frame": "surveyed_enu_z_up",
            "truth": {
                "kind": "surveyed_static_geometry",
                "heldout_gate_passed": True,
                "manifest_sha256": hashlib.sha256(b"static-truth").hexdigest(),
            },
            "site_to_map_transform": {
                "frozen": True,
                "model": "se2_fixed_scale",
                "artifact_sha256": hashlib.sha256(b"site-transform").hexdigest(),
            },
            "cameras": static_cameras,
        }
        lane_paths = {
            "east-0": [[-15.0, 0.0], [15.0, 0.0]],
            "east-7": [[-15.0, 7.0], [15.0, 7.0]],
            "north-5": [[5.0, -8.0], [5.0, 20.0]],
            "turn": [[-12.0, -3.0], [-3.0, -3.0], [2.0, 2.0], [2.0, 14.0]],
        }
        lane_map = {
            "schema": "v2x-surveyed-lane-map/v1",
            "site_frame": "surveyed_enu_z_up",
            "independent_of_detections": True,
            "survey_accuracy_m": 0.05,
            "survey_manifest_sha256": hashlib.sha256(b"lane-survey").hexdigest(),
            "lane_paths": lane_paths,
        }
        tracks = []
        for camera_index, camera_id in enumerate(CAMERAS):
            rvec, tvec = truth[camera_id]
            for track_index in range(32):
                lane_id = tuple(lane_paths)[track_index % 4]
                phase = (track_index // 4) % 6
                if lane_id == "east-0":
                    points = [[-12.0 + phase + step * 1.5, 0.0] for step in range(3)]
                elif lane_id == "east-7":
                    points = [[-10.0 + phase + step * 1.3, 7.0] for step in range(3)]
                elif lane_id == "north-5":
                    points = [[5.0, -5.0 + phase + step * 1.2] for step in range(3)]
                else:
                    # A straight portion of a globally turning path remains a
                    # constant-velocity track while adding directional diversity.
                    points = [[2.0, 5.0 + phase + step * 1.1] for step in range(3)]
                direction = 90.0 if lane_id.startswith("east") else 0.0
                event_ids = [f"{camera_id}-{track_index}-{step}" for step in range(3)]
                tracks.append({
                    "tracklet_id": f"{camera_id}-track-{track_index}",
                    "camera_id": camera_id,
                    "event_ids": event_ids,
                    "pixels": [project(point, rvec, tvec, matrix) for point in points],
                    "times_epoch": [1000.0 + track_index + step * 0.25 for step in range(3)],
                    "covariances_px2": [
                        [[4.0, 0.0], [0.0, 4.0]] for _ in points
                    ],
                    "lane_path_id": lane_id,
                    "motion_direction_deg": direction,
                    "includes_turn": lane_id == "turn",
                    "split": (
                        "fit"
                        if track_index < 28
                        else "validation"
                        if track_index < 30
                        else "holdout"
                    ),
                })
        return cameras, static_solution, lane_map, tracks, config_hash

    def test_synthetic_fit_improves_data_without_claiming_acceptance(self):
        cameras, static_solution, lane_map, tracks, config_hash = self.fixture()
        report = fit_detection_constraints(
            cameras=cameras,
            static_solution=static_solution,
            lane_map=lane_map,
            tracks=tracks,
            synchronized_pairs=[],
            cameras_json_sha256=config_hash,
            multistarts=2,
        )
        self.assertTrue(report["fit_completed"], report["reasons"])
        self.assertLess(
            report["objective"]["final_data_rms_normalized"],
            report["objective"]["initial_data_rms_normalized"],
        )
        self.assertFalse(report["acceptance_eligible"])
        self.assertFalse(report["contract"]["derived_gps_parsed"])
        self.assertEqual(set(report["cameras"]), set(CAMERAS))
        self.assertEqual(
            set(report["objective"]["splits"]["holdout"]["cameras"]),
            set(CAMERAS),
        )

    def test_refuses_unanchored_static_solution(self):
        cameras, static_solution, lane_map, tracks, config_hash = self.fixture()
        static_solution["truth"]["heldout_gate_passed"] = False
        with self.assertRaisesRegex(TrajectoryFitError, "independent truth"):
            fit_detection_constraints(
                cameras=cameras,
                static_solution=static_solution,
                lane_map=lane_map,
                tracks=tracks,
                synchronized_pairs=[],
                cameras_json_sha256=config_hash,
            )

    def test_reviewed_synchronized_pairs_connect_clock_to_trajectory(self):
        cameras, static_solution, lane_map, tracks, config_hash = self.fixture()
        pairs = [
            {
                "event_ids": [f"ch1-{index}-1", f"ch2-{index}-1"],
                "time_sigma_s": 0.05,
                "reviewed": True,
                "estimate_clock_offset": True,
            }
            for index in range(5)
        ]
        report = fit_detection_constraints(
            cameras=cameras,
            static_solution=static_solution,
            lane_map=lane_map,
            tracks=tracks,
            synchronized_pairs=pairs,
            cameras_json_sha256=config_hash,
            multistarts=2,
        )
        self.assertTrue(report["fit_completed"])
        self.assertEqual(report["cameras"]["ch1"]["clock_status"], "reference")
        self.assertEqual(report["cameras"]["ch2"]["clock_status"], "estimated")
        self.assertAlmostEqual(report["cameras"]["ch2"]["clock_offset_s"], 0.0, places=2)


if __name__ == "__main__":
    unittest.main()
