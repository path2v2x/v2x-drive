#!/usr/bin/env python3
"""Extract a bounded road-marking mesh neighborhood from a reviewed GLB.

The SimForge reviewed map bundle contains meshopt-compressed glTF geometry.
This diagnostic tool decodes only a named marking mesh, transforms it into the
shared OpenDRIVE x/y frame, clips triangles around the hash-bound camera site,
and writes immutable JSON/NPZ evidence.  It never loads or changes CARLA.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import xml.etree.ElementTree as ET

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from v2x_common.geodesy import TransverseMercator  # noqa: E402


COMPONENT_DTYPES = {
    5120: np.dtype("i1"),
    5121: np.dtype("u1"),
    5122: np.dtype("<i2"),
    5123: np.dtype("<u2"),
    5125: np.dtype("<u4"),
    5126: np.dtype("<f4"),
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json_exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_npz_exclusive(path, **arrays):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            np.savez_compressed(stream, **arrays)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_glb(path):
    with Path(path).open("rb") as stream:
        magic, version, declared_length = struct.unpack("<4sII", stream.read(12))
        if magic != b"glTF" or version != 2:
            raise ValueError("input is not a glTF 2 GLB")
        json_length, json_type = struct.unpack("<II", stream.read(8))
        if json_type != 0x4E4F534A:
            raise ValueError("first GLB chunk is not JSON")
        document = json.loads(stream.read(json_length).decode("utf-8").rstrip("\0 "))
        binary_length, binary_type = struct.unpack("<II", stream.read(8))
        if binary_type != 0x004E4942:
            raise ValueError("second GLB chunk is not BIN")
        binary = stream.read(binary_length)
        if len(binary) != binary_length or stream.tell() != declared_length:
            raise ValueError("GLB length/chunk declarations are inconsistent")
    return document, binary


def quaternion_matrix(value):
    x, y, z, w = (float(item) for item in value)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm <= 0:
        raise ValueError("node quaternion has zero norm")
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    return np.asarray([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0],
        [0, 0, 0, 1],
    ], dtype=float)


def node_matrix(node):
    if "matrix" in node:
        values = np.asarray(node["matrix"], dtype=float)
        if values.shape != (16,):
            raise ValueError("node matrix must contain 16 values")
        return values.reshape(4, 4).T
    translation = np.eye(4)
    translation[:3, 3] = np.asarray(node.get("translation", (0, 0, 0)), dtype=float)
    rotation = quaternion_matrix(node.get("rotation", (0, 0, 0, 1)))
    scale = np.eye(4)
    scale[np.arange(3), np.arange(3)] = np.asarray(
        node.get("scale", (1, 1, 1)), dtype=float
    )
    return translation @ rotation @ scale


def world_node_matrices(document):
    nodes = document.get("nodes", [])
    parents = {}
    for parent_index, node in enumerate(nodes):
        for child in node.get("children", []):
            if child in parents:
                raise ValueError(f"node {child} has multiple parents")
            parents[child] = parent_index
    cache = {}

    def resolve(index, active=()):
        if index in cache:
            return cache[index]
        if index in active:
            raise ValueError("node hierarchy contains a cycle")
        local = node_matrix(nodes[index])
        result = (
            resolve(parents[index], active + (index,)) @ local
            if index in parents else local
        )
        cache[index] = result
        return result

    return [resolve(index) for index in range(len(nodes))]


def active_scene_nodes(document):
    scenes = document.get("scenes", [])
    scene_index = int(document.get("scene", 0))
    if not 0 <= scene_index < len(scenes):
        raise ValueError("glTF active scene index is invalid")
    nodes = document.get("nodes", [])
    reachable = set()

    def visit(index, active=()):
        if not 0 <= index < len(nodes):
            raise ValueError("glTF scene references an invalid node")
        if index in active:
            raise ValueError("glTF active scene contains a node cycle")
        if index in reachable:
            return
        reachable.add(index)
        for child in nodes[index].get("children", []):
            visit(int(child), active + (index,))

    for root in scenes[scene_index].get("nodes", []):
        visit(int(root))
    return scene_index, reachable


def decode_meshopt_view(document, binary, view_index):
    try:
        import meshoptimizer
    except ImportError as exc:
        raise RuntimeError(
            "meshoptimizer is required to decode EXT_meshopt_compression"
        ) from exc
    view = document["bufferViews"][view_index]
    extension = (view.get("extensions") or {}).get("EXT_meshopt_compression")
    if extension is None:
        raise ValueError(f"bufferView {view_index} is not meshopt-compressed")
    if extension.get("buffer") != 0:
        raise ValueError("only the GLB BIN buffer is supported")
    offset = int(extension.get("byteOffset", 0))
    length = int(extension["byteLength"])
    encoded = binary[offset:offset + length]
    count = int(extension["count"])
    stride = int(extension["byteStride"])
    mode = extension["mode"]
    if mode == "ATTRIBUTES":
        dtype = np.dtype([("raw", "u1", (stride,))])
        return meshoptimizer.decode_vertex_buffer(
            count, stride, encoded, dtype=dtype
        )["raw"]
    if mode == "TRIANGLES":
        decoded = meshoptimizer.decode_index_buffer(count, stride, encoded)
        # meshoptimizer's Python wrapper allocates uint32 output even for a
        # 16-bit decoded stream. The C API writes a tightly packed stream, so
        # reinterpret the populated prefix at the declared index width.
        if stride == 2:
            return decoded.view(np.uint16)[:count].astype(np.uint32)
        if stride == 4:
            return decoded[:count]
        raise ValueError("meshopt triangle indices must use 2- or 4-byte stride")
    raise ValueError(f"unsupported meshopt mode {mode}")


def decode_position_accessor(document, binary, accessor_index):
    accessor = document["accessors"][accessor_index]
    if accessor.get("type") != "VEC3":
        raise ValueError("POSITION accessor must be VEC3")
    component_type = accessor["componentType"]
    dtype = COMPONENT_DTYPES.get(component_type)
    if dtype is None:
        raise ValueError(f"unsupported POSITION component type {component_type}")
    view_index = accessor["bufferView"]
    raw = decode_meshopt_view(document, binary, view_index)
    byte_offset = int(accessor.get("byteOffset", 0))
    item_byte_length = 3 * dtype.itemsize
    if byte_offset + item_byte_length > raw.shape[1]:
        raise ValueError("POSITION accessor exceeds its buffer-view stride")
    if int(accessor["count"]) != len(raw):
        raise ValueError("POSITION accessor count differs from decoded buffer-view count")
    packed = np.ascontiguousarray(raw[:, byte_offset:byte_offset + item_byte_length])
    values = packed.view(dtype).reshape(-1, 3).astype(float)
    if accessor.get("normalized"):
        if component_type == 5122:
            values = np.maximum(values / 32767.0, -1.0)
        elif component_type == 5123:
            values /= 65535.0
        elif component_type == 5120:
            values = np.maximum(values / 127.0, -1.0)
        elif component_type == 5121:
            values /= 255.0
        else:
            raise ValueError("normalized floating/uint32 POSITION is unsupported")
    return values


def decode_index_accessor(document, binary, accessor_index):
    accessor = document["accessors"][accessor_index]
    if accessor.get("type") != "SCALAR":
        raise ValueError("index accessor must be SCALAR")
    dtype = COMPONENT_DTYPES.get(accessor["componentType"])
    if dtype not in {COMPONENT_DTYPES[5121], COMPONENT_DTYPES[5123], COMPONENT_DTYPES[5125]}:
        raise ValueError("indices must use an unsigned integer component type")
    decoded = decode_meshopt_view(document, binary, accessor["bufferView"])
    item_offset = int(accessor.get("byteOffset", 0)) // dtype.itemsize
    count = int(accessor["count"])
    return np.asarray(decoded[item_offset:item_offset + count], dtype=np.int64)


class UnionFind:
    def __init__(self, size):
        self.parent = list(range(size))

    def find(self, item):
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left, right):
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def clip_polygon_half_plane(polygon, edge_start, edge_end):
    if not polygon:
        return []
    edge = edge_end - edge_start

    def side(point):
        relative = point[:2] - edge_start
        return edge[0] * relative[1] - edge[1] * relative[0]

    output = []
    previous = polygon[-1]
    previous_side = side(previous)
    for current in polygon:
        current_side = side(current)
        previous_inside = previous_side >= -1e-9
        current_inside = current_side >= -1e-9
        if previous_inside != current_inside:
            denominator = previous_side - current_side
            if abs(denominator) > 1e-15:
                fraction = previous_side / denominator
                output.append(previous + fraction * (current - previous))
        if current_inside:
            output.append(current)
        previous, previous_side = current, current_side
    return output


def clip_triangle_to_circle(triangle_xyz, center_xy, radius_m, sides=256):
    polygon = [np.asarray(point, dtype=float) for point in triangle_xyz]
    center = np.asarray(center_xy, dtype=float)
    boundary = [
        center + radius_m * np.asarray((math.cos(angle), math.sin(angle)))
        for angle in np.linspace(0.0, 2.0 * math.pi, sides, endpoint=False)
    ]
    for start, end in zip(boundary, boundary[1:] + boundary[:1]):
        polygon = clip_polygon_half_plane(polygon, start, end)
        if len(polygon) < 3:
            return []
    return polygon


def clipped_neighborhood(vertices_xyz, triangles, center_xy, radius_m):
    center = np.asarray(center_xy, dtype=float)
    selected_vertices = []
    selected_triangles = []
    vertex_lookup = {}

    def add_vertex(point):
        key = tuple(np.round(point, 7))
        index = vertex_lookup.get(key)
        if index is None:
            index = len(selected_vertices)
            vertex_lookup[key] = index
            selected_vertices.append(point)
        return index

    for triangle in vertices_xyz[triangles]:
        minimum = np.min(triangle[:, :2], axis=0)
        maximum = np.max(triangle[:, :2], axis=0)
        if np.any(maximum < center - radius_m) or np.any(minimum > center + radius_m):
            continue
        distances = np.linalg.norm(triangle[:, :2] - center, axis=1)
        polygon = (
            [point for point in triangle]
            if np.all(distances <= radius_m)
            else clip_triangle_to_circle(triangle, center, radius_m)
        )
        if len(polygon) < 3:
            continue
        first = add_vertex(polygon[0])
        for index in range(1, len(polygon) - 1):
            selected_triangles.append((
                first, add_vertex(polygon[index]), add_vertex(polygon[index + 1])
            ))
    return (
        np.asarray(selected_vertices, dtype=float).reshape(-1, 3),
        np.asarray(selected_triangles, dtype=np.int64).reshape(-1, 3),
    )


def component_metrics(vertices_xyz, triangles, material_name, component_id):
    indices = np.unique(triangles.reshape(-1))
    points = vertices_xyz[indices]
    xy = points[:, :2]
    centered = xy - xy.mean(axis=0)
    covariance = centered.T @ centered / max(1, len(centered))
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    order = np.argsort(eigenvalues)[::-1]
    eigenvalues, eigenvectors = eigenvalues[order], eigenvectors[:, order]
    projected = centered @ eigenvectors
    extents = np.ptp(projected, axis=0)
    triangle_points = vertices_xyz[triangles][:, :, :2]
    left = triangle_points[:, 1] - triangle_points[:, 0]
    right = triangle_points[:, 2] - triangle_points[:, 0]
    signed = left[:, 0] * right[:, 1] - left[:, 1] * right[:, 0]
    area = float(np.sum(np.abs(signed)) / 2.0)
    return {
        "component_id": component_id,
        "material": material_name,
        "vertex_count": int(len(indices)),
        "triangle_count": int(len(triangles)),
        "centroid_xy": np.mean(xy, axis=0).tolist(),
        "aabb_xy": {"min": np.min(xy, axis=0).tolist(), "max": np.max(xy, axis=0).tolist()},
        "principal_extent_m": extents.tolist(),
        "secondary_to_primary_extent_ratio": (
            float(extents[1] / extents[0]) if extents[0] > 0 else None
        ),
        "planar_triangle_area_m2": area,
        "eigenvalues": eigenvalues.tolist(),
    }


def extract_markings(document, binary, mesh_name, site_xy, radius_m):
    mesh_indices = [
        index for index, mesh in enumerate(document.get("meshes", []))
        if mesh.get("name") == mesh_name
    ]
    if len(mesh_indices) != 1:
        raise ValueError(f"expected one mesh named {mesh_name}, found {len(mesh_indices)}")
    mesh_index = mesh_indices[0]
    node_indices = [
        index for index, node in enumerate(document.get("nodes", []))
        if node.get("mesh") == mesh_index
    ]
    if len(node_indices) != 1:
        raise ValueError(f"expected one node for mesh {mesh_name}, found {len(node_indices)}")
    node_index = node_indices[0]
    scene_index, reachable_nodes = active_scene_nodes(document)
    if node_index not in reachable_nodes:
        raise ValueError(f"mesh node {node_index} is absent from the active glTF scene")
    transform = world_node_matrices(document)[node_index]
    output_vertices = []
    output_triangles = []
    triangle_material = []
    components = []
    vertex_base = 0
    component_base = 0
    materials = document.get("materials", [])
    material_names = {}
    for primitive_index, primitive in enumerate(document["meshes"][mesh_index]["primitives"]):
        if primitive.get("mode", 4) != 4:
            raise ValueError("road-marking primitive is not triangles")
        positions = decode_position_accessor(
            document, binary, primitive["attributes"]["POSITION"]
        )
        homogeneous = np.column_stack((positions, np.ones(len(positions))))
        world_gltf = (transform @ homogeneous.T).T[:, :3]
        # RoadRunner GLB uses X-right, Y-up, Z-back. OpenDRIVE uses x=east,
        # y=north, so the shared horizontal coordinates are (X, -Z).
        vertices_xyz = np.column_stack((
            world_gltf[:, 0], -world_gltf[:, 2], world_gltf[:, 1]
        ))
        indices = decode_index_accessor(document, binary, primitive["indices"])
        if len(indices) % 3:
            raise ValueError("triangle index count is not divisible by three")
        triangles = indices.reshape(-1, 3)
        if np.any(triangles < 0) or np.any(triangles >= len(vertices_xyz)):
            raise ValueError("triangle index falls outside POSITION accessor")
        local_vertices, local_triangles = clipped_neighborhood(
            vertices_xyz, triangles, site_xy, radius_m
        )
        if not len(local_triangles):
            continue
        material_index = int(primitive.get("material", -1))
        material_name = (
            materials[material_index].get("name", f"material-{material_index}")
            if 0 <= material_index < len(materials) else "unassigned"
        )
        material_names[material_index] = material_name
        union = UnionFind(len(local_vertices))
        for triangle in local_triangles:
            union.union(int(triangle[0]), int(triangle[1]))
            union.union(int(triangle[1]), int(triangle[2]))
        groups = {}
        for triangle in local_triangles:
            groups.setdefault(union.find(int(triangle[0])), []).append(triangle)
        for group in groups.values():
            group_triangles = np.asarray(group, dtype=np.int64)
            components.append(component_metrics(
                local_vertices, group_triangles, material_name, component_base
            ))
            component_base += 1
        output_vertices.append(local_vertices)
        output_triangles.append(local_triangles + vertex_base)
        triangle_material.extend([material_index] * len(local_triangles))
        vertex_base += len(local_vertices)
    if not output_vertices:
        raise ValueError("no road-marking triangles fall inside the requested radius")
    return {
        "vertices_xyz": np.vstack(output_vertices),
        "triangles": np.vstack(output_triangles),
        "triangle_material": np.asarray(triangle_material, dtype=np.int32),
        "components": sorted(
            components, key=lambda item: item["planar_triangle_area_m2"], reverse=True
        ),
        "mesh_index": mesh_index,
        "node_index": node_index,
        "node_world_matrix": transform.tolist(),
        "active_scene_index": scene_index,
        "material_index_to_name": {
            str(index): name for index, name in sorted(material_names.items())
        },
    }


def xodr_georeference(path):
    root = ET.parse(path).getroot()
    if root.tag != "OpenDRIVE":
        raise ValueError("bundle XODR is not an OpenDRIVE document")
    return (root.findtext("./header/geoReference") or "").strip()


def bundle_binding(manifest_path, glb_path, xodr_path, site):
    manifest_path = Path(manifest_path).resolve()
    glb_path = Path(glb_path).resolve()
    xodr_path = Path(xodr_path).resolve()
    manifest = json.loads(manifest_path.read_text())
    layers = [
        item for item in manifest.get("staticLayers", [])
        if Path(item.get("file", "")).name == glb_path.name
    ]
    if len(layers) != 1:
        raise ValueError("bundle manifest does not identify the road GLB exactly once")
    layer = layers[0]
    if int(layer.get("fileSize", -1)) != glb_path.stat().st_size:
        raise ValueError("road GLB size differs from the bundle manifest")
    georeference = xodr_georeference(xodr_path)
    if georeference != site["map_georeference"]:
        raise ValueError("bundle XODR and site config georeferences differ")
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256(manifest_path),
        "manifest_version": manifest.get("version"),
        "manifest_generator": manifest.get("generator"),
        "static_layer": layer,
        "xodr_path": str(xodr_path),
        "xodr_sha256": sha256(xodr_path),
        "xodr_georeference": georeference,
        "cryptographic_asset_hashes_in_manifest": False,
    }


def site_binding(path, fallback_georeference=None):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    site = config.get("site")
    if not isinstance(site, dict):
        raise ValueError("camera config has no site object")
    georeference = site.get("map_georeference") or fallback_georeference
    if not georeference:
        raise ValueError("camera config and bundle XODR provide no map georeference")
    projection = TransverseMercator.from_proj_string(georeference)
    return {
        "path": str(path),
        "sha256": sha256(path),
        "latitude": float(site["lat"]),
        "longitude": float(site["lon"]),
        "map_georeference": georeference,
        "map_georeference_source": (
            "camera_config" if site.get("map_georeference") else "bundle_xodr"
        ),
        "anchor_xy": list(projection.forward(float(site["lat"]), float(site["lon"]))),
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("glb", type=Path)
    parser.add_argument("--bundle-manifest", type=Path, required=True)
    parser.add_argument("--xodr", type=Path, required=True)
    parser.add_argument("--site-config", type=Path, required=True)
    parser.add_argument("--radius-m", type=float, default=40.0)
    parser.add_argument("--mesh-name", default="Roads_Marking_Layer0")
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-npz", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    if not 10.0 <= args.radius_m <= 100.0:
        raise SystemExit("--radius-m must be in [10.0, 100.0]")
    if args.output_json.exists() or args.output_npz.exists():
        raise SystemExit("refusing to overwrite immutable marking evidence")
    document, binary = read_glb(args.glb)
    try:
        site = site_binding(args.site_config, xodr_georeference(args.xodr))
    except ValueError as error:
        raise SystemExit(str(error)) from error
    try:
        bundle = bundle_binding(
            args.bundle_manifest, args.glb, args.xodr, site
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    extracted = extract_markings(
        document, binary, args.mesh_name, site["anchor_xy"], args.radius_m
    )
    metadata = {
        "schema": "v2x-reviewed-gltf-road-markings/v1",
        "acceptance_eligible": False,
        "created_at": utc_now(),
        "source_glb": str(args.glb.resolve()),
        "source_glb_sha256": sha256(args.glb),
        "site_config": site,
        "radius_m": args.radius_m,
        "mesh_name": args.mesh_name,
        "mesh_index": extracted["mesh_index"],
        "node_index": extracted["node_index"],
        "node_world_matrix": extracted["node_world_matrix"],
        "active_scene_index": extracted["active_scene_index"],
        "material_index_to_name": extracted["material_index_to_name"],
        "bundle_binding": bundle,
        "coordinate_conversion_hypothesis": (
            "candidate_OpenDRIVE_xyz=(glTF_world_x,-glTF_world_z,glTF_world_y)"
        ),
        "coordinate_alignment_validated": False,
        "vertex_count": int(len(extracted["vertices_xyz"])),
        "triangle_count": int(len(extracted["triangles"])),
        "component_count": len(extracted["components"]),
        "components": extracted["components"],
        "limitations": [
            "reviewed_mesh_is_not_surveyed_physical_truth",
            "manifest_binds_asset_path_and_size_but_not_a_cryptographic_hash",
            "gltf_to_opendrive_transform_lacks_independent_landmark_validation",
            "component_identity_is_not_inferred",
            "components_are_computed_per_primitive_not_globally",
            "circular_clip_boundary_is_approximated_by_a_256_sided_polygon",
        ],
    }
    write_npz_exclusive(
        args.output_npz,
        vertices_xyz=extracted["vertices_xyz"],
        triangles=extracted["triangles"],
        triangle_material=extracted["triangle_material"],
        metadata=np.asarray(json.dumps(metadata, sort_keys=True)),
    )
    metadata["output_npz"] = str(args.output_npz.resolve())
    metadata["output_npz_sha256"] = sha256(args.output_npz)
    write_json_exclusive(args.output_json, metadata)
    print(json.dumps({
        "output_json": str(args.output_json),
        "output_npz": str(args.output_npz),
        "vertex_count": metadata["vertex_count"],
        "triangle_count": metadata["triangle_count"],
        "component_count": metadata["component_count"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
