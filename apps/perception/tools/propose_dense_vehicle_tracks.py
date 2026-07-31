#!/usr/bin/env python3
"""Build hash-bound, proposal-only vehicle tracks from dense KVS windows.

The persisted object ID and sparse detection box are hints, never identity or
geometry truth.  A pinned segmentation model is tracked over every retained
frame.  Exact-frame, cross-model segmentation consensus anchors select the
target tracker ID.  Any anchor disagreement, missing frame binding, hash drift,
or tracker-ID switch is retained as a rejection rather than hidden.

Resource admission: do not run CPU model jobs while live perception is active
unless the job is isolated below a previously proven non-impact CPU and memory
cap.  This proposal tool does not establish that cap.

Every bound report, retained model, segmentation mask, and dense JPEG is copied
to a content-addressed staged input before inference.  Original file identity
and hashes are checked again before publication; the staged copies are also
rehash-verified so a published proposal is self-contained and reproducible.

SIGINT and SIGTERM atomically move only this invocation's exact staging inode
out of the active staging namespace into a private quarantine.  SIGKILL cannot
be handled and can leave the active directory behind.  Later runs do not sweep
either kind of directory: an operator must prove the owning process is gone
and the final output is absent before removing an orphan or quarantine.
"""

import argparse
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import signal
import shutil
import stat
import sys
import threading
import uuid

import cv2
import numpy as np

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from capture_static_kvs_window import (  # noqa: E402
    StaticCaptureError,
    atomic_publish_directory,
)
from capture_dense_kvs_window import (  # noqa: E402
    DenseCaptureError,
    load_source_report as load_dense_source_report,
)
from build_segmentation_contact_consensus import (  # noqa: E402
    ConsensusError,
    build as rebuild_segmentation_consensus,
)
from propose_segmentation_ground_contacts import (  # noqa: E402
    ContactProposalError,
    draw_overlay,
    estimate_contact,
    has_visibility_margin,
    largest_component,
    validate_bbox,
)
from redetect_selected_capture_frames import (  # noqa: E402
    VEHICLE_LABELS,
    bbox_iou,
    sha256_bytes,
    touches_boundary,
)


SCHEMA = "v2x-dense-vehicle-track-proposals/v1"
DENSE_SCHEMA = "v2x-dense-kvs-window/v1"
CONSENSUS_SCHEMA = "v2x-segmentation-ground-contact-consensus/v2"
CAMERAS = {"ch1", "ch2", "ch3", "ch4"}
MINIMUM_ANCHOR_IOU = 0.50
MINIMUM_PLAUSIBLE_RUNNER_UP_IOU = 0.20
MINIMUM_ANCHOR_IOU_MARGIN = 0.20
MINIMUM_DISTINCT_ANCHOR_FRAMES = 2
MINIMUM_ANCHOR_SPAN_MS = 200.0
REFERENCE_WIDTH = 1280.0
REFERENCE_HEIGHT = 960.0
MAXIMUM_CONTACT_RESIDUAL_JUMP_AT_REFERENCE_PX = 24.0
MAXIMUM_CONTACT_RESIDUAL_SPEED_AT_REFERENCE_PX_PER_SECOND = 160.0
MAXIMUM_CONTACT_RESIDUAL_ACCELERATION_AT_REFERENCE_PX_PER_SECOND2 = 1000.0
STAGING_OWNER_MARKER = ".v2x-dense-track-staging-owner.json"
HANDLED_TERMINATION_SIGNALS = (signal.SIGINT, signal.SIGTERM)
PUBLICATION_CONTRACTS = {}
PUBLICATION_CONTRACTS_LOCK = threading.Lock()


class DenseTrackError(RuntimeError):
    pass


class DenseTrackInterrupted(DenseTrackError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def valid_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def source_fingerprint(stat_result):
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def pin_source_file(path, expected_sha256, label):
    try:
        path = Path(path).expanduser().resolve()
    except TypeError as exc:
        raise DenseTrackError(f"{label} identity is invalid") from exc
    if not path.is_file() or not valid_sha256(expected_sha256):
        raise DenseTrackError(f"{label} identity is invalid")
    try:
        before = source_fingerprint(path.stat())
        actual_sha256 = sha256_file(path)
        after = source_fingerprint(path.stat())
    except OSError as exc:
        raise DenseTrackError(f"{label} cannot be pinned") from exc
    if before != after or actual_sha256 != expected_sha256:
        raise DenseTrackError(f"{label} changed while it was pinned")
    return {
        "source_path": str(path),
        "sha256": expected_sha256,
        "_source_fingerprint": after,
    }


def revalidate_source_files(bindings):
    checked = set()
    for binding in bindings:
        key = (
            binding["source_path"],
            binding["sha256"],
            binding["_source_fingerprint"],
        )
        if key in checked:
            continue
        checked.add(key)
        path = Path(binding["source_path"])
        try:
            before = source_fingerprint(path.stat())
            actual_sha256 = sha256_file(path)
            after = source_fingerprint(path.stat())
        except OSError as exc:
            raise DenseTrackError(
                f"bound source changed during dense tracking: {path}"
            ) from exc
        if (
            before != binding["_source_fingerprint"]
            or after != binding["_source_fingerprint"]
            or actual_sha256 != binding["sha256"]
        ):
            raise DenseTrackError(
                f"bound source changed during dense tracking: {path}"
            )


def snapshot_source_file(staged_root, binding, category, suffix, label):
    relative = Path("inputs") / category / f"{binding['sha256']}{suffix}"
    destination = Path(staged_root) / relative
    if destination.exists():
        if not destination.is_file() or sha256_file(destination) != binding["sha256"]:
            raise DenseTrackError(f"{label} snapshot collision")
    else:
        copy_file_exclusive(binding["source_path"], destination, f"{label} snapshot")
    if sha256_file(destination) != binding["sha256"]:
        raise DenseTrackError(f"{label} changed while it was snapshotted")
    return {
        "source_path": binding["source_path"],
        "path": relative.as_posix(),
        "sha256": binding["sha256"],
        "byte_count": destination.stat().st_size,
    }


def verify_bound_input_snapshots(staged_root, value):
    descriptors = []

    def collect(item):
        if isinstance(item, dict):
            if {"source_path", "path", "sha256", "byte_count"} <= set(item):
                descriptors.append(item)
            for nested in item.values():
                collect(nested)
        elif isinstance(item, list):
            for nested in item:
                collect(nested)

    collect(value)
    root = Path(staged_root).resolve()
    for descriptor in descriptors:
        relative = descriptor["path"]
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise DenseTrackError("bound input snapshot path is invalid")
        path = (root / relative).resolve()
        try:
            path.relative_to(root / "inputs")
        except ValueError as exc:
            raise DenseTrackError("bound input snapshot escapes inputs directory") from exc
        if (
            not path.is_file()
            or path.stat().st_size != descriptor["byte_count"]
            or sha256_file(path) != descriptor["sha256"]
        ):
            raise DenseTrackError("bound input snapshot changed during dense tracking")


def write_bytes_exclusive(path, value, label):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise DenseTrackError(f"{label} path collision") from exc


def write_text_exclusive(path, value, label):
    write_bytes_exclusive(path, value.encode("utf-8"), label)


def copy_file_exclusive(source, destination, label):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Path(source).open("rb") as input_stream, destination.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    except FileExistsError as exc:
        raise DenseTrackError(f"{label} path collision") from exc


def fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory_tree(root):
    root = Path(root)
    directories = [root]
    directories.extend(path for path in root.rglob("*") if path.is_dir())
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        fsync_directory(directory)


def quarantine_owned_staging(parent_fd, staged_name, owned_identity):
    """Move the exact owned stage out of its active name without deleting races."""
    names = [staged_name]
    try:
        names.extend(name for name in os.listdir(parent_fd) if name != staged_name)
    except OSError as exc:
        raise DenseTrackError("owned staging directory cannot be enumerated") from exc
    seen = set()
    for name in names:
        if (
            name in seen
            or not isinstance(name, str)
            or "/" in name
            or name in {"", ".", ".."}
        ):
            continue
        seen.add(name)
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DenseTrackError("owned staging candidate cannot be inspected") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != owned_identity
        ):
            continue
        for _attempt in range(100):
            output_stem = staged_name.split(".tmp-", 1)[0].lstrip(".")
            quarantine_name = (
                f".{output_stem}.staging-quarantine-{uuid.uuid4().hex}"
            )
            try:
                rename_noreplace_at(parent_fd, name, quarantine_name)
                break
            except FileExistsError:
                continue
            except FileNotFoundError:
                quarantine_name = None
                break
        else:
            raise DenseTrackError("staging quarantine name space is exhausted")
        if quarantine_name is None:
            continue
        quarantined = os.stat(
            quarantine_name, dir_fd=parent_fd, follow_symlinks=False
        )
        if (quarantined.st_dev, quarantined.st_ino) != owned_identity:
            # A foreign replacement won between inspection and rename.  Put it
            # back without replacement when possible; otherwise preserve it in
            # quarantine.  Never delete either foreign path.
            try:
                rename_noreplace_at(parent_fd, quarantine_name, name)
            except FileExistsError:
                pass
            continue
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        quarantine_fd = os.open(quarantine_name, flags, dir_fd=parent_fd)
        try:
            pinned = os.fstat(quarantine_fd)
            if (pinned.st_dev, pinned.st_ino) != owned_identity:
                raise DenseTrackError("owned staging quarantine was replaced")
            os.fchmod(quarantine_fd, stat.S_IRWXU)
            os.fsync(quarantine_fd)
        finally:
            os.close(quarantine_fd)
        os.fsync(parent_fd)
        return quarantine_name
    return None


@contextmanager
def invocation_staging_directory(output):
    """Yield one owned staging directory with scoped, cleanup-safe signals."""
    if not hasattr(signal, "pthread_sigmask"):
        raise DenseTrackError("scoped staging requires POSIX signal masking")
    output = Path(output).resolve()
    handled = set(HANDLED_TERMINATION_SIGNALS)
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled)
    previous_handlers = {}
    staged = None
    marker = None
    parent_fd = None
    staged_fd = None
    owned_identity = None
    body_completed = False
    return_boundary_verified = False
    state = {"cleanup_started": False, "received": None}

    def interrupt(signum, _frame):
        # A repeated signal must not interrupt the exact-directory cleanup.
        if state["cleanup_started"] or state["received"] is not None:
            return
        state["received"] = signum
        raise DenseTrackInterrupted(
            f"dense vehicle tracking interrupted by {signal.Signals(signum).name}"
        )

    cleanup_error = None
    try:
        try:
            for handled_signal in HANDLED_TERMINATION_SIGNALS:
                previous_handler = signal.getsignal(handled_signal)
                signal.signal(handled_signal, interrupt)
                previous_handlers[handled_signal] = previous_handler
        except ValueError as exc:
            raise DenseTrackError(
                "scoped staging signal handlers require the Python main thread"
            ) from exc
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(output.parent, directory_flags)
        for _attempt in range(100):
            staged_name = f".{output.name}.tmp-{uuid.uuid4().hex}"
            try:
                os.mkdir(staged_name, mode=0o700, dir_fd=parent_fd)
                break
            except FileExistsError:
                continue
        else:
            raise DenseTrackError("staging directory name space is exhausted")
        staged = output.parent / staged_name
        before_open = os.stat(staged_name, dir_fd=parent_fd, follow_symlinks=False)
        staged_fd = os.open(staged_name, directory_flags, dir_fd=parent_fd)
        staged_metadata = os.fstat(staged_fd)
        owned_identity = (staged_metadata.st_dev, staged_metadata.st_ino)
        after_open = os.stat(staged_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(before_open.st_mode)
            or stable_file_fields(before_open) != stable_file_fields(staged_metadata)
            or stable_file_fields(before_open) != stable_file_fields(after_open)
        ):
            raise DenseTrackError("owned staging directory changed while it was pinned")
        marker = staged / STAGING_OWNER_MARKER
        write_text_exclusive(
            marker,
            json.dumps({
                "schema": "v2x-dense-track-staging-owner/v1",
                "pid": os.getpid(),
                "created_at_utc": utc_now(),
                "nonce": os.urandom(16).hex(),
                "staging_directory": str(staged.resolve()),
                "final_output_directory": str(output),
            }, indent=2, sort_keys=True) + "\n",
            "dense track staging owner marker",
        )
        fsync_directory(staged)
        # The try/finally is active before pending signals are deliverable.
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        yield staged, marker
        body_completed = True
    finally:
        state["cleanup_started"] = True
        signal.pthread_sigmask(signal.SIG_BLOCK, handled)
        if staged is not None and parent_fd is not None and owned_identity is not None:
            try:
                return_boundary_error = None
                with PUBLICATION_CONTRACTS_LOCK:
                    publication_contract = PUBLICATION_CONTRACTS.pop(
                        owned_identity, None
                    )
                if body_completed:
                    if publication_contract is None:
                        return_boundary_error = DenseTrackError(
                            "dense-track return publication contract is missing"
                        )
                    else:
                        try:
                            verify_published_tree(output, publication_contract)
                        except DenseTrackError as exc:
                            return_boundary_error = exc
                        else:
                            return_boundary_verified = True
                try:
                    active = os.stat(
                        staged.name, dir_fd=parent_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    active = None
                active_is_owned = active is not None and (
                    active.st_dev, active.st_ino
                ) == owned_identity
                active_is_foreign = active is not None and not active_is_owned
                try:
                    published = os.stat(
                        output.name, dir_fd=parent_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    published = None
                published_is_owned = published is not None and (
                    published.st_dev, published.st_ino
                ) == owned_identity
                if return_boundary_verified and not published_is_owned:
                    return_boundary_verified = False
                    return_boundary_error = DenseTrackError(
                        "dense-track publication changed after return verification"
                    )
                if return_boundary_verified and published_is_owned:
                    # The body atomically published this exact pinned stage.
                    # A recreated temporary name is foreign and must survive;
                    # never scan the parent for the inode now valid at output.
                    pass
                elif (
                    active_is_owned
                    or active_is_foreign
                    or not body_completed
                    or return_boundary_error is not None
                ):
                    quarantined = quarantine_owned_staging(
                        parent_fd, staged.name, owned_identity
                    )
                    if quarantined is None and (
                        active_is_owned
                        or not body_completed
                        or return_boundary_error is not None
                    ):
                        raise DenseTrackError(
                            "owned staging directory could not be quarantined"
                        )
                if return_boundary_error is not None:
                    cleanup_error = return_boundary_error
            except (OSError, DenseTrackError) as exc:
                cleanup_error = exc
        try:
            for handled_signal, previous_handler in previous_handlers.items():
                signal.signal(handled_signal, previous_handler)
        finally:
            if staged_fd is not None:
                os.close(staged_fd)
            if parent_fd is not None:
                os.close(parent_fd)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        if cleanup_error is not None:
            if isinstance(cleanup_error, DenseTrackError):
                raise cleanup_error
            raise DenseTrackError(
                f"failed to quarantine invocation staging directory {staged}"
            ) from cleanup_error


def parse_utc(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DenseTrackError(f"{label} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DenseTrackError(f"{label} is invalid") from exc
    return parsed.astimezone(timezone.utc)


def load_json(path, label):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DenseTrackError(f"{label} is unreadable or invalid") from exc
    if not isinstance(value, dict):
        raise DenseTrackError(f"{label} must be a JSON object")
    return path, raw, value


def canonical_retained_path(report_path, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise DenseTrackError("dense frame path must be non-empty and relative")
    root = report_path.parent.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise DenseTrackError("dense frame path escapes its capture directory") from exc
    if not path.is_file():
        raise DenseTrackError("dense frame is missing")
    return path


def load_dense_report(path, expected_source_report_sha256=None):
    report_path, raw, report = load_json(path, "dense capture report")
    report_binding = pin_source_file(
        report_path, sha256_bytes(raw), "dense capture report"
    )
    if report.get("schema") != DENSE_SCHEMA:
        raise DenseTrackError("dense capture schema is unsupported")
    camera_id = report.get("camera_id")
    object_id = report.get("object_id")
    frames = report.get("frames")
    if (
        camera_id not in CAMERAS
        or not isinstance(object_id, str)
        or not object_id.strip()
        or not isinstance(frames, list)
        or len(frames) < 3
        or report.get("frame_count") != len(frames)
        or report.get("acceptance_eligible") is not False
    ):
        raise DenseTrackError("dense capture contract is incomplete")
    source = report.get("source_event_report")
    if not isinstance(source, dict):
        raise DenseTrackError("dense source-event report identity is missing")
    declared_source_path = source.get("path")
    declared_source_hash = source.get("sha256")
    source_path = Path(str(declared_source_path or "")).expanduser().resolve()
    if (
        not source_path.is_file()
        or str(source_path) != declared_source_path
        or not valid_sha256(declared_source_hash)
    ):
        raise DenseTrackError("dense source-event report identity mismatches disk")
    try:
        (
            verified_source_path,
            source_raw,
            verified_source_events,
            _first_source_time,
            _last_source_time,
        ) = load_dense_source_report(source_path, camera_id, object_id)
    except (DenseCaptureError, OSError, TypeError, ValueError) as exc:
        raise DenseTrackError(
            "dense source-event report content is invalid or unrelated"
        ) from exc
    actual_source_hash = sha256_bytes(source_raw)
    if (
        verified_source_path != source_path
        or actual_source_hash != declared_source_hash
        or (
            expected_source_report_sha256 is not None
            and actual_source_hash != expected_source_report_sha256
        )
    ):
        raise DenseTrackError(
            "dense source-event report is not the consensus capture denominator"
        )
    source_binding = pin_source_file(
        source_path, actual_source_hash, "dense source-event report"
    )

    decoded = []
    frame_bindings = []
    seen_indices = set()
    seen_hashes = set()
    previous_time = None
    resolution = report.get("resolution")
    for row in frames:
        if not isinstance(row, dict):
            raise DenseTrackError("dense frame descriptor is malformed")
        index = row.get("index")
        expected_hash = row.get("sha256")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index in seen_indices
            or index != len(decoded)
            or not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
            or expected_hash in seen_hashes
        ):
            raise DenseTrackError("dense frame indices or hashes are invalid")
        seen_indices.add(index)
        seen_hashes.add(expected_hash)
        timestamp = parse_utc(row.get("producer_timestamp_utc"), f"frame {index}")
        if previous_time is not None and timestamp <= previous_time:
            raise DenseTrackError("dense frame timestamps are not strictly increasing")
        previous_time = timestamp
        frame_path = canonical_retained_path(report_path, row.get("path"))
        encoded = frame_path.read_bytes()
        if len(encoded) != row.get("byte_count") or sha256_bytes(encoded) != expected_hash:
            raise DenseTrackError("dense frame byte identity mismatches report")
        frame_binding = pin_source_file(
            frame_path, expected_hash, f"dense frame {index}"
        )
        image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise DenseTrackError("dense frame is not a decodable JPEG")
        height, width = image.shape[:2]
        if [width, height] != resolution or [width, height] != [
            row.get("width"), row.get("height")
        ]:
            raise DenseTrackError("dense frame dimensions mismatch report")
        decoded.append({
            "index": index,
            "path": str(frame_path),
            "sha256": expected_hash,
            "timestamp_utc": row["producer_timestamp_utc"],
            "timestamp_epoch": timestamp.timestamp(),
            "image": image,
            "width": width,
            "height": height,
            "_source_binding": frame_binding,
        })
        frame_bindings.append(frame_binding)

    source_events = report.get("source_events")
    expected_source_events = [
        {
            "event_id": event["event_id"],
            "selected_frame_timestamp_utc": event["selected_frame_timestamp_utc"],
            "frame_sha256": event["frame"]["sha256"],
        }
        for event in verified_source_events
    ]
    if source_events != expected_source_events:
        raise DenseTrackError(
            "dense source events drift from the verified source-report denominator"
        )
    anchors = []
    event_ids = set()
    frame_index = {(row["sha256"], row["timestamp_utc"]): row for row in decoded}
    for event in source_events:
        event_id = event.get("event_id") if isinstance(event, dict) else None
        key = (
            event.get("frame_sha256") if isinstance(event, dict) else None,
            event.get("selected_frame_timestamp_utc") if isinstance(event, dict) else None,
        )
        if not isinstance(event_id, str) or not event_id or event_id in event_ids:
            raise DenseTrackError("source event IDs are invalid or duplicated")
        event_ids.add(event_id)
        if key not in frame_index:
            raise DenseTrackError("source event does not bind an exact dense frame")
        anchors.append({
            "event_id": event_id,
            "frame_sha256": key[0],
            "timestamp_utc": key[1],
            "frame_index": frame_index[key]["index"],
        })
    return report_path, raw, report, decoded, anchors, {
        "capture_report": report_binding,
        "source_event_report": source_binding,
        "frames": frame_bindings,
    }


def validated_native_bbox(value, frame, label):
    if (
        not isinstance(frame, dict)
        or not isinstance(frame.get("width"), int)
        or isinstance(frame.get("width"), bool)
        or not isinstance(frame.get("height"), int)
        or isinstance(frame.get("height"), bool)
        or frame["width"] <= 0
        or frame["height"] <= 0
        or not isinstance(value, (list, tuple))
        or len(value) != 4
        or not all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
    ):
        raise DenseTrackError(f"{label} bbox is invalid")
    x1, y1, x2, y2 = map(float, value)
    if not (
        0.0 <= x1 < x2 <= float(frame["width"])
        and 0.0 <= y1 < y2 <= float(frame["height"])
    ):
        raise DenseTrackError(f"{label} bbox is outside the frame")
    return [x1, y1, x2, y2]


def load_consensus(path, model_hash):
    consensus_path, raw, value = load_json(path, "segmentation consensus")
    consensus_binding = pin_source_file(
        consensus_path, sha256_bytes(raw), "segmentation consensus"
    )
    if value.get("schema") != CONSENSUS_SCHEMA or value.get("acceptance_eligible") is not False:
        raise DenseTrackError("segmentation consensus contract is invalid")
    inputs = value.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 2:
        raise DenseTrackError("segmentation consensus model bindings are incomplete")
    sides = ["left", "right"]
    selected_side = None
    input_paths = []
    input_evidence = []
    retained_model_hashes = set()
    for side, identity in zip(sides, inputs):
        model = identity.get("model") if isinstance(identity, dict) else None
        input_path = Path(str(identity.get("path", ""))).expanduser().resolve() if isinstance(identity, dict) else None
        model_path = (
            Path(str(model.get("path", ""))).expanduser().resolve()
            if isinstance(model, dict)
            else None
        )
        retained_model_hash = model.get("sha256") if isinstance(model, dict) else None
        if (
            not isinstance(model, dict)
            or not input_path
            or not input_path.is_file()
            or str(input_path) != identity.get("path")
            or sha256_file(input_path) != identity.get("sha256")
            or not model_path
            or not model_path.is_file()
            or str(model_path) != model.get("path")
            or not valid_sha256(retained_model_hash)
            or sha256_file(model_path) != retained_model_hash
            or retained_model_hash in retained_model_hashes
        ):
            raise DenseTrackError(
                "segmentation consensus report or model identity mismatches disk"
            )
        retained_model_hashes.add(retained_model_hash)
        input_paths.append(input_path)
        input_evidence.append({
            "report": pin_source_file(
                input_path, identity.get("sha256"),
                f"{side} segmentation proposal report",
            ),
            "model": pin_source_file(
                model_path, retained_model_hash,
                f"{side} retained segmentation model",
            ),
            "side": side,
        })
        if retained_model_hash == model_hash:
            selected_side = side
    if selected_side is None:
        raise DenseTrackError("tracking model is not one of the consensus models")
    try:
        recomputed = rebuild_segmentation_consensus(*input_paths)
    except (ConsensusError, OSError, TypeError, ValueError) as exc:
        raise DenseTrackError("segmentation consensus cannot be recomputed") from exc
    for key in (
        "schema", "acceptance_eligible", "inputs", "capture_report_sha256",
        "gate", "events", "summary", "acceptance_failures",
    ):
        if value.get(key) != recomputed.get(key):
            raise DenseTrackError(
                f"segmentation consensus {key} mismatches retained source evidence"
            )
    capture_binding = None
    for side, input_path, evidence in zip(sides, input_paths, input_evidence):
        _path, _raw, input_report = load_json(
            input_path, f"{side} segmentation proposal report"
        )
        capture = input_report.get("capture_report")
        capture_path = (
            Path(str(capture.get("path", ""))).expanduser().resolve()
            if isinstance(capture, dict)
            else None
        )
        capture_hash = capture.get("sha256") if isinstance(capture, dict) else None
        current_capture = pin_source_file(
            capture_path, capture_hash, "segmentation capture report"
        )
        if capture_binding is None:
            capture_binding = current_capture
        elif (
            current_capture["source_path"] != capture_binding["source_path"]
            or current_capture["sha256"] != capture_binding["sha256"]
        ):
            raise DenseTrackError("segmentation proposal capture bindings differ")
        masks = []
        events_value = input_report.get("events")
        if not isinstance(events_value, list):
            raise DenseTrackError("segmentation proposal event list is invalid")
        for event in events_value:
            descriptor = event.get("mask") if isinstance(event, dict) else None
            if descriptor is None:
                continue
            mask_path = (
                Path(str(descriptor.get("path", ""))).expanduser().resolve()
                if isinstance(descriptor, dict)
                else None
            )
            mask_hash = descriptor.get("sha256") if isinstance(descriptor, dict) else None
            masks.append(pin_source_file(
                mask_path, mask_hash, f"{side} segmentation proposal mask"
            ))
        evidence["masks"] = masks
    index = {}
    events = recomputed.get("events")
    if not isinstance(events, list):
        raise DenseTrackError("segmentation consensus event list is missing")
    for event in events:
        if not isinstance(event, dict):
            raise DenseTrackError("segmentation consensus event is malformed")
        event_id = event.get("event_id")
        camera_id = event.get("camera_id")
        if not isinstance(event_id, str) or camera_id not in CAMERAS:
            raise DenseTrackError("segmentation consensus event identity is invalid")
        key = (camera_id, event_id)
        if key in index:
            raise DenseTrackError("segmentation consensus events are duplicated")
        side_value = event.get(selected_side)
        instance = side_value.get("matched_instance") if isinstance(side_value, dict) else None
        frame = event.get("frame")
        if event.get("consensus") is not None and isinstance(instance, dict):
            for source_side in sides:
                source_event = event.get(source_side)
                source_instance = (
                    source_event.get("matched_instance")
                    if isinstance(source_event, dict)
                    else None
                )
                if not isinstance(source_instance, dict):
                    raise DenseTrackError(
                        f"accepted consensus lacks {source_side} matched instance"
                    )
                validated_native_bbox(
                    source_instance.get("bbox_xyxy"), frame,
                    f"accepted consensus {source_side}",
                )
            for metric in ("bbox_iou", "mask_iou"):
                metric_value = event.get(metric)
                if (
                    not isinstance(metric_value, (int, float))
                    or isinstance(metric_value, bool)
                    or not math.isfinite(float(metric_value))
                    or not 0.0 <= float(metric_value) <= 1.0
                ):
                    raise DenseTrackError(
                        f"accepted consensus {metric} is outside [0, 1]"
                    )
            index[key] = {
                "bbox_xyxy": validated_native_bbox(
                    instance.get("bbox_xyxy"), frame, "selected consensus model"
                ),
                "frame_sha256": frame.get("encoded_jpeg_sha256") if isinstance(frame, dict) else None,
                "consensus_pixel": event["consensus"].get("pixel"),
                "mask_iou": event.get("mask_iou"),
            }
    if capture_binding is None:
        raise DenseTrackError("segmentation capture binding is missing")
    return consensus_path, raw, value, index, selected_side, {
        "consensus": consensus_binding,
        "capture_report": capture_binding,
        "inputs": input_evidence,
    }


def snapshot_consensus_evidence(staged_root, evidence):
    bindings = [evidence["consensus"], evidence["capture_report"]]
    published_inputs = []
    for input_evidence in evidence["inputs"]:
        bindings.extend([input_evidence["report"], input_evidence["model"]])
        bindings.extend(input_evidence["masks"])
        published_inputs.append({
            "side": input_evidence["side"],
            "report": snapshot_source_file(
                staged_root, input_evidence["report"], "reports", ".json",
                f"{input_evidence['side']} segmentation proposal report",
            ),
            "model": snapshot_source_file(
                staged_root, input_evidence["model"], "models", ".pt",
                f"{input_evidence['side']} segmentation model",
            ),
            "masks": [
                snapshot_source_file(
                    staged_root, binding, "consensus-masks", ".png",
                    f"{input_evidence['side']} segmentation proposal mask",
                )
                for binding in input_evidence["masks"]
            ],
        })
    return {
        "consensus": snapshot_source_file(
            staged_root, evidence["consensus"], "reports", ".json",
            "segmentation consensus",
        ),
        "capture_report": snapshot_source_file(
            staged_root, evidence["capture_report"], "reports", ".json",
            "segmentation capture report",
        ),
        "inputs": published_inputs,
    }, bindings


def prepare_dense_window(
    report_value, staged_root, expected_source_report_sha256=None
):
    loaded = load_dense_report(
        report_value,
        expected_source_report_sha256=expected_source_report_sha256,
    )
    report_path, report_raw, report, frames, anchors, evidence = loaded
    capture_snapshot = snapshot_source_file(
        staged_root, evidence["capture_report"], "reports", ".json",
        "dense capture report",
    )
    source_snapshot = snapshot_source_file(
        staged_root, evidence["source_event_report"], "reports", ".json",
        "dense source-event report",
    )
    frame_snapshots = []
    for frame, binding in zip(frames, evidence["frames"]):
        snapshot = snapshot_source_file(
            staged_root, binding, "frames", ".jpg",
            f"dense frame {frame['index']}",
        )
        frame["_snapshot"] = snapshot
        frame_snapshots.append({
            "index": frame["index"],
            "producer_timestamp_utc": frame["timestamp_utc"],
            "width": frame["width"],
            "height": frame["height"],
            **snapshot,
        })
    return {
        "loaded": (report_path, report_raw, report, frames, anchors),
        "capture_snapshot": capture_snapshot,
        "source_snapshot": source_snapshot,
        "frame_snapshots": frame_snapshots,
        "bindings": [
            evidence["capture_report"],
            evidence["source_event_report"],
            *evidence["frames"],
        ],
    }


def exact_numeric_scalars(value, expected_count, label):
    try:
        if hasattr(value, "detach"):
            value = value.detach().cpu().numpy()
        array = np.asarray(value)
        if array.dtype.kind not in "iuf":
            raise DenseTrackError(f"{label} must contain only numeric scalars")
        if array.size != expected_count:
            raise DenseTrackError(
                f"{label} must contain exactly {expected_count} scalar values"
            )
        scalars = [float(item) for item in array.reshape(-1)]
    except DenseTrackError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise DenseTrackError(f"{label} is malformed") from exc
    if not all(math.isfinite(item) for item in scalars):
        raise DenseTrackError(f"{label} contains a non-finite scalar")
    return scalars


def model_instances_from_result(result, image):
    if result.boxes is None or len(result.boxes) == 0:
        return []
    if result.masks is None:
        raise DenseTrackError("tracking model returned boxes without masks")
    ids = result.boxes.id
    try:
        masks = np.asarray(result.masks.data.detach().cpu().numpy())
    except (TypeError, ValueError) as exc:
        raise DenseTrackError("tracking masks are malformed") from exc
    height, width = image.shape[:2]
    box_count = len(result.boxes)
    boxes = list(result.boxes)
    if len(boxes) != box_count:
        raise DenseTrackError("tracking boxes have inconsistent cardinality")
    if masks.ndim != 3 or masks.shape[0] != box_count:
        raise DenseTrackError("tracking masks are not ordered one-for-one with boxes")
    if masks.dtype.kind not in "iuf":
        raise DenseTrackError("tracking masks must contain only numeric scalars")
    if not np.isfinite(masks).all() or np.any(masks < 0.0) or np.any(masks > 1.0):
        raise DenseTrackError("tracking masks contain values outside [0, 1]")
    id_values = (
        None
        if ids is None
        else exact_numeric_scalars(ids, box_count, "tracking IDs")
    )
    if id_values is not None and any(
        not value.is_integer() or value < 0.0 for value in id_values
    ):
        raise DenseTrackError("tracking model returned a non-integral tracker ID")
    instances = []
    for index, box in enumerate(boxes):
        class_value = exact_numeric_scalars(
            box.cls, 1, "tracking class ID"
        )[0]
        if not class_value.is_integer() or class_value < 0.0:
            raise DenseTrackError("tracking model returned an invalid class ID")
        class_id = int(class_value)
        try:
            label_value = result.names[class_id]
        except (IndexError, KeyError, TypeError) as exc:
            raise DenseTrackError("tracking model class ID has no label") from exc
        if not isinstance(label_value, str) or not label_value:
            raise DenseTrackError("tracking model class label is invalid")
        label = label_value
        confidence = exact_numeric_scalars(
            box.conf, 1, "tracking confidence"
        )[0]
        if not 0.0 <= confidence <= 1.0:
            raise DenseTrackError("tracking model returned an invalid confidence")
        if ids is None:
            continue
        track_id_value = id_values[index]
        if label not in VEHICLE_LABELS:
            continue
        mask = masks[index]
        if mask.ndim != 2 or not np.isfinite(mask).all():
            raise DenseTrackError("tracking model returned an invalid mask plane")
        if mask.shape != (height, width):
            mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
        bbox = exact_numeric_scalars(box.xyxy, 4, "tracking bbox")
        try:
            validate_bbox(bbox, width, height)
        except (ContactProposalError, TypeError, ValueError) as exc:
            raise DenseTrackError("tracking model returned an invalid bbox") from exc
        x1, y1, x2, y2 = bbox
        ix1, iy1 = max(0, int(math.floor(x1))), max(0, int(math.floor(y1)))
        ix2, iy2 = min(width, int(math.ceil(x2))), min(height, int(math.ceil(y2)))
        clean_mask = np.zeros((height, width), dtype=bool)
        if ix2 > ix1 and iy2 > iy1:
            clean_mask[iy1:iy2, ix1:ix2] = largest_component(
                (mask[iy1:iy2, ix1:ix2] >= 0.5).astype(np.uint8)
            ).astype(bool)
        proposal = None
        reasons = []
        if touches_boundary(bbox, width, height) or not has_visibility_margin(
            bbox, width, height
        ):
            reasons.append("vehicle_instance_touches_frame_boundary")
        else:
            try:
                proposal = estimate_contact(clean_mask, bbox)
            except ContactProposalError as exc:
                reasons.append(str(exc).replace(" ", "_"))
        instances.append({
            "track_id": int(track_id_value),
            "label": label,
            "confidence": confidence,
            "bbox_xyxy": bbox,
            "mask": clean_mask,
            "ground_contact_proposal": proposal,
            "rejection_reasons": reasons,
        })
    return sorted(instances, key=lambda item: (item["track_id"], -item["confidence"]))


def select_target_track(tracked_frames, anchors, consensus_index, camera_id):
    matches = []
    reasons = []
    fatal_identity_ambiguity = False
    for anchor in anchors:
        truth = consensus_index.get((camera_id, anchor["event_id"]))
        if truth is None:
            continue
        if truth.get("frame_sha256") != anchor["frame_sha256"]:
            raise DenseTrackError("consensus anchor frame hash mismatches dense frame")
        candidates = tracked_frames[anchor["frame_index"]]["instances"]
        scored = sorted(
            ((bbox_iou(truth["bbox_xyxy"], item["bbox_xyxy"]), item) for item in candidates),
            key=lambda value: (value[0], value[1]["confidence"]),
            reverse=True,
        )
        if not scored or scored[0][0] < MINIMUM_ANCHOR_IOU:
            reasons.append(f"anchor_unmatched:{anchor['event_id']}")
            continue
        if len(scored) > 1:
            best_iou, runner_up_iou = scored[0][0], scored[1][0]
            if (
                runner_up_iou >= MINIMUM_ANCHOR_IOU
                or (
                    runner_up_iou >= MINIMUM_PLAUSIBLE_RUNNER_UP_IOU
                    and best_iou - runner_up_iou < MINIMUM_ANCHOR_IOU_MARGIN
                )
            ):
                reasons.append(f"anchor_ambiguous:{anchor['event_id']}")
                fatal_identity_ambiguity = True
                continue
        score, instance = scored[0]
        matches.append({
            **anchor,
            "track_id": instance["track_id"],
            "bbox_iou": score,
            "model_bbox_xyxy": instance["bbox_xyxy"],
            "consensus_bbox_xyxy": truth["bbox_xyxy"],
            "consensus_mask_iou": truth["mask_iou"],
        })
    track_ids = sorted({item["track_id"] for item in matches})
    frame_keys = {
        (item["frame_sha256"], item["timestamp_utc"]) for item in matches
    }
    distinct_hashes = {item["frame_sha256"] for item in matches}
    distinct_timestamps = {item["timestamp_utc"] for item in matches}
    if len(frame_keys) != len(matches):
        reasons.append("duplicate_anchor_source_frame")
        fatal_identity_ambiguity = True
    if not matches:
        reasons.append("no_cross_model_anchor_matched")
        target = None
    elif len(track_ids) != 1:
        reasons.append("anchor_tracker_identity_conflict")
        target = None
    elif fatal_identity_ambiguity:
        target = None
    elif (
        len(frame_keys) < MINIMUM_DISTINCT_ANCHOR_FRAMES
        or len(distinct_hashes) < MINIMUM_DISTINCT_ANCHOR_FRAMES
        or len(distinct_timestamps) < MINIMUM_DISTINCT_ANCHOR_FRAMES
    ):
        reasons.append("fewer_than_two_distinct_cross_model_anchor_frames")
        target = None
    else:
        anchor_times = sorted(
            parse_utc(value, "anchor timestamp").timestamp()
            for value in distinct_timestamps
        )
        if (anchor_times[-1] - anchor_times[0]) * 1000.0 < MINIMUM_ANCHOR_SPAN_MS:
            reasons.append("cross_model_anchor_span_below_200ms")
            target = None
        else:
            target = track_ids[0]
    return target, matches, reasons


def dense_sequence_identity(
    report_path, report_raw, report, model_sha256, consensus_sha256,
    capture_snapshot=None, source_snapshot=None,
):
    identity = {
        "camera_id": report["camera_id"],
        "model_object_id": report["object_id"],
        "capture_report_path": str(Path(report_path).resolve()),
        "capture_report_sha256": sha256_bytes(report_raw),
        "capture_report_snapshot_path": (
            capture_snapshot.get("path") if isinstance(capture_snapshot, dict) else None
        ),
        "source_event_report_path": report["source_event_report"]["path"],
        "source_event_report_sha256": report["source_event_report"]["sha256"],
        "source_event_report_snapshot_path": (
            source_snapshot.get("path") if isinstance(source_snapshot, dict) else None
        ),
        "tracking_model_sha256": model_sha256,
        "segmentation_consensus_sha256": consensus_sha256,
    }
    digest = canonical_json_sha256(identity)
    return f"{report['camera_id']}-{digest}", digest, identity


def finite_vector(value, size):
    return (
        isinstance(value, (list, tuple))
        and len(value) == size
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
    )


def validated_contact_state(instance, frame, row):
    proposal = instance.get("ground_contact_proposal")
    mask = np.asarray(instance.get("mask"))
    bbox = instance.get("bbox_xyxy")
    if not isinstance(proposal, dict):
        raise DenseTrackError("contact_proposal_missing")
    if proposal.get("method") != "segmentation_visible_support_midpoint_proposal":
        raise DenseTrackError("contact_proposal_method_invalid")
    if proposal.get("reviewed") is not False:
        raise DenseTrackError("contact_proposal_review_state_invalid")
    if mask.shape != (frame["height"], frame["width"]) or mask.dtype != np.bool_:
        raise DenseTrackError("contact_mask_shape_invalid")
    if not mask.any():
        raise DenseTrackError("contact_mask_empty")
    try:
        checked_bbox = validate_bbox(bbox, frame["width"], frame["height"])
    except (ContactProposalError, TypeError, ValueError) as exc:
        raise DenseTrackError("contact_bbox_invalid") from exc
    pixel = proposal.get("pixel")
    endpoints = proposal.get("support_endpoints")
    if not finite_vector(pixel, 2):
        raise DenseTrackError("contact_proposal_pixel_invalid")
    if (
        not isinstance(endpoints, (list, tuple))
        or len(endpoints) != 2
        or not all(finite_vector(endpoint, 2) for endpoint in endpoints)
    ):
        raise DenseTrackError("contact_proposal_support_invalid")
    pixel_array = np.asarray(pixel, dtype=float)
    endpoints_array = np.asarray(endpoints, dtype=float)
    x1, y1, x2, y2 = checked_bbox
    if not (
        x1 <= pixel_array[0] <= x2
        and y1 <= pixel_array[1] <= y2
        and np.all(endpoints_array[:, 0] >= x1)
        and np.all(endpoints_array[:, 0] <= x2)
        and np.all(endpoints_array[:, 1] >= y1)
        and np.all(endpoints_array[:, 1] <= y2)
    ):
        raise DenseTrackError("contact_proposal_outside_bbox")
    try:
        covariance = np.asarray(proposal.get("covariance_px2"), dtype=float)
    except (TypeError, ValueError) as exc:
        raise DenseTrackError("contact_proposal_covariance_invalid") from exc
    if (
        covariance.shape != (2, 2)
        or not np.isfinite(covariance).all()
        or not np.allclose(covariance, covariance.T, atol=1e-9)
        or float(np.linalg.eigvalsh(covariance).min()) < -1e-9
    ):
        raise DenseTrackError("contact_proposal_covariance_invalid")
    mask_area = int(mask.sum())
    if proposal.get("mask_area_px") != mask_area:
        raise DenseTrackError("contact_proposal_mask_area_mismatch")
    for key in ("mask_fraction_of_bbox", "support_span_fraction_of_bbox", "support_quantile"):
        value = proposal.get(key)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or not 0.0 < float(value) <= 1.0
        ):
            raise DenseTrackError(f"contact_proposal_{key}_invalid")
    support_count = proposal.get("support_column_count")
    if (
        not isinstance(support_count, int)
        or isinstance(support_count, bool)
        or support_count <= 0
    ):
        raise DenseTrackError("contact_proposal_support_count_invalid")
    y_coordinates, x_coordinates = np.nonzero(mask)
    mask_centroid = np.asarray(
        [float(np.mean(x_coordinates)), float(np.mean(y_coordinates))], dtype=float
    )
    bbox_center = np.asarray([(x1 + x2) / 2.0, (y1 + y2) / 2.0], dtype=float)
    return {
        "frame_index": frame["index"],
        "timestamp_epoch": frame["timestamp_epoch"],
        "contact": pixel_array,
        "bbox_center": bbox_center,
        "mask_centroid": mask_centroid,
        "row": row,
    }


def contact_temporal_gate(width, height):
    scale = max(float(width) / REFERENCE_WIDTH, float(height) / REFERENCE_HEIGHT)
    scale = max(scale, 0.25)
    return {
        "reference_canvas": {"width": int(REFERENCE_WIDTH), "height": int(REFERENCE_HEIGHT)},
        "native_scale": scale,
        "maximum_residual_jump_px": (
            MAXIMUM_CONTACT_RESIDUAL_JUMP_AT_REFERENCE_PX * scale
        ),
        "maximum_residual_speed_px_per_second": (
            MAXIMUM_CONTACT_RESIDUAL_SPEED_AT_REFERENCE_PX_PER_SECOND * scale
        ),
        "maximum_residual_acceleration_px_per_second2": (
            MAXIMUM_CONTACT_RESIDUAL_ACCELERATION_AT_REFERENCE_PX_PER_SECOND2 * scale
        ),
        "motion_references": ["bbox_center", "segmentation_mask_centroid"],
    }


def evaluate_contact_temporal_consistency(states, width, height):
    gate = contact_temporal_gate(width, height)
    diagnostics = []
    sequence_reasons = []
    previous_velocity = None
    previous_dt = None
    for left, right in zip(states, states[1:]):
        dt = right["timestamp_epoch"] - left["timestamp_epoch"]
        pair_reasons = []
        consecutive_frames = right["frame_index"] == left["frame_index"] + 1
        if not consecutive_frames:
            pair_reasons.append("contact_temporal_input_frame_gap")
            previous_velocity = None
            previous_dt = None
        if not math.isfinite(dt) or dt <= 0.0:
            pair_reasons.append("contact_temporal_timestamp_invalid")
            dt = math.nan
        contact_delta = right["contact"] - left["contact"]
        bbox_residual = contact_delta - (
            right["bbox_center"] - left["bbox_center"]
        )
        mask_residual = contact_delta - (
            right["mask_centroid"] - left["mask_centroid"]
        )
        bbox_jump = float(np.linalg.norm(bbox_residual))
        mask_jump = float(np.linalg.norm(mask_residual))
        bbox_speed = mask_speed = math.inf
        current_velocity = None
        if math.isfinite(dt) and consecutive_frames:
            bbox_velocity = bbox_residual / dt
            mask_velocity = mask_residual / dt
            bbox_speed = float(np.linalg.norm(bbox_velocity))
            mask_speed = float(np.linalg.norm(mask_velocity))
            current_velocity = (bbox_velocity, mask_velocity)
        if bbox_jump > gate["maximum_residual_jump_px"]:
            pair_reasons.append("contact_bbox_residual_jump_above_gate")
        if mask_jump > gate["maximum_residual_jump_px"]:
            pair_reasons.append("contact_mask_residual_jump_above_gate")
        if bbox_speed > gate["maximum_residual_speed_px_per_second"]:
            pair_reasons.append("contact_bbox_residual_speed_above_gate")
        if mask_speed > gate["maximum_residual_speed_px_per_second"]:
            pair_reasons.append("contact_mask_residual_speed_above_gate")
        bbox_acceleration = mask_acceleration = None
        if (
            current_velocity is not None
            and previous_velocity is not None
            and previous_dt is not None
        ):
            acceleration_dt = (dt + previous_dt) / 2.0
            bbox_acceleration = float(
                np.linalg.norm(current_velocity[0] - previous_velocity[0])
                / acceleration_dt
            )
            mask_acceleration = float(
                np.linalg.norm(current_velocity[1] - previous_velocity[1])
                / acceleration_dt
            )
            if (
                bbox_acceleration
                > gate["maximum_residual_acceleration_px_per_second2"]
            ):
                pair_reasons.append(
                    "contact_bbox_residual_acceleration_above_gate"
                )
            if (
                mask_acceleration
                > gate["maximum_residual_acceleration_px_per_second2"]
            ):
                pair_reasons.append(
                    "contact_mask_residual_acceleration_above_gate"
                )
        if current_velocity is not None:
            previous_velocity = current_velocity
            previous_dt = dt
        pair_reasons = sorted(set(pair_reasons))
        if pair_reasons:
            right["row"]["rejection_reasons"] = sorted(set(
                right["row"]["rejection_reasons"] + pair_reasons
            ))
            sequence_reasons.extend(pair_reasons)
        diagnostics.append({
            "left_frame_index": left["frame_index"],
            "right_frame_index": right["frame_index"],
            "delta_time_ms": None if not math.isfinite(dt) else dt * 1000.0,
            "contact_motion_px": float(np.linalg.norm(contact_delta)),
            "bbox_center_residual_jump_px": bbox_jump,
            "mask_centroid_residual_jump_px": mask_jump,
            "bbox_center_residual_speed_px_per_second": (
                None if not math.isfinite(bbox_speed) else bbox_speed
            ),
            "mask_centroid_residual_speed_px_per_second": (
                None if not math.isfinite(mask_speed) else mask_speed
            ),
            "bbox_center_residual_acceleration_px_per_second2": bbox_acceleration,
            "mask_centroid_residual_acceleration_px_per_second2": mask_acceleration,
            "rejection_reasons": pair_reasons,
        })
    return diagnostics, sorted(set(sequence_reasons)), gate


def verify_output_artifacts(staged_root, sequences):
    for sequence in sequences:
        descriptors = [sequence.get("review_sheet")]
        descriptors.extend(frame.get("mask") for frame in sequence.get("frames", []))
        for descriptor in descriptors:
            if descriptor is None:
                continue
            relative = descriptor.get("path") if isinstance(descriptor, dict) else None
            if not isinstance(relative, str) or Path(relative).is_absolute():
                raise DenseTrackError("dense-track output artifact path is invalid")
            path = (staged_root / relative).resolve()
            try:
                path.relative_to(staged_root.resolve())
            except ValueError as exc:
                raise DenseTrackError("dense-track output artifact path escapes stage") from exc
            if not path.is_file() or sha256_file(path) != descriptor.get("sha256"):
                raise DenseTrackError("dense-track output artifact hash binding failed")


def verify_staged_publication(
    staged_root, bound_snapshot_tree, sequences, expected_report_sha256
):
    staged_root = Path(staged_root).resolve()
    verify_bound_input_snapshots(staged_root, bound_snapshot_tree)
    verify_output_artifacts(staged_root, sequences)
    report_path = staged_root / "report.json"
    if (
        not valid_sha256(expected_report_sha256)
        or not report_path.is_file()
        or sha256_file(report_path) != expected_report_sha256
    ):
        raise DenseTrackError("dense-track report changed before publication")

    expected_paths = {"report.json"}

    def collect_bound_paths(item):
        if isinstance(item, dict):
            if {"source_path", "path", "sha256", "byte_count"} <= set(item):
                expected_paths.add(item["path"])
            for nested in item.values():
                collect_bound_paths(nested)
        elif isinstance(item, list):
            for nested in item:
                collect_bound_paths(nested)

    collect_bound_paths(bound_snapshot_tree)
    for sequence in sequences:
        descriptors = [sequence.get("review_sheet")]
        descriptors.extend(frame.get("mask") for frame in sequence.get("frames", []))
        expected_paths.update(
            descriptor["path"] for descriptor in descriptors if descriptor is not None
        )

    actual_paths = {
        path.relative_to(staged_root).as_posix()
        for path in staged_root.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    if actual_paths != expected_paths:
        raise DenseTrackError("dense-track staging tree contains unexpected artifacts")

    manifest_path = staged_root / "SHA256SUMS"
    try:
        lines = manifest_path.read_text().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise DenseTrackError("dense-track checksum manifest is unreadable") from exc
    manifest = {}
    for line in lines:
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise DenseTrackError("dense-track checksum manifest is malformed")
        digest, relative = parts
        if (
            not valid_sha256(digest)
            or not relative
            or Path(relative).is_absolute()
            or relative in manifest
        ):
            raise DenseTrackError("dense-track checksum manifest is malformed")
        path = (staged_root / relative).resolve()
        try:
            path.relative_to(staged_root)
        except ValueError as exc:
            raise DenseTrackError("dense-track checksum path escapes staging") from exc
        manifest[relative] = digest
    if set(manifest) != expected_paths:
        raise DenseTrackError("dense-track checksum manifest is incomplete")
    if any(
        not (staged_root / relative).is_file()
        or sha256_file(staged_root / relative) != digest
        for relative, digest in manifest.items()
    ):
        raise DenseTrackError("dense-track checksum manifest verification failed")


def stable_file_fields(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def pin_publication_tree(root):
    """Pin every staged inode and byte digest across the directory rename."""
    root = Path(root)
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise DenseTrackError("dense-track staging root cannot be pinned") from exc
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise DenseTrackError("dense-track staging root is not a directory")
    records = {}
    directories = {}
    try:
        candidates = sorted(root.rglob("*"))
    except OSError as exc:
        raise DenseTrackError("dense-track staging tree cannot be enumerated") from exc
    for path in candidates:
        try:
            before = path.lstat()
        except OSError as exc:
            raise DenseTrackError("dense-track staging entry cannot be pinned") from exc
        if stat.S_ISDIR(before.st_mode):
            directories[path.relative_to(root).as_posix()] = stable_file_fields(before)
            continue
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise DenseTrackError(
                "dense-track staging tree contains a non-regular or linked artifact"
            )
        relative = path.relative_to(root).as_posix()
        try:
            digest = sha256_file(path)
            after = path.lstat()
        except OSError as exc:
            raise DenseTrackError("dense-track staging artifact cannot be pinned") from exc
        if stable_file_fields(before) != stable_file_fields(after):
            raise DenseTrackError("dense-track staging artifact changed while pinned")
        records[relative] = {
            "fields": stable_file_fields(after),
            "sha256": digest,
        }
    if "report.json" not in records or "SHA256SUMS" not in records:
        raise DenseTrackError("dense-track staging publication contract is incomplete")
    return {
        "parent_identity": (
            root_metadata.st_dev,
            root.parent.lstat().st_ino,
        ),
        "root_identity": (root_metadata.st_dev, root_metadata.st_ino),
        "directories": directories,
        "files": records,
    }


def verify_published_tree(root, contract):
    """Reopen the caller-visible tree and prove it is the pinned staged tree."""
    root = Path(root)
    try:
        root_before = root.lstat()
    except OSError as exc:
        raise DenseTrackError("dense-track publication is unavailable") from exc
    if (
        not stat.S_ISDIR(root_before.st_mode)
        or (root_before.st_dev, root_before.st_ino) != contract["root_identity"]
    ):
        raise DenseTrackError("dense-track publication root was replaced")
    try:
        candidates = sorted(root.rglob("*"))
    except OSError as exc:
        raise DenseTrackError("dense-track publication cannot be enumerated") from exc
    actual_paths = set()
    actual_directories = set()
    for path in candidates:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise DenseTrackError("dense-track published entry is unavailable") from exc
        if stat.S_ISDIR(metadata.st_mode):
            relative = path.relative_to(root).as_posix()
            actual_directories.add(relative)
            if stable_file_fields(metadata) != contract["directories"].get(relative):
                raise DenseTrackError("dense-track directory changed during publication")
            continue
        relative = path.relative_to(root).as_posix()
        actual_paths.add(relative)
        record = contract["files"].get(relative)
        if (
            record is None
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stable_file_fields(metadata) != record["fields"]
        ):
            raise DenseTrackError("dense-track artifact changed during publication")
        try:
            digest = sha256_file(path)
            after = path.lstat()
        except OSError as exc:
            raise DenseTrackError("dense-track published artifact is unreadable") from exc
        if (
            stable_file_fields(metadata) != stable_file_fields(after)
            or digest != record["sha256"]
        ):
            raise DenseTrackError("dense-track artifact changed during publication")
    if actual_paths != set(contract["files"]):
        raise DenseTrackError("dense-track publication tree changed during publication")
    if actual_directories != set(contract["directories"]):
        raise DenseTrackError("dense-track directory tree changed during publication")
    try:
        root_after = root.lstat()
        final_entries = {
            path.relative_to(root).as_posix(): stable_file_fields(path.lstat())
            for path in root.rglob("*")
        }
    except OSError as exc:
        raise DenseTrackError("dense-track publication changed during final verification") from exc
    if (
        (root_after.st_dev, root_after.st_ino) != contract["root_identity"]
        or set(final_entries)
        != set(contract["files"]) | set(contract["directories"])
        or any(
            final_entries[relative] != record["fields"]
            for relative, record in contract["files"].items()
        )
        or any(
            final_entries[relative] != fields
            for relative, fields in contract["directories"].items()
        )
    ):
        raise DenseTrackError("dense-track publication changed during final verification")


def rename_noreplace_at(directory_fd, source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise DenseTrackError("atomic dense-track quarantine is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.ENOENT:
        raise FileNotFoundError(error, os.strerror(error), source)
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination)
    raise OSError(error, os.strerror(error), source)


def remove_failed_publication(output, contract):
    """Atomically quarantine only this invocation's exact published inode.

    POSIX has no conditional rmdir-by-inode operation.  Deleting through a
    pathname after an identity check could therefore remove a foreign swap.
    Keep an owned failed tree in a private, mode-700 quarantine instead.  A
    later offline cleaner can remove it after proving exclusive ownership.
    """
    output = Path(output)
    parent = output.parent
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = None
    quarantine_name = None
    try:
        directory_fd = os.open(parent, flags)
        parent_metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or (parent_metadata.st_dev, parent_metadata.st_ino)
            != contract["parent_identity"]
        ):
            raise DenseTrackError(
                "failed dense-track publication parent was replaced"
            )
        for _attempt in range(100):
            candidate = f".{output.name}.failed-{uuid.uuid4().hex}"
            try:
                rename_noreplace_at(directory_fd, output.name, candidate)
                quarantine_name = candidate
                break
            except FileExistsError:
                continue
            except FileNotFoundError:
                return None
        if quarantine_name is None:
            raise DenseTrackError("dense-track quarantine name space is exhausted")
        quarantined = os.stat(
            quarantine_name, dir_fd=directory_fd, follow_symlinks=False
        )
        quarantined_identity = (quarantined.st_dev, quarantined.st_ino)
        if quarantined_identity != contract["root_identity"]:
            # A foreign path won the race before the atomic rename.  Restore it
            # without replacement; if another path now occupies the output
            # name, retain both foreign objects rather than deleting either.
            try:
                rename_noreplace_at(directory_fd, quarantine_name, output.name)
                quarantine_name = None
            except FileExistsError:
                pass
            os.fsync(directory_fd)
            return None
        quarantine_fd = os.open(
            quarantine_name,
            flags,
            dir_fd=directory_fd,
        )
        try:
            pinned = os.fstat(quarantine_fd)
            if (pinned.st_dev, pinned.st_ino) != contract["root_identity"]:
                raise DenseTrackError("owned dense-track quarantine was replaced")
            os.fchmod(quarantine_fd, stat.S_IRWXU)
            os.fsync(quarantine_fd)
        finally:
            os.close(quarantine_fd)
        os.fsync(directory_fd)
        return parent / quarantine_name
    except OSError as exc:
        raise DenseTrackError("failed dense-track publication cannot be quarantined") from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def publish_staged_tree(staged, output):
    contract = pin_publication_tree(staged)
    try:
        atomic_publish_directory(staged, output)
        verify_published_tree(output, contract)
        fsync_directory(Path(output).parent)
        verify_published_tree(output, contract)
        with PUBLICATION_CONTRACTS_LOCK:
            identity = contract["root_identity"]
            if identity in PUBLICATION_CONTRACTS:
                raise DenseTrackError(
                    "dense-track publication contract identity is duplicated"
                )
            PUBLICATION_CONTRACTS[identity] = contract
    except (DenseTrackError, StaticCaptureError, OSError) as exc:
        remove_failed_publication(output, contract)
        if isinstance(exc, StaticCaptureError):
            raise DenseTrackError("atomic dense-track publication failed") from exc
        if isinstance(exc, OSError):
            raise DenseTrackError(
                "dense-track publication durability verification failed"
            ) from exc
        raise


def make_review_sheet(entries, columns=4, tile_width=480, tile_height=390):
    rows = max(1, math.ceil(len(entries) / columns))
    sheet = np.full((rows * tile_height, columns * tile_width, 3), 28, dtype=np.uint8)
    for position, (label, encoded) in enumerate(entries):
        image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
        available_height = tile_height - 30
        scale = min(tile_width / image.shape[1], available_height / image.shape[0])
        resized = cv2.resize(
            image,
            (max(1, int(image.shape[1] * scale)), max(1, int(image.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
        row, column = divmod(position, columns)
        x = column * tile_width + (tile_width - resized.shape[1]) // 2
        y = row * tile_height + 30 + (available_height - resized.shape[0]) // 2
        sheet[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
        cv2.putText(sheet, label, (column * tile_width + 6, row * tile_height + 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.50, (240, 240, 240), 1, cv2.LINE_AA)
    ok, encoded = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise DenseTrackError("failed to encode dense track review sheet")
    return encoded.tobytes()


def process_window(report_value, consensus_index, model_path, staged_root, model_factory,
                   confidence, iou_threshold, image_size, device,
                   expected_source_report_sha256=None, model_sha256=None,
                   consensus_sha256=None, prepared_window=None):
    if prepared_window is None:
        prepared_window = prepare_dense_window(
            report_value, staged_root,
            expected_source_report_sha256=expected_source_report_sha256,
        )
    report_path, report_raw, report, frames, anchors = prepared_window["loaded"]
    camera_id = report["camera_id"]
    if not valid_sha256(model_sha256) or not valid_sha256(consensus_sha256):
        raise DenseTrackError("dense sequence model or consensus binding is invalid")
    sequence_id, sequence_identity_sha256, sequence_identity = dense_sequence_identity(
        report_path, report_raw, report, model_sha256, consensus_sha256,
        prepared_window["capture_snapshot"], prepared_window["source_snapshot"],
    )
    model = model_factory(str(model_path))
    tracked = []
    for frame in frames:
        result = model.track(
            frame["image"], persist=True, tracker="bytetrack.yaml",
            conf=confidence, iou=iou_threshold, imgsz=image_size, device=device,
            retina_masks=True, verbose=False,
        )[0]
        tracked.append({
            "frame": frame,
            "instances": model_instances_from_result(result, frame["image"]),
        })
    target_id, anchor_matches, rejection_reasons = select_target_track(
        tracked, anchors, consensus_index, camera_id
    )
    selected_rows = []
    review_candidates = []
    temporal_states = []
    if target_id is not None:
        for item in tracked:
            frame = item["frame"]
            candidates = [value for value in item["instances"] if value["track_id"] == target_id]
            if len(candidates) > 1:
                raise DenseTrackError("tracker emitted duplicate target IDs in one frame")
            if not candidates:
                continue
            instance = candidates[0]
            proposal = instance["ground_contact_proposal"]
            frame_reasons = list(instance["rejection_reasons"])
            mask_descriptor = None
            row = {
                "frame_index": frame["index"],
                "producer_timestamp_utc": frame["timestamp_utc"],
                "frame_sha256": frame["sha256"],
                "frame_snapshot": frame["_snapshot"],
                "track_id": target_id,
                "label": instance["label"],
                "confidence": instance["confidence"],
                "bbox_xyxy": instance["bbox_xyxy"],
                "ground_contact_proposal": proposal,
                "mask": None,
                "rejection_reasons": frame_reasons,
                "acceptance_eligible": False,
            }
            if proposal is not None:
                try:
                    state = validated_contact_state(instance, frame, row)
                except DenseTrackError as exc:
                    proposal = None
                    row["ground_contact_proposal"] = None
                    frame_reasons.append(str(exc))
                else:
                    mask_relative = (
                        Path("masks") / sequence_id / f"frame-{frame['index']:03d}.png"
                    )
                    mask_path = staged_root / mask_relative
                    ok, encoded = cv2.imencode(
                        ".png", instance["mask"].astype(np.uint8) * 255,
                        [cv2.IMWRITE_PNG_COMPRESSION, 4],
                    )
                    if not ok:
                        raise DenseTrackError("failed to encode dense target mask")
                    write_bytes_exclusive(
                        mask_path, encoded.tobytes(), "dense target mask"
                    )
                    mask_descriptor = {
                        "path": mask_relative.as_posix(),
                        "sha256": sha256_file(mask_path),
                    }
                    row["mask"] = mask_descriptor
                    overlay = draw_overlay(
                        frame["image"], instance["bbox_xyxy"], instance["mask"], proposal
                    )
                    review_candidates.append((frame, overlay))
                    temporal_states.append(state)
            if proposal is None:
                frame_reasons.append("target_frame_has_no_valid_contact_proposal")
            row["rejection_reasons"] = sorted(set(frame_reasons))
            selected_rows.append(row)
    coverage = len(selected_rows) / len(frames)
    timestamps = [parse_utc(row["producer_timestamp_utc"], "selected frame").timestamp()
                  for row in selected_rows]
    maximum_gap_ms = max(
        ((right - left) * 1000.0 for left, right in zip(timestamps, timestamps[1:])),
        default=None,
    )
    if target_id is not None and coverage < 0.75:
        rejection_reasons.append("target_track_coverage_below_75_percent")
    if target_id is not None and coverage < 1.0:
        rejection_reasons.append("target_track_not_present_in_every_frame")
    if target_id is not None and maximum_gap_ms is not None and maximum_gap_ms > 1000.0:
        rejection_reasons.append("target_track_gap_above_1000ms")
    distinct_anchor_frames = {
        (item["frame_sha256"], item["timestamp_utc"]) for item in anchor_matches
    }
    if target_id is not None and len(distinct_anchor_frames) < 2:
        rejection_reasons.append("fewer_than_two_cross_model_anchors")
    contact_count = sum(
        row["ground_contact_proposal"] is not None and row["mask"] is not None
        for row in selected_rows
    )
    if target_id is not None and contact_count != len(frames):
        rejection_reasons.append("not_every_input_frame_has_a_valid_contact_and_mask")
    for row in selected_rows:
        rejection_reasons.extend(row["rejection_reasons"])

    temporal_diagnostics = []
    temporal_gate = contact_temporal_gate(frames[0]["width"], frames[0]["height"])
    if temporal_states:
        (
            temporal_diagnostics,
            temporal_reasons,
            temporal_gate,
        ) = evaluate_contact_temporal_consistency(
            temporal_states, frames[0]["width"], frames[0]["height"]
        )
        rejection_reasons.extend(temporal_reasons)

    review_descriptor = None
    if review_candidates:
        chosen_indices = np.linspace(
            0, len(review_candidates) - 1, min(12, len(review_candidates)), dtype=int
        )
        entries = []
        for index in sorted(set(int(value) for value in chosen_indices)):
            frame, overlay = review_candidates[index]
            entries.append((f"{camera_id} f{frame['index']} {frame['timestamp_utc']}", overlay))
        review_relative = Path("review-sheets") / f"{sequence_id}.jpg"
        review_path = staged_root / review_relative
        write_bytes_exclusive(
            review_path, make_review_sheet(entries), "dense track review sheet"
        )
        review_descriptor = {
            "path": review_relative.as_posix(),
            "sha256": sha256_file(review_path),
        }
    return {
        "sequence_id": sequence_id,
        "sequence_identity_sha256": sequence_identity_sha256,
        "sequence_identity": sequence_identity,
        "camera_id": camera_id,
        "model_object_id": report["object_id"],
        "model_object_id_is_identity_truth": False,
        "capture_report": prepared_window["capture_snapshot"],
        "source_event_report": prepared_window["source_snapshot"],
        "input_frames": prepared_window["frame_snapshots"],
        "target_track_id": target_id,
        "anchors": anchor_matches,
        "frames": selected_rows,
        "temporal_contact_gate": temporal_gate,
        "temporal_contact_diagnostics": temporal_diagnostics,
        "review_sheet": review_descriptor,
        "summary": {
            "input_frame_count": len(frames),
            "tracked_frame_count": len(selected_rows),
            "contact_proposal_count": contact_count,
            "coverage_fraction": coverage,
            "maximum_tracked_gap_ms": maximum_gap_ms,
            "matched_anchor_count": len(anchor_matches),
            "distinct_matched_anchor_frame_count": len(distinct_anchor_frames),
            "temporal_contact_rejection_pair_count": sum(
                bool(row["rejection_reasons"]) for row in temporal_diagnostics
            ),
        },
        "rejection_reasons": sorted(set(rejection_reasons)),
        "proposal_status": "ready_for_independent_review" if not rejection_reasons else "rejected",
        "acceptance_eligible": False,
    }


def propose(capture_reports, consensus_path, model_path, output_directory, *,
            confidence=0.20, iou_threshold=0.70, image_size=1280, device="cpu",
            model_factory=None):
    if not capture_reports:
        raise DenseTrackError("at least one dense capture report is required")
    if not 0.0 < confidence <= 1.0 or not 0.0 < iou_threshold <= 1.0:
        raise DenseTrackError("confidence and IoU thresholds must be in (0, 1]")
    if image_size < 320 or image_size > 2560 or image_size % 32:
        raise DenseTrackError("image size must be a multiple of 32 in [320, 2560]")
    model_path = Path(model_path).expanduser().resolve()
    if not model_path.is_file():
        raise DenseTrackError("segmentation tracking model is unavailable")
    model_hash = sha256_file(model_path)
    model_binding = pin_source_file(
        model_path, model_hash, "segmentation tracking model"
    )
    (
        consensus_file,
        consensus_raw,
        consensus_value,
        consensus_index,
        side,
        consensus_evidence,
    ) = load_consensus(consensus_path, model_hash)
    consensus_hash = sha256_bytes(consensus_raw)
    source_report_sha256 = consensus_value.get("capture_report_sha256")
    if not valid_sha256(source_report_sha256):
        raise DenseTrackError("segmentation consensus capture denominator is invalid")
    if model_factory is None:
        try:
            import torch
            import ultralytics
            from ultralytics import YOLO
        except ImportError as exc:
            raise DenseTrackError("pinned segmentation runtime is unavailable") from exc
        model_factory = YOLO
        runtime = {
            "torch_version": torch.__version__,
            "ultralytics_version": ultralytics.__version__,
        }
    else:
        runtime = {"torch_version": "test-double", "ultralytics_version": "test-double"}
    output = Path(output_directory).expanduser().resolve()
    if output.exists():
        raise DenseTrackError("dense track output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with invocation_staging_directory(output) as (staged, owner_marker):
        try:
            published_consensus, source_bindings = snapshot_consensus_evidence(
                staged, consensus_evidence
            )
            model_snapshot_descriptor = snapshot_source_file(
                staged, model_binding, "models", ".pt", "tracking model"
            )
            source_bindings.append(model_binding)
            model_snapshot = staged / model_snapshot_descriptor["path"]
            prepared_windows = [
                prepare_dense_window(
                    report, staged,
                    expected_source_report_sha256=source_report_sha256,
                )
                for report in capture_reports
            ]
            for prepared_window in prepared_windows:
                source_bindings.extend(prepared_window["bindings"])
            bound_snapshot_tree = {
                "consensus": published_consensus,
                "tracking_model": model_snapshot_descriptor,
                "dense_windows": [
                    {
                        "capture_report": prepared_window["capture_snapshot"],
                        "source_event_report": prepared_window["source_snapshot"],
                        "frames": prepared_window["frame_snapshots"],
                    }
                    for prepared_window in prepared_windows
                ],
            }
            sequences = [
                process_window(
                    report, consensus_index, model_snapshot, staged, model_factory,
                    confidence, iou_threshold, image_size, device,
                    expected_source_report_sha256=source_report_sha256,
                    model_sha256=model_hash,
                    consensus_sha256=consensus_hash,
                    prepared_window=prepared_window,
                )
                for report, prepared_window in zip(capture_reports, prepared_windows)
            ]
            sequence_ids = [sequence["sequence_id"] for sequence in sequences]
            if len(sequence_ids) != len(set(sequence_ids)):
                raise DenseTrackError(
                    "dense capture reports resolve to duplicate sequence IDs"
                )
            verify_output_artifacts(staged, sequences)
            if sha256_file(model_snapshot) != model_hash:
                raise DenseTrackError("tracking model snapshot changed during inference")
            verify_bound_input_snapshots(staged, bound_snapshot_tree)
            revalidate_source_files(source_bindings)
            payload = {
                "schema": SCHEMA,
                "generated_at_utc": utc_now(),
                "acceptance_eligible": False,
                "acceptance_failures": [
                    "tracker_identity_is_a_model_proposal_not_independent_same_car_truth",
                    "ground_contacts_are_unreviewed_segmentation_midpoints",
                    "static_camera_and_map_calibration_must_pass_before_backprojection",
                    "development_sequences_are_not_untouched_holdouts",
                ],
                "consensus": {
                    **published_consensus["consensus"],
                    "selected_model_side": side,
                    "capture_report_sha256": source_report_sha256,
                    "capture_report": published_consensus["capture_report"],
                    "inputs": published_consensus["inputs"],
                },
                "model": {
                    "source_path": str(model_path),
                    "sha256": model_hash,
                    "execution_snapshot": model_snapshot_descriptor,
                },
                "runtime": {
                    **runtime,
                    "python_version": platform.python_version(),
                    "opencv_version": cv2.__version__,
                    "device": device,
                    "image_size": image_size,
                    "confidence": confidence,
                    "nms_iou": iou_threshold,
                    "tracker": "bytetrack.yaml",
                },
                "sequences": sequences,
                "summary": {
                    "sequence_count": len(sequences),
                    "ready_for_independent_review_count": sum(
                        row["proposal_status"] == "ready_for_independent_review"
                        for row in sequences
                    ),
                    "rejected_count": sum(
                        row["proposal_status"] == "rejected" for row in sequences
                    ),
                    "input_frame_count": sum(
                        row["summary"]["input_frame_count"] for row in sequences
                    ),
                    "tracked_frame_count": sum(
                        row["summary"]["tracked_frame_count"] for row in sequences
                    ),
                    "contact_proposal_count": sum(
                        row["summary"]["contact_proposal_count"] for row in sequences
                    ),
                },
            }
            report_path = staged / "report.json"
            write_text_exclusive(
                report_path,
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                "dense track report",
            )
            report_sha256 = sha256_file(report_path)
            verify_bound_input_snapshots(staged, bound_snapshot_tree)
            revalidate_source_files(source_bindings)
            verify_output_artifacts(staged, sequences)
            owner_marker.unlink()
            fsync_directory(staged)
            write_text_exclusive(staged / "SHA256SUMS", "".join(
                f"{sha256_file(path)}  {path.relative_to(staged).as_posix()}\n"
                for path in sorted(item for item in staged.rglob("*") if item.is_file())
            ), "dense track checksum manifest")
            fsync_directory_tree(staged)
            revalidate_source_files(source_bindings)
            verify_staged_publication(
                staged, bound_snapshot_tree, sequences, report_sha256
            )
            publish_staged_tree(staged, output)
        except StaticCaptureError as exc:
            raise DenseTrackError("atomic dense-track publication failed") from exc
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-report", action="append", required=True)
    parser.add_argument("--consensus", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--confidence", type=float, default=0.20)
    parser.add_argument("--nms-iou", type=float, default=0.70)
    parser.add_argument("--image-size", type=int, default=1280)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    try:
        output = propose(
            args.capture_report, args.consensus, args.model, args.output_directory,
            confidence=args.confidence, iou_threshold=args.nms_iou,
            image_size=args.image_size, device=args.device,
        )
    except (DenseTrackError, OSError, ValueError) as exc:
        print(f"dense vehicle tracking failed: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
