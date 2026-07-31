#!/usr/bin/env python3
"""Shared fail-closed primitives for Tier-B dynamic Phase C0."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import unicodedata


NON_RELEASE_FLAGS = {
    "acceptance_eligible": False,
    "proposal_only": True,
    "release_eligible": False,
}
CAMERAS = ("ch1", "ch2", "ch3", "ch4")
SPLITS = ("fit", "development", "untouched_holdout")


class C0Error(ValueError):
    pass


def exact_keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise C0Error(f"{label} must contain exact fields {sorted(keys)}")
    return value


def nonblank(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise C0Error(f"{label} must be a nonblank canonical string")
    return value


def finite_number(value, label, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise C0Error(f"{label} must be a finite number")
    result = float(value)
    if not __import__("math").isfinite(result) or (minimum is not None and result < minimum):
        raise C0Error(f"{label} must be finite and >= {minimum}")
    return result


def require_non_release(value, label="document"):
    for key, expected in NON_RELEASE_FLAGS.items():
        if value.get(key) is not expected:
            raise C0Error(f"{label}.{key} must be {expected!r}")


def generator(value):
    exact_keys(value, {"commit", "worktree_clean"}, "generator")
    commit = nonblank(value["commit"], "generator.commit")
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise C0Error("generator.commit must be a full lowercase Git SHA")
    if value["worktree_clean"] is not True:
        raise C0Error("generator worktree must be clean")
    return {"commit": commit, "worktree_clean": True}


def _derived_generator():
    root = Path(__file__).resolve().parents[3]
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout
    return {"commit": commit, "worktree_clean": not bool(dirty)}


def _absolute(path_value, label):
    path = Path(str(path_value or ""))
    if (not path.is_absolute() or unicodedata.normalize("NFC", str(path)) != str(path)
            or any(part in {"", ".", ".."} for part in path.parts[1:])):
        raise C0Error(f"{label} path must be absolute normalized NFC")
    return path


def _open_parent_no_follow(path, label):
    """Open every ancestor by descriptor so a symlink cannot redirect the read."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    if not hasattr(os, "O_NOFOLLOW"):
        raise C0Error("platform lacks O_NOFOLLOW")
    flags |= os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:-1]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise C0Error(f"{label} ancestry cannot be opened without following links") from exc


def read_bound_file(value, label):
    exact_keys(value, {"path", "sha256"}, label)
    path = _absolute(value["path"], label)
    expected = value["sha256"]
    if (not isinstance(expected, str) or len(expected) != 64
            or any(char not in "0123456789abcdef" for char in expected)):
        raise C0Error(f"{label} SHA-256 is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if not hasattr(os, "O_NOFOLLOW"):
        raise C0Error("platform lacks O_NOFOLLOW")
    flags |= os.O_NOFOLLOW
    try:
        parent_descriptor = _open_parent_no_follow(path, label)
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        try:
            os.close(parent_descriptor)
        except (NameError, OSError):
            pass
        raise C0Error(f"{label} cannot be opened without following links") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size <= 0:
            raise C0Error(f"{label} must be a nonempty single-link regular file")
        digest = hashlib.sha256(); chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk); digest.update(chunk); remaining -= len(chunk)
        if remaining or os.read(descriptor, 1):
            raise C0Error(f"{label} changed size while read")
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_mode, before.st_nlink,
             before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_mode, after.st_nlink,
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise C0Error(f"{label} changed while read")
        actual = digest.hexdigest()
        if actual != expected:
            raise C0Error(f"{label} hash mismatch")
        binding = {"path": str(path), "sha256": actual, "bytes": before.st_size,
                   "uid": before.st_uid, "gid": before.st_gid,
                   "mode": stat.S_IMODE(before.st_mode)}
        return b"".join(chunks), binding
    finally:
        os.close(descriptor)
        os.close(parent_descriptor)


def bind_file(value, label):
    return read_bound_file(value, label)[1]


def load_bound_json(value, label):
    raw, binding = read_bound_file(value, label)
    try:
        document = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C0Error(f"{label} is invalid JSON") from exc
    if not isinstance(document, dict):
        raise C0Error(f"{label} must contain a JSON object")
    return document, binding


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise C0Error(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_input_json(path_value, label="input"):
    path = _absolute(path_value, label)
    parent = _open_parent_no_follow(path, label)
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        try:
            descriptor = os.open(path.name, flags, dir_fd=parent)
        except OSError as exc:
            raise C0Error(f"{label} cannot be opened without following links") from exc
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size <= 0:
            raise C0Error(f"{label} must be a nonempty single-link regular file")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk: break
            chunks.append(chunk)
        raw = b"".join(chunks); after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise C0Error(f"{label} changed while read")
    finally:
        try: os.close(descriptor)
        except (NameError, OSError): pass
        os.close(parent)
    try:
        document = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C0Error(f"{label} is invalid JSON") from exc
    if not isinstance(document, dict): raise C0Error(f"{label} must contain a JSON object")
    return document, hashlib.sha256(raw).hexdigest()


def identity_bindings(value, label="identities"):
    exact_keys(value, {"config", "models", "runtime"}, label)
    models = value["models"]
    if not isinstance(models, list) or not models:
        raise C0Error(f"{label}.models must be nonempty")
    result = {
        "config": bind_file(value["config"], f"{label}.config"),
        "models": [bind_file(item, f"{label}.models[{index}]")
                   for index, item in enumerate(models)],
        "runtime": bind_file(value["runtime"], f"{label}.runtime"),
    }
    hashes = [result["config"]["sha256"], result["runtime"]["sha256"],
              *(item["sha256"] for item in result["models"])]
    if len(hashes) != len(set(hashes)):
        raise C0Error(f"{label} artifacts must be distinct")
    return result


def publish_no_replace(path_value, report):
    require_non_release(report, "published report")
    declared = generator(report.get("generator"))
    derived = _derived_generator()
    if derived != declared or derived["worktree_clean"] is not True:
        raise C0Error("generator provenance does not match a clean current worktree")
    path = _absolute(path_value, "output")
    parent_fd = _open_parent_no_follow(path, "output")
    encoded = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    temporary = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o640, dir_fd=parent_fd)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                follow_symlinks=False)
        os.unlink(temporary, dir_fd=parent_fd)
        temporary = None
        os.fsync(parent_fd)
    except FileExistsError as exc:
        raise C0Error("refusing to replace existing C0 report") from exc
    finally:
        if temporary is not None:
            try: os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError: pass
        os.close(parent_fd)
