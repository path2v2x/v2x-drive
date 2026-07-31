import importlib.util
import math
from pathlib import Path

import pytest


TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "compare_opendrive_geometry.py"
SPEC = importlib.util.spec_from_file_location("compare_opendrive_geometry", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def geometry(shape, length=10.0, heading=0.0):
    return {
        "s": 0.0,
        "x": 1.0,
        "y": 2.0,
        "hdg": heading,
        "length": length,
        "shape": shape,
    }


def test_evaluate_line_and_arc():
    import xml.etree.ElementTree as ET

    x, y, heading = tool.evaluate_geometry(geometry(ET.Element("line")), 3.0)
    assert (x, y, heading) == (4.0, 2.0, 0.0)

    x, y, heading = tool.evaluate_geometry(
        geometry(ET.Element("arc", curvature="0.1")), 10.0
    )
    assert math.isclose(x, 1.0 + math.sin(1.0) / 0.1, abs_tol=1e-9)
    assert math.isclose(y, 2.0 + (1.0 - math.cos(1.0)) / 0.1, abs_tol=1e-9)
    assert math.isclose(heading, 1.0, abs_tol=1e-9)


def test_spiral_with_constant_curvature_matches_arc():
    import xml.etree.ElementTree as ET

    spiral = geometry(ET.Element("spiral", curvStart="0.1", curvEnd="0.1"))
    arc = geometry(ET.Element("arc", curvature="0.1"))
    spiral_pose = tool.evaluate_geometry(spiral, 10.0)
    arc_pose = tool.evaluate_geometry(arc, 10.0)
    for left, right in zip(spiral_pose, arc_pose):
        assert math.isclose(left, right, abs_tol=1e-7)


def test_parse_and_match_crosswalks(tmp_path):
    source = tmp_path / "source.xodr"
    source.write_text(
        """<OpenDRIVE>
<header revMajor="1" revMinor="4"><geoReference>same</geoReference></header>
<road id="1" length="20" junction="-1">
  <planView><geometry s="0" x="0" y="0" hdg="0" length="20"><line/></geometry></planView>
  <objects><object id="cw" name="LadderCrosswalk" type="crosswalk" s="10" t="2" hdg="0">
    <outline><cornerLocal u="-1" v="-1"/><cornerLocal u="1" v="-1"/>
      <cornerLocal u="1" v="1"/><cornerLocal u="-1" v="1"/></outline>
  </object></objects>
  <signals><signal id="sig" s="8" t="-1" type="1000001" subtype="0"/></signals>
</road><junction id="1"/></OpenDRIVE>"""
    )
    model = tool.parse_map(source)
    assert model["crosswalks"][0]["center_xy"] == [10.0, 2.0]
    assert model["crosswalks"][0]["outline_xy"][0] == [9.0, 1.0]
    assert model["signals"][0]["center_xy"] == [8.0, -1.0]

    report = tool.compare_maps(model, model, road_spacing_m=1.0)
    assert report["georeference_equal"] is True
    assert report["crosswalks"]["distance_m"]["max"] == 0.0
    assert report["signals"]["distance_m"]["max"] == 0.0
    assert report["road_reference_line"]["deployed_to_candidate"]["coverage"]["0.25"] == 1.0


def test_greedy_feature_match_is_one_to_one():
    left = [
        {"id": "a", "center_xy": [0.0, 0.0]},
        {"id": "b", "center_xy": [0.2, 0.0]},
    ]
    right = [{"id": "c", "center_xy": [0.1, 0.0]}]
    result = tool.match_features(left, right, 1.0)
    assert len(result["matches"]) == 1
    assert len(result["unmatched_deployed"]) == 1
    assert result["unmatched_candidate"] == []


def test_signal_match_rejects_different_feature_classes():
    left = [{"id": "vehicle", "feature_class": "vehicle_signal", "center_xy": [0, 0]}]
    right = [{"id": "pedestrian", "feature_class": "pedestrian_signal", "center_xy": [0, 0]}]
    result = tool.match_features(left, right, 1.0, require_same_class=True)
    assert result["matches"] == []
    assert len(result["unmatched_deployed"]) == 1
    assert len(result["unmatched_candidate"]) == 1


def test_site_anchor_uses_bound_map_georeference(tmp_path):
    config = tmp_path / "cameras.json"
    georeference = (
        "+proj=tmerc +lat_0=37 +lon_0=-122 +k=1 +x_0=0 +y_0=0 "
        "+datum=WGS84 +units=m"
    )
    config.write_text(
        '{"site":{"lat":37.0,"lon":-122.0,"map_georeference":'
        + repr(georeference).replace("'", '"')
        + "}}"
    )
    result = tool.site_anchor_from_config(config, georeference)
    x, y = result["anchor_xy"]
    assert math.isclose(x, 0.0, abs_tol=1e-9)
    assert math.isclose(y, 0.0, abs_tol=1e-9)


def test_outline_distance_metrics_are_symmetric():
    left = [[0, 0], [2, 0], [2, 1], [0, 1], [0, 0]]
    right = [[1, 0], [3, 0], [3, 1], [1, 1], [1, 0]]
    metrics = tool.outline_distance_metrics(left, right)
    assert math.isclose(metrics["symmetric_hausdorff_m"], 1.0, abs_tol=1e-9)
    assert math.isclose(metrics["deployed_area_m2"], 2.0, abs_tol=1e-9)
    assert math.isclose(metrics["candidate_area_m2"], 2.0, abs_tol=1e-9)


def test_assignment_is_global_and_reports_ambiguity():
    # Greedy takes row0->col0 (1.0), forcing row1->col1 (100). The global
    # solution is row0->col1 (2.0), row1->col0 (1.1).
    assert tool.minimum_cost_assignment([[1.0, 2.0], [1.1, 100.0]]) == [1, 0]


def test_exclusive_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "report.json"
    tool.write_json_exclusive(output, {"first": True})
    try:
        tool.write_json_exclusive(output, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("writer overwrote immutable evidence")


def test_lane_width_road_mark_and_connectivity_drift_is_explicit(tmp_path):
    template = """<OpenDRIVE>
<header revMajor="1" revMinor="4"><geoReference>same</geoReference></header>
<road id="1" length="10" junction="-1">
  <link><successor elementType="road" elementId="{successor}" contactPoint="start"/></link>
  <planView><geometry s="0" x="0" y="0" hdg="0" length="10"><line/></geometry></planView>
  <lanes><laneSection s="0"><right><lane id="-1" type="driving" level="false">
    <link><successor id="{lane_successor}"/></link>
    <width sOffset="0" a="{width}" b="0" c="0" d="0"/>
    <roadMark sOffset="0" type="{mark}" color="white"/>
  </lane></right></laneSection></lanes>
</road>
<junction id="7"><connection id="1" incomingRoad="1" connectingRoad="{successor}" contactPoint="start">
  <laneLink from="-1" to="{lane_successor}"/>
</connection></junction></OpenDRIVE>"""
    deployed_path, candidate_path = tmp_path / "deployed.xodr", tmp_path / "candidate.xodr"
    deployed_path.write_text(template.format(successor="2", lane_successor="-1", width="3.5", mark="solid"))
    candidate_path.write_text(template.format(successor="3", lane_successor="-2", width="3.8", mark="broken"))
    deployed, candidate = tool.parse_map(deployed_path), tool.parse_map(candidate_path)
    report = tool.compare_maps(deployed, candidate, road_spacing_m=1.0)
    lane_id = "road-1-section-s0.000-lane--1"
    assert report["lane_profiles"]["width_changed"] == [lane_id]
    assert report["lane_profiles"]["road_mark_changed"] == [lane_id]
    assert report["lane_profiles"]["lane_link_changed"] == [lane_id]
    assert report["lane_profiles"]["width_difference_m"]["max"] == pytest.approx(0.3)
    assert report["road_links"]["changed"] == ["1"]
    assert report["junction_links"]["changed"] == ["junction-7-connection-1"]


def test_identical_lane_profiles_have_no_false_drift(tmp_path):
    path = tmp_path / "same.xodr"
    path.write_text("""<OpenDRIVE><header><geoReference>same</geoReference></header>
<road id="1" length="2" junction="-1"><planView><geometry s="0" x="0" y="0" hdg="0" length="2"><line/></geometry></planView>
<lanes><laneSection s="0"><right><lane id="-1" type="driving"><width sOffset="0" a="3.5" b="0" c="0" d="0"/><roadMark sOffset="0" type="solid"/></lane></right></laneSection></lanes>
</road></OpenDRIVE>""")
    model = tool.parse_map(path)
    comparison = tool.compare_lane_profiles(model["lane_profiles"], model["lane_profiles"])
    assert comparison["width_changed"] == []
    assert comparison["road_mark_changed"] == []
    assert comparison["width_difference_m"]["max"] == 0.0


def test_vertical_lateral_lane_offset_and_junction_assignment_drift_is_explicit(tmp_path):
    template = """<OpenDRIVE><header><geoReference>same</geoReference></header>
<road id="1" length="10" junction="{junction}">
<planView><geometry s="0" x="0" y="0" hdg="0" length="10"><line/></geometry></planView>
<elevationProfile><elevation s="0" a="{elevation}" b="0" c="0" d="0"/></elevationProfile>
<lateralProfile><superelevation s="0" a="{superelevation}" b="0" c="0" d="0"/>
<shape s="0" t="0" a="{shape}" b="0" c="0" d="0"/></lateralProfile>
<lanes><laneOffset s="0" a="{offset}" b="0" c="0" d="0"/><laneSection s="0"/></lanes>
</road></OpenDRIVE>"""
    deployed_path, candidate_path = tmp_path / "vertical-a.xodr", tmp_path / "vertical-b.xodr"
    deployed_path.write_text(template.format(
        junction="-1", elevation="0", superelevation="0", shape="0", offset="0"
    ))
    candidate_path.write_text(template.format(
        junction="7", elevation="0.2", superelevation="0.01", shape="0.1", offset="0.3"
    ))
    report = tool.compare_maps(tool.parse_map(deployed_path), tool.parse_map(candidate_path))
    assert report["elevation_profiles"]["changed"] == ["road-1-elevation-s0.000000000"]
    assert report["lane_offsets"]["changed"] == ["road-1-lane_offset-s0.000000000"]
    assert report["lateral_profiles"]["superelevation"]["changed"] == [
        "road-1-superelevation-s0.000000000"
    ]
    assert report["lateral_profiles"]["shape"]["changed"] == [
        "road-1-lateral_shape-s0.000000000-t0.000000000"
    ]
    assert report["road_junction_assignment"]["changed"] == ["1"]


def test_complete_nested_road_mark_semantic_drift_is_explicit(tmp_path):
    template = """<OpenDRIVE><header><geoReference>same</geoReference></header>
<road id="1" length="10" junction="-1">
<planView><geometry s="0" x="0" y="0" hdg="0" length="10"><line/></geometry></planView>
<lanes><laneSection s="0"><right><lane id="-1" type="driving">
<width sOffset="0" a="3.5" b="0" c="0" d="0"/>
<roadMark sOffset="0" type="custom" color="white" weight="standard"
 material="{material}" width="0.2" laneChange="both" height="{height}">
 <sway ds="0.5" a="{sway}" b="2" c="3" d="4"/>
 <type name="{type_name}" width="0.4"><line length="{length}" space="{space}"
  tOffset="{type_t}" sOffset="{type_s}" rule="{type_rule}" width="{type_width}"/></type>
 <explicit><line length="{explicit_length}" space="1" tOffset="{explicit_t}"
  sOffset="{explicit_s}" rule="{explicit_rule}" width="{explicit_width}"/></explicit>
</roadMark></lane></right></laneSection></lanes></road></OpenDRIVE>"""
    base = dict(
        material="paint", height="0.01", sway="1", type_name="double",
        length="3", space="2", type_t="0.1", type_s="0.2",
        type_rule="caution", type_width="0.12", explicit_length="4",
        explicit_t="-0.1", explicit_s="0.3", explicit_rule="no_passing",
        explicit_width="0.14",
    )
    changed = dict(
        material="thermoplastic", height="0.02", sway="1.5", type_name="triple",
        length="3.5", space="2.5", type_t="0.15", type_s="0.25",
        type_rule="warning", type_width="0.13", explicit_length="4.5",
        explicit_t="-0.2", explicit_s="0.35", explicit_rule="stop",
        explicit_width="0.16",
    )
    deployed_path, candidate_path = tmp_path / "marks-a.xodr", tmp_path / "marks-b.xodr"
    deployed_path.write_text(template.format(**base))
    candidate_path.write_text(template.format(**changed))
    deployed = tool.parse_map(deployed_path)
    mark = deployed["lane_profiles"][0]["road_marks"][0]
    assert mark["material"] == "paint" and mark["height"] == 0.01
    assert mark["sway"][0]["a"] == 1.0
    assert mark["types"][0]["lines"][0]["space_m"] == 2.0
    assert mark["explicit_lines"][0]["rule"] == "no_passing"
    report = tool.compare_maps(deployed, tool.parse_map(candidate_path))
    assert report["lane_profiles"]["road_mark_changed"] == [
        "road-1-section-s0.000-lane--1"
    ]
