#!/usr/bin/env python3
"""Render synchronized UE5 RGB, semantic, instance, and depth calibration buffers."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digital_twin_bridge.twin_camera_rig import (  # noqa: E402
    camera_with_twin_pose,
    compute_twin_camera_transform,
    configure_twin_camera_blueprint,
    load_cameras_config,
    twin_horizontal_fov_deg,
)


EXPECTED_IMAGE_ID = (
    "sha256:8e7c7152f86a9e26878de5f280514f224290f70aad7b28d00d5087709504118e"
)
EXPECTED_IMAGE = "ghcr.io/simforgeinc/carla-rr-maps:0.10.0"
EXPECTED_MAP_NAME = "Carla/Maps/Richmond_Field_Station_Richmond_CA"
EXPECTED_OPENDRIVE_SHA256 = (
    "0737f3d9f9f344c06b2c63fe669afa8a15f814568ee9c16046795338f56f5ee1"
)
EXPECTED_CONTAINER = "v2x-calibration-ue5"
EXPECTED_HOST = "127.0.0.1"
EXPECTED_PORT = 2300
REQUEST_SCHEMA = "v2x-semantic-calibration-candidate/v1"
OUTPUT_SCHEMA = "v2x-semantic-calibration-render/v1"
ALLOWED_TWIN_POSE_KEYS = {
    "forward_offset_m",
    "right_offset_m",
    "height_offset_m",
    "pitch_offset_deg",
    "yaw_offset_deg",
    "roll_offset_deg",
    "fov_offset_deg",
}
SENSOR_BLUEPRINTS = {
    "rgb": "sensor.camera.rgb",
    "semantic": "sensor.camera.semantic_segmentation",
    "instance": "sensor.camera.instance_segmentation",
    "depth": "sensor.camera.depth",
}


class RenderError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def validate_endpoint(host, port, container):
    if (host, int(port), container) != (
        EXPECTED_HOST,
        EXPECTED_PORT,
        EXPECTED_CONTAINER,
    ):
        raise RenderError(
            "semantic calibration renders require the isolated UE5 worker on "
            "127.0.0.1:2300"
        )


def validate_worker_inspect(document):
    if isinstance(document, list):
        if len(document) != 1:
            raise RenderError("calibration worker inspect cardinality is invalid")
        document = document[0]
    config = document.get("Config") or {}
    host_config = document.get("HostConfig") or {}
    state = document.get("State") or {}
    labels = config.get("Labels") or {}
    if document.get("Name") != f"/{EXPECTED_CONTAINER}":
        raise RenderError("calibration worker name is invalid")
    if (
        document.get("Image") != EXPECTED_IMAGE_ID
        or config.get("Image") != EXPECTED_IMAGE
    ):
        raise RenderError("calibration worker image fingerprint is invalid")
    if labels.get("com.path2v2x.scope") != "calibration":
        raise RenderError("calibration worker scope label is missing")
    if not state.get("Running"):
        raise RenderError("calibration worker is not running")
    if host_config.get("Runtime") != "nvidia":
        raise RenderError("calibration worker is not using the NVIDIA runtime")
    if (host_config.get("RestartPolicy") or {}).get("Name") != "no":
        raise RenderError("calibration worker restart policy is unsafe")
    if host_config.get("NetworkMode") != "bridge":
        raise RenderError("calibration worker network mode is invalid")
    mounts = document.get("Mounts") or []
    if mounts:
        raise RenderError("calibration worker has unexpected filesystem mounts")
    bindings = host_config.get("PortBindings") or {}
    for port in range(EXPECTED_PORT, EXPECTED_PORT + 3):
        values = bindings.get(f"{port}/tcp") or []
        if not any(
            value.get("HostIp") == EXPECTED_HOST
            and value.get("HostPort") == str(port)
            for value in values
        ):
            raise RenderError("calibration worker ports are not loopback-bound")
    command = " ".join(str(value) for value in config.get("Cmd") or [])
    if "-carla-rpc-port=2300" not in command or "-RenderOffScreen" not in command:
        raise RenderError("calibration worker command is invalid")
    if "UE6" in command or "2100" in command:
        raise RenderError("calibration worker command references another runtime")
    return {
        "container_id": str(document.get("Id") or ""),
        "container_started_at": str(state.get("StartedAt") or ""),
        "image_id": document["Image"],
    }


def inspect_worker(container):
    result = subprocess.run(
        ["docker", "inspect", container],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return validate_worker_inspect(json.loads(result.stdout))


def validate_candidate(document, cameras_sha256):
    if (
        document.get("schema") != REQUEST_SCHEMA
        or document.get("acceptance_eligible") is not False
    ):
        raise RenderError("candidate does not have the diagnostic contract")
    camera_id = document.get("camera_id")
    if camera_id not in {"ch1", "ch2", "ch3", "ch4"}:
        raise RenderError("candidate camera ID is invalid")
    candidate_id = document.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise RenderError("candidate ID is missing")
    if document.get("cameras_json_sha256") != cameras_sha256:
        raise RenderError("candidate camera config hash is stale")
    twin_pose = document.get("twin_pose")
    if not isinstance(twin_pose, dict) or set(twin_pose) - ALLOWED_TWIN_POSE_KEYS:
        raise RenderError("candidate twin pose has unsupported fields")
    normalized = {}
    for key, value in twin_pose.items():
        value = float(value)
        if not math.isfinite(value):
            raise RenderError("candidate twin pose contains a non-finite value")
        normalized[key] = value
    return camera_id, candidate_id, normalized


def bgra_array(image):
    value = np.frombuffer(image.raw_data, dtype=np.uint8)
    expected = int(image.width) * int(image.height) * 4
    if value.size != expected:
        raise RenderError("CARLA image byte count is invalid")
    return value.reshape((int(image.height), int(image.width), 4)).copy()


def decode_buffers(frames):
    raw = {name: bgra_array(frame) for name, frame in frames.items()}
    rgb = raw["rgb"][:, :, :3][:, :, ::-1]
    semantic_tags = raw["semantic"][:, :, 2]
    instance_semantic_tags = raw["instance"][:, :, 2]
    instance_ids = (
        raw["instance"][:, :, 1].astype(np.uint16) * 256
        + raw["instance"][:, :, 0].astype(np.uint16)
    )
    depth_bgra = raw["depth"].astype(np.float64)
    normalized_depth = (
        depth_bgra[:, :, 2]
        + depth_bgra[:, :, 1] * 256.0
        + depth_bgra[:, :, 0] * 65536.0
    ) / 16777215.0
    depth_m = (normalized_depth * 1000.0).astype(np.float32)
    return raw, rgb, semantic_tags, instance_semantic_tags, instance_ids, depth_m


def buffer_statistics(decoded):
    _raw, _rgb, semantic, instance_semantic, instance_ids, depth_m = decoded
    semantic_values, semantic_counts = np.unique(semantic, return_counts=True)
    instance_values = np.unique(instance_ids)
    finite_depth = depth_m[np.isfinite(depth_m)]
    pixel_count = int(semantic.size)
    dominant_fraction = float(semantic_counts.max() / pixel_count)
    return {
        "semantic_tags": {
            "histogram": {
                str(int(value)): int(count)
                for value, count in zip(semantic_values, semantic_counts)
            },
            "dominant_fraction": dominant_fraction,
            "usable_for_class_alignment": bool(
                len(semantic_values) >= 2 and dominant_fraction < 0.999
            ),
        },
        "instance": {
            "semantic_matches": bool(np.array_equal(semantic, instance_semantic)),
            "unique_id_count": int(len(instance_values)),
            "sentinel_fraction": float(np.mean(instance_ids == 65535)),
            "usable_for_static_instances": bool(
                len(instance_values) >= 2 and np.mean(instance_ids == 65535) < 0.999
            ),
        },
        "depth_meters": {
            "finite_fraction": float(finite_depth.size / depth_m.size),
            "minimum": float(np.min(finite_depth)),
            "median": float(np.median(finite_depth)),
            "p99": float(np.quantile(finite_depth, 0.99)),
            "maximum": float(np.max(finite_depth)),
        },
    }


def decode_rgb_depth_buffers(frames):
    if set(frames) != {"rgb", "depth"}:
        raise RenderError("RGB+depth mode received an unexpected sensor set")
    raw = {name: bgra_array(frame) for name, frame in frames.items()}
    rgb = raw["rgb"][:, :, :3][:, :, ::-1]
    depth_bgra = raw["depth"].astype(np.float64)
    normalized_depth = (
        depth_bgra[:, :, 2]
        + depth_bgra[:, :, 1] * 256.0
        + depth_bgra[:, :, 0] * 65536.0
    ) / 16777215.0
    depth_m = (normalized_depth * 1000.0).astype(np.float32)
    return raw, rgb, depth_m


def rgb_depth_statistics(decoded):
    _raw, _rgb, depth_m = decoded
    finite_depth = depth_m[np.isfinite(depth_m)]
    if finite_depth.size == 0:
        raise RenderError("RGB+depth render has no finite depth values")
    return {
        "semantic_tags": {"captured": False, "usable_for_class_alignment": False},
        "instance": {"captured": False, "usable_for_static_instances": False},
        "depth_meters": {
            "finite_fraction": float(finite_depth.size / depth_m.size),
            "minimum": float(np.min(finite_depth)),
            "median": float(np.median(finite_depth)),
            "p99": float(np.quantile(finite_depth, 0.99)),
            "maximum": float(np.max(finite_depth)),
        },
    }


def validate_degenerate_segmentation_evidence(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    report = json.loads(raw)
    statistics = report.get("buffer_statistics") or {}
    semantic = statistics.get("semantic_tags") or {}
    instance = statistics.get("instance") or {}
    if (
        report.get("schema") != OUTPUT_SCHEMA
        or report.get("acceptance_eligible") is not False
        or report.get("map_name") != EXPECTED_MAP_NAME
        or report.get("opendrive_sha256") != EXPECTED_OPENDRIVE_SHA256
        or (report.get("worker") or {}).get("image_id") != EXPECTED_IMAGE_ID
        or semantic.get("usable_for_class_alignment") is not False
        or instance.get("usable_for_static_instances") is not False
        or semantic.get("dominant_fraction", 0.0) < 0.999
        or instance.get("sentinel_fraction", 0.0) < 0.999
    ):
        raise RenderError("segmentation-degeneracy evidence is invalid")
    return {"path": str(path), "sha256": sha256_bytes(raw)}


def wait_for_frame(sensor_queue, target_frame, timeout_seconds):
    while True:
        try:
            frame = sensor_queue.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise RenderError("timed out waiting for synchronized sensor data") from exc
        if int(frame.frame) >= int(target_frame):
            return frame


def capture_synchronized(world, sensors, timeout_seconds, maximum_ticks=20):
    queues = {name: queue.Queue(maxsize=4) for name in sensors}
    for name, sensor in sensors.items():
        sensor.listen(queues[name].put)
    for _ in range(maximum_ticks):
        target = int(world.tick())
        frames = {
            name: wait_for_frame(sensor_queue, target, timeout_seconds)
            for name, sensor_queue in queues.items()
        }
        frame_ids = {int(frame.frame) for frame in frames.values()}
        if frame_ids == {target}:
            return target, frames
    raise RenderError("sensors did not converge on one CARLA frame")


def configure_blueprint(blueprint, camera, width, height, role_name):
    configure_twin_camera_blueprint(blueprint, camera, width, height)
    if blueprint.has_attribute("role_name"):
        blueprint.set_attribute("role_name", role_name)


def output_file_metadata(directory, names):
    return {
        name: {
            "path": name,
            "sha256": sha256_file(directory / name),
            "bytes": int((directory / name).stat().st_size),
        }
        for name in names
    }


def write_outputs(directory, frames, decoded):
    raw, rgb, semantic, instance_semantic, instance_ids, depth_m = decoded
    Image.fromarray(rgb).save(directory / "rgb.png")
    Image.fromarray(semantic).save(directory / "semantic-tags.png")
    Image.fromarray(instance_semantic).save(
        directory / "instance-semantic-tags.png"
    )
    np.save(directory / "instance-ids.npy", instance_ids, allow_pickle=False)
    np.save(directory / "depth-meters.npy", depth_m, allow_pickle=False)
    for name, value in raw.items():
        (directory / f"{name}.bgra").write_bytes(value.tobytes(order="C"))
    names = [
        "rgb.png",
        "semantic-tags.png",
        "instance-semantic-tags.png",
        "instance-ids.npy",
        "depth-meters.npy",
        *[f"{name}.bgra" for name in sorted(frames)],
    ]
    return output_file_metadata(directory, names)


def write_rgb_depth_outputs(directory, frames, decoded):
    raw, rgb, depth_m = decoded
    Image.fromarray(rgb).save(directory / "rgb.png")
    np.save(directory / "depth-meters.npy", depth_m, allow_pickle=False)
    for name, value in raw.items():
        (directory / f"{name}.bgra").write_bytes(value.tobytes(order="C"))
    names = [
        "rgb.png",
        "depth-meters.npy",
        *[f"{name}.bgra" for name in sorted(frames)],
    ]
    return output_file_metadata(directory, names)


def render(args):
    validate_endpoint(args.host, args.port, args.container)
    if not args.authorized_isolated_worker:
        raise RenderError("--authorized-isolated-worker is required")
    degeneration_evidence = None
    sensor_blueprints = SENSOR_BLUEPRINTS
    if args.rgb_depth_only:
        if not args.known_degenerate_segmentation_render:
            raise RenderError(
                "RGB+depth mode requires --known-degenerate-segmentation-render"
            )
        degeneration_evidence = validate_degenerate_segmentation_evidence(
            args.known_degenerate_segmentation_render
        )
        sensor_blueprints = {
            key: SENSOR_BLUEPRINTS[key] for key in ("rgb", "depth")
        }
    worker = inspect_worker(args.container)
    cameras_path = Path(args.cameras_json).resolve()
    cameras_bytes = cameras_path.read_bytes()
    cameras_sha256 = sha256_bytes(cameras_bytes)
    candidate_path = Path(args.candidate).resolve()
    candidate_bytes = candidate_path.read_bytes()
    candidate = json.loads(candidate_bytes)
    camera_id, candidate_id, twin_pose = validate_candidate(
        candidate, cameras_sha256
    )
    config = load_cameras_config(str(cameras_path))
    if config is None:
        raise RenderError("camera config could not be loaded")
    source_camera = next(
        (item for item in config["cameras"] if item["id"] == camera_id), None
    )
    if source_camera is None:
        raise RenderError("candidate camera is absent from camera config")
    camera = camera_with_twin_pose(source_camera, twin_pose)
    output = Path(args.output_dir).resolve()
    if output.exists():
        raise RenderError("refusing to overwrite an existing render directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent)
    )

    import carla

    client = carla.Client(args.host, args.port)
    client.set_timeout(args.timeout_seconds)
    world = client.get_world()
    carla_map = world.get_map()
    opendrive_sha256 = sha256_bytes(carla_map.to_opendrive().encode("utf-8"))
    if (
        carla_map.name != EXPECTED_MAP_NAME
        or opendrive_sha256 != EXPECTED_OPENDRIVE_SHA256
    ):
        raise RenderError("isolated UE5 map fingerprint is invalid")
    existing_cameras = list(world.get_actors().filter("sensor.camera.*"))
    if existing_cameras:
        raise RenderError("isolated worker contains pre-existing camera sensors")

    original_settings = world.get_settings()
    synchronous_settings = world.get_settings()
    synchronous_settings.synchronous_mode = True
    synchronous_settings.fixed_delta_seconds = 0.05
    synchronous_settings.no_rendering_mode = False
    sensors = {}
    destroyed = {}
    role_name = f"v2x_calibration:{candidate_id}"
    try:
        world.apply_settings(synchronous_settings)
        transform = compute_twin_camera_transform(carla_map, config["site"], camera)
        library = world.get_blueprint_library()
        for name, blueprint_id in sensor_blueprints.items():
            blueprint = library.find(blueprint_id)
            configure_blueprint(
                blueprint, camera, args.width, args.height, role_name
            )
            actor = world.spawn_actor(blueprint, transform)
            sensors[name] = actor
        frame_id, frames = capture_synchronized(
            world, sensors, args.timeout_seconds
        )
        if args.rgb_depth_only:
            decoded = decode_rgb_depth_buffers(frames)
            files = write_rgb_depth_outputs(temporary, frames, decoded)
            statistics = rgb_depth_statistics(decoded)
        else:
            decoded = decode_buffers(frames)
            files = write_outputs(temporary, frames, decoded)
            statistics = buffer_statistics(decoded)
        frame_timestamps = {
            name: float(frame.timestamp) for name, frame in frames.items()
        }
        frame_transforms = {
            name: {
                "location": [
                    float(frame.transform.location.x),
                    float(frame.transform.location.y),
                    float(frame.transform.location.z),
                ],
                "rotation": [
                    float(frame.transform.rotation.pitch),
                    float(frame.transform.rotation.yaw),
                    float(frame.transform.rotation.roll),
                ],
            }
            for name, frame in frames.items()
        }
    finally:
        for name, actor in sensors.items():
            try:
                actor.stop()
            except Exception:
                pass
            result = actor.destroy() if actor.is_alive else True
            destroyed[name] = bool(result is not False and not actor.is_alive)
        world.apply_settings(original_settings)
    if set(destroyed) != set(sensor_blueprints) or not all(destroyed.values()):
        shutil.rmtree(temporary, ignore_errors=True)
        raise RenderError("temporary calibration sensor cleanup failed")
    try:
        world.wait_for_tick(seconds=5.0)
    except RuntimeError:
        pass
    if list(client.get_world().get_actors().filter("sensor.camera.*")):
        shutil.rmtree(temporary, ignore_errors=True)
        raise RenderError("camera sensors remain after calibration render cleanup")

    metadata = {
        "schema": OUTPUT_SCHEMA,
        "acceptance_eligible": False,
        "created_at_utc": utc_now(),
        "camera_id": camera_id,
        "candidate_id": candidate_id,
        "candidate_sha256": sha256_bytes(candidate_bytes),
        "cameras_json_sha256": cameras_sha256,
        "worker": worker,
        "map_name": carla_map.name,
        "opendrive_sha256": opendrive_sha256,
        "resolution": [args.width, args.height],
        "carla_frame": frame_id,
        "sensor_timestamps": frame_timestamps,
        "sensor_transforms": frame_transforms,
        "fov_deg": twin_horizontal_fov_deg(camera),
        "twin_pose": twin_pose,
        "files": files,
        "buffer_statistics": statistics,
        "modalities": sorted(sensor_blueprints),
        "known_degenerate_segmentation_evidence": degeneration_evidence,
        "temporary_sensors_destroyed": destroyed,
        "buffer_encodings": {
            "raw": "CARLA BGRA uint8 row-major",
            "depth_meters": "float32 metric depth from CARLA 24-bit encoding",
            **(
                {}
                if args.rgb_depth_only
                else {
                    "semantic_tags": "CARLA semantic tag from raw R channel",
                    "instance_ids": "uint16 from raw G*256+B",
                }
            ),
        },
        "limitations": [
            "diagnostic_render_is_not_camera_calibration_acceptance",
            "physical_intrinsics_and_site_gauge_must_be_independently_constrained",
            *(
                ["segmentation_omitted_only_after_bound_degeneracy_evidence"]
                if args.rgb_depth_only
                else []
            ),
        ],
    }
    (temporary / "render.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    return output / "render.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--host", default=EXPECTED_HOST)
    parser.add_argument("--port", type=int, default=EXPECTED_PORT)
    parser.add_argument("--container", default=EXPECTED_CONTAINER)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--authorized-isolated-worker", action="store_true")
    parser.add_argument("--rgb-depth-only", action="store_true")
    parser.add_argument("--known-degenerate-segmentation-render")
    args = parser.parse_args(argv)
    if not 320 <= args.width <= 2560 or not 240 <= args.height <= 1920:
        parser.error("render dimensions are outside the bounded range")
    try:
        result = render(args)
    except (OSError, ValueError, RenderError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc)) from exc
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
