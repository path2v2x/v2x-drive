import sys
from pathlib import Path
import unittest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from export_map_calibration_geometry import (  # noqa: E402
    bind_sampled_road_marks,
    canonical_hash,
    lane_geometry_from_waypoints,
    objects_from_source,
    opendrive_road_mark_ranges,
    split_crosswalk_polygons,
    stable_crosswalk_id,
)


class Location:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class Value:
    def __init__(self, value):
        self.type = value
        self.color = "White"
        self.width = 0.15
        self.lane_change = "Both"


class Rotation:
    yaw = 0


class Transform:
    def __init__(self, x):
        self.location = Location(x, 0, 0)
        self.rotation = Rotation()


class Waypoint:
    def __init__(self, s, left, right, width=4.0):
        self.s = s
        self.transform = Transform(s)
        self.lane_width = width
        self.left_lane_marking = Value(left)
        self.right_lane_marking = Value(right)


class ExportMapCalibrationGeometryTests(unittest.TestCase):
    def test_canonical_hash_ignores_dictionary_key_order(self):
        self.assertEqual(canonical_hash({"b": 2, "a": 1}), canonical_hash({"a": 1, "b": 2}))

    def test_crosswalk_list_splits_only_closed_polygons(self):
        values = [
            Location(0, 0, 0), Location(1, 0, 0),
            Location(1, 1, 0), Location(0, 0, 0),
            Location(2, 2, 0), Location(3, 2, 0),
            Location(3, 3, 0), Location(2, 2, 0),
        ]
        polygons = split_crosswalk_polygons(values)
        self.assertEqual(len(polygons), 2)
        self.assertEqual(polygons[1][0], [2.0, 2.0, 0.0])

    def test_open_crosswalk_list_fails_closed(self):
        with self.assertRaises(RuntimeError):
            split_crosswalk_polygons([
                Location(0, 0, 0), Location(1, 0, 0), Location(1, 1, 0)
            ])

    def test_crosswalk_identity_is_stable_across_start_and_direction(self):
        polygon = [[0, 0, 0], [2, 0, 0], [2, 1, 0], [0, 1, 0], [0, 0, 0]]
        rotated = [[2, 1, 0], [0, 1, 0], [0, 0, 0], [2, 0, 0], [2, 1, 0]]
        reversed_polygon = list(reversed(polygon))
        self.assertEqual(stable_crosswalk_id(polygon), stable_crosswalk_id(rotated))
        self.assertEqual(stable_crosswalk_id(polygon), stable_crosswalk_id(reversed_polygon))

    def test_object_category_is_derived_from_exact_native_semantics(self):
        objects = objects_from_source([{
            "source_object_id": "signal-1", "name": "signal",
            "category": "StableLandmark",
            "semantic_source": {
                "schema": "v2x-carla-native-environment-object/v1",
                "api": "carla.World.get_environment_objects",
                "native_type": "CityObjectLabel.TrafficLight",
                "native_subtype": None,
            },
            "center_world": [1, 2, 3], "extent": [0.1, 0.2, 1.0],
        }])
        self.assertEqual(objects[0]["category"], "TrafficLight")
        self.assertEqual(objects[0]["id"], "environment-TrafficLight-signal-1")

    def test_caller_stable_landmark_label_without_native_semantics_fails(self):
        with self.assertRaises(RuntimeError):
            objects_from_source([{
                "source_object_id": "caller-1", "name": "caller",
                "category": "StableLandmark",
                "semantic_source": {
                    "schema": "caller-defined", "api": "caller",
                    "native_type": "StableLandmark", "native_subtype": "fixed",
                },
                "center_world": [1, 2, 3], "extent": [0.1, 0.2, 1.0],
            }])

    def test_lane_export_preserves_every_contiguous_road_mark_range(self):
        waypoints = [
            Waypoint(0, "Solid", "Broken"),
            Waypoint(1, "Solid", "Broken"),
            Waypoint(2, "Broken", "Broken"),
            Waypoint(3, "Broken", "Solid"),
            Waypoint(4, "Broken", "Solid"),
        ]
        lane = lane_geometry_from_waypoints((12, 0, -1), waypoints)
        ranges = [
            (
                item["side"], item["type"], item["color"], item["width_m"],
                item["lane_change"], item["start_s_m"], item["end_s_m"],
            )
            for item in lane["road_mark_segments"]
        ]
        self.assertEqual(ranges, [
            ("left", "solid", "white", 0.15, "both", 0.0, 1.0),
            ("left", "broken", "white", 0.15, "both", 2.0, 4.0),
            ("right", "broken", "white", 0.15, "both", 0.0, 2.0),
            ("right", "solid", "white", 0.15, "both", 3.0, 4.0),
        ])
        self.assertEqual(len({item["id"] for item in lane["road_mark_segments"]}), 4)
        self.assertNotIn("marking_types", lane)

    def test_exact_opendrive_road_mark_ranges_are_not_collapsed(self):
        ranges = opendrive_road_mark_ranges(b"""<OpenDRIVE>
<road id="7" length="20"><lanes><laneSection s="2"><right>
<lane id="-1"><roadMark sOffset="0" type="solid"/><roadMark sOffset="5" type="broken"/>
<roadMark sOffset="12" type="solid"/></lane></right></laneSection></lanes></road>
</OpenDRIVE>""")
        ordered = sorted(ranges, key=lambda item: item["start_s_m"])
        self.assertEqual(
            [(item["type"], item["start_s_m"], item["end_s_m"]) for item in ordered],
            [("solid", 2.0, 7.0), ("broken", 7.0, 14.0), ("solid", 14.0, 20.0)],
        )
        self.assertEqual(len({item["id"] for item in ranges}), 3)

    def test_road_mark_material_height_sway_type_and_explicit_lines_are_preserved(self):
        ranges = opendrive_road_mark_ranges(b"""<OpenDRIVE>
<road id="9" length="20"><lanes><laneSection s="0"><right><lane id="-1">
<roadMark sOffset="0" type="custom" weight="standard" color="yellow"
 material="thermoplastic" width="0.2" laneChange="decrease" height="0.015">
 <sway ds="0.5" a="1" b="2" c="3" d="4"/>
 <type name="double" width="0.4"><line length="3" space="2" tOffset="0.1"
  sOffset="0.2" rule="caution" width="0.12"/></type>
 <explicit><line length="4" tOffset="-0.1" sOffset="0.3" rule="no_passing" width="0.14"/></explicit>
</roadMark></lane></right></laneSection></lanes></road></OpenDRIVE>""")
        mark = ranges[0]
        self.assertEqual(mark["material"], "thermoplastic")
        self.assertEqual(mark["height_m"], 0.015)
        self.assertEqual(mark["sway"], [{"ds_m": 0.5, "a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}])
        self.assertEqual(mark["types"][0]["lines"][0], {
            "length_m": 3.0, "space_m": 2.0, "t_offset_m": 0.1,
            "s_offset_m": 0.2, "rule": "caution", "width_m": 0.12,
        })
        self.assertEqual(mark["explicit_lines"][0]["rule"], "no_passing")
        self.assertEqual(mark["explicit_lines"][0]["width_m"], 0.14)

    def test_sampled_mark_geometry_binds_exact_xodr_range_and_attributes(self):
        lanes = [lane_geometry_from_waypoints(
            (7, 0, -1), [Waypoint(value, "Solid", "Solid") for value in range(5)]
        )]
        exact = opendrive_road_mark_ranges(b"""<OpenDRIVE><road id="7" length="4">
<lanes><laneSection s="0"><center><lane id="0"><roadMark sOffset="0" type="solid" color="white" width="0.15" laneChange="both"/></lane></center>
<right><lane id="-1"><roadMark sOffset="0" type="solid" color="white" width="0.15" laneChange="both"/></lane></right>
</laneSection></lanes></road></OpenDRIVE>""")
        segments = bind_sampled_road_marks(lanes, exact, spacing_m=1.0)
        self.assertEqual(len(segments), 2)
        self.assertTrue(all(item["id"].startswith(item["opendrive_range_id"]) for item in segments))
        self.assertTrue(all(item["type"] == "solid" and item["color"] == "white" for item in segments))
        self.assertTrue(all(item["width_m"] == 0.15 and item["lane_change"] == "both" for item in segments))
        self.assertTrue(all(item["boundary_world_sha256"] for item in segments))


if __name__ == "__main__":
    unittest.main()
