import sys
from pathlib import Path

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from render_diagnostic_camera_candidate import (  # noqa: E402
    validate_report_source_binding,
    validate_v2x_target,
)


def test_renderer_accepts_only_local_ue5_v2x_target():
    validate_v2x_target("127.0.0.1", 2000, "ws://127.0.0.1:8765")
    with pytest.raises(ValueError):
        validate_v2x_target("127.0.0.1", 2100, "ws://127.0.0.1:8765")
    with pytest.raises(ValueError):
        validate_v2x_target("127.0.0.1", 2000, "ws://127.0.0.1:9999")
    with pytest.raises(ValueError):
        validate_v2x_target("100.72.252.40", 2000, "ws://127.0.0.1:8765")


def test_signal_candidate_must_bind_adjacent_search(tmp_path):
    source = tmp_path / "signal-hypothesis-search.json"
    source.write_text("{}\n")
    import hashlib

    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    report_path = tmp_path / "rank-01-candidate.json"
    validate_report_source_binding(
        report_path, {"source_signal_search_sha256": expected}
    )
    with pytest.raises(ValueError):
        validate_report_source_binding(
            report_path, {"source_signal_search_sha256": "0" * 64}
        )
