import sys
from pathlib import Path
import unittest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fit_twin_camera_poses import (  # noqa: E402
    absolute_pose_bounds,
    bounded_initial_pose,
    candidate_config_is_eligible,
)


class FitTwinCameraPosesSafetyTests(unittest.TestCase):
    def test_default_seed_ignores_accumulated_config_offsets(self):
        camera = {
            "twin_pose": {
                "yaw_offset_deg": 14.0,
                "pitch_offset_deg": 15.49,
                "height_offset_m": 1.48,
                "forward_offset_m": 1.5,
            }
        }
        bounds = absolute_pose_bounds(False)
        self.assertEqual(bounded_initial_pose(camera, bounds, False), [0.0] * 7)

    def test_config_seed_is_clamped_to_absolute_not_relative_bounds(self):
        camera = {
            "twin_pose": {
                "yaw_offset_deg": 99.0,
                "pitch_offset_deg": -99.0,
                "roll_offset_deg": 22.0,
                "fov_offset_deg": -50.0,
                "height_offset_m": 8.0,
                "forward_offset_m": -8.0,
                "right_offset_m": 4.0,
            }
        }
        bounds = absolute_pose_bounds(False)
        self.assertEqual(
            bounded_initial_pose(camera, bounds, True),
            [15.0, -15.0, 0.0, 8.0, 0.0, 0.0, -20.0],
        )

    def test_candidate_config_requires_dataset_and_heldout_gates(self):
        report = {
            "cameras": {
                "ch1": {
                    "dataset_gate": {"passed": True},
                    "heldout_gate": {"passed": False},
                    "acceptance_eligible": False,
                }
            }
        }
        self.assertFalse(candidate_config_is_eligible(report))
        report["cameras"]["ch1"].update({
            "heldout_gate": {"passed": True},
            "acceptance_eligible": True,
        })
        self.assertTrue(candidate_config_is_eligible(report))


if __name__ == "__main__":
    unittest.main()
