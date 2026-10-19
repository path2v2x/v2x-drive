"""Start/stop control for the background-traffic script from the web app.

The script is an external program: on RFS the SUMO bridge (``~/sumo-bg/run-sumo-bg.sh``),
which runs as the docker container ``sumo-bg`` and drives the PATH-BG slot cars.  This
module only runs four configured shell commands and reports what it saw:

* start ``DTB_BG_TRAFFIC_START``: launches the script and returns (a detached container),
* stop ``DTB_BG_TRAFFIC_STOP``: asks it to stop (the bridge parks its slot cars and exits),
* probe ``DTB_BG_TRAFFIC_PROBE``: exit 0 and a first word other than "false" = running; an
  optional second word is the start time (RFC 3339, as ``docker inspect`` prints it),
* tail ``DTB_BG_TRAFFIC_TAIL``: its last log line (read whether it runs or not, so the
  reason for an exit stays visible), shown in the editor's status block.

The control is offered when a start command is configured and ``DTB_BG_TRAFFIC_REQUIRES``
(the script) exists.  Nothing here touches CARLA; the editor polls ``bg_traffic_status``
and sends ``bg_traffic_start`` / ``bg_traffic_stop`` (see docs/editor-mode.md).
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import os
import re
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

STATES = ("unavailable", "stopped", "starting", "running", "stopping")
_RFC3339 = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def _expand_home(cmd: str) -> str:
    """``~/`` inside a command becomes the home directory (the service may run without HOME)."""
    return cmd.replace("~/", os.path.expanduser("~") + "/") if cmd else ""


def parse_rfc3339(text: str) -> Optional[float]:
    """Epoch seconds of an RFC 3339 time such as ``2026-10-19T08:16:31.558245105Z``; None if not one."""
    m = _RFC3339.match((text or "").strip())
    if not m:
        return None
    date, clock, frac, zone = m.groups()
    try:
        base = calendar.timegm(time.strptime(date + "T" + clock, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    seconds = base + (float(frac) if frac else 0.0)
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        seconds -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return seconds


def unavailable_status(label: str = "") -> dict:
    return {"type": "bg_traffic", "available": False, "label": label, "state": "unavailable", "running": False,
            "since": None, "uptime_s": None, "last_start": None, "last_stop": None, "last_line": ""}


class BackgroundTraffic:
    """Runs the configured start/stop/probe/tail commands and keeps the last outcomes."""

    def __init__(self, start_cmd: str, stop_cmd: str = "", probe_cmd: str = "", tail_cmd: str = "",
                 requires: str = "", label: str = "Background traffic", command_timeout_s: float = 60.0,
                 start_grace_s: float = 20.0, stop_grace_s: float = 45.0, now_fn=time.time):
        self.start_cmd = _expand_home(start_cmd)
        self.stop_cmd = _expand_home(stop_cmd)
        self.probe_cmd = _expand_home(probe_cmd)
        self.tail_cmd = _expand_home(tail_cmd)
        self.requires = os.path.expanduser(requires) if requires else ""
        self.label = label
        self.command_timeout_s = float(command_timeout_s)
        self.start_grace_s = float(start_grace_s)
        self.stop_grace_s = float(stop_grace_s)
        self._now = now_fn
        self._lock = asyncio.Lock()
        self._pending: Optional[str] = None  # "starting" | "stopping" until the probe agrees
        self._pending_since = 0.0
        self._since: Optional[float] = None
        self._last_start: Optional[dict] = None
        self._last_stop: Optional[dict] = None
        self._last_line = ""

    @property
    def available(self) -> bool:
        return bool(self.start_cmd) and (not self.requires or os.path.exists(self.requires))

    async def _run(self, cmd: str, timeout: Optional[float] = None) -> Tuple[int, str]:
        """Exit code and combined output of a shell command; (-1, reason) when it could not run."""
        timeout = timeout or self.command_timeout_s
        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            return int(proc.returncode or 0), out.decode("utf-8", "replace")
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            return -1, "timed out after %.0f s" % timeout
        except Exception as exc:
            return -1, str(exc)

    async def _probe(self) -> bool:
        if not self.probe_cmd:
            return False
        code, out = await self._run(self.probe_cmd, timeout=15.0)
        words = out.split()
        running = code == 0 and bool(words) and words[0].lower() not in ("false", "0", "no")
        if running:
            since = parse_rfc3339(words[1]) if len(words) > 1 else None
            if since is not None:
                self._since = since
            elif self._since is None:
                self._since = self._now()  # first seen by this server
        else:
            self._since = None
        return running

    async def _tail(self) -> str:
        if self.tail_cmd:
            code, out = await self._run(self.tail_cmd, timeout=15.0)
            if code == 0:
                lines = [line.strip() for line in out.splitlines() if line.strip()]
                if lines:
                    self._last_line = lines[-1][-240:]
        return self._last_line

    async def status(self) -> dict:
        if not self.available:
            return unavailable_status(self.label)
        now = self._now()
        running = await self._probe()
        state = "running" if running else "stopped"
        if self._pending == "starting":
            if running or now - self._pending_since >= self.start_grace_s:
                self._pending = None
            else:
                state = "starting"
        elif self._pending == "stopping":
            if not running or now - self._pending_since >= self.stop_grace_s:
                self._pending = None
            else:
                state = "stopping"
        line = await self._tail()
        return {
            "type": "bg_traffic", "available": True, "label": self.label, "state": state, "running": running,
            "since": self._since if running else None,
            "uptime_s": round(max(0.0, now - self._since), 1) if running and self._since is not None else None,
            "last_start": self._last_start, "last_stop": self._last_stop, "last_line": line,
        }

    async def start(self) -> dict:
        async with self._lock:
            current = await self.status()
            if not current["available"]:
                return {**current, "error": "background traffic control is not configured on this server"}
            if current["state"] != "stopped":
                return {**current, "error": "already %s" % current["state"]}
            code, out = await self._run(self.start_cmd)
            self._last_start = {"at": self._now(), "ok": code == 0, "code": code, "output": out.strip()[-400:]}
            if code == 0:
                self._pending, self._pending_since = "starting", self._now()
            logger.info("Background traffic start: exit %d %s", code, out.strip()[-200:].replace("\n", " | "))
            return await self.status()

    async def stop(self) -> dict:
        async with self._lock:
            current = await self.status()
            if not current["available"]:
                return {**current, "error": "background traffic control is not configured on this server"}
            if current["state"] == "stopped":
                return {**current, "error": "not running"}
            if current["state"] == "stopping":
                return {**current, "error": "already stopping"}
            if not self.stop_cmd:
                return {**current, "error": "no stop command configured"}
            code, out = await self._run(self.stop_cmd, timeout=30.0)
            self._last_stop = {"at": self._now(), "ok": code == 0, "code": code, "output": out.strip()[-400:]}
            if code == 0:
                self._pending, self._pending_since = "stopping", self._now()
            logger.info("Background traffic stop: exit %d %s", code, out.strip()[-200:].replace("\n", " | "))
            return await self.status()
