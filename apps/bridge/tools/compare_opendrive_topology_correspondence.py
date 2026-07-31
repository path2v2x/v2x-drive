#!/usr/bin/env python3
"""Classify fail-closed OpenDRIVE road and junction correspondence.

This source-only diagnostic consumes a previously accepted map-candidate
lineage manifest and the exact two XODRs bound by that manifest.  Geometry can
describe correspondence, but cannot prove which authoring/export lineage is
correct, so this tool can never resolve lineage or permit scoring.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import pyexpat
import xml.etree.ElementTree as ET

try:
    from apps.bridge.tools import build_map_candidate_lineage_manifest as lineage
except ModuleNotFoundError:  # Direct execution from the tools directory.
    import build_map_candidate_lineage_manifest as lineage


SCHEMA = "v2x-opendrive-topology-correspondence/v1"
ALGORITHM = "world-xyz-arclength-bipartite-components/v1"
SAMPLE_INTERVAL_M = 1.0
STRICT_DISTANCE_M = 0.05
LOOSE_DISTANCE_M = 0.25
STRICT_FULL_COVERAGE = 0.98
LOOSE_FULL_COVERAGE = 0.90
MIN_PARTIAL_COVERAGE = 0.05
MAX_CHILD_OVERLAP = 0.10
MAX_ONE_TO_ONE_LENGTH_RELATIVE_DELTA = 0.02
MAX_SPLIT_MERGE_LENGTH_RELATIVE_DELTA = 0.02
ORIENTATION_WIN_MARGIN_M = 0.01
COORDINATE_QUANTIZATION_M = 0.001
NUMERIC_TOLERANCE = 1e-6
SUPPORTED_GEOMETRIES = {"line", "arc", "spiral"}
RICHMOND_ACCEPTED_BINDING = {
    "old_xodr_sha256": lineage.EXPECTED_OLD_XODR_SHA256,
    "deployed_xodr_sha256": lineage.EXPECTED_LIVE_XODR_SHA256,
    "package_inventory_sha256": (
        "c91c8e77fa26f22d6c9a672aea6ee0516729cee7492a5cb58f38f7f73166481c"
    ),
    "package_file_count": 91,
    "package_directory_count": 2,
    "recovered_candidate_id": (
        "recovered_authoring_package-sha256-"
        "961dec8b180bf240564bf182ade23ab6081e9b02688dac94c30d2fb8a78bfe8b"
    ),
    "deployed_candidate_id": (
        "live_deployed_opendrive-sha256-"
        "30df183d9a70cc619635b2c81078062287122956495bb26c90c2153ed3e71d41"
    ),
}
# Tests replace the whole mapping with a synthetic, internally consistent
# binding. Production callers have no CLI override and always use the frozen
# real Richmond binding above.
ACCEPTED_BINDING = RICHMOND_ACCEPTED_BINDING


class CorrespondenceError(lineage.LineageError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sha(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CorrespondenceError(f"lineage manifest contains duplicate JSON key {key}")
        value[key] = item
    return value


def _finite(value: str | None, label: str) -> float:
    if value is None or value == "":
        raise CorrespondenceError(f"{label} is blank")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise CorrespondenceError(f"{label} is not numeric") from exc
    if not math.isfinite(parsed):
        raise CorrespondenceError(f"{label} is not finite")
    return parsed


def _normalized_projection(root: ET.Element) -> str | None:
    value = root.findtext("./header/geoReference")
    return " ".join(value.split()) if value and value.strip() else None


def _header_offset(root: ET.Element) -> dict:
    offset = root.find("./header/offset")
    if offset is None:
        return {"x": 0.0, "y": 0.0, "z": 0.0, "hdg": 0.0}
    allowed = {"x", "y", "z", "hdg"}
    unknown = set(offset.attrib) - allowed
    if unknown:
        raise CorrespondenceError(f"header offset has unsupported attributes {sorted(unknown)}")
    return {
        key: _finite(offset.get(key, "0"), f"header offset {key}")
        for key in sorted(allowed)
    }


def _canonical_attributes(element: ET.Element, *, exclude: set[str] | None = None):
    excluded = exclude or set()
    return tuple(sorted((key, value) for key, value in element.attrib.items() if key not in excluded))


def _lane_profile(lane: ET.Element, side: str) -> tuple:
    lane_id = lane.get("id")
    if lane_id is None or lane_id == "":
        raise CorrespondenceError("lane has a blank ID")
    widths = tuple(
        sorted(_canonical_attributes(item) for item in lane.findall("./width"))
    )
    borders = tuple(
        sorted(_canonical_attributes(item) for item in lane.findall("./border"))
    )
    marks = []
    for mark in lane.findall("./roadMark"):
        children = tuple(
            (_local_name(child.tag), _canonical_attributes(child))
            for child in mark.iter() if child is not mark
        )
        marks.append((_canonical_attributes(mark), children))
    links = []
    for kind in ("predecessor", "successor"):
        for item in lane.findall(f"./link/{kind}"):
            linked_id = item.get("id")
            if linked_id is None or linked_id == "":
                raise CorrespondenceError("lane link has a blank ID")
            links.append((kind, linked_id))
    return (
        side, lane_id, lane.get("type") or "", lane.get("level") or "",
        widths, borders, tuple(marks), tuple(sorted(links)),
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _road_lane_signatures(road: ET.Element, length: float) -> tuple[str, str, bool]:
    detailed = []
    family = set()
    sections = road.findall("./lanes/laneSection")
    starts = []
    for section in sections:
        starts.append(_finite(section.get("s"), f"road {road.get('id')} laneSection s"))
    if starts != sorted(starts) or len(starts) != len(set(starts)):
        raise CorrespondenceError(f"road {road.get('id')} lane sections are not strictly ordered")
    for index, section in enumerate(sections):
        start = starts[index]
        end = starts[index + 1] if index + 1 < len(starts) else length
        if start < -NUMERIC_TOLERANCE or end <= start or end > length + NUMERIC_TOLERANCE:
            raise CorrespondenceError(f"road {road.get('id')} has an invalid lane-section range")
        profiles = []
        for side in ("left", "center", "right"):
            seen = set()
            for lane in section.findall(f"./{side}/lane"):
                lane_id = lane.get("id")
                if lane_id in seen:
                    raise CorrespondenceError(f"road {road.get('id')} has a duplicate lane ID")
                seen.add(lane_id)
                profile = _lane_profile(lane, side)
                profiles.append(profile)
                family.add(profile)
        detailed.append((round(start / length, 12), round(end / length, 12), tuple(sorted(profiles))))
    return _sha(detailed), _sha(sorted(family)), bool(family)


def _road_lane_ids(road: ET.Element) -> set[str]:
    return {
        lane.get("id")
        for lane in road.findall("./lanes/laneSection/*/lane")
        if lane.get("id") is not None
    }


def _parse_plan_view(road: ET.Element, length: float) -> list[dict]:
    records = []
    for element in road.findall("./planView/geometry"):
        children = [child for child in element if _local_name(child.tag) in SUPPORTED_GEOMETRIES]
        if len(children) != 1 or len(list(element)) != 1:
            raise CorrespondenceError(
                f"road {road.get('id')} has unsupported or ambiguous plan-view geometry"
            )
        shape = _local_name(children[0].tag)
        record = {
            "s": _finite(element.get("s"), f"road {road.get('id')} geometry s"),
            "x": _finite(element.get("x"), f"road {road.get('id')} geometry x"),
            "y": _finite(element.get("y"), f"road {road.get('id')} geometry y"),
            "hdg": _finite(element.get("hdg"), f"road {road.get('id')} geometry hdg"),
            "length": _finite(element.get("length"), f"road {road.get('id')} geometry length"),
            "shape": shape,
        }
        if record["length"] <= 0:
            raise CorrespondenceError(f"road {road.get('id')} geometry has non-positive length")
        if shape == "arc":
            record["curvature"] = _finite(children[0].get("curvature"), "arc curvature")
        elif shape == "spiral":
            record["curv_start"] = _finite(children[0].get("curvStart"), "spiral curvStart")
            record["curv_end"] = _finite(children[0].get("curvEnd"), "spiral curvEnd")
        records.append(record)
    records.sort(key=lambda item: item["s"])
    if not records or abs(records[0]["s"]) > NUMERIC_TOLERANCE:
        raise CorrespondenceError(f"road {road.get('id')} plan view does not begin at s=0")
    for index, item in enumerate(records):
        expected_end = item["s"] + item["length"]
        next_start = records[index + 1]["s"] if index + 1 < len(records) else length
        if abs(expected_end - next_start) > 1e-3:
            raise CorrespondenceError(f"road {road.get('id')} plan view is not contiguous")
    return records


def _elevation_records(road: ET.Element, length: float) -> list[dict]:
    records = []
    for item in road.findall("./elevationProfile/elevation"):
        record = {"s": _finite(item.get("s"), "elevation s")}
        for key in ("a", "b", "c", "d"):
            record[key] = _finite(item.get(key), f"elevation {key}")
        records.append(record)
    records.sort(key=lambda item: item["s"])
    if records and (records[0]["s"] < -NUMERIC_TOLERANCE or records[-1]["s"] > length):
        raise CorrespondenceError(f"road {road.get('id')} elevation is outside road bounds")
    if records and abs(records[0]["s"]) > NUMERIC_TOLERANCE:
        raise CorrespondenceError(f"road {road.get('id')} elevation does not begin at s=0")
    if len({item["s"] for item in records}) != len(records):
        raise CorrespondenceError(f"road {road.get('id')} has duplicate elevation stations")
    return records


def _evaluate_plan(record: dict, distance: float) -> tuple[float, float, float]:
    distance = min(max(distance, 0.0), record["length"])
    heading = record["hdg"]
    if record["shape"] == "line":
        return (
            record["x"] + distance * math.cos(heading),
            record["y"] + distance * math.sin(heading), heading,
        )
    if record["shape"] == "arc":
        curvature = record["curvature"]
        if abs(curvature) < 1e-14:
            return (
                record["x"] + distance * math.cos(heading),
                record["y"] + distance * math.sin(heading), heading,
            )
        end = heading + curvature * distance
        return (
            record["x"] + (math.sin(end) - math.sin(heading)) / curvature,
            record["y"] - (math.cos(end) - math.cos(heading)) / curvature,
            end,
        )
    # Deterministic Simpson integration of a clothoid.
    slope = (record["curv_end"] - record["curv_start"]) / record["length"]
    intervals = max(2, int(math.ceil(distance / 0.05)))
    if intervals % 2:
        intervals += 1
    step = distance / intervals if intervals else 0.0

    def angle(value: float) -> float:
        return heading + record["curv_start"] * value + 0.5 * slope * value * value

    cos_sum = math.cos(angle(0.0)) + math.cos(angle(distance))
    sin_sum = math.sin(angle(0.0)) + math.sin(angle(distance))
    for index in range(1, intervals):
        factor = 4 if index % 2 else 2
        cos_sum += factor * math.cos(angle(index * step))
        sin_sum += factor * math.sin(angle(index * step))
    return (
        record["x"] + step * cos_sum / 3.0,
        record["y"] + step * sin_sum / 3.0,
        angle(distance),
    )


def _road_xyz(road: dict, station: float) -> tuple[float, float, float]:
    selected = road["plan"][0]
    for record in road["plan"]:
        if record["s"] <= station + NUMERIC_TOLERANCE:
            selected = record
        else:
            break
    x, y, _heading = _evaluate_plan(selected, station - selected["s"])
    z = 0.0
    selected_elevation = None
    for record in road["elevation"]:
        if record["s"] <= station + NUMERIC_TOLERANCE:
            selected_elevation = record
        else:
            break
    if selected_elevation is not None:
        ds = station - selected_elevation["s"]
        z = (
            selected_elevation["a"] + selected_elevation["b"] * ds
            + selected_elevation["c"] * ds**2 + selected_elevation["d"] * ds**3
        )
    if not all(math.isfinite(value) for value in (x, y, z)):
        raise CorrespondenceError(f"road {road['id']} produced non-finite geometry")
    return x, y, z


def _sample_road(road: dict) -> None:
    intervals = max(1, int(math.ceil(road["length"] / SAMPLE_INTERVAL_M)))
    stations = [road["length"] * index / intervals for index in range(intervals + 1)]
    points = [_road_xyz(road, station) for station in stations]
    weights = []
    for index, station in enumerate(stations):
        before = station - stations[index - 1] if index else 0.0
        after = stations[index + 1] - station if index + 1 < len(stations) else 0.0
        weights.append((before + after) / 2.0)
    quantized = [
        tuple(round(value / COORDINATE_QUANTIZATION_M) for value in point)
        for point in points
    ]
    forward = _sha(quantized)
    reverse = _sha(list(reversed(quantized)))
    road["stations"] = stations
    road["points"] = points
    road["weights"] = weights
    road["geometry_signature"] = min(forward, reverse)
    road["bbox"] = (
        min(point[0] for point in points), max(point[0] for point in points),
        min(point[1] for point in points), max(point[1] for point in points),
        min(point[2] for point in points), max(point[2] for point in points),
    )


def _road_link_signature(road: ET.Element) -> tuple[str, list[dict]]:
    records = []
    for kind in ("predecessor", "successor"):
        for item in road.findall(f"./link/{kind}"):
            target = item.get("elementId")
            element_type = item.get("elementType") or ""
            if not target:
                raise CorrespondenceError(f"road {road.get('id')} has a blank road-link target")
            if element_type not in {"road", "junction"}:
                raise CorrespondenceError(f"road {road.get('id')} has an invalid road-link type")
            records.append({
                "kind": kind, "element_type": element_type, "target": target,
                "contact_point": item.get("contactPoint") or "",
            })
    shape = sorted(
        (item["kind"], item["element_type"], item["contact_point"])
        for item in records
    )
    return _sha(shape), records


def parse_xodr(content: bytes, label: str) -> dict:
    if b"<!doctype" in content.lower():
        raise CorrespondenceError(f"{label} must not contain a DTD")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise CorrespondenceError(f"{label} is not valid XML") from exc
    if _local_name(root.tag) != "OpenDRIVE":
        raise CorrespondenceError(f"{label} is not OpenDRIVE")
    roads = {}
    for element in root.findall("./road"):
        road_id = element.get("id")
        if not road_id or road_id in roads:
            raise CorrespondenceError(f"{label} has a blank or duplicate road ID")
        length = _finite(element.get("length"), f"road {road_id} length")
        if length <= 0:
            raise CorrespondenceError(f"road {road_id} has non-positive length")
        junction = element.get("junction")
        if junction is None or junction == "":
            raise CorrespondenceError(f"road {road_id} has blank junction membership")
        lane_detail, lane_family, has_lanes = _road_lane_signatures(element, length)
        link_signature, links = _road_link_signature(element)
        road = {
            "id": road_id, "length": length, "junction": junction,
            "plan": _parse_plan_view(element, length),
            "elevation": _elevation_records(element, length),
            "lane_detail_signature": lane_detail,
            "lane_family_signature": lane_family,
            "has_lanes": has_lanes,
            "lane_ids": _road_lane_ids(element),
            "link_signature": link_signature, "links": links,
        }
        _sample_road(road)
        roads[road_id] = road
    if not roads:
        raise CorrespondenceError(f"{label} contains no roads")
    junctions = {}
    for element in root.findall("./junction"):
        junction_id = element.get("id")
        if not junction_id or junction_id in junctions:
            raise CorrespondenceError(f"{label} has a blank or duplicate junction ID")
        connections = []
        seen_connections = set()
        for connection in element.findall("./connection"):
            connection_id = connection.get("id")
            if not connection_id or connection_id in seen_connections:
                raise CorrespondenceError(f"junction {junction_id} has a blank or duplicate connection ID")
            seen_connections.add(connection_id)
            incoming = connection.get("incomingRoad")
            connecting = connection.get("connectingRoad")
            if incoming not in roads or connecting not in roads:
                raise CorrespondenceError(f"junction {junction_id} references a missing road")
            lane_links = []
            for lane_link in connection.findall("./laneLink"):
                source = lane_link.get("from")
                target = lane_link.get("to")
                if source is None or source == "" or target is None or target == "":
                    raise CorrespondenceError(f"junction {junction_id} has a blank lane link")
                if source not in roads[incoming]["lane_ids"] or target not in roads[connecting]["lane_ids"]:
                    raise CorrespondenceError(
                        f"junction {junction_id} lane link references a missing lane"
                    )
                lane_links.append((source, target))
            connections.append({
                "id": connection_id, "incoming": incoming, "connecting": connecting,
                "contact_point": connection.get("contactPoint") or "",
                "lane_links": tuple(sorted(lane_links)),
            })
        junctions[junction_id] = {"id": junction_id, "connections": connections}
    for road in roads.values():
        if road["junction"] != "-1" and road["junction"] not in junctions:
            raise CorrespondenceError(f"road {road['id']} references missing junction {road['junction']}")
        for link in road["links"]:
            collection = roads if link["element_type"] == "road" else junctions
            if link["target"] not in collection:
                raise CorrespondenceError(f"road {road['id']} link references a missing element")
    return {
        "projection": _normalized_projection(root),
        "header_offset": _header_offset(root),
        "roads": roads, "junctions": junctions,
    }


def _bbox_close(left: tuple, right: tuple, tolerance: float) -> bool:
    return not (
        left[1] + tolerance < right[0] or right[1] + tolerance < left[0]
        or left[3] + tolerance < right[2] or right[3] + tolerance < left[2]
        or left[5] + tolerance < right[4] or right[5] + tolerance < left[4]
    )


def _point_segment_distance(point, start, end) -> float:
    vector = tuple(end[index] - start[index] for index in range(3))
    offset = tuple(point[index] - start[index] for index in range(3))
    denominator = sum(value * value for value in vector)
    if denominator <= 1e-20:
        return math.dist(point, start)
    fraction = min(1.0, max(0.0, sum(offset[index] * vector[index] for index in range(3)) / denominator))
    projection = tuple(start[index] + fraction * vector[index] for index in range(3))
    return math.dist(point, projection)


def _distances_to_polyline(points: list[tuple], polyline: list[tuple]) -> list[float]:
    segments = list(zip(polyline, polyline[1:]))
    return [min(_point_segment_distance(point, start, end) for start, end in segments) for point in points]


def _coverage(road: dict, distances: list[float], threshold: float) -> float:
    matched = sum(weight for weight, distance in zip(road["weights"], distances) if distance <= threshold)
    return min(1.0, matched / road["length"])


def _round(value: float) -> float:
    return round(value, 9)


def _orientation_mean_distance(
    old: dict, deployed: dict, *, reversed_direction: bool
) -> float:
    total = 0.0
    weight_total = 0.0
    for station, point, weight in zip(old["stations"], old["points"], old["weights"]):
        fraction = station / old["length"]
        if reversed_direction:
            fraction = 1.0 - fraction
        total += weight * math.dist(
            point, _road_xyz(deployed, fraction * deployed["length"])
        )
        weight_total += weight
    for station, point, weight in zip(
        deployed["stations"], deployed["points"], deployed["weights"]
    ):
        fraction = station / deployed["length"]
        if reversed_direction:
            fraction = 1.0 - fraction
        total += weight * math.dist(point, _road_xyz(old, fraction * old["length"]))
        weight_total += weight
    return total / weight_total


def road_edge(old: dict, deployed: dict) -> dict | None:
    if not _bbox_close(old["bbox"], deployed["bbox"], LOOSE_DISTANCE_M):
        return None
    old_distances = _distances_to_polyline(old["points"], deployed["points"])
    new_distances = _distances_to_polyline(deployed["points"], old["points"])
    metrics = {
        "old_to_deployed_strict": _coverage(old, old_distances, STRICT_DISTANCE_M),
        "deployed_to_old_strict": _coverage(deployed, new_distances, STRICT_DISTANCE_M),
        "old_to_deployed_loose": _coverage(old, old_distances, LOOSE_DISTANCE_M),
        "deployed_to_old_loose": _coverage(deployed, new_distances, LOOSE_DISTANCE_M),
        "symmetric_hausdorff_m": max(max(old_distances), max(new_distances)),
    }
    strict = (
        max(metrics["old_to_deployed_strict"], metrics["deployed_to_old_strict"])
        >= STRICT_FULL_COVERAGE
        and min(metrics["old_to_deployed_strict"], metrics["deployed_to_old_strict"])
        >= MIN_PARTIAL_COVERAGE
    )
    loose = (
        max(metrics["old_to_deployed_loose"], metrics["deployed_to_old_loose"])
        >= LOOSE_FULL_COVERAGE
        and min(metrics["old_to_deployed_loose"], metrics["deployed_to_old_loose"])
        >= MIN_PARTIAL_COVERAGE
    )
    if not strict and not loose:
        return None
    forward_endpoints = math.dist(old["points"][0], deployed["points"][0]) + math.dist(old["points"][-1], deployed["points"][-1])
    reverse_endpoints = math.dist(old["points"][0], deployed["points"][-1]) + math.dist(old["points"][-1], deployed["points"][0])
    forward_mean = _orientation_mean_distance(
        old, deployed, reversed_direction=False
    )
    reverse_mean = _orientation_mean_distance(
        old, deployed, reversed_direction=True
    )
    if (
        forward_mean <= STRICT_DISTANCE_M
        and reverse_mean - forward_mean >= ORIENTATION_WIN_MARGIN_M
    ):
        orientation = "forward"
    elif (
        reverse_mean <= STRICT_DISTANCE_M
        and forward_mean - reverse_mean >= ORIENTATION_WIN_MARGIN_M
    ):
        orientation = "reversed"
    else:
        orientation = "indeterminate"
    return {
        "old_id": old["id"], "deployed_id": deployed["id"],
        "strength": "strict" if strict else "ambiguous_band",
        "orientation": orientation,
        "forward_endpoint_sum_m": _round(forward_endpoints),
        "reverse_endpoint_sum_m": _round(reverse_endpoints),
        "best_endpoint_sum_m": _round(min(forward_endpoints, reverse_endpoints)),
        "orientation_forward_mean_m": _round(forward_mean),
        "orientation_reversed_mean_m": _round(reverse_mean),
        "orientation_win_margin_m": ORIENTATION_WIN_MARGIN_M,
        "length_relative_delta": _round(
            abs(old["length"] - deployed["length"])
            / max(old["length"], deployed["length"])
        ),
        "metrics": {key: _round(value) for key, value in sorted(metrics.items())},
        "lane_family_equal": old["lane_family_signature"] == deployed["lane_family_signature"],
        "lane_detail_equal": old["lane_detail_signature"] == deployed["lane_detail_signature"],
        "link_shape_equal": old["link_signature"] == deployed["link_signature"],
        "junction_membership_equal": (old["junction"] == "-1") == (deployed["junction"] == "-1"),
    }


def _components(old_ids: list[str], new_ids: list[str], edges: list[dict], prefix: str) -> list[dict]:
    adjacency = defaultdict(set)
    for edge in edges:
        left = ("old", edge["old_id"])
        right = ("deployed", edge["deployed_id"])
        adjacency[left].add(right)
        adjacency[right].add(left)
    nodes = [("old", value) for value in old_ids] + [("deployed", value) for value in new_ids]
    found = []
    seen = set()
    for node in sorted(nodes):
        if node in seen:
            continue
        queue = deque([node]); seen.add(node); members = []
        while queue:
            current = queue.popleft(); members.append(current)
            for neighbor in sorted(adjacency[current]):
                if neighbor not in seen:
                    seen.add(neighbor); queue.append(neighbor)
        old = sorted(value for side, value in members if side == "old")
        deployed = sorted(value for side, value in members if side == "deployed")
        component_edges = sorted(
            (edge for edge in edges if edge["old_id"] in old and edge["deployed_id"] in deployed),
            key=lambda item: (item["old_id"], item["deployed_id"]),
        )
        found.append({"old_ids": old, "deployed_ids": deployed, "edges": component_edges})
    found.sort(key=lambda item: (item["old_ids"], item["deployed_ids"]))
    for index, item in enumerate(found, 1):
        item["component_id"] = f"{prefix}-{index:04d}"
    return found


def _mapped_link_signature(road: dict, road_components: dict[str, str]) -> Counter:
    values = Counter()
    for link in road["links"]:
        target = (
            road_components[link["target"]]
            if link["element_type"] == "road"
            else "junction-target-unresolved"
        )
        values[(
            link["kind"], link["element_type"], target, link["contact_point"]
        )] += 1
    return values


def _boundary_signature(
    roads: dict[str, dict], members: set[str], road_components: dict[str, str]
) -> Counter:
    values = Counter()
    for road_id in members:
        for link in roads[road_id]["links"]:
            if link["element_type"] == "road" and link["target"] in members:
                continue
            target = (
                road_components[link["target"]]
                if link["element_type"] == "road"
                else "junction-target-unresolved"
            )
            values[(
                link["kind"], link["element_type"], target, link["contact_point"]
            )] += 1
    return values


def _joint_coverage(source: dict, targets: list[dict], threshold: float) -> float:
    distances = [
        min(_distances_to_polyline([point], target["points"])[0] for target in targets)
        for point in source["points"]
    ]
    return _coverage(source, distances, threshold)


def _max_pair_overlap(roads: list[dict]) -> float:
    maximum = 0.0
    for index, left in enumerate(roads):
        for right in roads[index + 1:]:
            left_distances = _distances_to_polyline(left["points"], right["points"])
            right_distances = _distances_to_polyline(right["points"], left["points"])
            maximum = max(
                maximum,
                max(_coverage(left, left_distances, STRICT_DISTANCE_M),
                    _coverage(right, right_distances, STRICT_DISTANCE_M)),
            )
    return maximum


def classify_road_components(old_map: dict, deployed_map: dict) -> tuple[list[dict], dict, dict]:
    edges = []
    for old_id in sorted(old_map["roads"]):
        for deployed_id in sorted(deployed_map["roads"]):
            edge = road_edge(old_map["roads"][old_id], deployed_map["roads"][deployed_id])
            if edge is not None:
                edges.append(edge)
    components = _components(sorted(old_map["roads"]), sorted(deployed_map["roads"]), edges, "road-component")
    old_component = {
        value: component["component_id"]
        for component in components for value in component["old_ids"]
    }
    deployed_component = {
        value: component["component_id"]
        for component in components for value in component["deployed_ids"]
    }
    for component in components:
        old_ids = component["old_ids"]; new_ids = component["deployed_ids"]
        reasons = []
        if not old_ids:
            category = "added"
        elif not new_ids:
            category = "removed"
        elif any(edge["strength"] != "strict" for edge in component["edges"]):
            category = "ambiguous"; reasons.append("candidate edge falls in ambiguity band")
        elif len(old_ids) == len(new_ids) == 1:
            edge = component["edges"][0]
            bidirectional_geometry = (
                edge["metrics"]["old_to_deployed_strict"] >= STRICT_FULL_COVERAGE
                and edge["metrics"]["deployed_to_old_strict"] >= STRICT_FULL_COVERAGE
                and edge["best_endpoint_sum_m"] <= 2 * STRICT_DISTANCE_M
                and edge["length_relative_delta"]
                <= MAX_ONE_TO_ONE_LENGTH_RELATIVE_DELTA
            )
            semantic = (
                edge["lane_detail_equal"]
                and edge["junction_membership_equal"]
            )
            old_link_targets = _mapped_link_signature(
                old_map["roads"][old_ids[0]], old_component
            )
            deployed_link_targets = _mapped_link_signature(
                deployed_map["roads"][new_ids[0]], deployed_component
            )
            link_targets_proven = old_link_targets == deployed_link_targets
            component["one_to_one_evidence"] = {
                "mapped_link_targets_equal": old_link_targets == deployed_link_targets,
            }
            semantic = semantic and link_targets_proven
            if edge["orientation"] != "forward" and (
                old_map["roads"][old_ids[0]]["has_lanes"]
                or deployed_map["roads"][new_ids[0]]["has_lanes"]
            ):
                semantic = False
                reasons.append("forward lane orientation is not proven")
            if not bidirectional_geometry:
                category = "ambiguous"
                reasons.append("one-to-one bidirectional geometry/length predicates differ")
            elif not semantic:
                category = "ambiguous"; reasons.append("one-to-one semantic/topology signatures differ")
            else:
                category = "unchanged" if old_ids[0] == new_ids[0] else "renumbered"
        elif len(old_ids) == 1 and len(new_ids) > 1:
            old = old_map["roads"][old_ids[0]]
            children = [deployed_map["roads"][value] for value in new_ids]
            joint = _joint_coverage(old, children, STRICT_DISTANCE_M)
            overlap = _max_pair_overlap(children)
            contained = all(
                edge["metrics"]["deployed_to_old_strict"]
                >= STRICT_FULL_COVERAGE
                for edge in component["edges"]
            )
            length_delta = abs(
                sum(child["length"] for child in children) - old["length"]
            ) / old["length"]
            lane_bearing = old["has_lanes"] or any(
                child["has_lanes"] for child in children
            )
            orientation_ok = not lane_bearing or all(
                edge["orientation"] == "forward" for edge in component["edges"]
            )
            lane_ok = all(old["lane_family_signature"] == child["lane_family_signature"] for child in children)
            boundary_ok = _boundary_signature(
                old_map["roads"], set(old_ids), old_component
            ) == _boundary_signature(
                deployed_map["roads"], set(new_ids), deployed_component
            )
            component["many_evidence"] = {
                "joint_parent_coverage": _round(joint), "max_child_overlap": _round(overlap),
                "every_child_contained": contained,
                "length_relative_delta": _round(length_delta),
                "lane_orientation_compatible": orientation_ok,
                "lane_family_compatible": lane_ok, "boundary_topology_compatible": boundary_ok,
            }
            if (
                joint >= STRICT_FULL_COVERAGE
                and contained
                and overlap <= MAX_CHILD_OVERLAP
                and length_delta <= MAX_SPLIT_MERGE_LENGTH_RELATIVE_DELTA
                and orientation_ok
                and lane_ok and boundary_ok
            ):
                category = "split"
            else:
                category = "ambiguous"; reasons.append("one-to-many split predicates did not all pass")
        elif len(old_ids) > 1 and len(new_ids) == 1:
            deployed = deployed_map["roads"][new_ids[0]]
            parents = [old_map["roads"][value] for value in old_ids]
            joint = _joint_coverage(deployed, parents, STRICT_DISTANCE_M)
            overlap = _max_pair_overlap(parents)
            contained = all(
                edge["metrics"]["old_to_deployed_strict"]
                >= STRICT_FULL_COVERAGE
                for edge in component["edges"]
            )
            length_delta = abs(
                sum(parent["length"] for parent in parents) - deployed["length"]
            ) / deployed["length"]
            lane_bearing = deployed["has_lanes"] or any(
                parent["has_lanes"] for parent in parents
            )
            orientation_ok = not lane_bearing or all(
                edge["orientation"] == "forward" for edge in component["edges"]
            )
            lane_ok = all(deployed["lane_family_signature"] == parent["lane_family_signature"] for parent in parents)
            boundary_ok = _boundary_signature(
                old_map["roads"], set(old_ids), old_component
            ) == _boundary_signature(
                deployed_map["roads"], set(new_ids), deployed_component
            )
            component["many_evidence"] = {
                "joint_deployed_coverage": _round(joint), "max_parent_overlap": _round(overlap),
                "every_parent_contained": contained,
                "length_relative_delta": _round(length_delta),
                "lane_orientation_compatible": orientation_ok,
                "lane_family_compatible": lane_ok, "boundary_topology_compatible": boundary_ok,
            }
            if (
                joint >= STRICT_FULL_COVERAGE
                and contained
                and overlap <= MAX_CHILD_OVERLAP
                and length_delta <= MAX_SPLIT_MERGE_LENGTH_RELATIVE_DELTA
                and orientation_ok
                and lane_ok and boundary_ok
            ):
                category = "merged"
            else:
                category = "ambiguous"; reasons.append("many-to-one merge predicates did not all pass")
        else:
            category = "ambiguous"; reasons.append("N-to-M component has no deterministic classification")
        component["category"] = category
        component["reasons"] = sorted(set(reasons))
    return components, old_component, deployed_component


def _junction_tokens(junction: dict, road_components: dict[str, str]) -> tuple:
    tokens = []
    for connection in junction["connections"]:
        tokens.append((
            road_components[connection["incoming"]],
            road_components[connection["connecting"]],
            connection["contact_point"], connection["lane_links"],
        ))
    return tuple(sorted(tokens))


def classify_junction_components(
    old_map: dict,
    deployed_map: dict,
    old_roads: dict,
    deployed_roads: dict,
    road_component_categories: dict[str, str],
) -> list[dict]:
    old_tokens = {key: _junction_tokens(value, old_roads) for key, value in old_map["junctions"].items()}
    new_tokens = {key: _junction_tokens(value, deployed_roads) for key, value in deployed_map["junctions"].items()}
    edges = []
    for old_id, left in sorted(old_tokens.items()):
        for new_id, right in sorted(new_tokens.items()):
            left_set, right_set = set(left), set(right)
            if not left_set and not right_set:
                edges.append({"old_id": old_id, "deployed_id": new_id, "strength": "ambiguous_band", "token_intersection": 0})
                continue
            if not left_set or not right_set:
                edges.append({
                    "old_id": old_id, "deployed_id": new_id,
                    "strength": "ambiguous_band", "token_intersection": 0,
                    "old_token_count": len(left_set),
                    "deployed_token_count": len(right_set),
                })
                continue
            intersection = len(left_set & right_set)
            if intersection and (left_set <= right_set or right_set <= left_set):
                edges.append({
                    "old_id": old_id, "deployed_id": new_id, "strength": "strict",
                    "token_intersection": intersection,
                    "old_token_count": len(left_set), "deployed_token_count": len(right_set),
                    "exact_tokens": left == right,
                })
            elif intersection:
                edges.append({
                    "old_id": old_id, "deployed_id": new_id,
                    "strength": "ambiguous_band",
                    "token_intersection": intersection,
                    "old_token_count": len(left_set),
                    "deployed_token_count": len(right_set),
                })
    components = _components(sorted(old_tokens), sorted(new_tokens), edges, "junction-component")
    for component in components:
        old_ids, new_ids = component["old_ids"], component["deployed_ids"]
        reasons = []
        referenced_road_components = {
            road_component
            for junction_id in old_ids for token in old_tokens[junction_id]
            for road_component in token[:2]
        } | {
            road_component
            for junction_id in new_ids for token in new_tokens[junction_id]
            for road_component in token[:2]
        }
        road_mapping_safe = all(
            road_component_categories.get(value)
            in {"unchanged", "renumbered", "split", "merged"}
            for value in referenced_road_components
        )
        if not road_mapping_safe:
            category = "ambiguous"
            reasons.append("referenced road correspondence is ambiguous or terminally unmatched")
        elif not old_ids:
            category = "added"
        elif not new_ids:
            category = "removed"
        elif any(edge["strength"] != "strict" for edge in component["edges"]):
            category = "ambiguous"; reasons.append("empty or insufficient junction topology")
        elif len(old_ids) == len(new_ids) == 1 and component["edges"][0].get("exact_tokens"):
            category = "unchanged" if old_ids[0] == new_ids[0] else "renumbered"
        elif len(old_ids) == 1 and len(new_ids) > 1:
            child_counters = [Counter(new_tokens[value]) for value in new_ids]
            combined = sum(child_counters, Counter())
            disjoint = all(
                not (set(left) & set(right))
                for index, left in enumerate(child_counters)
                for right in child_counters[index + 1:]
            )
            if combined == Counter(old_tokens[old_ids[0]]) and disjoint:
                category = "split"
            else:
                category = "ambiguous"
                reasons.append("junction split is not a disjoint multiplicity-preserving partition")
        elif len(old_ids) > 1 and len(new_ids) == 1:
            parent_counters = [Counter(old_tokens[value]) for value in old_ids]
            combined = sum(parent_counters, Counter())
            disjoint = all(
                not (set(left) & set(right))
                for index, left in enumerate(parent_counters)
                for right in parent_counters[index + 1:]
            )
            if combined == Counter(new_tokens[new_ids[0]]) and disjoint:
                category = "merged"
            else:
                category = "ambiguous"
                reasons.append("junction merge is not a disjoint multiplicity-preserving partition")
        elif len(old_ids) == len(new_ids) == 1:
            category = "ambiguous"; reasons.append("junction one-to-one topology tokens differ")
        else:
            category = "ambiguous"; reasons.append("junction N-to-M component")
        component["category"] = category
        component["reasons"] = sorted(set(reasons))
        component["old_topology_sha256"] = _sha([old_tokens[value] for value in old_ids])
        component["deployed_topology_sha256"] = _sha([new_tokens[value] for value in new_ids])
    return components


def _safe_junction_maps(components: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    old = {}
    deployed = {}
    for component in components:
        if (
            component["category"] in {"unchanged", "renumbered"}
            and len(component["old_ids"]) == 1
            and len(component["deployed_ids"]) == 1
        ):
            old[component["old_ids"][0]] = component["component_id"]
            deployed[component["deployed_ids"][0]] = component["component_id"]
    return old, deployed


def _final_component_topology(
    roads: dict[str, dict],
    member_ids: set[str],
    road_components: dict[str, str],
    road_categories: dict[str, str],
    junction_components: dict[str, str],
    *,
    boundary_only: bool,
) -> tuple[Counter, set[str], list[str]]:
    signature = Counter()
    memberships = set()
    failures = []
    for road_id in sorted(member_ids):
        road = roads[road_id]
        if road["junction"] != "-1":
            mapped_junction = junction_components.get(road["junction"])
            if mapped_junction is None:
                failures.append(f"road {road_id} junction membership is not safely mapped")
            else:
                memberships.add(mapped_junction)
        for link in road["links"]:
            if (
                boundary_only
                and link["element_type"] == "road"
                and link["target"] in member_ids
            ):
                continue
            if link["element_type"] == "road":
                target = road_components[link["target"]]
                if road_categories.get(target) not in {"unchanged", "renumbered"}:
                    failures.append(
                        f"linked road component {target} is not finally unchanged/renumbered"
                    )
            else:
                target = junction_components.get(link["target"])
                if target is None:
                    failures.append(
                        f"linked junction {link['target']} is not safely unchanged/renumbered"
                    )
                    target = f"unsafe-junction:{link['target']}"
            signature[(
                link["kind"], link["element_type"], target, link["contact_point"]
            )] += 1
    return signature, memberships, sorted(set(failures))


def reconcile_road_junction_fixpoint(
    old_map: dict,
    deployed_map: dict,
    roads: list[dict],
    old_road_components: dict[str, str],
    deployed_road_components: dict[str, str],
) -> tuple[list[dict], int]:
    maximum_iterations = len(roads) + len(old_map["junctions"]) + len(deployed_map["junctions"]) + 1
    for iteration in range(1, maximum_iterations + 1):
        road_categories = {
            component["component_id"]: component["category"] for component in roads
        }
        junctions = classify_junction_components(
            old_map,
            deployed_map,
            old_road_components,
            deployed_road_components,
            road_categories,
        )
        old_junction_components, deployed_junction_components = _safe_junction_maps(
            junctions
        )
        changed = False
        for component in roads:
            if component["category"] not in {
                "unchanged", "renumbered", "split", "merged"
            }:
                continue
            boundary_only = component["category"] in {"split", "merged"}
            old_signature, old_memberships, old_failures = _final_component_topology(
                old_map["roads"],
                set(component["old_ids"]),
                old_road_components,
                road_categories,
                old_junction_components,
                boundary_only=boundary_only,
            )
            deployed_signature, deployed_memberships, deployed_failures = (
                _final_component_topology(
                    deployed_map["roads"],
                    set(component["deployed_ids"]),
                    deployed_road_components,
                    road_categories,
                    deployed_junction_components,
                    boundary_only=boundary_only,
                )
            )
            evidence = {
                "mapped_boundary_links_equal": old_signature == deployed_signature,
                "mapped_junction_memberships_equal": (
                    old_memberships == deployed_memberships
                ),
                "old_failures": old_failures,
                "deployed_failures": deployed_failures,
            }
            component["final_topology_evidence"] = evidence
            if (
                old_signature != deployed_signature
                or old_memberships != deployed_memberships
                or old_failures
                or deployed_failures
            ):
                component["category"] = "ambiguous"
                component["reasons"] = sorted(set(
                    component["reasons"]
                    + ["final mapped road/junction topology is not safely equivalent"]
                ))
                changed = True
        if not changed:
            return junctions, iteration
    raise CorrespondenceError("road/junction topology fixpoint did not converge")


def _account(components: list[dict], old_ids: set[str], deployed_ids: set[str], label: str) -> dict:
    seen_old = [value for component in components for value in component["old_ids"]]
    seen_new = [value for component in components for value in component["deployed_ids"]]
    if len(seen_old) != len(set(seen_old)) or set(seen_old) != old_ids:
        raise CorrespondenceError(f"{label} old terminal accounting is invalid")
    if len(seen_new) != len(set(seen_new)) or set(seen_new) != deployed_ids:
        raise CorrespondenceError(f"{label} deployed terminal accounting is invalid")
    counts = Counter(component["category"] for component in components)
    return {
        "component_category_counts": dict(sorted(counts.items())),
        "component_count": len(components),
        "old_item_count": len(seen_old), "deployed_item_count": len(seen_new),
        "old_terminal_accounting_complete": True,
        "deployed_terminal_accounting_complete": True,
    }


def _manifest_binding(
    manifest: dict,
    old_artifact: dict,
    deployed_artifact: dict,
    old_summary: dict,
    deployed_summary: dict,
) -> dict:
    if manifest.get("schema") != lineage.SCHEMA:
        raise CorrespondenceError("lineage manifest schema is not accepted")
    if manifest.get("acceptance_eligible") is not False:
        raise CorrespondenceError("lineage manifest must remain acceptance-ineligible")
    selection = manifest.get("selection")
    if (
        not isinstance(selection, dict)
        or selection.get("status") != "blocked_unresolved_opendrive_lineage"
        or selection.get("scoring_permitted") is not False
        or selection.get("selected_candidate_id") is not None
    ):
        raise CorrespondenceError("lineage manifest selection policy is not fail-closed")
    if manifest.get("manifest_mutability") != "exclusive_no_replace":
        raise CorrespondenceError("lineage manifest mutability is not accepted")
    reconciliation = manifest.get("lineage_reconciliation")
    if not isinstance(reconciliation, dict) or reconciliation.get("status") != "unresolved_blocking":
        raise CorrespondenceError("lineage manifest reconciliation is not unresolved-blocking")
    dependency_graph = manifest.get("recovered_material_dependency_graph")
    if (
        not isinstance(dependency_graph, dict)
        or dependency_graph.get("status")
        != "complete_package_inventory_frozen_dependency_edges_unreviewed"
        or dependency_graph.get("selection_blocking_until_complete") is not True
    ):
        raise CorrespondenceError("lineage manifest lacks a frozen complete package inventory")
    candidates = manifest.get("candidates")
    if not isinstance(candidates, list) or len(candidates) < 2:
        raise CorrespondenceError("lineage manifest candidate set is incomplete")
    candidate_ids = []
    artifacts = {}
    candidates_by_name = {}
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in candidate_ids:
            raise CorrespondenceError("lineage manifest candidate IDs are invalid")
        candidate_ids.append(candidate_id)
        candidate_name = candidate.get("candidate_name")
        candidate_artifacts = candidate.get("artifacts", [])
        if not isinstance(candidate_name, str) or not candidate_name:
            raise CorrespondenceError("lineage manifest candidate name is invalid")
        if candidate_name in candidates_by_name:
            raise CorrespondenceError("lineage manifest duplicates a candidate name")
        candidates_by_name[candidate_name] = candidate
        if candidate_id != lineage.candidate_id(candidate_name, candidate_artifacts):
            raise CorrespondenceError("lineage manifest candidate ID does not bind its artifacts")
        for artifact in candidate_artifacts:
            label = artifact.get("label")
            if label in {"recovered_old_xodr", "live_deployed_xodr"}:
                if label in artifacts:
                    raise CorrespondenceError("lineage manifest duplicates a bound XODR")
                artifacts[label] = (candidate_id, artifact)
    if set(candidates_by_name) != {
        "recovered_authoring_package", "live_deployed_opendrive"
    }:
        raise CorrespondenceError("lineage manifest candidate names are not accepted")
    if (
        candidates_by_name["recovered_authoring_package"].get("candidate_id")
        != ACCEPTED_BINDING["recovered_candidate_id"]
        or candidates_by_name["live_deployed_opendrive"].get("candidate_id")
        != ACCEPTED_BINDING["deployed_candidate_id"]
    ):
        raise CorrespondenceError("lineage manifest candidate IDs differ from the accepted site binding")
    recovered_artifacts = candidates_by_name["recovered_authoring_package"].get(
        "artifacts", []
    )
    recovered_labels = {item.get("label") for item in recovered_artifacts}
    if not {
        "recovered_fbx", "recovered_geojson", "recovered_old_xodr"
    } <= recovered_labels or not any(
        isinstance(label, str) and label.startswith("rrdata_xml:")
        for label in recovered_labels
    ) or not any(
        isinstance(label, str) and label.startswith("material_file:")
        for label in recovered_labels
    ):
        raise CorrespondenceError("lineage manifest recovered package artifacts are incomplete")
    package_paths = dependency_graph.get("complete_package_paths")
    package_count = dependency_graph.get("package_file_count")
    inventory_sha = dependency_graph.get("package_inventory_sha256")
    old_package_path = Path(old_artifact["path"])
    try:
        artifact_relative_paths = sorted(
            str(Path(item["path"]).relative_to(old_package_path.parent).as_posix())
            for item in recovered_artifacts
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CorrespondenceError("lineage recovered artifacts are outside the package root") from exc
    if (
        package_paths != artifact_relative_paths
        or package_count != len(recovered_artifacts)
        or package_count != ACCEPTED_BINDING["package_file_count"]
        or len(recovered_artifacts) != ACCEPTED_BINDING["package_file_count"]
        or dependency_graph.get("package_directory_count")
        != ACCEPTED_BINDING["package_directory_count"]
        or inventory_sha != ACCEPTED_BINDING["package_inventory_sha256"]
        or not isinstance(inventory_sha, str)
        or len(inventory_sha) != 64
        or any(character not in "0123456789abcdef" for character in inventory_sha)
    ):
        raise CorrespondenceError("lineage complete package inventory binding is invalid")
    expected = {
        "recovered_old_xodr": (old_artifact, old_summary),
        "live_deployed_xodr": (deployed_artifact, deployed_summary),
    }
    output = {}
    for label, (actual, summary) in expected.items():
        if label not in artifacts:
            raise CorrespondenceError(f"lineage manifest lacks {label}")
        candidate_id, bound = artifacts[label]
        if (
            bound.get("sha256") != actual["sha256"]
            or bound.get("bytes") != actual["bytes"]
            or bound.get("path") != actual["path"]
            or bound.get("kind") != "xodr"
            or bound.get("summary") != summary
        ):
            raise CorrespondenceError(f"lineage manifest hash binding differs for {label}")
        output[label] = {"candidate_id": candidate_id, "artifact_sha256": actual["sha256"]}
    if (
        old_artifact["sha256"] != ACCEPTED_BINDING["old_xodr_sha256"]
        or deployed_artifact["sha256"]
        != ACCEPTED_BINDING["deployed_xodr_sha256"]
    ):
        raise CorrespondenceError("XODR hashes differ from the accepted site binding")
    if (
        reconciliation.get("recovered_topology")
        != {"roads": old_summary["road_count"], "junctions": old_summary["junction_count"]}
        or reconciliation.get("live_topology")
        != {
            "roads": deployed_summary["road_count"],
            "junctions": deployed_summary["junction_count"],
        }
        or reconciliation.get("same_projection_text")
        is not (old_summary["projection"] == deployed_summary["projection"])
        or reconciliation.get("same_topology_sha256")
        is not (
            old_summary["topology_sha256"] == deployed_summary["topology_sha256"]
        )
    ):
        raise CorrespondenceError("lineage reconciliation does not bind XODR summaries")
    return output


def _build_snapshot(args: argparse.Namespace) -> dict:
    old_content, old_artifact = lineage.read_input(args.old_xodr, "old_xodr")
    deployed_content, deployed_artifact = lineage.read_input(args.deployed_xodr, "deployed_xodr")
    manifest_content, manifest_artifact = lineage.read_input(args.lineage_manifest, "lineage_manifest")
    tool_content, tool_artifact = lineage.read_input(str(Path(__file__).absolute()), "correspondence_tool")
    try:
        manifest = json.loads(manifest_content, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorrespondenceError("lineage manifest is not valid JSON") from exc
    old_summary = lineage.summarize_xodr(old_content, "old_xodr")
    deployed_summary = lineage.summarize_xodr(deployed_content, "deployed_xodr")
    bindings = _manifest_binding(
        manifest, old_artifact, deployed_artifact, old_summary, deployed_summary
    )
    old_map = parse_xodr(old_content, "old_xodr")
    deployed_map = parse_xodr(deployed_content, "deployed_xodr")
    if old_map["projection"] != deployed_map["projection"]:
        raise CorrespondenceError("XODR georeference gauges differ")
    if old_map["header_offset"] != deployed_map["header_offset"]:
        raise CorrespondenceError("XODR header-offset gauges differ")
    roads, old_road_components, deployed_road_components = classify_road_components(old_map, deployed_map)
    junctions, reconciliation_iterations = reconcile_road_junction_fixpoint(
        old_map,
        deployed_map,
        roads,
        old_road_components,
        deployed_road_components,
    )
    report = {
        "schema": SCHEMA, "created_at_utc": utc_now(),
        "algorithm": {
            "name": ALGORITHM,
            "python_version": platform.python_version(),
            "xml_parser": "xml.etree.ElementTree",
            "expat_version": pyexpat.EXPAT_VERSION,
            "thresholds": {
                "sample_interval_m": SAMPLE_INTERVAL_M,
                "strict_distance_m": STRICT_DISTANCE_M,
                "loose_distance_m": LOOSE_DISTANCE_M,
                "strict_full_coverage": STRICT_FULL_COVERAGE,
                "loose_full_coverage": LOOSE_FULL_COVERAGE,
                "minimum_partial_coverage": MIN_PARTIAL_COVERAGE,
                "maximum_child_overlap": MAX_CHILD_OVERLAP,
                "maximum_one_to_one_length_relative_delta": MAX_ONE_TO_ONE_LENGTH_RELATIVE_DELTA,
                "maximum_split_merge_length_relative_delta": MAX_SPLIT_MERGE_LENGTH_RELATIVE_DELTA,
                "orientation_win_margin_m": ORIENTATION_WIN_MARGIN_M,
                "coordinate_quantization_m": COORDINATE_QUANTIZATION_M,
            },
        },
        "tool": {**tool_artifact, "sha256": hashlib.sha256(tool_content).hexdigest()},
        "inputs": {
            "old_xodr": old_artifact, "deployed_xodr": deployed_artifact,
            "lineage_manifest": manifest_artifact,
            "lineage_bindings": bindings,
        },
        "coordinate_gauge": {
            "projection": old_map["projection"], "header_offset": old_map["header_offset"],
            "identical": True,
        },
        "roads": {
            "components": roads,
            "signatures": {
                "old": {
                    road_id: {
                        "geometry_sha256": value["geometry_signature"],
                        "lane_detail_sha256": value["lane_detail_signature"],
                        "lane_family_sha256": value["lane_family_signature"],
                        "link_shape_sha256": value["link_signature"],
                    }
                    for road_id, value in sorted(old_map["roads"].items())
                },
                "deployed": {
                    road_id: {
                        "geometry_sha256": value["geometry_signature"],
                        "lane_detail_sha256": value["lane_detail_signature"],
                        "lane_family_sha256": value["lane_family_signature"],
                        "link_shape_sha256": value["link_signature"],
                    }
                    for road_id, value in sorted(deployed_map["roads"].items())
                },
            },
            "accounting": _account(roads, set(old_map["roads"]), set(deployed_map["roads"]), "road"),
            "road_junction_fixpoint_iterations": reconciliation_iterations,
        },
        "junctions": {
            "components": junctions,
            "accounting": _account(junctions, set(old_map["junctions"]), set(deployed_map["junctions"]), "junction"),
        },
        "acceptance_eligible": False,
        "scoring_permitted": False,
        "lineage_resolved": False,
        "selection": None,
        "limitations": [
            "geometry_and_topology_correspondence_cannot_prove_authoring_or_export_lineage",
            "no_holdout_runtime_ue5_or_live_service_evidence_consumed",
            "ambiguous_components_require_separate_versioned_provenance",
        ],
    }
    return report


def _comparable(report: dict) -> bytes:
    value = dict(report); value.pop("created_at_utc", None)
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def build(args: argparse.Namespace) -> dict:
    first = _build_snapshot(args)
    second = _build_snapshot(args)
    if _comparable(first) != _comparable(second):
        raise CorrespondenceError("input snapshot changed between complete passes")
    return second


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-xodr", required=True)
    parser.add_argument("--deployed-xodr", required=True)
    parser.add_argument("--lineage-manifest", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        report = build(args)
        lineage.publish_no_replace(args.output, report)
    except (lineage.LineageError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
