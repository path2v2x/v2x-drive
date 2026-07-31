import argparse
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.bridge.tools import build_map_candidate_lineage_manifest as lineage
from apps.bridge.tools import compare_opendrive_topology_correspondence as tool


PROJECTION = "+proj=tmerc +lat_0=37 +lon_0=-122 +datum=WGS84"
REAL_ROOT = Path("/home/path/Downloads/entire scene-20260211T002439Z-1-001 (2)/entire scene")
REAL_DEPLOYED = Path(
    "/home/path/V2XCarla/v2x-evidence/calibration/"
    "20260712T104500Z-opendrive-source-audit/deployed.xodr"
)


def road(
    road_id,
    *,
    x=0,
    y=0,
    z=0,
    heading=0,
    length=10,
    junction="-1",
    lane=False,
    lane_type="driving",
    predecessor=None,
    successor=None,
    predecessor_type="road",
    successor_type="road",
):
    links = []
    if predecessor is not None:
        links.append(
            f'<predecessor elementType="{predecessor_type}" elementId="{predecessor}" contactPoint="end"/>'
        )
    if successor is not None:
        links.append(
            f'<successor elementType="{successor_type}" elementId="{successor}" contactPoint="start"/>'
        )
    lanes = (
        f'<lanes><laneSection s="0"><right><lane id="-1" type="{lane_type}" level="false">'
        '<width sOffset="0" a="3.5" b="0" c="0" d="0"/>'
        '<roadMark sOffset="0" type="solid"/></lane></right></laneSection></lanes>'
        if lane else "<lanes/>"
    )
    elevation = (
        f'<elevationProfile><elevation s="0" a="{z}" b="0" c="0" d="0"/>'
        "</elevationProfile>"
    )
    return (
        f'<road id="{road_id}" length="{length}" junction="{junction}">'
        f'<link>{"".join(links)}</link><planView><geometry s="0" x="{x}" y="{y}" '
        f'hdg="{heading}" length="{length}"><line/></geometry></planView>'
        f"{elevation}{lanes}</road>"
    )


def xodr(roads, junctions=(), *, projection=PROJECTION, offset=None):
    offset_xml = ""
    if offset is not None:
        offset_xml = (
            f'<offset x="{offset[0]}" y="{offset[1]}" z="{offset[2]}" hdg="{offset[3]}"/>'
        )
    return (
        f"<OpenDRIVE><header><geoReference>{projection}</geoReference>{offset_xml}</header>"
        + "".join(roads)
        + "".join(junctions)
        + "</OpenDRIVE>"
    ).encode()


def junction(junction_id, connections):
    values = []
    for index, (incoming, connecting) in enumerate(connections):
        values.append(
            f'<connection id="{index}" incomingRoad="{incoming}" '
            f'connectingRoad="{connecting}" contactPoint="start">'
            '<laneLink from="-1" to="-1"/></connection>'
        )
    return f'<junction id="{junction_id}">{"".join(values)}</junction>'


def manifest_for(old_path: Path, deployed_path: Path):
    def artifact(path, label):
        content = path.read_bytes()
        return {
            "label": label,
            "path": str(path),
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "kind": "xodr",
            "summary": lineage.summarize_xodr(content, label),
        }

    def recovered_artifact(label, relative_path, kind):
        content = f"synthetic-{label}".encode()
        return {
            "label": label,
            "path": str(old_path.parent / relative_path),
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "kind": kind,
            "summary": {"synthetic": True},
        }

    recovered_artifacts = [
        artifact(old_path, "recovered_old_xodr"),
        recovered_artifact("recovered_fbx", "Richmond.fbx", "fbx"),
        recovered_artifact("recovered_geojson", "Richmond.geojson", "geojson"),
        recovered_artifact(
            "rrdata_xml:Richmond.rrdata.xml", "Richmond.rrdata.xml", "xml"
        ),
        recovered_artifact(
            "material_file:Richmond.fbm/a.png", "Richmond.fbm/a.png", "file"
        ),
    ]
    complete_paths = sorted(
        str(Path(item["path"]).relative_to(old_path.parent).as_posix())
        for item in recovered_artifacts
    )
    old_summary = recovered_artifacts[0]["summary"]
    deployed_artifact = artifact(deployed_path, "live_deployed_xodr")
    deployed_summary = deployed_artifact["summary"]
    return {
        "schema": lineage.SCHEMA,
        "acceptance_eligible": False,
        "manifest_mutability": "exclusive_no_replace",
        "lineage_reconciliation": {
            "status": "unresolved_blocking",
            "recovered_topology": {
                "roads": old_summary["road_count"],
                "junctions": old_summary["junction_count"],
            },
            "live_topology": {
                "roads": deployed_summary["road_count"],
                "junctions": deployed_summary["junction_count"],
            },
            "same_projection_text": old_summary["projection"]
            == deployed_summary["projection"],
            "same_topology_sha256": old_summary["topology_sha256"]
            == deployed_summary["topology_sha256"],
        },
        "recovered_material_dependency_graph": {
            "status": "complete_package_inventory_frozen_dependency_edges_unreviewed",
            "package_inventory_sha256": "1" * 64,
            "package_file_count": len(recovered_artifacts),
            "package_directory_count": 1,
            "complete_package_paths": complete_paths,
            "selection_blocking_until_complete": True,
        },
        "selection": {
            "status": "blocked_unresolved_opendrive_lineage",
            "selected_candidate_id": None,
            "scoring_permitted": False,
        },
        "candidates": [
            {
                "candidate_name": "recovered_authoring_package",
                "artifacts": recovered_artifacts,
            },
            {
                "candidate_name": "live_deployed_opendrive",
                "artifacts": [deployed_artifact],
            },
        ],
    }


def finalize_manifest(value):
    for candidate in value["candidates"]:
        candidate["candidate_id"] = lineage.candidate_id(
            candidate["candidate_name"], candidate["artifacts"]
        )
    return value


def bind_tool_to_synthetic_manifest(value):
    """Test-only replacement for the immutable real Richmond site binding."""
    candidates = {item["candidate_name"]: item for item in value["candidates"]}
    recovered = candidates["recovered_authoring_package"]
    deployed = candidates["live_deployed_opendrive"]
    old_xodr = next(
        item for item in recovered["artifacts"] if item["label"] == "recovered_old_xodr"
    )
    deployed_xodr = next(
        item for item in deployed["artifacts"] if item["label"] == "live_deployed_xodr"
    )
    graph = value["recovered_material_dependency_graph"]
    tool.ACCEPTED_BINDING = {
        "old_xodr_sha256": old_xodr["sha256"],
        "deployed_xodr_sha256": deployed_xodr["sha256"],
        "package_inventory_sha256": graph["package_inventory_sha256"],
        "package_file_count": graph["package_file_count"],
        "package_directory_count": graph["package_directory_count"],
        "recovered_candidate_id": recovered["candidate_id"],
        "deployed_candidate_id": deployed["candidate_id"],
    }


@pytest.fixture(autouse=True)
def restore_real_site_binding_after_test():
    tool.ACCEPTED_BINDING = tool.RICHMOND_ACCEPTED_BINDING
    yield
    tool.ACCEPTED_BINDING = tool.RICHMOND_ACCEPTED_BINDING


def build_report(tmp_path, old_bytes, deployed_bytes):
    tmp_path.mkdir(parents=True, exist_ok=True)
    old_path = tmp_path / "old.xodr"
    deployed_path = tmp_path / "deployed.xodr"
    manifest_path = tmp_path / "lineage.json"
    old_path.write_bytes(old_bytes)
    deployed_path.write_bytes(deployed_bytes)
    manifest = finalize_manifest(manifest_for(old_path, deployed_path))
    bind_tool_to_synthetic_manifest(manifest)
    manifest_path.write_text(json.dumps(manifest))
    args = argparse.Namespace(
        old_xodr=str(old_path),
        deployed_xodr=str(deployed_path),
        lineage_manifest=str(manifest_path),
        output=str(tmp_path / "report.json"),
    )
    return tool.build(args), args


def road_categories(report):
    return [item["category"] for item in report["roads"]["components"]]


def test_unchanged_and_renumbered_are_distinct(tmp_path):
    report, _ = build_report(
        tmp_path,
        xodr([road("same", x=0), road("old", x=30)]),
        xodr([road("same", x=0), road("new", x=30)]),
    )
    categories = {
        (tuple(item["old_ids"]), tuple(item["deployed_ids"])): item["category"]
        for item in report["roads"]["components"]
    }
    assert categories[("same",), ("same",)] == "unchanged"
    assert categories[("old",), ("new",)] == "renumbered"
    assert report["lineage_resolved"] is False
    assert report["scoring_permitted"] is False
    assert report["acceptance_eligible"] is False


def test_split_and_merge_have_joint_coverage_evidence(tmp_path):
    split, _ = build_report(
        tmp_path / "split",
        xodr([road("parent", length=10)]),
        xodr([road("left", length=5), road("right", x=5, length=5)]),
    )
    component = split["roads"]["components"][0]
    assert component["category"] == "split"
    assert component["many_evidence"]["joint_parent_coverage"] == 1.0
    assert component["many_evidence"]["max_child_overlap"] <= tool.MAX_CHILD_OVERLAP

    merged, _ = build_report(
        tmp_path / "merge",
        xodr([road("left", length=5), road("right", x=5, length=5)]),
        xodr([road("parent", length=10)]),
    )
    assert merged["roads"]["components"][0]["category"] == "merged"


@pytest.mark.parametrize(
    "deployed_roads",
    [
        [road("extension", length=30), road("stub", length=0.9)],
        [road("copy", length=10), road("stub", length=0.9)],
    ],
)
def test_split_rejects_extension_or_overlapping_stub(tmp_path, deployed_roads):
    report, _ = build_report(
        tmp_path,
        xodr([road("parent", length=10)]),
        xodr(deployed_roads),
    )
    assert report["roads"]["components"][0]["category"] == "ambiguous"


@pytest.mark.parametrize(
    "old_roads",
    [
        [road("extension", length=30), road("stub", length=0.9)],
        [road("copy", length=10), road("stub", length=0.9)],
    ],
)
def test_merge_rejects_extension_or_overlapping_stub(tmp_path, old_roads):
    report, _ = build_report(
        tmp_path,
        xodr(old_roads),
        xodr([road("parent", length=10)]),
    )
    assert report["roads"]["components"][0]["category"] == "ambiguous"


def test_lane_bearing_split_rejects_reversed_child(tmp_path):
    report, _ = build_report(
        tmp_path,
        xodr([road("parent", length=10, lane=True)]),
        xodr(
            [
                road("left", x=0, length=5, lane=True),
                road("right", x=10, heading=math.pi, length=5, lane=True),
            ]
        ),
    )
    component = report["roads"]["components"][0]
    assert component["category"] == "ambiguous"
    assert component["many_evidence"]["lane_orientation_compatible"] is False


def test_lane_bearing_merge_rejects_reversed_parent(tmp_path):
    report, _ = build_report(
        tmp_path,
        xodr(
            [
                road("left", x=0, length=5, lane=True),
                road("right", x=10, heading=math.pi, length=5, lane=True),
            ]
        ),
        xodr([road("parent", length=10, lane=True)]),
    )
    component = report["roads"]["components"][0]
    assert component["category"] == "ambiguous"
    assert component["many_evidence"]["lane_orientation_compatible"] is False


def test_added_removed_and_terminal_accounting(tmp_path):
    report, _ = build_report(
        tmp_path,
        xodr([road("old", x=0)]),
        xodr([road("new", x=100)]),
    )
    assert sorted(road_categories(report)) == ["added", "removed"]
    assert report["roads"]["accounting"] == {
        "component_category_counts": {"added": 1, "removed": 1},
        "component_count": 2,
        "old_item_count": 1,
        "deployed_item_count": 1,
        "old_terminal_accounting_complete": True,
        "deployed_terminal_accounting_complete": True,
    }


def test_semantic_difference_and_n_to_m_are_ambiguous(tmp_path):
    semantic, _ = build_report(
        tmp_path / "semantic",
        xodr([road("a", lane=False)]),
        xodr([road("a", lane=True)]),
    )
    assert road_categories(semantic) == ["ambiguous"]
    assert "semantic/topology" in semantic["roads"]["components"][0]["reasons"][0]

    many, _ = build_report(
        tmp_path / "many",
        xodr([road("o1"), road("o2")]),
        xodr([road("n1"), road("n2")]),
    )
    assert road_categories(many) == ["ambiguous"]
    assert many["roads"]["components"][0]["old_ids"] == ["o1", "o2"]
    assert many["roads"]["components"][0]["deployed_ids"] == ["n1", "n2"]


def test_one_to_one_requires_bidirectional_coverage_and_length_agreement(tmp_path):
    report, _ = build_report(
        tmp_path,
        xodr([road("same", length=10)]),
        xodr([road("same", length=100)]),
    )
    component = report["roads"]["components"][0]
    assert component["category"] == "ambiguous"
    assert component["edges"][0]["metrics"]["old_to_deployed_strict"] == 1.0
    assert component["edges"][0]["metrics"]["deployed_to_old_strict"] < 0.2
    assert component["edges"][0]["length_relative_delta"] == pytest.approx(0.9)
    assert "bidirectional" in component["reasons"][0]


def test_one_to_one_requires_mapped_link_target_correspondence(tmp_path):
    report, _ = build_report(
        tmp_path,
        xodr(
            [
                road("a", x=0, successor="b"),
                road("b", x=30),
                road("c", x=60),
            ]
        ),
        xodr(
            [
                road("a", x=0, successor="c"),
                road("b", x=30),
                road("c", x=60),
            ]
        ),
    )
    components = {
        tuple(item["old_ids"]): item for item in report["roads"]["components"]
    }
    assert components[("a",)]["category"] == "ambiguous"
    assert components[("a",)]["one_to_one_evidence"]["mapped_link_targets_equal"] is False
    assert components[("b",)]["category"] == "unchanged"
    assert components[("c",)]["category"] == "unchanged"


def test_linked_ambiguous_stub_downgrades_dependent_road_at_fixpoint(tmp_path):
    report, _ = build_report(
        tmp_path,
        xodr(
            [road("target", x=0, length=10), road("anchor", x=100, successor="target")]
        ),
        xodr(
            [road("target", x=0, length=30), road("anchor", x=100, successor="target")]
        ),
    )
    components = {
        tuple(item["old_ids"]): item for item in report["roads"]["components"]
    }
    assert components[("target",)]["category"] == "ambiguous"
    assert components[("anchor",)]["category"] == "ambiguous"
    assert any(
        "linked road component" in failure
        for failure in components[("anchor",)]["final_topology_evidence"]["old_failures"]
    )


def test_ambiguity_band_reversal_and_stacked_geometry(tmp_path):
    band, _ = build_report(
        tmp_path / "band", xodr([road("old")]), xodr([road("new", y=0.1)])
    )
    edge = band["roads"]["components"][0]["edges"][0]
    assert edge["strength"] == "ambiguous_band"
    assert road_categories(band) == ["ambiguous"]

    reversed_report, _ = build_report(
        tmp_path / "reversed",
        xodr([road("old", x=0, heading=0)]),
        xodr([road("new", x=10, heading=math.pi)]),
    )
    component = reversed_report["roads"]["components"][0]
    assert component["category"] == "renumbered"
    assert component["edges"][0]["orientation"] == "reversed"

    stacked, _ = build_report(
        tmp_path / "stacked",
        xodr([road("lower", z=0)]),
        xodr([road("upper", z=10)]),
    )
    assert sorted(road_categories(stacked)) == ["added", "removed"]


def test_distance_thresholds_have_explicit_strict_ambiguous_and_none_regions(tmp_path):
    strict, _ = build_report(
        tmp_path / "strict",
        xodr([road("old")]),
        xodr([road("new", y=tool.STRICT_DISTANCE_M - 0.001)]),
    )
    assert road_categories(strict) == ["renumbered"]
    assert strict["roads"]["components"][0]["edges"][0]["strength"] == "strict"

    ambiguous, _ = build_report(
        tmp_path / "ambiguous",
        xodr([road("old")]),
        xodr([road("new", y=tool.STRICT_DISTANCE_M + 0.001)]),
    )
    assert road_categories(ambiguous) == ["ambiguous"]

    none, _ = build_report(
        tmp_path / "none",
        xodr([road("old")]),
        xodr([road("new", y=tool.LOOSE_DISTANCE_M + 0.001)]),
    )
    assert sorted(road_categories(none)) == ["added", "removed"]


def test_input_road_permutation_does_not_change_classification(tmp_path):
    old_a = xodr([road("a", x=0), road("b", x=30)])
    old_b = xodr([road("b", x=30), road("a", x=0)])
    deployed_a = xodr([road("x", x=0), road("y", x=30)])
    deployed_b = xodr([road("y", x=30), road("x", x=0)])
    first, _ = build_report(tmp_path / "first", old_a, deployed_a)
    second, _ = build_report(tmp_path / "second", old_b, deployed_b)
    assert first["roads"] == second["roads"]


def test_junction_renumber_split_merge_and_empty_ambiguity(tmp_path):
    old_roads = [road("a", x=0, lane=True), road("b", x=30, lane=True), road("c", x=60, lane=True)]
    new_roads = [road("x", x=0, lane=True), road("y", x=30, lane=True), road("z", x=60, lane=True)]
    renamed, _ = build_report(
        tmp_path / "renamed",
        xodr(old_roads, [junction("j-old", [("a", "b")])]),
        xodr(new_roads, [junction("j-new", [("x", "y")])]),
    )
    assert renamed["junctions"]["components"][0]["category"] == "renumbered"

    split, _ = build_report(
        tmp_path / "split",
        xodr(old_roads, [junction("j", [("a", "b"), ("b", "c")])]),
        xodr(new_roads, [junction("j1", [("x", "y")]), junction("j2", [("y", "z")])]),
    )
    assert split["junctions"]["components"][0]["category"] == "split"

    merged, _ = build_report(
        tmp_path / "merge",
        xodr(old_roads, [junction("j1", [("a", "b")]), junction("j2", [("b", "c")])]),
        xodr(new_roads, [junction("j", [("x", "y"), ("y", "z")])]),
    )
    assert merged["junctions"]["components"][0]["category"] == "merged"

    empty, _ = build_report(
        tmp_path / "empty",
        xodr([road("a", lane=True)], [junction("old", [])]),
        xodr([road("b", lane=True)], [junction("new", [])]),
    )
    assert empty["junctions"]["components"][0]["category"] == "ambiguous"

    partial, _ = build_report(
        tmp_path / "partial",
        xodr(
            old_roads,
            [junction("old", [("a", "b"), ("b", "c")])],
        ),
        xodr(
            new_roads,
            [junction("new", [("x", "y"), ("z", "y")])],
        ),
    )
    partial_component = partial["junctions"]["components"][0]
    assert partial_component["category"] == "ambiguous"
    assert partial_component["edges"][0]["strength"] == "ambiguous_band"

    overlapping, _ = build_report(
        tmp_path / "overlapping",
        xodr(old_roads, [junction("j", [("a", "b"), ("b", "c")])]),
        xodr(
            new_roads,
            [
                junction("jfull", [("x", "y"), ("y", "z")]),
                junction("jdup", [("x", "y")]),
            ],
        ),
    )
    overlapping_component = overlapping["junctions"]["components"][0]
    assert overlapping_component["category"] == "ambiguous"
    assert "disjoint" in overlapping_component["reasons"][0]


def test_junction_cannot_inherit_ambiguous_road_correspondence(tmp_path):
    report, _ = build_report(
        tmp_path,
        xodr(
            [road("a", x=0, lane=True), road("b", x=30, lane=True)],
            [junction("jo", [("a", "b")])],
        ),
        xodr(
            [
                road("x", x=0, lane=True, lane_type="shoulder"),
                road("y", x=30, lane=True),
            ],
            [junction("jn", [("x", "y")])],
        ),
    )
    assert any(
        component["category"] == "ambiguous"
        and component["old_ids"] == ["a"]
        for component in report["roads"]["components"]
    )
    junction_component = report["junctions"]["components"][0]
    assert junction_component["category"] == "ambiguous"
    assert "road correspondence" in junction_component["reasons"][0]


def test_swapped_junction_link_targets_downgrade_roads(tmp_path):
    old_roads = [
        road("a", x=0, lane=True, successor="j1", successor_type="junction"),
        road("b", x=30, lane=True, successor="j2", successor_type="junction"),
        road("c", x=60, lane=True),
        road("d", x=90, lane=True),
    ]
    deployed_roads = [
        road("a", x=0, lane=True, successor="k2", successor_type="junction"),
        road("b", x=30, lane=True, successor="k1", successor_type="junction"),
        road("c", x=60, lane=True),
        road("d", x=90, lane=True),
    ]
    report, _ = build_report(
        tmp_path,
        xodr(
            old_roads,
            [junction("j1", [("a", "c")]), junction("j2", [("b", "d")])],
        ),
        xodr(
            deployed_roads,
            [junction("k1", [("a", "c")]), junction("k2", [("b", "d")])],
        ),
    )
    components = {
        tuple(item["old_ids"]): item for item in report["roads"]["components"]
    }
    assert components[("a",)]["category"] == "ambiguous"
    assert components[("b",)]["category"] == "ambiguous"
    assert components[("a",)]["final_topology_evidence"]["mapped_boundary_links_equal"] is False


def test_junction_input_permutation_is_invariant(tmp_path):
    roads_old = [road("a", x=0, lane=True), road("b", x=30, lane=True), road("c", x=60, lane=True)]
    roads_new = [road("x", x=0, lane=True), road("y", x=30, lane=True), road("z", x=60, lane=True)]
    first, _ = build_report(
        tmp_path / "first",
        xodr(roads_old, [junction("j1", [("a", "b")]), junction("j2", [("b", "c")])]),
        xodr(roads_new, [junction("k1", [("x", "y")]), junction("k2", [("y", "z")])]),
    )
    second, _ = build_report(
        tmp_path / "second",
        xodr(list(reversed(roads_old)), [junction("j2", [("b", "c")]), junction("j1", [("a", "b")])]),
        xodr(list(reversed(roads_new)), [junction("k2", [("y", "z")]), junction("k1", [("x", "y")])]),
    )
    assert first["junctions"] == second["junctions"]


def test_arc_spiral_and_unsupported_geometry_paths(tmp_path):
    def shaped(road_id, child):
        return (
            f'<road id="{road_id}" length="10" junction="-1"><link/>'
            '<planView><geometry s="0" x="0" y="0" hdg="0" length="10">'
            f"{child}</geometry></planView><elevationProfile>"
            '<elevation s="0" a="0" b="0" c="0" d="0"/>'
            '</elevationProfile><lanes/></road>'
        )

    for name, child in (
        ("arc", '<arc curvature="0.05"/>'),
        ("spiral", '<spiral curvStart="0" curvEnd="0.1"/>'),
    ):
        report, _ = build_report(
            tmp_path / name,
            xodr([shaped("old", child)]),
            xodr([shaped("new", child)]),
        )
        assert road_categories(report) == ["renumbered"]

    with pytest.raises(tool.CorrespondenceError, match="unsupported or ambiguous"):
        build_report(
            tmp_path / "unsupported",
            xodr([shaped("old", '<poly3 a="0" b="0" c="0" d="0"/>')]),
            xodr([road("new")]),
        )


def test_closed_loop_orientation_uses_arclength_not_tied_endpoints(tmp_path):
    length = 2 * math.pi / 0.1

    def circle(road_id, heading, curvature):
        return (
            f'<road id="{road_id}" length="{length:.15f}" junction="-1"><link/>'
            f'<planView><geometry s="0" x="0" y="0" hdg="{heading:.15f}" '
            f'length="{length:.15f}"><arc curvature="{curvature}"/></geometry></planView>'
            '<elevationProfile><elevation s="0" a="0" b="0" c="0" d="0"/>'
            '</elevationProfile><lanes><laneSection s="0"><right>'
            '<lane id="-1" type="driving" level="false">'
            '<width sOffset="0" a="3.5" b="0" c="0" d="0"/>'
            '<roadMark sOffset="0" type="solid"/></lane>'
            '</right></laneSection></lanes></road>'
        )

    forward, _ = build_report(
        tmp_path / "forward",
        xodr([circle("loop", 0.0, "0.1")]),
        xodr([circle("loop", 0.0, "0.1")]),
    )
    forward_component = forward["roads"]["components"][0]
    assert forward_component["category"] == "unchanged"
    assert forward_component["edges"][0]["orientation"] == "forward"

    reversed_report, _ = build_report(
        tmp_path / "reversed",
        xodr([circle("loop", 0.0, "0.1")]),
        xodr([circle("loop", math.pi, "-0.1")]),
    )
    reversed_component = reversed_report["roads"]["components"][0]
    assert reversed_component["category"] == "ambiguous"
    assert reversed_component["edges"][0]["orientation"] == "reversed"
    assert reversed_component["edges"][0]["forward_endpoint_sum_m"] == pytest.approx(0.0, abs=1e-6)
    assert reversed_component["edges"][0]["reverse_endpoint_sum_m"] == pytest.approx(0.0, abs=1e-6)


def test_elevation_profile_must_start_at_zero(tmp_path):
    invalid = (
        '<road id="old" length="10" junction="-1"><link/>'
        '<planView><geometry s="0" x="0" y="0" hdg="0" length="10"><line/></geometry></planView>'
        '<elevationProfile><elevation s="1" a="0" b="0" c="0" d="0"/></elevationProfile>'
        '<lanes/></road>'
    )
    with pytest.raises(tool.CorrespondenceError, match="does not begin at s=0"):
        build_report(tmp_path, xodr([invalid]), xodr([road("new")]))


def test_invalid_topology_projection_and_manifest_binding_fail_closed(tmp_path):
    missing = xodr(
        [road("a")],
        ['<junction id="j"><connection id="1" incomingRoad="a" connectingRoad="missing"/></junction>'],
    )
    with pytest.raises(tool.CorrespondenceError, match="missing road"):
        build_report(tmp_path / "missing", missing, xodr([road("a")]))

    with pytest.raises(tool.CorrespondenceError, match="georeference"):
        build_report(
            tmp_path / "projection",
            xodr([road("a")], projection=PROJECTION),
            xodr([road("a")], projection=PROJECTION + " +x_0=1"),
        )

    old = tmp_path / "binding" / "old.xodr"
    deployed = tmp_path / "binding" / "deployed.xodr"
    manifest = tmp_path / "binding" / "manifest.json"
    old.parent.mkdir(parents=True)
    old.write_bytes(xodr([road("a")]))
    deployed.write_bytes(xodr([road("a")]))
    value = finalize_manifest(manifest_for(old, deployed))
    value["candidates"][0]["artifacts"][0]["sha256"] = "0" * 64
    value = finalize_manifest(value)
    bind_tool_to_synthetic_manifest(value)
    manifest.write_text(json.dumps(value))
    args = argparse.Namespace(
        old_xodr=str(old), deployed_xodr=str(deployed),
        lineage_manifest=str(manifest), output=str(tmp_path / "unused.json"),
    )
    with pytest.raises(tool.CorrespondenceError, match="hash binding"):
        tool.build(args)

    accepted_shape = finalize_manifest(manifest_for(old, deployed))
    bind_tool_to_synthetic_manifest(accepted_shape)
    del accepted_shape["recovered_material_dependency_graph"]
    manifest.write_text(json.dumps(accepted_shape))
    with pytest.raises(tool.CorrespondenceError, match="complete package inventory"):
        tool.build(args)


def test_synthetic_five_artifact_manifest_rejected_by_real_site_binding(tmp_path):
    old = tmp_path / "old.xodr"
    deployed = tmp_path / "deployed.xodr"
    manifest = tmp_path / "manifest.json"
    old.write_bytes(xodr([road("a")]))
    deployed.write_bytes(xodr([road("a")]))
    value = finalize_manifest(manifest_for(old, deployed))
    manifest.write_text(json.dumps(value))
    tool.ACCEPTED_BINDING = tool.RICHMOND_ACCEPTED_BINDING
    args = argparse.Namespace(
        old_xodr=str(old), deployed_xodr=str(deployed),
        lineage_manifest=str(manifest), output=str(tmp_path / "unused.json"),
    )
    with pytest.raises(tool.CorrespondenceError, match="accepted site binding"):
        tool.build(args)


def test_duplicate_manifest_json_key_is_rejected(tmp_path):
    old = tmp_path / "old.xodr"
    deployed = tmp_path / "deployed.xodr"
    manifest_path = tmp_path / "manifest.json"
    old.write_bytes(xodr([road("a")]))
    deployed.write_bytes(xodr([road("a")]))
    value = finalize_manifest(manifest_for(old, deployed))
    bind_tool_to_synthetic_manifest(value)
    encoded = json.dumps(value)
    manifest_path.write_text('{"schema":"duplicate",' + encoded[1:])
    args = argparse.Namespace(
        old_xodr=str(old), deployed_xodr=str(deployed),
        lineage_manifest=str(manifest_path), output=str(tmp_path / "unused.json"),
    )
    with pytest.raises(tool.CorrespondenceError, match="duplicate JSON key schema"):
        tool.build(args)


def test_symlink_double_snapshot_and_exclusive_publication(tmp_path, monkeypatch):
    report, args = build_report(
        tmp_path / "normal", xodr([road("a")]), xodr([road("a")])
    )
    output = Path(args.output)
    lineage.publish_no_replace(str(output), report)
    assert json.loads(output.read_text())["lineage_resolved"] is False
    with pytest.raises(lineage.LineageError, match="refusing to replace"):
        lineage.publish_no_replace(str(output), report)

    link = tmp_path / "old-link.xodr"
    link.symlink_to(Path(args.old_xodr))
    args.old_xodr = str(link)
    with pytest.raises(lineage.LineageError, match="symbolic-link"):
        tool.build(args)

    calls = 0
    original = tool._build_snapshot

    def changed(namespace):
        nonlocal calls
        calls += 1
        value = original(namespace)
        if calls == 2:
            value["limitations"] = value["limitations"] + ["simulated_race"]
        return value

    args.old_xodr = str(Path(args.old_xodr).resolve())
    monkeypatch.setattr(tool, "_build_snapshot", changed)
    with pytest.raises(tool.CorrespondenceError, match="changed between complete passes"):
        tool.build(args)


@pytest.mark.skipif(
    not (REAL_ROOT / "Richmond.xodr").is_file() or not REAL_DEPLOYED.is_file(),
    reason="real read-only Richmond source pair is unavailable",
)
def test_real_richmond_report_is_hash_bound_accounted_and_unresolved(tmp_path):
    tool.ACCEPTED_BINDING = tool.RICHMOND_ACCEPTED_BINDING
    materials = sorted(
        str(path) for path in (REAL_ROOT / "Richmond.fbm").rglob("*") if path.is_file()
    )
    lineage_args = SimpleNamespace(
        package_root=str(REAL_ROOT),
        fbx=str(REAL_ROOT / "Richmond.fbx"),
        old_xodr=str(REAL_ROOT / "Richmond.xodr"),
        live_xodr=str(REAL_DEPLOYED),
        geojson=str(REAL_ROOT / "Richmond.geojson"),
        rrdata_xml=[str(REAL_ROOT / "Richmond.rrdata.xml")],
        material_file=materials,
        output=str(tmp_path / "unused-lineage.json"),
    )
    accepted = lineage.build(lineage_args)
    manifest_path = tmp_path / "accepted-lineage.json"
    manifest_path.write_text(json.dumps(accepted))
    args = argparse.Namespace(
        old_xodr=str(REAL_ROOT / "Richmond.xodr"),
        deployed_xodr=str(REAL_DEPLOYED),
        lineage_manifest=str(manifest_path),
        output=str(tmp_path / "correspondence.json"),
    )
    report = tool.build(args)
    assert report["inputs"]["old_xodr"]["sha256"] == lineage.EXPECTED_OLD_XODR_SHA256
    assert report["inputs"]["deployed_xodr"]["sha256"] == lineage.EXPECTED_LIVE_XODR_SHA256
    assert report["roads"]["accounting"]["old_item_count"] == 222
    assert report["roads"]["accounting"]["deployed_item_count"] == 208
    assert report["junctions"]["accounting"]["old_item_count"] == 29
    assert report["junctions"]["accounting"]["deployed_item_count"] == 32
    assert report["roads"]["accounting"]["old_terminal_accounting_complete"] is True
    assert report["junctions"]["accounting"]["deployed_terminal_accounting_complete"] is True
    assert report["lineage_resolved"] is False
    assert report["scoring_permitted"] is False
    assert report["acceptance_eligible"] is False
