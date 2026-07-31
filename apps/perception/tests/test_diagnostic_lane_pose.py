import sys
from pathlib import Path
import unittest

import numpy as np


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from search_diagnostic_lane_pose import (  # noqa: E402
    canonical_hash,
    intersections,
    lane_cloud,
    metrics,
    partition_for,
    proposal_partition_owners,
)


class DiagnosticLanePoseTests(unittest.TestCase):
    def test_partition_and_canonical_hash_are_stable(self):
        self.assertEqual(partition_for("tracklet-1"), partition_for("tracklet-1"))
        self.assertEqual(canonical_hash({"b": 2, "a": 1}), canonical_hash({"a": 1, "b": 2}))

    def test_center_ray_intersects_synthetic_lane(self):
        geometry = {
            "geometry": {
                "lanes": [{
                    "lane_width_m": 4.0,
                    "center_world": [[9.5, 0.0, 0.0], [10.0, 0.0, 0.0], [10.5, 0.0, 0.0]],
                }]
            }
        }
        lane_points, lane_widths, tree = lane_cloud(geometry)
        params = np.asarray([0.0, 0.0, 10.05, -45.0, 0.0, 0.0, 90.0])
        world, distance, offroad, valid = intersections(
            params,
            np.asarray([[50.0, 50.0]]),
            np.asarray([[100.0, 100.0]]),
            lane_points,
            lane_widths,
            tree,
        )
        self.assertTrue(valid[0])
        self.assertAlmostEqual(world[0, 0], 10.0, places=1)
        self.assertLess(distance[0], 0.1)
        self.assertFalse(offroad[0])

    def test_metrics_keep_unresolved_cardinality(self):
        value = metrics(
            np.asarray([0.5, 2.0]),
            np.asarray([False, True]),
            np.asarray([True, False]),
            np.asarray([True, True]),
        )
        self.assertEqual(value["count"], 2)
        self.assertEqual(value["resolved"], 1)
        self.assertEqual(value["median_lane_center_m"], 0.5)
        self.assertEqual(value["offroad_fraction"], 0.0)

    def test_overlapping_tracklet_proposals_fail_closed(self):
        proposals = [
            {"proposal_id": "a", "camera_id": "ch1", "event_ids": ["shared"]},
            {"proposal_id": "b", "camera_id": "ch1", "event_ids": ["shared"]},
        ]
        with self.assertRaises(ValueError):
            proposal_partition_owners(proposals, "ch1")


if __name__ == "__main__":
    unittest.main()
