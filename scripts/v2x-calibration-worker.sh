#!/usr/bin/env bash
set -euo pipefail

# Manage an isolated, non-restarting copy of the approved UE5.5 V2X worker.
# This container is never a production service and binds only to loopback.

ACTION="${1:-status}"
CONTAINER="${V2X_CALIBRATION_CONTAINER:-v2x-calibration-ue5}"
IMAGE="${V2X_CALIBRATION_IMAGE:-ghcr.io/simforgeinc/carla-rr-maps:0.10.0}"
EXPECTED_IMAGE_ID="${V2X_CALIBRATION_EXPECTED_IMAGE_ID:-sha256:8e7c7152f86a9e26878de5f280514f224290f70aad7b28d00d5087709504118e}"
RPC_PORT=2300
SCOPE_LABEL="com.path2v2x.scope=calibration"
CARLA_PYTHON="${V2X_CALIBRATION_PYTHON:-/home/path/V2XCarla/carla-venv-310/bin/python}"
RICHMOND_MAP="Carla/Maps/Richmond_Field_Station_Richmond_CA"
RICHMOND_LOAD_MAP="/Game/Carla/Maps/Richmond_Field_Station_Richmond_CA"
RICHMOND_OPENDRIVE_SHA256="0737f3d9f9f344c06b2c63fe669afa8a15f814568ee9c16046795338f56f5ee1"
MAP_READY_TIMEOUT_SECONDS="${V2X_CALIBRATION_MAP_READY_TIMEOUT_SECONDS:-180}"

if ! [[ "$MAP_READY_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] \
    || (( MAP_READY_TIMEOUT_SECONDS < 90 || MAP_READY_TIMEOUT_SECONDS > 300 )); then
    echo "Calibration map readiness timeout must be an integer in [90, 300]" >&2
    exit 2
fi

if [[ "$CONTAINER" == "carla-rr-maps" ]]; then
    echo "Refusing to operate the production CARLA container" >&2
    exit 2
fi

verify_image() {
    local image_id
    image_id="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
    if [[ "$image_id" != "$EXPECTED_IMAGE_ID" ]]; then
        echo "Expected calibration worker image ID is unavailable" >&2
        exit 1
    fi
}

inspect_worker() {
    docker inspect "$CONTAINER" | jq -e --arg image "$IMAGE" '
        .[0] as $root
        | ($root.Config.Image == $image)
          and ($root.Config.Labels["com.path2v2x.scope"] == "calibration")
          and (($root.Mounts // []) | length == 0)
          and ($root.HostConfig.Runtime == "nvidia")
          and ($root.HostConfig.RestartPolicy.Name == "no")
          and ($root.HostConfig.NetworkMode == "bridge")
          and ([2300, 2301, 2302] | all(. as $port |
            ($root.HostConfig.PortBindings[($port|tostring) + "/tcp"] // [])
            | any(
                .HostIp == "127.0.0.1"
                and .HostPort == ($port|tostring)
              )
          ))
          and (($root.Config.Cmd | join(" "))
            | contains("-carla-rpc-port=2300"))
          and (($root.Config.Cmd | join(" "))
            | contains("-RenderOffScreen"))
    ' >/dev/null
}

ensure_richmond_map() {
    local status
    if timeout --signal=TERM --kill-after=5s \
        "${MAP_READY_TIMEOUT_SECONDS}s" \
        "$CARLA_PYTHON" - "$RPC_PORT" "$RICHMOND_MAP" "$RICHMOND_LOAD_MAP" \
        "$RICHMOND_OPENDRIVE_SHA256" "$MAP_READY_TIMEOUT_SECONDS" <<'PY'
import hashlib
import sys
import time

import carla

port = int(sys.argv[1])
expected_map = sys.argv[2]
load_map = sys.argv[3]
expected_hash = sys.argv[4]
timeout_seconds = int(sys.argv[5])
client = carla.Client("127.0.0.1", port)
client.set_timeout(30.0)
deadline = time.monotonic() + timeout_seconds
last_error = None
last_load_request = None
load_request_count = 0
while time.monotonic() < deadline:
    try:
        world = client.get_world()
        if world.get_map().name != expected_map:
            now = time.monotonic()
            if last_load_request is None or now - last_load_request >= 120.0:
                last_load_request = now
                load_request_count += 1
                world = client.load_world(load_map)
            else:
                last_error = RuntimeError(
                    "Richmond load is still pending after one bounded request"
                )
                time.sleep(1.0)
                continue
        carla_map = world.get_map()
        actual_hash = hashlib.sha256(
            carla_map.to_opendrive().encode("utf-8")
        ).hexdigest()
        if carla_map.name != expected_map or actual_hash != expected_hash:
            raise RuntimeError("isolated worker Richmond fingerprint mismatch")
        if list(world.get_actors().filter("sensor.camera.*")):
            raise RuntimeError("isolated worker contains unexpected camera sensors")
        print(
            f"map={carla_map.name} opendrive_sha256={actual_hash} "
            f"load_requests={load_request_count}"
        )
        raise SystemExit(0)
    except (RuntimeError, OSError) as exc:
        last_error = exc
        time.sleep(1.0)
raise SystemExit(f"isolated worker did not become Richmond-ready: {last_error}")
PY
    then
        return 0
    else
        status=$?
    fi
    if (( status == 124 || status == 137 || status == 143 )); then
        echo "Isolated worker Richmond readiness exceeded the hard "\
"${MAP_READY_TIMEOUT_SECONDS}s deadline" >&2
        return 1
    fi
    return "$status"
}

case "$ACTION" in
    start)
        verify_image
        if docker inspect "$CONTAINER" >/dev/null 2>&1; then
            inspect_worker || {
                echo "Existing calibration container does not match the tracked "\
"definition" \
                    >&2
                exit 1
            }
            if [[ "$(docker inspect -f '{{.State.Running}}' \
                "$CONTAINER")" == true ]]; then
                inspect_worker
                ensure_richmond_map
                echo "$CONTAINER already running and Richmond-ready"
                exit 0
            fi
            docker start "$CONTAINER" >/dev/null
        else
            docker run -d \
                --name "$CONTAINER" \
                --runtime=nvidia \
                --restart=no \
                --publish 127.0.0.1:2300:2300 \
                --publish 127.0.0.1:2301:2301 \
                --publish 127.0.0.1:2302:2302 \
                --env NVIDIA_VISIBLE_DEVICES=all \
                --env NVIDIA_DRIVER_CAPABILITIES=all \
                --label "$SCOPE_LABEL" \
                "$IMAGE" \
                ./CarlaUnreal.sh -RenderOffScreen -vulkan -nosound \
                "-carla-rpc-port=$RPC_PORT" >/dev/null
        fi
        inspect_worker
        ensure_richmond_map
        echo "$CONTAINER started on 127.0.0.1:$RPC_PORT"
        ;;
    status)
        inspect_worker
        docker inspect "$CONTAINER" --format \
            'name={{.Name}} running={{.State.Running}}'\
' started={{.State.StartedAt}} image={{.Image}}'
        ;;
    stop)
        inspect_worker
        if [[ "$(docker inspect -f '{{.State.Running}}' \
            "$CONTAINER")" == true ]]; then
            timeout 20 docker stop --time 10 "$CONTAINER" >/dev/null || true
            if [[ "$(docker inspect -f '{{.State.Running}}' \
                "$CONTAINER")" == true ]]; then
                docker kill "$CONTAINER" >/dev/null
            fi
        fi
        echo "$CONTAINER stopped"
        ;;
    remove)
        inspect_worker
        if [[ "$(docker inspect -f '{{.State.Running}}' \
            "$CONTAINER")" == true ]]; then
            echo "Stop the owned calibration worker before removing it" >&2
            exit 1
        fi
        docker rm "$CONTAINER" >/dev/null
        echo "$CONTAINER removed"
        ;;
    *)
        echo "Usage: $0 {start|status|stop|remove}" >&2
        exit 2
        ;;
esac
