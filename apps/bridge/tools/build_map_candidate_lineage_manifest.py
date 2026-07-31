#!/usr/bin/env python3
"""Freeze a fail-closed Richmond map candidate-set lineage manifest.

This source-only tool inventories recovered authoring inputs and deployed
OpenDRIVE evidence.  It deliberately cannot select a candidate while the
222-road/29-junction versus 208-road/32-junction lineage is unresolved.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import unicodedata
import xml.etree.ElementTree as ET


SCHEMA = "v2x-map-candidate-lineage-manifest/v1"
MAX_INPUT_BYTES = 1_000_000_000
CHUNK_BYTES = 1024 * 1024
EXPECTED_OLD = (222, 29)
EXPECTED_LIVE = (208, 32)
EXPECTED_FBX_BYTES = 163_879_392
EXPECTED_FBX_SHA256 = "68e889cf8d2ab17cc2005c5e7364fd64608723b819df747c102d95a53757e3e0"
EXPECTED_OLD_XODR_SHA256 = "ed2e44492616901fbb20b89191ab03d666c0217620d0247e55235c116f5cf2b1"
EXPECTED_LIVE_XODR_SHA256 = "0737f3d9f9f344c06b2c63fe669afa8a15f814568ee9c16046795338f56f5ee1"
SCORE_PRECEDENCE = [
    "worst_camera_road_max_px",
    "worst_camera_road_rmse_px",
    "worst_camera_point_p95_px",
    "total_robust_loss",
]


class LineageError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
        value.st_size, value.st_mtime_ns, value.st_ctime_ns,
    )


def require_no_follow_support() -> None:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise LineageError("platform lacks required no-follow directory semantics")


def _directory_identity(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode


def _walk_directory_no_follow(path: Path) -> tuple[int, list[tuple[int, int, int]]]:
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    descriptor = os.open("/", directory_flags)
    try:
        identities = [_directory_identity(os.fstat(descriptor))]
        for component in path.parts[1:]:
            next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            try:
                next_identity = _directory_identity(os.fstat(next_descriptor))
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
            identities.append(next_identity)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, identities


def _validate_path_text(path: Path) -> None:
    text = str(path)
    if not path.is_absolute() or unicodedata.normalize("NFC", text) != text:
        raise LineageError("input paths must be absolute NFC paths")
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise LineageError("input path contains a forbidden component")
    for component in (path, *path.parents[:-1]):
        try:
            if stat.S_ISLNK(os.lstat(component).st_mode):
                raise LineageError("input path contains a symbolic-link component")
        except FileNotFoundError as exc:
            raise LineageError("input path component does not exist") from exc


def read_input(path_value: str, label: str) -> tuple[bytes, dict]:
    require_no_follow_support()
    path = Path(path_value)
    _validate_path_text(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    directory_descriptor, ancestor_identities = _walk_directory_no_follow(path.parent)
    try:
        descriptor = os.open(path.name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        os.close(directory_descriptor)
        raise LineageError(f"{label} cannot be opened without following links") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise LineageError(f"{label} must be a single-link regular file")
        if before.st_size <= 0 or before.st_size > MAX_INPUT_BYTES:
            raise LineageError(f"{label} has an invalid size")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining or os.read(descriptor, 1):
            raise LineageError(f"{label} changed size while being read")
        after = os.fstat(descriptor)
        try:
            at_path = os.stat(
                path.name, dir_fd=directory_descriptor, follow_symlinks=False
            )
        except OSError as exc:
            raise LineageError(f"{label} path changed while being read") from exc
        if _identity(before) != _identity(after) or _identity(before) != _identity(at_path):
            raise LineageError(f"{label} changed while being read")
        try:
            fresh_parent, fresh_ancestors = _walk_directory_no_follow(path.parent)
            try:
                fresh_final = os.stat(
                    path.name, dir_fd=fresh_parent, follow_symlinks=False
                )
            finally:
                os.close(fresh_parent)
        except OSError as exc:
            raise LineageError(f"{label} absolute path ancestry changed while being read") from exc
        if fresh_ancestors != ancestor_identities or _identity(fresh_final) != _identity(before):
            raise LineageError(f"{label} absolute path ancestry changed while being read")
    finally:
        os.close(descriptor)
        os.close(directory_descriptor)
    content = b"".join(chunks)
    return content, {
        "label": label,
        "path": str(path),
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def inventory_package_tree(package_root: Path) -> dict:
    """Return the complete regular-file inventory through held no-follow dirfds."""
    require_no_follow_support()
    root_descriptor, root_ancestors = _walk_directory_no_follow(package_root)
    try:
        root_identity = _identity(os.fstat(root_descriptor))
    except BaseException:
        os.close(root_descriptor)
        raise
    files: list[str] = []
    directories: list[str] = []
    file_records: list[dict] = []
    held_directories: list[tuple[int, tuple[int, ...]]] = [
        (root_descriptor, root_identity)
    ]
    held_files: list[tuple[int, tuple[int, ...]]] = []

    def recurse(descriptor: int, prefix: str) -> None:
        for name in sorted(os.listdir(descriptor)):
            if name in {".", ".."} or "/" in name or unicodedata.normalize("NFC", name) != name:
                raise LineageError("package contains a forbidden entry name")
            value = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISLNK(value.st_mode):
                raise LineageError("package inventory contains a symbolic link")
            if stat.S_ISDIR(value.st_mode):
                child = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                    | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                try:
                    child_identity = _identity(os.fstat(child))
                except BaseException:
                    os.close(child)
                    raise
                directories.append(relative)
                held_directories.append((child, child_identity))
                recurse(child, relative)
            elif stat.S_ISREG(value.st_mode) and value.st_nlink == 1:
                file_descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                transferred = False
                try:
                    opened = os.fstat(file_descriptor)
                    if _identity(opened) != _identity(value):
                        raise LineageError("package file changed while inventory opened it")
                    remaining = opened.st_size
                    digest = hashlib.sha256()
                    while remaining:
                        chunk = os.read(file_descriptor, min(CHUNK_BYTES, remaining))
                        if not chunk:
                            break
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if remaining or os.read(file_descriptor, 1):
                        raise LineageError("package file changed size during inventory")
                    after = os.fstat(file_descriptor)
                    if _identity(after) != _identity(opened):
                        raise LineageError("package file changed during inventory")
                    held_files.append((file_descriptor, _identity(opened)))
                    transferred = True
                finally:
                    if not transferred:
                        os.close(file_descriptor)
                files.append(relative)
                file_records.append({
                    "path": relative,
                    "bytes": opened.st_size,
                    "sha256": digest.hexdigest(),
                })
            else:
                raise LineageError("package inventory contains a non-regular or hard-linked file")

    try:
        recurse(root_descriptor, "")
        if any(_identity(os.fstat(fd)) != identity for fd, identity in held_files):
            raise LineageError("package file identity changed before inventory completed")
        if any(_identity(os.fstat(fd)) != identity for fd, identity in held_directories):
            raise LineageError("package directory identity changed before inventory completed")
        fresh_root, fresh_ancestors = _walk_directory_no_follow(package_root)
        try:
            if fresh_ancestors != root_ancestors:
                raise LineageError("package root ancestry changed during inventory")
            if _identity(os.fstat(fresh_root)) != held_directories[0][1]:
                raise LineageError("package root identity changed during inventory")
        finally:
            os.close(fresh_root)
        return {
            "files": sorted(files),
            "directories": sorted(directories),
            "file_records": sorted(file_records, key=lambda item: item["path"]),
        }
    finally:
        for descriptor, _identity_value in held_files:
            os.close(descriptor)
        for descriptor, _identity_value in reversed(held_directories):
            os.close(descriptor)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def summarize_xodr(content: bytes, label: str) -> dict:
    if b"<!doctype" in content.lower():
        raise LineageError(f"{label} must not contain a DTD")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise LineageError(f"{label} is not valid XML") from exc
    if _local_name(root.tag) != "OpenDRIVE":
        raise LineageError(f"{label} is not OpenDRIVE")
    roads = root.findall("./road")
    junctions = root.findall("./junction")
    road_ids = [item.get("id") for item in roads]
    junction_ids = [item.get("id") for item in junctions]
    if any(value is None or value == "" for value in road_ids + junction_ids):
        raise LineageError(f"{label} contains blank road or junction IDs")
    if len(set(road_ids)) != len(road_ids) or len(set(junction_ids)) != len(junction_ids):
        raise LineageError(f"{label} contains duplicate road or junction IDs")
    objects = root.findall("./road/objects/object")
    road_marks = root.findall("./road/lanes/laneSection/*/lane/roadMark")
    projection = root.findtext("./header/geoReference")
    projection = projection.strip() if projection else None
    lane_records = []
    lane_link_records = []
    road_mark_records = []
    object_records = []
    for road in roads:
        road_id = road.get("id")
        for section in road.findall("./lanes/laneSection"):
            section_s = section.get("s")
            if section_s is None or section_s == "":
                raise LineageError(f"{label} contains a laneSection with blank s")
            for side in ("left", "center", "right"):
                seen_lane_ids = set()
                for lane in section.findall(f"./{side}/lane"):
                    lane_id = lane.get("id")
                    if lane_id is None or lane_id == "" or lane_id in seen_lane_ids:
                        raise LineageError(f"{label} contains a blank or duplicate lane ID")
                    seen_lane_ids.add(lane_id)
                    lane_records.append((
                        road_id, section_s, side, lane_id,
                        lane.get("type") or "", lane.get("level") or "",
                    ))
                    for link_kind in ("predecessor", "successor"):
                        for link in lane.findall(f"./link/{link_kind}"):
                            linked_id = link.get("id")
                            if linked_id is None or linked_id == "":
                                raise LineageError(f"{label} contains a lane link with blank ID")
                            lane_link_records.append((
                                road_id, section_s, side, lane_id, link_kind, linked_id,
                            ))
                    for mark in lane.findall("roadMark"):
                        children = tuple(
                            sorted(
                                (_local_name(child.tag), tuple(sorted(child.attrib.items())))
                                for child in mark.iter() if child is not mark
                            )
                        )
                        road_mark_records.append((
                            road_id, section_s, side, lane_id,
                            tuple(sorted(mark.attrib.items())), children,
                        ))
        seen_object_ids = set()
        for item in road.findall("./objects/object"):
            object_id = item.get("id")
            if not object_id or object_id in seen_object_ids:
                raise LineageError(f"{label} has blank or duplicate object ID within road {road_id}")
            seen_object_ids.add(object_id)
            object_records.append((road_id, object_id, tuple(sorted(item.attrib.items()))))
    junction_connections = []
    junction_lane_links = []
    for junction in junctions:
        seen_connection_ids = set()
        for connection in junction.findall("./connection"):
            connection_id = connection.get("id")
            if not connection_id or connection_id in seen_connection_ids:
                raise LineageError(f"{label} has blank or duplicate junction connection ID")
            seen_connection_ids.add(connection_id)
            junction_connections.append((
                junction.get("id"), connection_id, connection.get("incomingRoad") or "",
                connection.get("connectingRoad") or "", connection.get("contactPoint") or "",
            ))
            for link in connection.findall("laneLink"):
                from_id = link.get("from")
                to_id = link.get("to")
                if from_id is None or from_id == "" or to_id is None or to_id == "":
                    raise LineageError(
                        f"{label} contains a junction laneLink with blank from/to"
                    )
                junction_lane_links.append(
                    (junction.get("id"), connection_id, from_id, to_id)
                )
    road_link_records = []
    for road in roads:
        for child in road.findall("./link/*"):
            element_id = child.get("elementId")
            if element_id is None or element_id == "":
                raise LineageError(f"{label} contains a road link with blank elementId")
            road_link_records.append((
                road.get("id"), _local_name(child.tag), child.get("elementType") or "",
                element_id, child.get("contactPoint") or "",
            ))
    canonical_topology = {
        "road_ids": sorted(road_ids),
        "road_junction_membership": sorted(
            (road.get("id"), road.get("junction") or "") for road in roads
        ),
        "junction_ids": sorted(junction_ids),
        "road_links": sorted(road_link_records),
        "junction_connections": sorted(junction_connections),
        "junction_lane_links": sorted(junction_lane_links),
        "lanes": sorted(lane_records),
        "lane_predecessor_successor_links": sorted(lane_link_records),
        "road_marks": sorted(road_mark_records),
        "objects": sorted(object_records),
    }
    topology_bytes = json.dumps(
        canonical_topology, sort_keys=True, separators=(",", ":")
    ).encode()
    mark_types = Counter((mark.get("type") or "").strip() for mark in road_marks)
    object_types = Counter((item.get("type") or "").strip() for item in objects)
    return {
        "projection": projection,
        "projection_present": projection is not None,
        "road_count": len(roads),
        "road_ids_sha256": hashlib.sha256(
            json.dumps(sorted(road_ids), separators=(",", ":")).encode()
        ).hexdigest(),
        "junction_count": len(junctions),
        "junction_ids_sha256": hashlib.sha256(
            json.dumps(sorted(junction_ids), separators=(",", ":")).encode()
        ).hexdigest(),
        "object_count": len(objects),
        "object_type_counts": dict(sorted(object_types.items())),
        "road_mark_count": len(road_marks),
        "road_mark_type_counts": dict(sorted(mark_types.items())),
        "road_mark_segmented_lane_count": sum(
            len(lane.findall("roadMark")) > 1
            for lane in root.findall("./road/lanes/laneSection/*/lane")
        ),
        "topology_sha256": hashlib.sha256(topology_bytes).hexdigest(),
    }


def summarize_geojson(content: bytes, label: str) -> dict:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LineageError(f"{label} is not valid GeoJSON JSON") from exc
    if not isinstance(value, dict) or value.get("type") not in {
        "FeatureCollection", "Feature", "GeometryCollection",
    }:
        raise LineageError(f"{label} lacks a supported GeoJSON root")
    features = value.get("features", []) if value.get("type") == "FeatureCollection" else [value]
    if not isinstance(features, list):
        raise LineageError(f"{label} has invalid features")
    geometries = Counter()
    for feature in features:
        if not isinstance(feature, dict):
            raise LineageError(f"{label} contains an invalid feature")
        geometry = feature.get("geometry") or {}
        geometries[str(geometry.get("type") or "null")] += 1
    return {
        "root_type": value["type"], "feature_count": len(features),
        "geometry_type_counts": dict(sorted(geometries.items())),
        "crs": value.get("crs"),
    }


def summarize_xml(content: bytes, label: str) -> dict:
    if b"<!doctype" in content.lower():
        raise LineageError(f"{label} must not contain a DTD")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise LineageError(f"{label} is not valid XML") from exc
    counts = Counter(_local_name(item.tag) for item in root.iter())
    return {
        "root_tag": _local_name(root.tag), "element_count": sum(counts.values()),
        "element_type_counts": dict(sorted(counts.items())),
    }


def summarize_fbx(content: bytes) -> dict:
    binary_prefix = b"Kaydara FBX Binary  "
    if content.startswith(binary_prefix) and len(content) >= 27:
        return {
            "format": "binary", "version": int.from_bytes(content[23:27], "little")
        }
    try:
        prefix = content[:4096].decode("ascii")
    except UnicodeDecodeError as exc:
        raise LineageError("fbx has an unrecognized header") from exc
    if "FBX" not in prefix:
        raise LineageError("fbx has an unrecognized header")
    return {"format": "ascii", "version": None}


def artifact(path: str, label: str, kind: str) -> dict:
    content, result = read_input(path, label)
    if kind == "xodr":
        result["summary"] = summarize_xodr(content, label)
    elif kind == "geojson":
        result["summary"] = summarize_geojson(content, label)
    elif kind == "xml":
        result["summary"] = summarize_xml(content, label)
    elif kind == "fbx":
        result["summary"] = summarize_fbx(content)
    else:
        result["summary"] = {"extension": Path(path).suffix.lower()}
    result["kind"] = kind
    return result


def candidate_id(name: str, artifacts: list[dict]) -> str:
    identity = {
        "name": name,
        "artifacts": [
            {"label": item["label"], "kind": item["kind"],
             "bytes": item["bytes"], "sha256": item["sha256"]}
            for item in sorted(artifacts, key=lambda item: item["label"])
        ],
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{name}-sha256-{digest}"


def _build_snapshot(args: argparse.Namespace) -> dict:
    package_root = Path(args.package_root)
    _validate_path_text(package_root)

    def package_label(prefix: str, value: str) -> str:
        path = Path(value)
        _validate_path_text(path)
        try:
            relative = path.relative_to(package_root)
        except ValueError as exc:
            raise LineageError("recovered artifact is outside package root") from exc
        return f"{prefix}:{relative.as_posix()}"

    for prefix, value in (
        ("fbx", args.fbx), ("old_xodr", args.old_xodr), ("geojson", args.geojson)
    ):
        package_label(prefix, value)

    recovered = [
        artifact(args.fbx, "recovered_fbx", "fbx"),
        artifact(args.old_xodr, "recovered_old_xodr", "xodr"),
        artifact(args.geojson, "recovered_geojson", "geojson"),
    ]
    recovered.extend(
        artifact(path, package_label("rrdata_xml", path), "xml")
        for path in sorted(args.rrdata_xml)
    )
    recovered.extend(
        artifact(path, package_label("material_file", path), "file")
        for path in sorted(args.material_file)
    )
    live = [artifact(args.live_xodr, "live_deployed_xodr", "xodr")]
    labels = [item["label"] for item in recovered + live]
    paths = [item["path"] for item in recovered + live]
    if len(set(labels)) != len(labels) or len(set(paths)) != len(paths):
        raise LineageError("artifacts must have unique labels and paths")
    package_inventory = inventory_package_tree(package_root)
    supplied_package_paths = sorted(
        str(Path(item["path"]).relative_to(package_root).as_posix()) for item in recovered
    )
    if package_inventory["files"] != supplied_package_paths:
        missing = sorted(set(package_inventory["files"]) - set(supplied_package_paths))
        extra = sorted(set(supplied_package_paths) - set(package_inventory["files"]))
        raise LineageError(
            f"recovered package inventory is incomplete or inconsistent; missing={missing} extra={extra}"
        )
    artifact_by_relative_path = {
        str(Path(item["path"]).relative_to(package_root).as_posix()): item
        for item in recovered
    }
    for record in package_inventory["file_records"]:
        item = artifact_by_relative_path[record["path"]]
        if item["bytes"] != record["bytes"] or item["sha256"] != record["sha256"]:
            raise LineageError(
                "recovered artifact hash differs from terminal package inventory snapshot"
            )
    inventory_contract = {
        "file_records": package_inventory["file_records"],
        "directories": package_inventory["directories"],
    }
    inventory_sha256 = hashlib.sha256(
        json.dumps(inventory_contract, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    old_summary = recovered[1]["summary"]
    live_summary = live[0]["summary"]
    fbx_identity = recovered[0]
    if (
        fbx_identity["bytes"] != EXPECTED_FBX_BYTES
        or fbx_identity["sha256"] != EXPECTED_FBX_SHA256
    ):
        raise LineageError("recovered FBX does not match the pinned Richmond identity")
    if (old_summary["road_count"], old_summary["junction_count"]) != EXPECTED_OLD:
        raise LineageError("recovered OpenDRIVE is not the expected 222/29 topology")
    if (live_summary["road_count"], live_summary["junction_count"]) != EXPECTED_LIVE:
        raise LineageError("live OpenDRIVE evidence is not the expected 208/32 topology")
    if not old_summary["projection_present"] or not live_summary["projection_present"]:
        raise LineageError("both OpenDRIVE inputs require explicit geoReference projection")
    if recovered[1]["sha256"] != EXPECTED_OLD_XODR_SHA256:
        raise LineageError("recovered OpenDRIVE does not match the pinned old identity")
    if live[0]["sha256"] != EXPECTED_LIVE_XODR_SHA256:
        raise LineageError("live OpenDRIVE evidence does not match the pinned deployed identity")
    candidates = []
    for name, values in (
        ("recovered_authoring_package", recovered),
        ("live_deployed_opendrive", live),
    ):
        candidates.append({
            "candidate_id": candidate_id(name, values), "candidate_name": name,
            "artifacts": sorted(values, key=lambda item: item["label"]),
            "score": None, "topology_contradiction": "unreviewed",
            "selection_eligible": False,
        })
    return {
        "schema": SCHEMA,
        "created_at_utc": utc_now(),
        "acceptance_eligible": False,
        "manifest_mutability": "exclusive_no_replace",
        "candidates": sorted(candidates, key=lambda item: item["candidate_id"]),
        "selection_policy": {
            "topology_contradiction_precedence": "reject_before_scoring",
            "lexicographic_score_precedence": SCORE_PRECEDENCE,
            "metric_direction": "ascending",
            "tie_rule": "fail_competing_map_basin_when_first_differing_metric_within_2_percent",
            "class_conflict_rule": "fail_when_different_required_classes_prefer_different_candidates",
            "policy_mutable_after_manifest": False,
        },
        "lineage_reconciliation": {
            "status": "unresolved_blocking",
            "recovered_topology": {"roads": 222, "junctions": 29},
            "live_topology": {"roads": 208, "junctions": 32},
            "same_projection_text": old_summary["projection"] == live_summary["projection"],
            "same_topology_sha256": old_summary["topology_sha256"] == live_summary["topology_sha256"],
            "required_resolution": "reviewed provenance must explain 222/29 versus 208/32 and select a versioned target fingerprint",
        },
        "recovered_material_dependency_graph": {
            "status": "complete_package_inventory_frozen_dependency_edges_unreviewed",
            "package_inventory_sha256": inventory_sha256,
            "package_file_count": len(package_inventory["files"]),
            "package_directory_count": len(package_inventory["directories"]),
            "complete_package_paths": package_inventory["files"],
            "rrdata_xml_labels": sorted(
                item["label"] for item in recovered if item["label"].startswith("rrdata_xml:")
            ),
            "material_file_labels": sorted(
                item["label"] for item in recovered if item["label"].startswith("material_file:")
            ),
            "edge_semantics": "RoadRunner metadata and FBX references to supplied material files require separate completeness validation",
            "selection_blocking_until_complete": True,
        },
        "selection": {
            "status": "blocked_unresolved_opendrive_lineage",
            "selected_candidate_id": None,
            "scoring_permitted": False,
        },
        "limitations": [
            "source_inventory_only_no_ue5_import_cook_or_runtime",
            "no_holdout_was_read_or_consumed",
            "candidate_ids_bind_bytes_not_geometry_correctness_or_survey_truth",
            "tier_a_absolute_world_truth_remains_unavailable",
        ],
    }


def _snapshot_comparison_bytes(report: dict) -> bytes:
    comparable = dict(report)
    comparable.pop("created_at_utc", None)
    return json.dumps(comparable, sort_keys=True, separators=(",", ":")).encode()


def build(args: argparse.Namespace) -> dict:
    """Require two identical complete reads and return the terminal snapshot.

    A complete pass reopens and hashes every supplied artifact and recursively
    inventories the package through no-follow dirfds. Comparing two complete
    passes catches replacement of an already-read nested directory or file
    during a later inventory step; candidate IDs and the returned report bind
    only the second, coherent terminal snapshot.
    """
    first = _build_snapshot(args)
    second = _build_snapshot(args)
    if _snapshot_comparison_bytes(first) != _snapshot_comparison_bytes(second):
        raise LineageError(
            "recovered package snapshot changed between complete no-follow passes"
        )
    return second


def publish_no_replace(path_value: str, report: dict) -> None:
    require_no_follow_support()
    destination = Path(path_value)
    if (
        not destination.is_absolute()
        or unicodedata.normalize("NFC", str(destination)) != str(destination)
        or any(part in {"", ".", ".."} for part in destination.parts[1:])
    ):
        raise LineageError("output path must be absolute")
    parent = destination.parent
    encoded = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_descriptor = os.open("/", directory_flags)
    temporary_name = f".{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    temporary_descriptor = None
    published = False
    success = False
    try:
        for component in parent.parts[1:]:
            next_descriptor = os.open(component, directory_flags, dir_fd=parent_descriptor)
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor
        parent_before = os.fstat(parent_descriptor)
        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
            0o640,
            dir_fd=parent_descriptor,
        )
        view = memoryview(encoded)
        while view:
            written = os.write(temporary_descriptor, view)
            if written <= 0:
                raise LineageError("manifest write made no progress")
            view = view[written:]
        os.fsync(temporary_descriptor)
        staged = os.fstat(temporary_descriptor)
        if not stat.S_ISREG(staged.st_mode) or staged.st_nlink != 1 or staged.st_size != len(encoded):
            raise LineageError("staged manifest identity is invalid")
        os.link(
            temporary_name, destination.name,
            src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        published = True
        linked = os.stat(destination.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if linked.st_dev != staged.st_dev or linked.st_ino != staged.st_ino or linked.st_size != len(encoded):
            raise LineageError("published manifest identity differs from staged bytes")
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        temporary_name = ""
        os.fsync(parent_descriptor)
        final = os.stat(destination.name, dir_fd=parent_descriptor, follow_symlinks=False)
        parent_after = os.stat(parent, follow_symlinks=False)
        if (
            final.st_dev != staged.st_dev or final.st_ino != staged.st_ino
            or final.st_nlink != 1 or final.st_size != len(encoded)
            or (parent_before.st_dev, parent_before.st_ino, parent_before.st_mode)
            != (parent_after.st_dev, parent_after.st_ino, parent_after.st_mode)
        ):
            raise LineageError("published manifest or parent changed during publication")
        success = True
    except FileExistsError as exc:
        raise LineageError("refusing to replace existing manifest") from exc
    finally:
        try:
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                except FileNotFoundError:
                    pass
        finally:
            try:
                if published and not success:
                    try:
                        final = os.stat(
                            destination.name, dir_fd=parent_descriptor,
                            follow_symlinks=False,
                        )
                        if final.st_dev == staged.st_dev and final.st_ino == staged.st_ino:
                            os.unlink(destination.name, dir_fd=parent_descriptor)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(parent_descriptor)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", required=True)
    parser.add_argument("--fbx", required=True)
    parser.add_argument("--old-xodr", required=True)
    parser.add_argument("--live-xodr", required=True)
    parser.add_argument("--geojson", required=True)
    parser.add_argument("--rrdata-xml", action="append", required=True)
    parser.add_argument("--material-file", action="append", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        report = build(args)
        publish_no_replace(args.output, report)
    except (LineageError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
