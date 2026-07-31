import importlib.util
from pathlib import Path

import numpy as np
import pytest


TOOL = Path(__file__).resolve().parents[1] / "tools" / "propose_segmentation_ground_contacts.py"
SPEC = importlib.util.spec_from_file_location("segmentation_contacts", TOOL)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def vehicle_mask(wheels):
    mask = np.zeros((100, 140), dtype=np.uint8)
    mask[25:65, 20:120] = 1
    for x1, x2 in wheels:
        mask[55:78, x1:x2] = 1
    return mask


def test_contact_uses_distributed_wheel_support_not_bbox_center():
    mask = vehicle_mask([(28, 42), (92, 106)])
    result = module.estimate_contact(mask, [20, 25, 120, 82])
    assert result["method"] == "segmentation_visible_support_midpoint_proposal"
    assert result["pixel"] == pytest.approx([66.5, 77.0])
    assert result["support_endpoints"] == [[28.0, 77.0], [105.0, 77.0]]
    assert result["support_span_fraction_of_bbox"] == pytest.approx(0.77)
    assert result["reviewed"] is False


def test_truck_midpoint_spans_first_and_last_visible_wheels():
    mask = vehicle_mask([(24, 36), (58, 70), (98, 112)])
    result = module.estimate_contact(mask, [20, 25, 120, 82])
    assert result["pixel"] == pytest.approx([67.5, 77.0])
    assert result["support_endpoints"] == [[24.0, 77.0], [111.0, 77.0]]


def test_contact_rejects_tiny_or_narrow_support():
    tiny = np.zeros((100, 140), dtype=np.uint8)
    tiny[50:55, 60:65] = 1
    with pytest.raises(module.ContactProposalError, match="too small"):
        module.estimate_contact(tiny, [20, 25, 120, 82])

    narrow = np.zeros((100, 140), dtype=np.uint8)
    narrow[25:65, 20:120] = 1
    narrow[55:78, 64:69] = 1
    with pytest.raises(module.ContactProposalError, match="too narrow"):
        module.estimate_contact(narrow, [20, 25, 120, 82])


def test_contact_rejects_invalid_quantile_and_bbox():
    mask = vehicle_mask([(28, 42), (92, 106)])
    with pytest.raises(module.ContactProposalError, match="quantile"):
        module.estimate_contact(mask, [20, 25, 120, 82], support_quantile=0.2)
    with pytest.raises(module.ContactProposalError, match="outside"):
        module.estimate_contact(mask, [-1, 25, 120, 82])


def test_visibility_margin_rejects_nearly_clipped_boxes():
    assert module.has_visibility_margin([26, 20, 2534, 1900], 2560, 1920)
    assert not module.has_visibility_margin([26, 20, 2558, 1900], 2560, 1920)


def test_segmentation_assets_publish_atomically_without_overwrite(tmp_path):
    destination = tmp_path / "assets"
    staged = module.StagedAssetDirectory(destination)
    assert not destination.exists()
    (staged.path / "masks").mkdir()
    (staged.path / "masks" / "event.png").write_bytes(b"retained")
    staged.publish()
    assert (destination / "masks" / "event.png").read_bytes() == b"retained"

    with pytest.raises(module.ContactProposalError, match="already exists"):
        module.StagedAssetDirectory(destination)
