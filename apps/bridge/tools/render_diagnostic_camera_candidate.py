#!/usr/bin/env python3
"""Render one proposal-only calibration candidate with a temporary UE5 camera."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
from PIL import Image
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digital_twin_bridge.twin_camera_rig import (  # noqa: E402
    camera_with_twin_pose,
    compute_twin_camera_transform,
    configure_twin_camera_blueprint,
    load_cameras_config,
)


EXPECTED_MAP_NAME = "Carla/Maps/Richmond_Field_Station_Richmond_CA"
EXPECTED_OPENDRIVE_SHA256 = (
    "0737f3d9f9f344c06b2c63fe669afa8a15f814568ee9c16046795338f56f5ee1"
)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


async def query_server_status(url):
    async with websockets.connect(url, open_timeout=5.0, close_timeout=2.0) as socket:
        await socket.send(json.dumps({"type": "server_status"}))
        while True:
            message = await asyncio.wait_for(socket.recv(), timeout=5.0)
            if isinstance(message, str):
                payload = json.loads(message)
                if payload.get("type") == "server_status":
                    return payload


def verify_zero_active_sessions(url):
    status = asyncio.run(query_server_status(url))
    if status.get("active_sessions") != 0:
        raise RuntimeError(
            f"refusing candidate render with active_sessions={status.get('active_sessions')}"
        )


def wait_for_image(frames, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if frames:
            return frames[-1]
        time.sleep(0.05)
    raise RuntimeError("temporary candidate camera produced no frame")


def validate_v2x_target(host, port, drive_ws_url):
    if (host, int(port), drive_ws_url) != (
        "127.0.0.1", 2000, "ws://127.0.0.1:8765"
    ):
        raise ValueError(
            "diagnostic renders are locked to the local UE5 V2X worker and drive bridge"
        )


def validate_report_source_binding(report_path, report):
    expected = report.get("source_signal_search_sha256")
    if expected is None:
        return
    source = Path(report_path).resolve().parent / "signal-hypothesis-search.json"
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or not source.is_file()
        or hashlib.sha256(source.read_bytes()).hexdigest() != expected
    ):
        raise ValueError("candidate report does not bind its signal search evidence")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", required=True, choices=("ch1", "ch2", "ch3", "ch4"))
    parser.add_argument("--report", required=True)
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    # The live UE5 worker reliably accepts one extra 640x480 RGB sensor but
    # killed the same temporary actor at 1280x960 while the four production
    # sensors were active.  Keep the safe diagnostic default small.
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--drive-ws-url", default="ws://127.0.0.1:8765")
    parser.add_argument(
        "--authorized-zero-session",
        action="store_true",
        help="required acknowledgement for temporary CARLA sensor mutation",
    )
    parser.add_argument(
        "--allow-rejected-diagnostic",
        action="store_true",
        help="render a rejected candidate for visual debugging; never deployment evidence",
    )
    args = parser.parse_args()

    if not args.authorized_zero_session:
        parser.error("--authorized-zero-session is required")
    try:
        validate_v2x_target(args.host, args.port, args.drive_ws_url)
    except ValueError as exc:
        parser.error(str(exc))
    report_path = Path(args.report).resolve()
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)
    try:
        validate_report_source_binding(report_path, report)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if report.get("schema") not in {
        "v2x-diagnostic-visual-calibration/v1",
        "v2x-diagnostic-map-calibration/v1",
    } or report.get("acceptance_eligible") is not False:
        raise SystemExit("refusing a report without the diagnostic contract")
    result = report.get("cameras", {}).get(args.camera, {})
    if (
        result.get("candidate_recommendation") != "continue_offline_render_review"
        and not args.allow_rejected_diagnostic
    ):
        raise SystemExit("candidate is not eligible for offline render review")

    config_path = Path(args.cameras_json).resolve()
    config_bytes = config_path.read_bytes()
    if hashlib.sha256(config_bytes).hexdigest() != report.get("cameras_json_sha256"):
        raise SystemExit("cameras JSON does not match the fitted report")
    config = load_cameras_config(str(config_path))
    camera = next(item for item in config["cameras"] if item["id"] == args.camera)
    candidate = camera_with_twin_pose(camera, result["candidate_twin_pose"])
    output = Path(args.output).resolve()
    if output.exists() or output.with_suffix(output.suffix + ".json").exists():
        raise SystemExit("refusing to overwrite an existing candidate render")
    verify_zero_active_sessions(args.drive_ws_url)

    import carla

    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    carla_map = world.get_map()
    opendrive_sha256 = hashlib.sha256(
        carla_map.to_opendrive().encode("utf-8")
    ).hexdigest()
    if (
        carla_map.name != EXPECTED_MAP_NAME
        or opendrive_sha256 != EXPECTED_OPENDRIVE_SHA256
    ):
        raise RuntimeError("active UE5 Richmond map fingerprint is not approved")
    transform = compute_twin_camera_transform(carla_map, config["site"], candidate)
    blueprint = world.get_blueprint_library().find("sensor.camera.rgb")
    configure_twin_camera_blueprint(blueprint, candidate, args.width, args.height)
    frames, actor = [], None
    destroyed = False
    try:
        # Minimize the zero-session TOCTOU window. The drive bridge does not
        # yet expose a mutation lease, so check again immediately before the
        # CARLA mutation and fail closed if a session appeared.
        verify_zero_active_sessions(args.drive_ws_url)
        actor = world.spawn_actor(blueprint, transform)
        actor.listen(frames.append)
        frame = wait_for_image(frames)
        bgra = np.frombuffer(frame.raw_data, dtype=np.uint8).reshape(
            frame.height, frame.width, 4
        )
        rgb = bgra[:, :, :3][:, :, ::-1]
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgb).save(output, quality=94)
    finally:
        if actor is not None:
            try:
                actor.stop()
            except Exception:
                pass
            if actor.is_alive:
                result_destroy = actor.destroy()
                destroyed = result_destroy is not False and not actor.is_alive
            else:
                destroyed = True
    if not destroyed:
        raise RuntimeError("temporary candidate camera cleanup could not be verified")
    verify_zero_active_sessions(args.drive_ws_url)

    frame_bytes = output.read_bytes()
    metadata = {
        "schema": "v2x-diagnostic-candidate-render/v1",
        "acceptance_eligible": False,
        "camera": args.camera,
        "created_at_utc": utc_now(),
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "cameras_json_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "render_sha256": hashlib.sha256(frame_bytes).hexdigest(),
        "map_name": carla_map.name,
        "opendrive_sha256": opendrive_sha256,
        "carla_frame": int(frame.frame),
        "sensor_timestamp": float(frame.timestamp),
        "candidate_twin_pose": result["candidate_twin_pose"],
        "candidate_recommendation": result.get("candidate_recommendation"),
        "rendered_rejected_candidate": (
            result.get("candidate_recommendation")
            != "continue_offline_render_review"
        ),
        "temporary_actor_destroyed": destroyed,
    }
    metadata_path = output.with_suffix(output.suffix + ".json")
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    print(metadata_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
