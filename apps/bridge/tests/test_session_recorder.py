"""Tests for Session Recorder — TDD."""

import json
import os
import tempfile
import pytest

@pytest.mark.unit
class TestSessionRecorder:

    def test_start_creates_file(self):
        """Starting a recording should create a JSONL file with metadata header."""
        from digital_twin_bridge.session_recorder import SessionRecorder

        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = SessionRecorder(session_dir=tmpdir)
            session_id = recorder.start(
                scene_start="2026-03-22T17:00:00Z",
                scene_end="2026-03-22T17:30:00Z",
                objects_count=5,
            )

            assert session_id is not None
            filepath = os.path.join(tmpdir, f"{session_id}.jsonl")
            assert os.path.exists(filepath)

            # First line should be metadata
            with open(filepath) as f:
                header = json.loads(f.readline())
            assert header["type"] == "metadata"
            assert header["scene_start"] == "2026-03-22T17:00:00Z"
            assert header["objects_count"] == 5
            recorder.stop()

    def test_record_frame(self):
        """Recording a frame should append a JSONL line with correct data."""
        from digital_twin_bridge.session_recorder import SessionRecorder

        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = SessionRecorder(session_dir=tmpdir)
            session_id = recorder.start(
                scene_start="2026-03-22T17:00:00Z",
                scene_end="2026-03-22T17:30:00Z",
                objects_count=3,
            )

            recorder.record_frame(
                steer=-0.3, throttle=0.5, brake=0.0,
                pos=[100.0, 200.0, 0.1], rot=[0, 45.0, 0],
                speed_kmh=47.2,
            )

            filepath = os.path.join(tmpdir, f"{session_id}.jsonl")
            with open(filepath) as f:
                lines = f.readlines()
            assert len(lines) == 2  # metadata + 1 frame

            frame = json.loads(lines[1])
            assert frame["steer"] == -0.3
            assert frame["throttle"] == 0.5
            assert frame["speed_kmh"] == 47.2
            assert "t" in frame  # timestamp
            recorder.stop()

    def test_multiple_frames(self):
        """Multiple frames should all be recorded in order."""
        from digital_twin_bridge.session_recorder import SessionRecorder

        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = SessionRecorder(session_dir=tmpdir)
            session_id = recorder.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z", 0)

            for i in range(10):
                recorder.record_frame(
                    steer=i * 0.1, throttle=0.5, brake=0.0,
                    pos=[i, 0, 0], rot=[0, 0, 0], speed_kmh=float(i * 5),
                )

            filepath = os.path.join(tmpdir, f"{session_id}.jsonl")
            with open(filepath) as f:
                lines = f.readlines()
            assert len(lines) == 11  # metadata + 10 frames
            recorder.stop()

    def test_stop_writes_footer(self):
        """Stopping the recording should write a footer with summary."""
        from digital_twin_bridge.session_recorder import SessionRecorder

        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = SessionRecorder(session_dir=tmpdir)
            session_id = recorder.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z", 0)
            recorder.record_frame(steer=0, throttle=0.5, brake=0, pos=[0,0,0], rot=[0,0,0], speed_kmh=0)

            summary = recorder.stop()

            assert summary["frames_recorded"] == 1
            assert "duration_seconds" in summary

    def test_record_before_start_raises(self):
        """Recording without starting should raise."""
        from digital_twin_bridge.session_recorder import SessionRecorder

        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = SessionRecorder(session_dir=tmpdir)
            with pytest.raises(RuntimeError, match="No active recording"):
                recorder.record_frame(steer=0, throttle=0, brake=0, pos=[0,0,0], rot=[0,0,0], speed_kmh=0)
