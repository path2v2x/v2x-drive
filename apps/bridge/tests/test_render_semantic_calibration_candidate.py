import copy
import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from render_semantic_calibration_candidate import (  # noqa: E402
    EXPECTED_IMAGE,
    EXPECTED_IMAGE_ID,
    RenderError,
    buffer_statistics,
    decode_buffers,
    decode_rgb_depth_buffers,
    rgb_depth_statistics,
    validate_candidate,
    validate_endpoint,
    validate_worker_inspect,
)


def worker_inspect():
    return {
        "Id": "container-id",
        "Name": "/v2x-calibration-ue5",
        "Image": EXPECTED_IMAGE_ID,
        "Config": {
            "Image": EXPECTED_IMAGE,
            "Labels": {"com.path2v2x.scope": "calibration"},
            "Cmd": [
                "./CarlaUnreal.sh",
                "-RenderOffScreen",
                "-vulkan",
                "-carla-rpc-port=2300",
            ],
        },
        "HostConfig": {
            "Runtime": "nvidia",
            "RestartPolicy": {"Name": "no"},
            "NetworkMode": "bridge",
            "PortBindings": {
                f"{port}/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": str(port)}
                ]
                for port in (2300, 2301, 2302)
            },
        },
        "Mounts": [],
        "State": {"Running": True, "StartedAt": "2026-07-12T00:00:00Z"},
    }


def test_endpoint_is_locked_to_isolated_ue5_worker():
    validate_endpoint("127.0.0.1", 2300, "v2x-calibration-ue5")
    for values in (
        ("127.0.0.1", 2000, "v2x-calibration-ue5"),
        ("127.0.0.1", 2300, "carla-rr-maps"),
        ("0.0.0.0", 2300, "v2x-calibration-ue5"),
    ):
        with pytest.raises(RenderError):
            validate_endpoint(*values)


def test_worker_inspect_requires_exact_image_scope_and_loopback_ports():
    result = validate_worker_inspect(worker_inspect())
    assert result["image_id"] == EXPECTED_IMAGE_ID
    for mutation in ("image", "label", "port", "restart", "running"):
        value = copy.deepcopy(worker_inspect())
        if mutation == "image":
            value["Image"] = "sha256:wrong"
        elif mutation == "label":
            value["Config"]["Labels"].clear()
        elif mutation == "port":
            value["HostConfig"]["PortBindings"]["2300/tcp"][0][
                "HostIp"
            ] = "0.0.0.0"
        elif mutation == "restart":
            value["HostConfig"]["RestartPolicy"]["Name"] = "always"
        else:
            value["State"]["Running"] = False
        with pytest.raises(RenderError):
            validate_worker_inspect(value)


def test_worker_inspect_rejects_every_filesystem_mount(tmp_path):
    value = worker_inspect()
    value["Mounts"] = [{
        "Type": "bind",
        "Source": str(tmp_path),
        "Destination": "/home/carla/CarlaUnreal/Content/Berkley",
        "RW": False,
    }]
    with pytest.raises(RenderError, match="unexpected filesystem mounts"):
        validate_worker_inspect(value)


def test_candidate_is_hash_bound_and_finite():
    config_hash = hashlib.sha256(b"cameras").hexdigest()
    candidate = {
        "schema": "v2x-semantic-calibration-candidate/v1",
        "acceptance_eligible": False,
        "camera_id": "ch4",
        "candidate_id": "baseline",
        "cameras_json_sha256": config_hash,
        "twin_pose": {"yaw_offset_deg": 1.5},
    }
    assert validate_candidate(candidate, config_hash) == (
        "ch4",
        "baseline",
        {"yaw_offset_deg": 1.5},
    )
    candidate["twin_pose"]["yaw_offset_deg"] = float("nan")
    with pytest.raises(RenderError):
        validate_candidate(candidate, config_hash)


class Frame:
    width = 1
    height = 1

    def __init__(self, bgra):
        self.raw_data = bytes(bgra)


def test_buffer_decoding_is_explicit_and_lossless():
    frames = {
        "rgb": Frame([10, 20, 30, 255]),
        "semantic": Frame([0, 0, 7, 255]),
        "instance": Frame([4, 3, 7, 255]),
        "depth": Frame([0, 0, 0, 255]),
    }
    raw, rgb, semantic, instance_semantic, instance_ids, depth = decode_buffers(
        frames
    )
    assert raw["rgb"].tolist() == [[[[10, 20, 30, 255]]]][0]
    assert rgb.tolist() == [[[30, 20, 10]]]
    assert semantic.tolist() == [[7]]
    assert instance_semantic.tolist() == [[7]]
    assert instance_ids.tolist() == [[3 * 256 + 4]]
    assert np.array_equal(depth, np.zeros((1, 1), dtype=np.float32))
    statistics = buffer_statistics(
        (raw, rgb, semantic, instance_semantic, instance_ids, depth)
    )
    assert statistics["semantic_tags"]["usable_for_class_alignment"] is False
    assert statistics["instance"]["usable_for_static_instances"] is False
    assert statistics["depth_meters"]["finite_fraction"] == 1.0


def test_rgb_depth_mode_rejects_extra_modalities_and_reports_missing_segmentation():
    frames = {
        "rgb": Frame([10, 20, 30, 255]),
        "depth": Frame([0, 0, 0, 255]),
    }
    decoded = decode_rgb_depth_buffers(frames)
    statistics = rgb_depth_statistics(decoded)
    assert statistics["semantic_tags"] == {
        "captured": False,
        "usable_for_class_alignment": False,
    }
    assert statistics["depth_meters"]["finite_fraction"] == 1.0
    frames["semantic"] = Frame([0, 0, 7, 255])
    with pytest.raises(RenderError):
        decode_rgb_depth_buffers(frames)
