import importlib.util
import math
from pathlib import Path

import numpy as np


TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "extract_gltf_road_markings.py"
SPEC = importlib.util.spec_from_file_location("extract_gltf_road_markings", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_node_matrix_applies_scale_then_translation():
    matrix = tool.node_matrix({"translation": [1, 2, 3], "scale": [2, 3, 4]})
    result = matrix @ np.asarray([5, 6, 7, 1], dtype=float)
    assert result.tolist() == [11, 20, 31, 1]


def test_world_node_matrices_follow_parent_chain():
    document = {
        "nodes": [
            {"scale": [10, 10, 10], "children": [1]},
            {"translation": [1, 2, 3]},
        ]
    }
    matrices = tool.world_node_matrices(document)
    result = matrices[1] @ np.asarray([0, 0, 0, 1], dtype=float)
    assert result.tolist() == [10, 20, 30, 1]


def test_active_scene_nodes_excludes_unreachable_mesh_node():
    document = {
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"children": [1]}, {}, {}],
    }
    scene, nodes = tool.active_scene_nodes(document)
    assert scene == 0
    assert nodes == {0, 1}
    assert 2 not in nodes


def test_component_metrics_for_rectangle():
    vertices = np.asarray([
        [0, 0, 0], [2, 0, 0], [2, 1, 0], [0, 1, 0]
    ], dtype=float)
    triangles = np.asarray([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    metrics = tool.component_metrics(vertices, triangles, "white", 0)
    assert math.isclose(metrics["planar_triangle_area_m2"], 2.0, abs_tol=1e-12)
    assert metrics["triangle_count"] == 2
    assert metrics["principal_extent_m"] == [2.0, 1.0]


def test_exclusive_npz_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "markings.npz"
    tool.write_npz_exclusive(output, values=np.asarray([1, 2, 3]))
    try:
        tool.write_npz_exclusive(output, values=np.asarray([4]))
    except FileExistsError:
        pass
    else:
        raise AssertionError("writer overwrote immutable mesh evidence")


def test_long_triangle_is_clipped_to_neighborhood():
    vertices = np.asarray([
        [-10, -0.2, 0], [10, -0.2, 0], [0, 0.2, 0]
    ], dtype=float)
    triangles = np.asarray([[0, 1, 2]], dtype=np.int64)
    clipped_vertices, clipped_triangles = tool.clipped_neighborhood(
        vertices, triangles, [0, 0], 1.0
    )
    assert len(clipped_triangles) > 0
    assert np.max(np.linalg.norm(clipped_vertices[:, :2], axis=1)) <= 1.0 + 1e-9
    metrics = tool.component_metrics(clipped_vertices, clipped_triangles, "white", 0)
    assert metrics["planar_triangle_area_m2"] > 0


def test_position_decode_preserves_per_vertex_stride(monkeypatch):
    first = np.asarray([1000, 2000, 3000], dtype="<i2").view("u1")
    second = np.asarray([4000, 5000, 6000], dtype="<i2").view("u1")
    raw = np.vstack((
        np.concatenate((first, np.asarray([99, 98], dtype="u1"))),
        np.concatenate((second, np.asarray([97, 96], dtype="u1"))),
    ))
    monkeypatch.setattr(tool, "decode_meshopt_view", lambda *_args: raw)
    document = {
        "accessors": [{
            "type": "VEC3", "componentType": 5122, "count": 2,
            "normalized": False, "bufferView": 0,
        }]
    }
    assert tool.decode_position_accessor(document, b"", 0).tolist() == [
        [1000, 2000, 3000], [4000, 5000, 6000]
    ]


def test_site_binding_can_bind_georeference_from_bundle_xodr(tmp_path):
    config = tmp_path / "cameras.json"
    config.write_text('{"site":{"lat":37.0,"lon":-122.0}}')
    georeference = (
        "+proj=tmerc +lat_0=37 +lon_0=-122 +k=1 +x_0=0 +y_0=0 "
        "+datum=WGS84 +units=m"
    )
    result = tool.site_binding(config, georeference)
    assert result["map_georeference_source"] == "bundle_xodr"
    assert result["anchor_xy"] == [0.0, 0.0]
