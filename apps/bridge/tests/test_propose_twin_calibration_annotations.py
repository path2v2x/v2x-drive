from argparse import Namespace
import hashlib

import cv2
import numpy as np
from PIL import Image
import pytest

from tools.build_twin_calibration_manifest import validate_annotations
from tools.propose_twin_calibration_annotations import (
    ProposalError,
    build_report,
    detect_mutual_sift_matches,
    read_gray_image,
    select_spatially_distributed,
    write_report_exclusive,
)


def candidate(real, twin, ratio=0.5, response=1.0):
    return {
        "real_pixel": list(real),
        "twin_pixel": list(twin),
        "descriptor_distance": 10.0,
        "ratio": ratio,
        "real_response": response,
        "twin_response": response,
        "homography_inlier": None,
    }


def args(**overrides):
    values = {
        "maximum": 48,
        "ratio": 0.72,
        "ransac_px": 4.0,
        "grid_columns": 6,
        "grid_rows": 4,
        "minimum_separation_fraction": 0.025,
    }
    values.update(overrides)
    return Namespace(**values)


def frame(path, payload, width=1000, height=500):
    return {
        "path": str(path),
        "bytes": payload,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "width": width,
        "height": height,
        "image": np.zeros((height, width), dtype=np.uint8),
    }


def test_selection_round_robins_cells_and_rejects_near_duplicates():
    matches = [
        candidate((10, 10), (20, 20), ratio=0.4),
        candidate((12, 12), (22, 22), ratio=0.3),
        candidate((500, 10), (600, 20), ratio=0.5),
        candidate((900, 400), (950, 450), ratio=0.6),
    ]
    selected = select_spatially_distributed(
        matches, (1000, 500), (1000, 500), maximum=4,
        minimum_separation_fraction=0.025,
    )
    assert len(selected) == 3
    assert [12, 12] in [item["real_pixel"] for item in selected]
    assert [10, 10] not in [item["real_pixel"] for item in selected]


def test_report_is_explicitly_ineligible_and_manifest_rejects_it(tmp_path):
    real = frame(tmp_path / "real.jpg", b"real")
    twin = frame(tmp_path / "twin.jpg", b"twin")
    matches = [
        candidate((50 + index * 70, 40 + (index % 4) * 100),
                  (60 + index * 60, 50 + (index % 4) * 90))
        for index in range(12)
    ]
    report = build_report(
        "ch1", real, twin, matches,
        {"mutual_matches": len(matches), "homography": None}, args(),
    )
    assert report["acceptance_eligible"] is False
    assert report["provenance"] == "matcher_proposal_only"
    assert all(item["acceptance_eligible"] is False for item in report["proposals"])
    assert all(item["provenance"] == "matcher_proposal_only" for item in report["proposals"])
    assert report["conversion_policy"]["automatic_conversion_allowed"] is False
    with pytest.raises(ValueError, match="annotation real frame hash is invalid"):
        validate_annotations(report, "ch1", (1000, 500), (1000, 500))


def test_identical_frames_cannot_build_report(tmp_path):
    same = frame(tmp_path / "same.jpg", b"same")
    with pytest.raises(ProposalError, match="byte-identical"):
        build_report("ch1", same, same, [], {"homography": None}, args())


def test_reads_valid_image_and_rejects_invalid(tmp_path):
    image = np.zeros((128, 128, 3), dtype=np.uint8)
    cv2.circle(image, (64, 64), 20, (255, 255, 255), -1)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    valid = tmp_path / "valid.jpg"
    valid.write_bytes(encoded.tobytes())
    observed = read_gray_image(valid, "real")
    assert observed["width"] == 128
    invalid = tmp_path / "invalid.jpg"
    invalid.write_bytes(b"not-an-image")
    with pytest.raises(ProposalError, match="not a usable image"):
        read_gray_image(invalid, "real")


def test_mutual_sift_matching_finds_translated_unique_features():
    rng = np.random.default_rng(42)
    real = np.zeros((480, 640), dtype=np.uint8)
    for _ in range(80):
        center = tuple(int(value) for value in rng.integers([20, 20], [620, 460]))
        radius = int(rng.integers(3, 12))
        color = int(rng.integers(100, 256))
        cv2.circle(real, center, radius, color, 1)
    matrix = np.float32([[1, 0, 8], [0, 1, 5]])
    twin = cv2.warpAffine(real, matrix, (640, 480))
    matches, diagnostics = detect_mutual_sift_matches(real, twin)
    assert diagnostics["mutual_matches"] >= 12
    assert len(matches) == diagnostics["mutual_matches"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"maximum": 0},
        {"grid_columns": 0},
        {"minimum_separation_fraction": 0.5},
    ],
)
def test_selection_rejects_invalid_bounds(kwargs):
    with pytest.raises(ProposalError):
        select_spatially_distributed([], (100, 100), (100, 100), **kwargs)


@pytest.mark.parametrize(
    "bad",
    [
        candidate((-1, 10), (10, 10)),
        candidate((10, 10), (100, 10)),
        candidate((float("nan"), 10), (10, 10)),
        {"real_pixel": [10, 10]},
    ],
)
def test_selection_rejects_malformed_or_out_of_frame_matches(bad):
    with pytest.raises(ProposalError, match="matcher proposal"):
        select_spatially_distributed([bad], (100, 100), (100, 100))


def test_direct_report_builder_rejects_unknown_camera(tmp_path):
    real = frame(tmp_path / "real.jpg", b"real")
    twin = frame(tmp_path / "twin.jpg", b"twin")
    with pytest.raises(ProposalError, match="camera ID"):
        build_report("camera-1", real, twin, [], {"homography": None}, args())


def test_exclusive_write_rejects_existing_file_and_dangling_symlink(tmp_path):
    existing = tmp_path / "existing.json"
    existing.write_text("keep")
    with pytest.raises(ProposalError, match="already exists"):
        write_report_exclusive(existing, {"new": True})
    assert existing.read_text() == "keep"

    target = tmp_path / "outside.json"
    link = tmp_path / "proposal.json"
    link.symlink_to(target)
    with pytest.raises(ProposalError, match="already exists"):
        write_report_exclusive(link, {"new": True})
    assert not target.exists()


def test_header_dimensions_are_bounded_before_opencv_decode(tmp_path, monkeypatch):
    image = Image.new("L", (9000, 64), color=0)
    oversized = tmp_path / "oversized.png"
    image.save(oversized)
    called = False

    def forbidden_decode(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("OpenCV decode must not run")

    monkeypatch.setattr(cv2, "imdecode", forbidden_decode)
    with pytest.raises(ProposalError, match="decoded dimensions"):
        read_gray_image(oversized, "real")
    assert called is False
