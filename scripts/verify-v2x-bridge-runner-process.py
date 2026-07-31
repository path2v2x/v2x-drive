#!/usr/bin/python3 -I
"""Fail unless the parent is directly executing the tracked bridge runner."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def main() -> int:
    parent = os.getppid()
    proc = Path("/proc") / str(parent)
    try:
        arguments = (proc / "cmdline").read_bytes().split(b"\0")
        if arguments and arguments[-1] == b"":
            arguments.pop()
        executable = (proc / "exe").resolve(strict=True)
        cwd = (proc / "cwd").resolve(strict=True)
    except OSError:
        return 2
    if len(arguments) != 3 or arguments[:2] != [b"/bin/bash", b"-p"]:
        return 2
    try:
        script_argument = os.fsdecode(arguments[2])
    except UnicodeError:
        return 2
    candidate = Path(script_argument)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    expected = Path(__file__).resolve(strict=True).with_name("test-v2x-bridge.sh")
    try:
        candidate = candidate.resolve(strict=True)
    except OSError:
        return 2
    if executable != Path("/bin/bash").resolve(strict=True) or candidate != expected:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
