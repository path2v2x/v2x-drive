#!/usr/bin/env python3
"""Compare geometry in two OpenDRIVE revisions without loading CARLA.

Road reference lines, crosswalk polygons, signal locations, lane-width and
road-mark profiles, road links, and junction lane links are evaluated in their
shared OpenDRIVE frame.  It is diagnostic-only and intended to distinguish a
camera-fit residual from geometric map-content drift.  The tool never changes
a CARLA world or Unreal asset.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import xml.etree.ElementTree as ET

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from v2x_common.geodesy import TransverseMercator  # noqa: E402


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


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _integrate_spiral(geometry, distance):
    length = float(geometry["length"])
    spiral = geometry["shape"]
    curvature_start = float(spiral.get("curvStart", 0.0))
    curvature_end = float(spiral.get("curvEnd", 0.0))
    slope = (curvature_end - curvature_start) / length
    # Simpson integration at <=5 cm is deterministic and comfortably below
    # the decimetre-scale topology thresholds reported by this tool.
    intervals = max(2, int(math.ceil(distance / 0.05)))
    if intervals % 2:
        intervals += 1
    step = distance / intervals

    def heading(offset):
        return geometry["hdg"] + curvature_start * offset + 0.5 * slope * offset**2

    cos_sum = math.cos(heading(0.0)) + math.cos(heading(distance))
    sin_sum = math.sin(heading(0.0)) + math.sin(heading(distance))
    for index in range(1, intervals):
        factor = 4 if index % 2 else 2
        angle = heading(index * step)
        cos_sum += factor * math.cos(angle)
        sin_sum += factor * math.sin(angle)
    x = geometry["x"] + step * cos_sum / 3.0
    y = geometry["y"] + step * sin_sum / 3.0
    return x, y, heading(distance)


def evaluate_geometry(geometry, distance):
    if distance < -1e-9 or distance > geometry["length"] + 1e-6:
        raise ValueError("distance falls outside plan-view geometry")
    distance = min(max(0.0, distance), geometry["length"])
    shape = geometry["shape"].tag
    heading = geometry["hdg"]
    if shape == "line":
        return (
            geometry["x"] + distance * math.cos(heading),
            geometry["y"] + distance * math.sin(heading),
            heading,
        )
    if shape == "arc":
        curvature = float(geometry["shape"].get("curvature"))
        if abs(curvature) < 1e-12:
            return (
                geometry["x"] + distance * math.cos(heading),
                geometry["y"] + distance * math.sin(heading),
                heading,
            )
        end_heading = heading + curvature * distance
        return (
            geometry["x"] + (math.sin(end_heading) - math.sin(heading)) / curvature,
            geometry["y"] - (math.cos(end_heading) - math.cos(heading)) / curvature,
            end_heading,
        )
    if shape == "spiral":
        return _integrate_spiral(geometry, distance)
    raise ValueError(f"unsupported OpenDRIVE geometry type: {shape}")


def parse_plan_view(road):
    geometries = []
    for element in road.findall("./planView/geometry"):
        shapes = [child for child in element if child.tag in {"line", "arc", "spiral"}]
        if len(shapes) != 1:
            tags = [child.tag for child in element]
            raise ValueError(
                f"road {road.get('id')} geometry at s={element.get('s')} has "
                f"unsupported/ambiguous children {tags}"
            )
        geometries.append({
            "s": float(element.get("s")),
            "x": float(element.get("x")),
            "y": float(element.get("y")),
            "hdg": float(element.get("hdg")),
            "length": float(element.get("length")),
            "shape": shapes[0],
        })
    geometries.sort(key=lambda item: item["s"])
    if not geometries:
        raise ValueError(f"road {road.get('id')} has no plan-view geometry")
    return geometries


def road_pose(geometries, station):
    selected = geometries[0]
    for geometry in geometries:
        if geometry["s"] <= station + 1e-9:
            selected = geometry
        else:
            break
    return evaluate_geometry(selected, station - selected["s"])


def local_to_world(x, y, heading, longitudinal, lateral):
    return (
        x + longitudinal * math.cos(heading) - lateral * math.sin(heading),
        y + longitudinal * math.sin(heading) + lateral * math.cos(heading),
    )


def signal_class(element):
    signal_type = (element.get("type") or "").lower()
    name = (element.get("name") or "").lower()
    if signal_type in {"1000001", "1000011"} or "signal_3light" in name:
        return "vehicle_signal"
    if signal_type in {"1000002", "1000012"} or "walk_light" in name:
        return "pedestrian_signal"
    if signal_type == "roadmark" or any(
        token in name for token in ("stopline", "solidsingle", "broken")
    ):
        return "road_mark"
    if signal_type in {"206", "274"} or any(
        token in name for token in ("sign_", "stop_us", "speedlimit")
    ):
        return "traffic_sign"
    if name in {"crow dr", "way"}:
        return "traffic_sign_text"
    return "unknown"


def object_geometry(road, plan_view, element, kind):
    station = float(element.get("s"))
    lateral = float(element.get("t", 0.0))
    road_x, road_y, road_heading = road_pose(plan_view, station)
    center_x, center_y = local_to_world(road_x, road_y, road_heading, 0.0, lateral)
    object_heading = road_heading + float(element.get("hdg", 0.0))
    corners = []
    outline = element.find("outline")
    if outline is not None:
        for corner in outline:
            if corner.tag == "cornerLocal":
                corner_x, corner_y = local_to_world(
                    center_x,
                    center_y,
                    object_heading,
                    float(corner.get("u")),
                    float(corner.get("v")),
                )
            elif corner.tag == "cornerRoad":
                corner_station = float(corner.get("s"))
                corner_road_x, corner_road_y, corner_road_heading = road_pose(
                    plan_view, corner_station
                )
                corner_x, corner_y = local_to_world(
                    corner_road_x,
                    corner_road_y,
                    corner_road_heading,
                    0.0,
                    float(corner.get("t")),
                )
            else:
                raise ValueError(
                    f"unsupported outline corner {corner.tag} on object {element.get('id')}"
                )
            corners.append([corner_x, corner_y])
    return {
        "kind": kind,
        "id": element.get("id"),
        "road_id": road.get("id"),
        "name": element.get("name"),
        "type": element.get("type"),
        "subtype": element.get("subtype"),
        "feature_class": "crosswalk" if kind == "crosswalk" else signal_class(element),
        "center_xy": [center_x, center_y],
        "heading_rad": object_heading,
        "outline_xy": corners,
    }


def _float_attributes(element, names):
    return {name: float(element.get(name, 0.0)) for name in names}


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


def parse_road_mark_record(element):
    explicit = element.find("explicit")
    return {
        "s_offset": float(element.get("sOffset")),
        "type": element.get("type"),
        "color": element.get("color"),
        "weight": element.get("weight"),
        "material": element.get("material"),
        "lane_change": element.get("laneChange"),
        "width": _optional_float(element, "width"),
        "height": _optional_float(element, "height"),
        "sway": [{
            "ds": float(item.get("ds")),
            **_float_attributes(item, ("a", "b", "c", "d")),
        } for item in element.findall("sway")],
        "types": [{
            "name": item.get("name"),
            "width": _optional_float(item, "width"),
            "lines": [_road_mark_line_record(line) for line in item.findall("line")],
        } for item in element.findall("type")],
        "explicit_lines": [] if explicit is None else [
            _road_mark_line_record(line) for line in explicit.findall("line")
        ],
    }


def parse_lane_profiles(road):
    sections = road.findall("./lanes/laneSection")
    road_length = float(road.get("length"))
    profiles = []
    for section_index, section in enumerate(sections):
        section_start = float(section.get("s"))
        section_end = (
            float(sections[section_index + 1].get("s"))
            if section_index + 1 < len(sections) else road_length
        )
        if section_end <= section_start:
            raise ValueError(f"road {road.get('id')} has an invalid lane-section range")
        for lane in section.findall("./left/lane") + section.findall("./center/lane") + section.findall("./right/lane"):
            lane_id = lane.get("id")
            identity = f"road-{road.get('id')}-section-s{section_start:.3f}-lane-{lane_id}"
            widths = [
                {"s_offset": float(item.get("sOffset")), **_float_attributes(item, ("a", "b", "c", "d"))}
                for item in lane.findall("width")
            ]
            widths.sort(key=lambda item: item["s_offset"])
            road_marks = [parse_road_mark_record(item) for item in lane.findall("roadMark")]
            road_marks.sort(key=lambda item: item["s_offset"])
            link = lane.find("link")
            profiles.append({
                "id": identity,
                "road_id": road.get("id"),
                "section_start_s": section_start,
                "section_end_s": section_end,
                "lane_id": lane_id,
                "type": lane.get("type"),
                "level": lane.get("level"),
                "widths": widths,
                "road_marks": road_marks,
                "predecessor_lane_id": (
                    link.find("predecessor").get("id")
                    if link is not None and link.find("predecessor") is not None else None
                ),
                "successor_lane_id": (
                    link.find("successor").get("id")
                    if link is not None and link.find("successor") is not None else None
                ),
            })
    return sorted(profiles, key=lambda item: item["id"])


def parse_road_link(road):
    link = road.find("link")
    result = {}
    for relation in ("predecessor", "successor"):
        element = link.find(relation) if link is not None else None
        result[relation] = None if element is None else {
            "element_type": element.get("elementType"),
            "element_id": element.get("elementId"),
            "contact_point": element.get("contactPoint"),
        }
    return result


def parse_junction_links(root):
    output = []
    for junction in root.findall("junction"):
        for connection in junction.findall("connection"):
            lane_links = sorted(
                (item.get("from"), item.get("to")) for item in connection.findall("laneLink")
            )
            output.append({
                "id": f"junction-{junction.get('id')}-connection-{connection.get('id')}",
                "junction_id": junction.get("id"),
                "connection_id": connection.get("id"),
                "incoming_road": connection.get("incomingRoad"),
                "connecting_road": connection.get("connectingRoad"),
                "contact_point": connection.get("contactPoint"),
                "lane_links": lane_links,
            })
    return sorted(output, key=lambda item: item["id"])


def parse_polynomial_profiles(road):
    road_id = road.get("id")
    definitions = (
        ("elevation", "./elevationProfile/elevation", ("s",)),
        ("lane_offset", "./lanes/laneOffset", ("s",)),
        ("superelevation", "./lateralProfile/superelevation", ("s",)),
        ("lateral_shape", "./lateralProfile/shape", ("s", "t")),
    )
    output = {name: [] for name, _, _ in definitions}
    for name, query, identity_fields in definitions:
        for element in road.findall(query):
            identity_values = tuple(round(float(element.get(field)), 9) for field in identity_fields)
            record = {
                "id": f"road-{road_id}-{name}-" + "-".join(
                    f"{field}{value:.9f}" for field, value in zip(identity_fields, identity_values)
                ),
                "road_id": road_id,
                **{field: value for field, value in zip(identity_fields, identity_values)},
                **_float_attributes(element, ("a", "b", "c", "d")),
            }
            output[name].append(record)
        output[name].sort(key=lambda item: item["id"])
    return output


def parse_map(path):
    root = ET.parse(path).getroot()
    if root.tag != "OpenDRIVE":
        raise ValueError(f"{path} is not an OpenDRIVE document")
    header = root.find("header")
    georef = (header.findtext("geoReference") or "").strip() if header is not None else ""
    offset_element = header.find("offset") if header is not None else None
    offset = dict(offset_element.attrib) if offset_element is not None else {}
    if offset and any(abs(float(offset.get(key, 0.0))) > 1e-12 for key in (
        "x", "y", "z", "hdg"
    )):
        raise ValueError(f"{path} declares a non-zero header offset; comparison is unsafe")
    roads = []
    crosswalks = []
    signals = []
    polynomial_profiles = {
        "elevation": [], "lane_offset": [], "superelevation": [], "lateral_shape": []
    }
    shape_counts = Counter()
    for road in root.findall("road"):
        plan_view = parse_plan_view(road)
        shape_counts.update(item["shape"].tag for item in plan_view)
        roads.append({
            "id": road.get("id"),
            "length": float(road.get("length")),
            "junction": road.get("junction"),
            "plan_view": plan_view,
            "link": parse_road_link(road),
        })
        parsed_profiles = parse_polynomial_profiles(road)
        for name, values in parsed_profiles.items():
            polynomial_profiles[name].extend(values)
        for element in road.findall("./objects/object"):
            if (element.get("type") or "").lower() == "crosswalk":
                crosswalks.append(object_geometry(road, plan_view, element, "crosswalk"))
        for element in road.findall("./signals/signal"):
            signals.append(object_geometry(road, plan_view, element, "signal"))
    lane_profiles = [profile for road in root.findall("road") for profile in parse_lane_profiles(road)]
    junction_links = parse_junction_links(root)
    return {
        "path": str(Path(path).resolve()),
        "sha256": sha256(path),
        "header": dict(header.attrib) if header is not None else {},
        "georeference": georef,
        "header_offset": offset,
        "road_count": len(roads),
        "junction_count": len(root.findall("junction")),
        "object_count": len(root.findall(".//object")),
        "signal_count": len(signals),
        "crosswalk_count": len(crosswalks),
        "plan_view_geometry_counts": dict(sorted(shape_counts.items())),
        "lane_profile_count": len(lane_profiles),
        "road_mark_range_count": sum(len(item["road_marks"]) for item in lane_profiles),
        "junction_connection_count": len(junction_links),
        "elevation_profile_count": len(polynomial_profiles["elevation"]),
        "lane_offset_count": len(polynomial_profiles["lane_offset"]),
        "superelevation_count": len(polynomial_profiles["superelevation"]),
        "lateral_shape_count": len(polynomial_profiles["lateral_shape"]),
        "roads": roads,
        "lane_profiles": lane_profiles,
        "junction_links": junction_links,
        "polynomial_profiles": polynomial_profiles,
        "crosswalks": crosswalks,
        "signals": signals,
    }


def sample_roads(model, spacing_m):
    points = []
    for road in model["roads"]:
        count = max(1, int(math.ceil(road["length"] / spacing_m)))
        stations = [min(index * spacing_m, road["length"]) for index in range(count + 1)]
        for station in stations:
            x, y, _ = road_pose(road["plan_view"], station)
            points.append((x, y))
    return points


class GridIndex:
    def __init__(self, points, cell_size):
        self.points = points
        self.cell_size = cell_size
        self.cells = {}
        for point in points:
            self.cells.setdefault(self._cell(point), []).append(point)

    def _cell(self, point):
        return (
            math.floor(point[0] / self.cell_size),
            math.floor(point[1] / self.cell_size),
        )

    def nearest_within(self, point, radius):
        cell_x, cell_y = self._cell(point)
        cell_radius = int(math.ceil(radius / self.cell_size))
        best = None
        for dx in range(-cell_radius, cell_radius + 1):
            for dy in range(-cell_radius, cell_radius + 1):
                for candidate in self.cells.get((cell_x + dx, cell_y + dy), ()):
                    distance = math.hypot(point[0] - candidate[0], point[1] - candidate[1])
                    if distance <= radius and (best is None or distance < best):
                        best = distance
        return best


def directional_road_comparison(source_points, target_points, max_distance_m):
    index = GridIndex(target_points, max(1.0, max_distance_m / 4.0))
    distances = [index.nearest_within(point, max_distance_m) for point in source_points]
    finite = [distance for distance in distances if distance is not None]
    coverage_within_radius = len(finite) / len(distances) if distances else 0.0
    distance_summary = {
        "median": statistics.median(finite) if finite else None,
        "p95": percentile(finite, 0.95),
        "max": max(finite) if finite else None,
    }
    if coverage_within_radius < 0.95:
        distance_summary = {
            "median": None,
            "p95": None,
            "max": None,
            "suppressed_reason": "coverage_within_search_radius_below_0.95",
        }
    return {
        "sample_count": len(distances),
        "matched_within_search_radius": len(finite),
        "unmatched_outside_search_radius": len(distances) - len(finite),
        "coverage_within_search_radius": coverage_within_radius,
        "search_radius_m": max_distance_m,
        "coverage": {
            str(threshold): sum(
                distance is not None and distance <= threshold for distance in distances
            ) / len(distances)
            for threshold in (0.25, 0.5, 1.0, 2.0, 5.0)
        },
        "distance_m_conditional_on_match": distance_summary,
    }


def within_radius(items, center_xy, radius_m):
    return [
        item for item in items
        if math.dist(item["center_xy"], center_xy) <= radius_m
    ]


def points_within_radius(points, center_xy, radius_m):
    return [point for point in points if math.dist(point, center_xy) <= radius_m]


def sample_outline(points, spacing_m=0.1):
    if len(points) < 2:
        return list(points)
    sampled = []
    pairs = list(zip(points, points[1:]))
    if points[0] != points[-1]:
        pairs.append((points[-1], points[0]))
    for start, end in pairs:
        length = math.dist(start, end)
        count = max(1, int(math.ceil(length / spacing_m)))
        for index in range(count):
            fraction = index / count
            sampled.append((
                start[0] + fraction * (end[0] - start[0]),
                start[1] + fraction * (end[1] - start[1]),
            ))
    return sampled


def polygon_area(points):
    if len(points) < 3:
        return None
    ring = points[:-1] if points[0] == points[-1] else points
    return abs(sum(
        left[0] * right[1] - right[0] * left[1]
        for left, right in zip(ring, ring[1:] + ring[:1])
    )) / 2.0


def outline_distance_metrics(left_points, right_points):
    if len(left_points) < 2 or len(right_points) < 2:
        return None
    left_samples = sample_outline(left_points)
    right_samples = sample_outline(right_points)

    def directional(source, target):
        return [min(math.dist(point, other) for other in target) for point in source]

    left_to_right = directional(left_samples, right_samples)
    right_to_left = directional(right_samples, left_samples)
    combined = left_to_right + right_to_left
    return {
        "sample_spacing_m": 0.1,
        "symmetric_hausdorff_m": max(combined),
        "symmetric_mean_m": statistics.mean(combined),
        "symmetric_p95_m": percentile(combined, 0.95),
        "deployed_area_m2": polygon_area(left_points),
        "candidate_area_m2": polygon_area(right_points),
    }


def minimum_cost_assignment(costs):
    """Return row->column indices for a square finite cost matrix."""
    size = len(costs)
    if size == 0 or any(len(row) != size for row in costs):
        raise ValueError("assignment matrix must be non-empty and square")
    potentials_rows = [0.0] * (size + 1)
    potentials_columns = [0.0] * (size + 1)
    matching = [0] * (size + 1)
    previous = [0] * (size + 1)
    for row_index in range(1, size + 1):
        matching[0] = row_index
        column0 = 0
        minimum = [math.inf] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[column0] = True
            current_row = matching[column0]
            delta = math.inf
            column1 = 0
            for column in range(1, size + 1):
                if used[column]:
                    continue
                reduced = (
                    costs[current_row - 1][column - 1]
                    - potentials_rows[current_row]
                    - potentials_columns[column]
                )
                if reduced < minimum[column]:
                    minimum[column] = reduced
                    previous[column] = column0
                if minimum[column] < delta:
                    delta = minimum[column]
                    column1 = column
            if not math.isfinite(delta):
                raise ValueError("assignment matrix has no finite solution")
            for column in range(size + 1):
                if used[column]:
                    potentials_rows[matching[column]] += delta
                    potentials_columns[column] -= delta
                else:
                    minimum[column] -= delta
            column0 = column1
            if matching[column0] == 0:
                break
        while True:
            column1 = previous[column0]
            matching[column0] = matching[column1]
            column0 = column1
            if column0 == 0:
                break
    assignment = [None] * size
    for column in range(1, size + 1):
        assignment[matching[column] - 1] = column - 1
    return assignment


def match_features(left, right, max_distance_m, require_same_class=False):
    left_count, right_count = len(left), len(right)
    if not left_count and not right_count:
        return {
            "assignment_method": "global_minimum_cost_with_explicit_unmatched_nodes",
            "max_match_distance_m": max_distance_m,
            "matches": [],
            "unmatched_deployed": [],
            "unmatched_candidate": [],
            "distance_m": {"median": None, "p95": None, "max": None},
        }
    size = left_count + right_count
    invalid_cost = 1e9
    unmatched_cost = max_distance_m
    costs = [[invalid_cost] * size for _ in range(size)]
    candidate_distances = {}
    for left_index, left_item in enumerate(left):
        for right_index, right_item in enumerate(right):
            if (
                require_same_class
                and left_item.get("feature_class") != right_item.get("feature_class")
            ):
                continue
            distance = math.dist(left_item["center_xy"], right_item["center_xy"])
            if distance <= max_distance_m:
                costs[left_index][right_index] = distance
                candidate_distances[left_index, right_index] = distance
        costs[left_index][right_count + left_index] = unmatched_cost
    for right_index in range(right_count):
        costs[left_count + right_index][right_index] = unmatched_cost
        for left_index in range(left_count):
            costs[left_count + right_index][right_count + left_index] = 0.0
    assignment = minimum_cost_assignment(costs)
    matched_left = set()
    matched_right = set()
    matches = []
    for left_index in range(left_count):
        right_index = assignment[left_index]
        if right_index >= right_count or (left_index, right_index) not in candidate_distances:
            continue
        distance = candidate_distances[left_index, right_index]
        matched_left.add(left_index)
        matched_right.add(right_index)
        left_item = left[left_index]
        right_item = right[right_index]
        left_alternatives = sorted(
            value for (li, ri), value in candidate_distances.items()
            if li == left_index and ri != right_index
        )
        right_alternatives = sorted(
            value for (li, ri), value in candidate_distances.items()
            if ri == right_index and li != left_index
        )
        margins = [values[0] - distance for values in (left_alternatives, right_alternatives) if values]
        matches.append({
            "deployed": {key: left_item.get(key) for key in ("id", "road_id", "name", "type", "subtype", "feature_class", "center_xy")},
            "candidate": {key: right_item.get(key) for key in ("id", "road_id", "name", "type", "subtype", "feature_class", "center_xy")},
            "center_distance_m": distance,
            "semantic_equal": (
                left_item.get("name"), left_item.get("type"), left_item.get("subtype")
            ) == (
                right_item.get("name"), right_item.get("type"), right_item.get("subtype")
            ),
            "outline_distance": outline_distance_metrics(
                left_item.get("outline_xy", []), right_item.get("outline_xy", [])
            ),
            "nearest_alternative_margin_m": min(margins) if margins else None,
        })
    return {
        "assignment_method": "global_minimum_cost_with_explicit_unmatched_nodes",
        "max_match_distance_m": max_distance_m,
        "matches": matches,
        "unmatched_deployed": [
            item for index, item in enumerate(left) if index not in matched_left
        ],
        "unmatched_candidate": [
            item for index, item in enumerate(right) if index not in matched_right
        ],
        "distance_m": {
            "median": statistics.median(
                item["center_distance_m"] for item in matches
            ) if matches else None,
            "p95": percentile([item["center_distance_m"] for item in matches], 0.95),
            "max": max((item["center_distance_m"] for item in matches), default=None),
        },
    }


def _evaluate_width(profile, station_offset):
    selected = None
    for width in profile["widths"]:
        if width["s_offset"] <= station_offset + 1e-12:
            selected = width
        else:
            break
    if selected is None:
        return None
    delta = station_offset - selected["s_offset"]
    return selected["a"] + selected["b"] * delta + selected["c"] * delta**2 + selected["d"] * delta**3


def compare_lane_profiles(left_profiles, right_profiles, spacing_m=0.25):
    left = {item["id"]: item for item in left_profiles}
    right = {item["id"]: item for item in right_profiles}
    common = sorted(set(left) & set(right))
    width_differences, width_changed = [], []
    road_mark_changed, lane_link_changed, semantic_changed = [], [], []
    for identity in common:
        deployed, candidate = left[identity], right[identity]
        length = min(
            deployed["section_end_s"] - deployed["section_start_s"],
            candidate["section_end_s"] - candidate["section_start_s"],
        )
        stations = np.arange(0.0, length + 1e-9, spacing_m) if length > 0 else []
        local_differences = []
        for station in stations:
            deployed_width = _evaluate_width(deployed, float(station))
            candidate_width = _evaluate_width(candidate, float(station))
            if deployed_width is None and candidate_width is None:
                continue
            if deployed_width is None or candidate_width is None:
                local_differences.append(math.inf)
            else:
                local_differences.append(abs(deployed_width - candidate_width))
        width_differences.extend(local_differences)
        if any(not math.isfinite(value) or value > 1e-9 for value in local_differences):
            width_changed.append(identity)
        if deployed["road_marks"] != candidate["road_marks"]:
            road_mark_changed.append(identity)
        if (
            deployed["predecessor_lane_id"], deployed["successor_lane_id"]
        ) != (
            candidate["predecessor_lane_id"], candidate["successor_lane_id"]
        ):
            lane_link_changed.append(identity)
        if (deployed["type"], deployed["level"]) != (candidate["type"], candidate["level"]):
            semantic_changed.append(identity)
    finite = [value for value in width_differences if math.isfinite(value)]
    return {
        "sample_spacing_m": spacing_m,
        "matched_lane_profile_count": len(common),
        "missing_from_candidate": sorted(set(left) - set(right)),
        "added_in_candidate": sorted(set(right) - set(left)),
        "width_changed": width_changed,
        "width_difference_m": {
            "rmse": math.sqrt(statistics.mean(value * value for value in finite)) if finite else None,
            "max": max(finite) if finite else None,
            "unmatched_width_samples": len(width_differences) - len(finite),
        },
        "road_mark_changed": road_mark_changed,
        "lane_link_changed": lane_link_changed,
        "lane_semantic_changed": semantic_changed,
    }


def compare_identity_records(left, right, identity_key="id"):
    deployed = {item[identity_key]: item for item in left}
    candidate = {item[identity_key]: item for item in right}
    common = sorted(set(deployed) & set(candidate))
    return {
        "missing_from_candidate": sorted(set(deployed) - set(candidate)),
        "added_in_candidate": sorted(set(candidate) - set(deployed)),
        "changed": [identity for identity in common if deployed[identity] != candidate[identity]],
        "unchanged_count": sum(deployed[identity] == candidate[identity] for identity in common),
    }


def public_map_summary(model):
    return {key: model[key] for key in (
        "path", "sha256", "header", "georeference", "header_offset", "road_count",
        "junction_count", "object_count", "signal_count", "crosswalk_count",
        "plan_view_geometry_counts", "lane_profile_count", "road_mark_range_count",
        "junction_connection_count",
        "elevation_profile_count", "lane_offset_count", "superelevation_count",
        "lateral_shape_count",
    )}


def compare_maps(deployed, candidate, road_spacing_m=1.0, max_road_distance_m=10.0,
                 feature_match_distance_m=5.0, site_anchor_xy=None,
                 site_radius_m=40.0):
    deployed_points = sample_roads(deployed, road_spacing_m)
    candidate_points = sample_roads(candidate, road_spacing_m)
    report = {
        "schema": "v2x-opendrive-geometry-comparison/v1",
        "acceptance_eligible": False,
        "limitations": [
            "lane_widths_are_compared_at_fixed_0.25m_samples",
            "connectivity_comparison_is_structural_and_does_not_execute_routes",
            "feature_assignment_is_geometric_and_not_identity_truth",
            "cannot_certify_physical_site_alignment_without_surveyed_holdouts",
        ],
        "generated_at": utc_now(),
        "method": {
            "road_sample_spacing_m": road_spacing_m,
            "max_road_search_distance_m": max_road_distance_m,
            "feature_match_distance_m": feature_match_distance_m,
            "coordinate_frame": "shared OpenDRIVE x/y with non-zero header offsets rejected",
            "spiral_integration_max_step_m": 0.05,
        },
        "georeference_equal": deployed["georeference"] == candidate["georeference"],
        "deployed": public_map_summary(deployed),
        "candidate": public_map_summary(candidate),
        "road_reference_line": {
            "deployed_to_candidate": directional_road_comparison(
                deployed_points, candidate_points, max_road_distance_m
            ),
            "candidate_to_deployed": directional_road_comparison(
                candidate_points, deployed_points, max_road_distance_m
            ),
        },
        "crosswalks": match_features(
            deployed["crosswalks"], candidate["crosswalks"], feature_match_distance_m
        ),
        "signals": match_features(
            deployed["signals"], candidate["signals"], feature_match_distance_m,
            require_same_class=True,
        ),
        "lane_profiles": compare_lane_profiles(
            deployed["lane_profiles"], candidate["lane_profiles"]
        ),
        "road_links": compare_identity_records([
            {"id": item["id"], "junction": item["junction"], **item["link"]}
            for item in deployed["roads"]
        ], [
            {"id": item["id"], "junction": item["junction"], **item["link"]}
            for item in candidate["roads"]
        ]),
        "road_junction_assignment": compare_identity_records([
            {"id": item["id"], "junction": item["junction"]} for item in deployed["roads"]
        ], [
            {"id": item["id"], "junction": item["junction"]} for item in candidate["roads"]
        ]),
        "junction_links": compare_identity_records(
            deployed["junction_links"], candidate["junction_links"]
        ),
        "elevation_profiles": compare_identity_records(
            deployed["polynomial_profiles"]["elevation"],
            candidate["polynomial_profiles"]["elevation"],
        ),
        "lane_offsets": compare_identity_records(
            deployed["polynomial_profiles"]["lane_offset"],
            candidate["polynomial_profiles"]["lane_offset"],
        ),
        "lateral_profiles": {
            "superelevation": compare_identity_records(
                deployed["polynomial_profiles"]["superelevation"],
                candidate["polynomial_profiles"]["superelevation"],
            ),
            "shape": compare_identity_records(
                deployed["polynomial_profiles"]["lateral_shape"],
                candidate["polynomial_profiles"]["lateral_shape"],
            ),
        },
    }
    if site_anchor_xy is not None:
        anchor_xy = site_anchor_xy["anchor_xy"]
        deployed_local_points = points_within_radius(
            deployed_points, anchor_xy, site_radius_m
        )
        candidate_local_points = points_within_radius(
            candidate_points, anchor_xy, site_radius_m
        )
        deployed_crosswalks = within_radius(
            deployed["crosswalks"], anchor_xy, site_radius_m
        )
        candidate_crosswalks = within_radius(
            candidate["crosswalks"], anchor_xy, site_radius_m
        )
        deployed_signals = within_radius(
            deployed["signals"], anchor_xy, site_radius_m
        )
        candidate_signals = within_radius(
            candidate["signals"], anchor_xy, site_radius_m
        )
        report["site_neighborhood"] = {
            "site_config": site_anchor_xy,
            "anchor_xy": list(anchor_xy),
            "radius_m": site_radius_m,
            "road_reference_line": {
                "deployed_to_candidate": directional_road_comparison(
                    deployed_local_points, candidate_points, max_road_distance_m
                ),
                "candidate_to_deployed": directional_road_comparison(
                    candidate_local_points, deployed_points, max_road_distance_m
                ),
            },
            "crosswalks": match_features(
                deployed_crosswalks,
                candidate_crosswalks,
                feature_match_distance_m,
            ),
            "signals": match_features(
                deployed_signals,
                candidate_signals,
                feature_match_distance_m,
                require_same_class=True,
            ),
        }
    return report


def site_anchor_from_config(path, expected_georeference):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    site = config.get("site")
    if not isinstance(site, dict):
        raise ValueError("camera config has no site object")
    declared_georeference = site.get("map_georeference")
    if declared_georeference != expected_georeference:
        raise ValueError("camera config georeference does not match compared maps")
    projection = TransverseMercator.from_proj_string(declared_georeference)
    return {
        "path": str(path),
        "sha256": sha256(path),
        "latitude": float(site["lat"]),
        "longitude": float(site["lon"]),
        "map_georeference": declared_georeference,
        "anchor_xy": list(projection.forward(float(site["lat"]), float(site["lon"]))),
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployed", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--road-spacing-m", type=float, default=1.0)
    parser.add_argument("--max-road-distance-m", type=float, default=10.0)
    parser.add_argument("--feature-match-distance-m", type=float, default=5.0)
    parser.add_argument("--site-config", type=Path)
    parser.add_argument("--site-radius-m", type=float, default=40.0)
    return parser.parse_args()


def main():
    args = parse_args()
    if not 0.25 <= args.road_spacing_m <= 5.0:
        raise SystemExit("--road-spacing-m must be in [0.25, 5.0]")
    if not 0.5 <= args.max_road_distance_m <= 25.0:
        raise SystemExit("--max-road-distance-m must be in [0.5, 25.0]")
    if not 0.1 <= args.feature_match_distance_m <= 10.0:
        raise SystemExit("--feature-match-distance-m must be in [0.1, 10.0]")
    if not 10.0 <= args.site_radius_m <= 500.0:
        raise SystemExit("--site-radius-m must be in [10.0, 500.0]")
    deployed = parse_map(args.deployed)
    candidate = parse_map(args.candidate)
    if deployed["georeference"] != candidate["georeference"]:
        raise SystemExit("map georeferences differ; comparison in a shared frame is unsafe")
    site_anchor = (
        site_anchor_from_config(args.site_config, deployed["georeference"])
        if args.site_config else None
    )
    report = compare_maps(
        deployed,
        candidate,
        args.road_spacing_m,
        args.max_road_distance_m,
        args.feature_match_distance_m,
        site_anchor,
        args.site_radius_m,
    )
    write_json_exclusive(args.output, report)
    print(json.dumps({
        "output": str(args.output),
        "deployed_sha256": deployed["sha256"],
        "candidate_sha256": candidate["sha256"],
        "crosswalk_matches": len(report["crosswalks"]["matches"]),
        "signal_matches": len(report["signals"]["matches"]),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
