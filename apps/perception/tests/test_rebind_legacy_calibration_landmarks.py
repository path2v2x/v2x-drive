import csv
import importlib.util
import json
from pathlib import Path
import subprocess

import cv2
import numpy as np


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "rebind_legacy_calibration_landmarks.py"
)
SPEC = importlib.util.spec_from_file_location("legacy_landmark_rebind", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_analyze_landmark_supports_painted_line():
    image = np.full((240, 320, 3), 70, dtype=np.uint8)
    cv2.line(image, (20, 120), (300, 120), (20, 220, 240), 18)
    supported = tool.analyze_landmark(image, 160, 120)
    absent = tool.analyze_landmark(image, 160, 50)
    assert supported["heuristic_supported"] is True
    assert supported["paint_distance_px"] == 0.0
    assert supported["paint_nearest_vector_dx_dy_px"] == [0, 0]
    assert supported["edge_distance_px"] <= 20.0
    assert absent["heuristic_supported"] is False


def test_remote_url_credentials_are_redacted():
    assert tool.sanitize_remote_url(
        "https://secret-token@example.com/owner/repo.git"
    ) == "https://example.com/owner/repo.git"
    assert tool.sanitize_remote_url(
        "git@github.com:owner/repo.git"
    ) == "git@github.com:owner/repo.git"


def test_manifest_rejects_hash_mismatch(tmp_path):
    image_path = tmp_path / "ch1.jpg"
    cv2.imwrite(str(image_path), np.zeros((20, 30, 3), dtype=np.uint8))
    frames = {}
    for camera_id in ("ch1", "ch2", "ch3", "ch4"):
        path = tmp_path / f"{camera_id}.jpg"
        path.write_bytes(image_path.read_bytes())
        frames[camera_id] = {
            "file": path.name,
            "width": 30,
            "height": 20,
            "sha256": tool.sha256(path),
            "receipt_time_utc": "2026-01-01T00:00:00Z",
            "stream": camera_id,
        }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema": "v2x-diagnostic-fullres-static-frames/v1",
        "frames": frames,
    }))
    tool.load_frame_manifest(manifest)
    frames["ch3"]["sha256"] = "0" * 64
    manifest.write_text(json.dumps({
        "schema": "v2x-diagnostic-fullres-static-frames/v1",
        "frames": frames,
    }))
    try:
        tool.load_frame_manifest(manifest)
    except ValueError as error:
        assert "hash disagrees" in str(error)
    else:
        raise AssertionError("tampered frame was accepted")


def test_git_binding_rejects_dirty_input(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / "input.csv"
    path.write_text("first\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "input.csv"], check=True)
    subprocess.run(
        [
            "git", "-C", str(tmp_path), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "input",
        ],
        check=True,
    )
    binding = tool.bind_repository_input(tmp_path, path)
    assert binding["clean_at_head"] is True
    path.write_text("second\n")
    try:
        tool.bind_repository_input(tmp_path, path)
    except ValueError as error:
        assert "differs from repository HEAD" in str(error)
    else:
        raise AssertionError("dirty repository input was accepted")


def test_parse_csv_rejects_out_of_frame_point(tmp_path):
    path = tmp_path / "ch1_calibration_errors.csv"
    columns = [
        "Point_ID", "u_pixel", "v_pixel", "True_X_m", "True_Z_m",
        "Pred_X_m", "Pred_Z_m", "Error_X_m", "Error_Z_m", "Total_Error_m",
    ]
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for point_id in range(1, 4):
            writer.writerow({
                "Point_ID": point_id,
                "u_pixel": 100 if point_id < 3 else 1000,
                "v_pixel": 20,
                "True_X_m": 0,
                "True_Z_m": point_id,
                "Pred_X_m": 0,
                "Pred_Z_m": point_id,
                "Error_X_m": 0,
                "Error_Z_m": 0,
                "Total_Error_m": 0,
            })
    try:
        tool.parse_calibration_csv(path, 200, 100)
    except ValueError as error:
        assert "outside its frame" in str(error)
    else:
        raise AssertionError("out-of-frame point was accepted")


def test_exclusive_writers_refuse_overwrite(tmp_path):
    output = tmp_path / "report.json"
    tool.write_json_exclusive(output, {"first": True})
    try:
        tool.write_json_exclusive(output, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("report was overwritten")

    image_path = tmp_path / "image.png"
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    tool.write_image_exclusive(image_path, image)
    try:
        tool.write_image_exclusive(image_path, image)
    except FileExistsError:
        pass
    else:
        raise AssertionError("image was overwritten")
