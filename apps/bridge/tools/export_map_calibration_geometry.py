#!/usr/bin/env python3
"""Export and project globally identified UE5 map calibration geometry.

This is a read-only proposal builder.  It binds the active OpenDRIVE map,
camera configuration, and retained real/twin frames, then exports nearby
crosswalk polygons, lane center/boundary polylines, and static traffic-control
objects in CARLA world coordinates.  It also renders labeled overlays using
the current camera model.  The output is map truth, but the real-image feature
identity still requires topology review before it can become acceptance input.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digital_twin_bridge.twin_camera_rig import (  # noqa: E402
    compute_twin_camera_transform,
    load_cameras_config,
    twin_horizontal_fov_deg,
)


CAMERAS = ("ch1", "ch2", "ch3", "ch4")
CARLA_SOURCE_SCHEMA = "v2x-retained-carla-map-export/v2"
NATIVE_OBJECT_SEMANTIC_SCHEMA = "v2x-carla-native-environment-object/v1"
NATIVE_OBJECT_API = "carla.World.get_environment_objects"
NATIVE_OBJECT_CATEGORIES = {
    "CityObjectLabel.TrafficLight": "TrafficLight",
    "CityObjectLabel.TrafficSigns": "TrafficSigns",
    "CityObjectLabel.Other": "Other",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def rotation_matrix(pitch_deg, yaw_deg, roll_deg):
    pitch, yaw, roll = np.radians([pitch_deg, yaw_deg, roll_deg])
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    return np.array([
        [cp * cy, cy * sp * sr - sy * cr, -cy * sp * cr - sy * sr],
        [cp * sy, sy * sp * sr + cy * cr, -sy * sp * cr + cy * sr],
        [sp, -cp * sr, cp * cr],
    ])


def project(points, transform, fov_deg, width, height):
    if not points:
        return [], []
    world = np.asarray(points, dtype=float)
    location = np.asarray([
        transform.location.x, transform.location.y, transform.location.z
    ])
    rotation = rotation_matrix(
        transform.rotation.pitch, transform.rotation.yaw, transform.rotation.roll
    )
    local = (rotation.T @ (world - location).T).T
    depth = local[:, 0]
    focal = (width / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        pixels = np.column_stack((
            width / 2.0 + focal * local[:, 1] / depth,
            height / 2.0 - focal * local[:, 2] / depth,
        ))
    return pixels.tolist(), depth.tolist()


def distance_xy(left, right):
    return math.hypot(left.x - right.x, left.y - right.y)


def split_crosswalk_polygons(locations):
    polygons, current = [], []
    for location in locations:
        point = [float(location.x), float(location.y), float(location.z)]
        if not current:
            current = [point]
            continue
        current.append(point)
        if len(current) >= 4 and np.linalg.norm(
            np.asarray(current[-1]) - np.asarray(current[0])
        ) <= 0.05:
            polygons.append(current)
            current = []
    if current:
        raise RuntimeError("OpenDRIVE crosswalk list ended with an open polygon")
    return polygons


def stable_crosswalk_id(points):
    """Return an orientation/start-order independent content identity."""
    ring = [tuple(round(float(value), 3) for value in point) for point in points]
    if len(ring) >= 2 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3 or len(set(ring)) < 3:
        raise RuntimeError("crosswalk polygon is degenerate")
    candidates = []
    for oriented in (ring, list(reversed(ring))):
        candidates.extend(
            tuple(oriented[index:] + oriented[:index]) for index in range(len(oriented))
        )
    canonical = min(candidates)
    digest = canonical_hash(canonical)[:16]
    return f"crosswalk-geometry-{digest}"


def _world_polyline(waypoints, side=None, z_offset=0.04):
    points = []
    for waypoint in waypoints:
        transform = waypoint.transform
        location = transform.location
        yaw = math.radians(transform.rotation.yaw)
        right_x, right_y = -math.sin(yaw), math.cos(yaw)
        offset = 0.0 if side is None else float(waypoint.lane_width) / 2.0
        if side == "left":
            offset *= -1.0
        points.append([
            location.x + right_x * offset,
            location.y + right_y * offset,
            location.z + z_offset,
        ])
    return points


def _normalized_marking_value(value):
    if value is None:
        return None
    return "".join(character for character in str(value).split(".")[-1].lower() if character.isalnum())


def marking_signature(marking):
    width = getattr(marking, "width", None)
    width = float(width) if width is not None else None
    if width is not None and (not math.isfinite(width) or width < 0):
        raise RuntimeError("CARLA lane marking width is invalid")
    return {
        "type": _normalized_marking_value(getattr(marking, "type", None)),
        "color": _normalized_marking_value(getattr(marking, "color", None)),
        "width_m": width,
        "lane_change": _normalized_marking_value(getattr(marking, "lane_change", None)),
    }


def _optional_float(element, name):
    value = element.get(name)
    return None if value is None else float(value)


def _road_mark_line_record(element):
    return {
        "length_m": _optional_float(element, "length"),
        "space_m": _optional_float(element, "space"),
        "t_offset_m": _optional_float(element, "tOffset"),
        "s_offset_m": _optional_float(element, "sOffset"),
        "rule": element.get("rule"),
        "width_m": _optional_float(element, "width"),
    }


def road_mark_record(element):
    """Preserve every OpenDRIVE roadMark semantic field and child record."""
    explicit = element.find("explicit")
    return {
        "s_offset_m": float(element.get("sOffset")),
        "type": _normalized_marking_value(element.get("type")),
        "weight": element.get("weight"),
        "color": _normalized_marking_value(element.get("color")),
        "material": element.get("material"),
        "width_m": _optional_float(element, "width"),
        "lane_change": _normalized_marking_value(element.get("laneChange")),
        "height_m": _optional_float(element, "height"),
        "sway": [{
            "ds_m": float(item.get("ds")),
            "a": float(item.get("a")),
            "b": float(item.get("b")),
            "c": float(item.get("c")),
            "d": float(item.get("d")),
        } for item in element.findall("sway")],
        "types": [{
            "name": item.get("name"),
            "width_m": _optional_float(item, "width"),
            "lines": [_road_mark_line_record(line) for line in item.findall("line")],
        } for item in element.findall("type")],
        "explicit_lines": [] if explicit is None else [
            _road_mark_line_record(line) for line in explicit.findall("line")
        ],
    }


def segmented_road_marks(key, waypoints):
    """Preserve each contiguous sampled road-mark range with a stable ID."""
    output = []
    for side in ("left", "right"):
        values = [marking_signature(getattr(item, f"{side}_lane_marking")) for item in waypoints]
        start = 0
        for end in range(1, len(waypoints) + 1):
            if end < len(waypoints) and values[end] == values[start]:
                continue
            selected = waypoints[start:end]
            start_s, end_s = float(selected[0].s), float(selected[-1].s)
            identity_input = {
                "road_id": key[0], "section_id": key[1], "lane_id": key[2],
                "side": side, **values[start],
                "start_s_m": round(start_s, 3), "end_s_m": round(end_s, 3),
            }
            output.append({
                "id": f"unbound-road-mark-{canonical_hash(identity_input)[:16]}",
                **identity_input,
                "sample_count": len(selected),
                "boundary_world": _world_polyline(selected, side=side),
                "usable_as_polyline": len(selected) >= 2,
            })
            start = end
    return output


def lane_geometry_from_waypoints(key, waypoints):
    widths = [float(item.lane_width) for item in waypoints]
    segments = segmented_road_marks(key, waypoints)
    return {
        "id": f"road-{key[0]}-section-{key[1]}-lane-{key[2]}",
        "road_id": key[0],
        "section_id": key[1],
        "lane_id": key[2],
        "s_range_m": [float(waypoints[0].s), float(waypoints[-1].s)],
        "lane_width_m": float(np.median(widths)),
        "lane_width_range_m": [min(widths), max(widths)],
        "center_world": _world_polyline(waypoints, side=None, z_offset=0.03),
        "left_boundary_world": _world_polyline(waypoints, side="left"),
        "right_boundary_world": _world_polyline(waypoints, side="right"),
        "road_mark_segment_ids": [item["id"] for item in segments],
        "road_mark_segments": segments,
    }


def opendrive_road_mark_ranges(opendrive_bytes):
    """Extract exact lane-section roadMark ranges without collapsing changes."""
    root = ET.fromstring(opendrive_bytes)
    if root.tag != "OpenDRIVE":
        raise RuntimeError("active map did not return an OpenDRIVE document")
    ranges = []
    for road in root.findall("road"):
        road_id, road_length = road.get("id"), float(road.get("length"))
        sections = road.findall("./lanes/laneSection")
        for section_index, section in enumerate(sections):
            section_start = float(section.get("s"))
            section_end = (
                float(sections[section_index + 1].get("s"))
                if section_index + 1 < len(sections) else road_length
            )
            for lane in (
                section.findall("./left/lane")
                + section.findall("./center/lane")
                + section.findall("./right/lane")
            ):
                marks = sorted(
                    lane.findall("roadMark"), key=lambda item: float(item.get("sOffset"))
                )
                for mark_index, mark in enumerate(marks):
                    start_s = section_start + float(mark.get("sOffset"))
                    end_s = (
                        section_start + float(marks[mark_index + 1].get("sOffset"))
                        if mark_index + 1 < len(marks) else section_end
                    )
                    if not section_start <= start_s < end_s <= section_end + 1e-8:
                        raise RuntimeError(
                            f"road {road_id} lane {lane.get('id')} has an invalid roadMark range"
                        )
                    record = road_mark_record(mark)
                    values = {
                        "road_id": road_id,
                        "section_index": section_index,
                        "section_start_s_m": round(section_start, 6),
                        "lane_id": lane.get("id"),
                        "start_s_m": round(start_s, 6),
                        "end_s_m": round(end_s, 6),
                        **record,
                    }
                    ranges.append({
                        "id": f"opendrive-road-mark-{canonical_hash(values)[:16]}",
                        **values,
                    })
    return sorted(ranges, key=lambda item: item["id"])


def _source_lane_for_boundary(lane_id, side):
    if lane_id < 0:
        return lane_id if side == "right" else lane_id + 1
    if lane_id > 0:
        return lane_id if side == "left" else lane_id - 1
    return 0


def _marking_signatures_compatible(sampled, exact):
    for key in ("type", "color", "lane_change"):
        if exact.get(key) is not None and sampled.get(key) != exact.get(key):
            return False
    exact_width, sampled_width = exact.get("width_m"), sampled.get("width_m")
    if exact_width is not None and (
        sampled_width is None or not math.isclose(sampled_width, exact_width, abs_tol=0.01)
    ):
        return False
    return True


def bind_sampled_road_marks(lanes, exact_ranges, spacing_m):
    """Bind every CARLA-sampled boundary segment to one exact XODR range."""
    output = []
    for lane in lanes:
        bound_ids = []
        for sampled in lane.pop("road_mark_segments"):
            source_lane_id = _source_lane_for_boundary(int(lane["lane_id"]), sampled["side"])
            candidates = [
                item for item in exact_ranges
                if str(item["road_id"]) == str(lane["road_id"])
                and int(item["section_index"]) == int(lane["section_id"])
                and int(item["lane_id"]) == source_lane_id
                and item["start_s_m"] - spacing_m <= sampled["start_s_m"]
                and sampled["end_s_m"] <= item["end_s_m"] + spacing_m
                and _marking_signatures_compatible(sampled, item)
            ]
            if len(candidates) != 1:
                raise RuntimeError(
                    f"sampled road marking {sampled['id']} maps to {len(candidates)} exact ranges"
                )
            exact = candidates[0]
            identity = (
                f"{exact['id']}-sample-{sampled['side']}-"
                f"road-{lane['road_id']}-lane-{lane['lane_id']}"
            )
            bound = {
                **sampled,
                "id": identity,
                "opendrive_range_id": exact["id"],
                "opendrive_source_lane_id": source_lane_id,
                "opendrive_range": exact,
                "boundary_world_sha256": canonical_hash(sampled["boundary_world"]),
            }
            output.append(bound)
            bound_ids.append(identity)
        lane["road_mark_segment_ids"] = bound_ids
    identities = [item["id"] for item in output]
    if len(identities) != len(set(identities)):
        raise RuntimeError("bound sampled road-mark identities are not unique")
    return sorted(output, key=lambda item: item["id"])


def waypoint_source_record(waypoint):
    location = waypoint.transform.location
    return {
        "s_m": float(waypoint.s),
        "location": [float(location.x), float(location.y), float(location.z)],
        "yaw_deg": float(waypoint.transform.rotation.yaw),
        "lane_width_m": float(waypoint.lane_width),
        "left_lane_marking": marking_signature(waypoint.left_lane_marking),
        "right_lane_marking": marking_signature(waypoint.right_lane_marking),
    }


def _waypoint_from_source(record):
    from types import SimpleNamespace

    location = record["location"]
    if (
        not isinstance(location, list) or len(location) != 3
        or not all(math.isfinite(float(value)) for value in location)
    ):
        raise RuntimeError("retained CARLA waypoint location is invalid")
    width = float(record["lane_width_m"])
    if not math.isfinite(width) or width <= 0:
        raise RuntimeError("retained CARLA waypoint width is invalid")

    def marking(value):
        required = {"type", "color", "width_m", "lane_change"}
        if not isinstance(value, dict) or set(value) != required:
            raise RuntimeError("retained CARLA lane marking is malformed")
        return SimpleNamespace(
            type=value["type"], color=value["color"], width=value["width_m"],
            lane_change=value["lane_change"],
        )

    return SimpleNamespace(
        s=float(record["s_m"]),
        lane_width=width,
        transform=SimpleNamespace(
            location=SimpleNamespace(x=location[0], y=location[1], z=location[2]),
            rotation=SimpleNamespace(yaw=float(record["yaw_deg"])),
        ),
        left_lane_marking=marking(record["left_lane_marking"]),
        right_lane_marking=marking(record["right_lane_marking"]),
    )


def lanes_from_source(groups):
    lanes = []
    for group in groups:
        try:
            key = (int(group["road_id"]), int(group["section_id"]), int(group["lane_id"]))
            waypoints = [_waypoint_from_source(item) for item in group["waypoints"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("retained CARLA lane source is malformed") from exc
        waypoints.sort(key=lambda item: item.s)
        if len(waypoints) < 3:
            raise RuntimeError("retained CARLA lane source has fewer than three waypoints")
        lanes.append(lane_geometry_from_waypoints(key, waypoints))
    identities = [item["id"] for item in lanes]
    if len(identities) != len(set(identities)):
        raise RuntimeError("retained CARLA lane source identities are not unique")
    return sorted(lanes, key=lambda item: item["id"])


def nearby_lane_source(carla_map, anchor, radius_m, spacing_m):
    import carla

    groups = {}
    for waypoint in carla_map.generate_waypoints(spacing_m):
        location = waypoint.transform.location
        if distance_xy(location, anchor) > radius_m:
            continue
        if waypoint.lane_type != carla.LaneType.Driving:
            continue
        key = (int(waypoint.road_id), int(waypoint.section_id), int(waypoint.lane_id))
        groups.setdefault(key, []).append(waypoint)

    output = []
    for key, waypoints in sorted(groups.items()):
        waypoints.sort(key=lambda item: float(item.s))
        if len(waypoints) < 3:
            continue
        output.append({
            "road_id": key[0], "section_id": key[1], "lane_id": key[2],
            "waypoints": [waypoint_source_record(item) for item in waypoints],
        })
    return output


def nearby_lane_polylines(carla_map, anchor, radius_m, spacing_m):
    return lanes_from_source(nearby_lane_source(carla_map, anchor, radius_m, spacing_m))


def static_object_source(world, label_name, anchor, radius_m):
    import carla

    label = getattr(carla.CityObjectLabel, label_name)
    output = []
    for item in world.get_environment_objects(label):
        location = item.bounding_box.location
        if distance_xy(location, anchor) > radius_m:
            continue
        output.append({
            "source_object_id": str(item.id),
            "name": str(item.name),
            "semantic_source": {
                "schema": NATIVE_OBJECT_SEMANTIC_SCHEMA,
                "api": NATIVE_OBJECT_API,
                "native_type": f"CityObjectLabel.{label_name}",
                "native_subtype": None,
            },
            "center_world": [location.x, location.y, location.z],
            "extent": [
                item.bounding_box.extent.x,
                item.bounding_box.extent.y,
                item.bounding_box.extent.z,
            ],
        })
    return sorted(
        output,
        key=lambda item: (
            item["semantic_source"]["native_type"], item["source_object_id"]
        ),
    )


def native_object_category(semantic_source):
    if (
        not isinstance(semantic_source, dict)
        or set(semantic_source) != {
            "schema", "api", "native_type", "native_subtype"
        }
        or semantic_source.get("schema") != NATIVE_OBJECT_SEMANTIC_SCHEMA
        or semantic_source.get("api") != NATIVE_OBJECT_API
        or semantic_source.get("native_subtype") is not None
        or semantic_source.get("native_type") not in NATIVE_OBJECT_CATEGORIES
    ):
        raise RuntimeError(
            "retained CARLA object lacks recognized native object semantics"
        )
    return NATIVE_OBJECT_CATEGORIES[semantic_source["native_type"]]


def objects_from_source(values):
    output = []
    for item in values:
        center, extent = item.get("center_world"), item.get("extent")
        if (
            not isinstance(center, list) or len(center) != 3
            or not isinstance(extent, list) or len(extent) != 3
            or not all(math.isfinite(float(value)) for value in center + extent)
            or any(float(value) < 0 for value in extent)
        ):
            raise RuntimeError("retained CARLA environment object geometry is invalid")
        source_id = item.get("source_object_id")
        category = native_object_category(item.get("semantic_source"))
        if not isinstance(source_id, str) or not source_id:
            raise RuntimeError("retained CARLA environment object identity is invalid")
        output.append({
            "id": f"environment-{category}-{source_id}",
            "source_object_id": source_id,
            "name": str(item.get("name", "")),
            "category": category,
            "semantic_source": item["semantic_source"],
            "center_world": [float(value) for value in center],
            "extent": [float(value) for value in extent],
        })
    output.sort(key=lambda item: item["id"])
    if len({item["id"] for item in output}) != len(output):
        raise RuntimeError("retained CARLA environment object identities are not unique")
    return output


def geometry_from_carla_source(source, exact_ranges):
    if source.get("schema") != CARLA_SOURCE_SCHEMA:
        raise RuntimeError("retained CARLA map export schema is unsupported")
    polygons = []
    for points in source.get("crosswalk_polygons", []):
        polygons.append({"id": stable_crosswalk_id(points), "world": points})
    polygons.sort(key=lambda item: item["id"])
    if len({item["id"] for item in polygons}) != len(polygons):
        raise RuntimeError("retained CARLA crosswalk identities are not unique")
    lanes = lanes_from_source(source.get("lane_waypoint_groups", []))
    segments = bind_sampled_road_marks(
        lanes, exact_ranges, float(source["lane_spacing_m"])
    )
    return {
        "crosswalks": polygons,
        "lanes": lanes,
        "road_mark_segments": segments,
        "opendrive_road_mark_ranges": exact_ranges,
        "objects": objects_from_source(source.get("environment_objects", [])),
    }


def visible_polyline(points, depths, width, height):
    visible = []
    for point, depth in zip(points, depths):
        if (
            depth > 0.2 and math.isfinite(depth)
            and math.isfinite(point[0]) and math.isfinite(point[1])
            and -width <= point[0] <= 2 * width
            and -height <= point[1] <= 2 * height
        ):
            visible.append(tuple(point))
        else:
            visible.append(None)
    return visible


def draw_segments(draw, points, color, width):
    previous = None
    for point in points:
        if point is not None:
            if previous is not None:
                draw.line((previous, point), fill=color, width=width)
            previous = point
        else:
            previous = None


def render_overlay_image(image_path, projection):
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    for lane in projection["lanes"]:
        draw_segments(draw, lane["left"], (0, 180, 255), 2)
        draw_segments(draw, lane["right"], (0, 180, 255), 2)
        draw_segments(draw, lane["center"], (255, 210, 0), 1)
    for crosswalk in projection["crosswalks"]:
        draw_segments(draw, crosswalk["pixels"], (255, 40, 40), 4)
        valid = [point for point in crosswalk["pixels"] if point is not None]
        if valid:
            x, y = valid[0]
            draw.text((max(0, x + 3), max(0, y + 3)), crosswalk["id"], fill=(255, 255, 255))
    for item in projection["objects"]:
        point = item["pixel"]
        if point is None:
            continue
        x, y = point
        if 0 <= x < width and 0 <= y < height:
            radius = 5 if item["category"] == "TrafficLight" else 3
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=(255, 0, 255), width=2)
            draw.text((x + 4, y + 3), item["short_id"], fill=(255, 255, 255))
    return image


def render_overlay_bytes(image_path, projection):
    stream = io.BytesIO()
    render_overlay_image(image_path, projection).save(stream, format="JPEG", quality=94)
    return stream.getvalue()


def render_overlay(image_path, projection, output_path):
    Path(output_path).write_bytes(render_overlay_bytes(image_path, projection))


def projected_geometry(geometry, transform, fov, width, height):
    lanes = []
    for lane in geometry["lanes"]:
        entry = {"id": lane["id"]}
        any_visible = False
        for source_key, output_key in (
            ("center_world", "center"),
            ("left_boundary_world", "left"),
            ("right_boundary_world", "right"),
        ):
            pixels, depths = project(lane[source_key], transform, fov, width, height)
            visible = visible_polyline(pixels, depths, width, height)
            entry[output_key] = visible
            any_visible |= sum(point is not None for point in visible) >= 2
        if any_visible:
            lanes.append(entry)
    crosswalks = []
    for item in geometry["crosswalks"]:
        pixels, depths = project(item["world"], transform, fov, width, height)
        visible = visible_polyline(pixels, depths, width, height)
        if sum(point is not None for point in visible) >= 2:
            crosswalks.append({"id": item["id"], "pixels": visible})
    objects = []
    for item in geometry["objects"]:
        pixels, depths = project([item["center_world"]], transform, fov, width, height)
        pixel = None
        if depths and depths[0] > 0.2 and all(math.isfinite(value) for value in pixels[0]):
            pixel = pixels[0]
        objects.append({
            "id": item["id"],
            "short_id": item["id"][-4:],
            "category": item["category"],
            "pixel": pixel,
            "depth_m": depths[0] if depths else None,
        })
    return {"lanes": lanes, "crosswalks": crosswalks, "objects": objects}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--pair-manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--radius-m", type=float, default=80.0)
    parser.add_argument("--lane-spacing-m", type=float, default=0.5)
    args = parser.parse_args()

    if not 10 <= args.radius_m <= 250:
        parser.error("--radius-m must be in [10, 250]")
    if not 0.1 <= args.lane_spacing_m <= 2.0:
        parser.error("--lane-spacing-m must be in [0.1, 2.0]")
    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit("refusing to overwrite a nonempty geometry directory")

    cameras_path = Path(args.cameras_json).resolve()
    pair_path = Path(args.pair_manifest).resolve()
    cameras_bytes, pair_bytes = cameras_path.read_bytes(), pair_path.read_bytes()
    config, pair = load_cameras_config(str(cameras_path)), json.loads(pair_bytes)
    if pair.get("schema") != "v2x-observational-calibration-pairs/v1":
        raise SystemExit("pair manifest schema is unsupported")
    if hashlib.sha256(cameras_bytes).hexdigest() != pair.get("cameras_file_sha256"):
        raise SystemExit("cameras file does not match the retained pair manifest")

    import carla

    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    carla_map = world.get_map()
    opendrive = carla_map.to_opendrive().encode("utf-8")
    exact_road_mark_ranges = opendrive_road_mark_ranges(opendrive)
    transforms, computed_transforms = {}, {}
    for camera in config["cameras"]:
        camera_id = camera["id"]
        observed = pair["cameras"][camera_id]["twin"]["camera_model"]["transform"]
        transforms[camera_id] = carla.Transform(
            carla.Location(
                x=float(observed["location"]["x"]),
                y=float(observed["location"]["y"]),
                z=float(observed["location"]["z"]),
            ),
            carla.Rotation(
                pitch=float(observed["rotation"]["pitch"]),
                yaw=float(observed["rotation"]["yaw"]),
                roll=float(observed["rotation"]["roll"]),
            ),
        )
        computed_transforms[camera_id] = compute_twin_camera_transform(
            carla_map, config["site"], camera
        )
    anchor = transforms["ch1"].location
    crosswalk_polygons = []
    for polygon in split_crosswalk_polygons(carla_map.get_crosswalks()):
        if min(math.hypot(point[0] - anchor.x, point[1] - anchor.y) for point in polygon) <= args.radius_m:
            crosswalk_polygons.append(polygon)
    lane_source = nearby_lane_source(carla_map, anchor, args.radius_m, args.lane_spacing_m)
    object_source = []
    for label in ("TrafficLight", "TrafficSigns"):
        object_source.extend(static_object_source(world, label, anchor, args.radius_m))
    # The custom Richmond asset classifies poles, cabinets, and signal arms as
    # ``Other`` rather than ``Poles``.  Keep only the immediate intersection
    # neighborhood so those globally identified objects remain reviewable.
    object_source.extend(
        static_object_source(world, "Other", anchor, min(args.radius_m, 30.0))
    )
    source_export = {
        "schema": CARLA_SOURCE_SCHEMA,
        "created_at_utc": utc_now(),
        "map": carla_map.name,
        "opendrive_sha256": hashlib.sha256(opendrive).hexdigest(),
        "radius_m": args.radius_m,
        "lane_spacing_m": args.lane_spacing_m,
        "anchor_world": [float(anchor.x), float(anchor.y), float(anchor.z)],
        "crosswalk_polygons": crosswalk_polygons,
        "lane_waypoint_groups": lane_source,
        "environment_objects": object_source,
    }
    geometry = geometry_from_carla_source(source_export, exact_road_mark_ranges)

    output_dir.mkdir(parents=True, exist_ok=True)
    source_export_path = output_dir / "carla-source-export.json"
    source_export_path.write_text(json.dumps(source_export, indent=2, sort_keys=True) + "\n")
    camera_reports = {}
    for camera_id in CAMERAS:
        camera = next(item for item in config["cameras"] if item["id"] == camera_id)
        twin = pair["cameras"][camera_id]["twin"]
        if canonical_hash(camera) != twin["camera_config_sha256"]:
            raise SystemExit(f"{camera_id}: camera config hash mismatch")
        fov = float(twin["camera_model"]["image"]["horizontal_fov_deg"])
        reports = {}
        for kind in ("real", "twin"):
            frame = pair["cameras"][camera_id][kind]
            image_path = pair_path.parent / frame["file"]
            if sha256(image_path) != frame["sha256"]:
                raise SystemExit(f"{camera_id}: {kind} frame hash mismatch")
            with Image.open(image_path) as image:
                width, height = image.size
            projection = projected_geometry(
                geometry, transforms[camera_id], fov, width, height
            )
            overlay = output_dir / f"{camera_id}-{kind}-map-overlay.jpg"
            render_overlay(image_path, projection, overlay)
            reports[kind] = {
                "frame": str(image_path),
                "frame_sha256": frame["sha256"],
                "width": width,
                "height": height,
                "overlay": overlay.name,
                "overlay_sha256": sha256(overlay),
                "projection": projection,
            }
        transform = transforms[camera_id]
        computed = computed_transforms[camera_id]
        camera_reports[camera_id] = {
            "camera_config_sha256": twin["camera_config_sha256"],
            "horizontal_fov_deg": fov,
            "baseline_transform": {
                "location": [transform.location.x, transform.location.y, transform.location.z],
                "rotation": [
                    transform.rotation.pitch,
                    transform.rotation.yaw,
                    transform.rotation.roll,
                ],
            },
            "baseline_source": "retained_twin_actor_metadata",
            "tracked_helper_transform": {
                "location": [
                    computed.location.x, computed.location.y, computed.location.z
                ],
                "rotation": [
                    computed.rotation.pitch,
                    computed.rotation.yaw,
                    computed.rotation.roll,
                ],
            },
            "tracked_helper_delta": {
                "location_m": [
                    computed.location.x - transform.location.x,
                    computed.location.y - transform.location.y,
                    computed.location.z - transform.location.z,
                ],
                "rotation_deg": [
                    computed.rotation.pitch - transform.rotation.pitch,
                    computed.rotation.yaw - transform.rotation.yaw,
                    computed.rotation.roll - transform.rotation.roll,
                ],
                "fov_deg": twin_horizontal_fov_deg(camera) - fov,
            },
            **reports,
        }

    opendrive_root = ET.fromstring(opendrive)
    georeference = (opendrive_root.findtext("./header/geoReference") or "").strip()
    if not georeference:
        raise SystemExit("active OpenDRIVE map has no georeference")
    geometry_provenance = {
        "schema": "v2x-map-geometry-provenance/v1",
        "exporter_sha256": sha256(Path(__file__).resolve()),
        "map": carla_map.name,
        "opendrive_sha256": hashlib.sha256(opendrive).hexdigest(),
        "opendrive_georeference_sha256": hashlib.sha256(georeference.encode()).hexdigest(),
        "pair_manifest_sha256": hashlib.sha256(pair_bytes).hexdigest(),
        "cameras_file_sha256": hashlib.sha256(cameras_bytes).hexdigest(),
        "carla_source_export_sha256": sha256(source_export_path),
        "radius_m": args.radius_m,
        "lane_spacing_m": args.lane_spacing_m,
        "geometry_payload_sha256": canonical_hash(geometry),
        "exact_road_mark_ranges_sha256": canonical_hash(exact_road_mark_ranges),
    }
    report = {
        "schema": "v2x-map-calibration-geometry/v1",
        "acceptance_eligible": False,
        "created_at_utc": utc_now(),
        "warning": "map-bound proposal overlay; real feature identities require frozen topology review",
        "map": carla_map.name,
        "opendrive_sha256": hashlib.sha256(opendrive).hexdigest(),
        "pair_manifest_sha256": hashlib.sha256(pair_bytes).hexdigest(),
        "cameras_file_sha256": hashlib.sha256(cameras_bytes).hexdigest(),
        "carla_source_export": source_export_path.name,
        "carla_source_export_sha256": sha256(source_export_path),
        "radius_m": args.radius_m,
        "lane_spacing_m": args.lane_spacing_m,
        "geometry_provenance": geometry_provenance,
        "geometry": geometry,
        "cameras": camera_reports,
    }
    report_path = output_dir / "map-calibration-geometry.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
