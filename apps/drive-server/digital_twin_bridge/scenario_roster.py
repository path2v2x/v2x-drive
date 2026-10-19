"""
Scenario roster: which vehicle names exist in the VOICES scenario and who owns them.

The DT adapter binds CARLA actors to scenario entries by ``role_name`` and the
TENA V2X messages carry the same name as ``senderIdentifier``.  The roster lets
the viewer classify vehicles (ours vs. another organization's ghost) and map
BSM ids back to names when a message arrives without a sender (raw UDP tap).

Sources, merged in this order:
1. the scenario XML DT is configured with (``VUG_SCENARIO_FILE`` in
   ``~/.dt_scenario_config``, resolved under ``scenario_files/``), re-read when
   its mtime changes;
2. the live ``VUG::Configuration::Scenario`` object seen on the JSON stream.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

logger = logging.getLogger(__name__)

DEFAULT_DT_SCENARIO_CONFIG = os.path.expanduser("~/.dt_scenario_config")
DEFAULT_DT_REPO = os.path.expanduser("~/distributed-testing")


@dataclass
class RosterEntry:
    name: str
    bsm_id: str = ""            # 8 hex chars, lower case
    org: str = ""
    vehicle_type: str = ""
    sim_id: str = ""
    lvc: str = ""
    generate_bsms: bool = True
    source: str = "xml"

    def to_dict(self) -> dict:
        return {
            "name": self.name, "bsm_id": self.bsm_id, "org": self.org,
            "type": self.vehicle_type, "sim_id": self.sim_id, "lvc": self.lvc,
            "generate_bsms": self.generate_bsms, "source": self.source,
        }


def _norm_bsm_id(value) -> str:
    """Normalize a bsmid from the XML ("11111110") or the live object's byte list."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        octets = []
        for item in value:
            if isinstance(item, dict):
                item = next(iter(item.values()), 0)
            try:
                octets.append(int(item) & 0xFF)
            except (TypeError, ValueError):
                octets.append(0)
        return "".join("%02x" % b for b in octets[:4]).ljust(8, "0")
    text = str(value).strip().lower()
    if re.fullmatch(r"[0-9a-f]{1,8}", text):
        return text.zfill(8)
    return text


def _org_from_name(name: str) -> str:
    return name.split("-")[0] if "-" in name else ""


def resolve_scenario_xml(explicit: str = "", dt_scenario_config: str = DEFAULT_DT_SCENARIO_CONFIG,
                         dt_repo: str = DEFAULT_DT_REPO) -> Optional[str]:
    """Find the scenario XML: explicit path, else DT's active scenario config."""
    if explicit:
        return explicit if os.path.exists(explicit) else None
    try:
        with open(dt_scenario_config, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r"VUG_SCENARIO_FILE\s*=\s*['\"]?([^'\"\s]+)", text)
    if not m:
        return None
    candidate = m.group(1)
    if not os.path.isabs(candidate):
        candidate = os.path.join(dt_repo, "scenario_files", candidate)
    return candidate if os.path.exists(candidate) else None


def parse_scenario_xml(path: str) -> Dict[str, RosterEntry]:
    entries: Dict[str, RosterEntry] = {}
    tree = ET.parse(path)
    for veh in tree.getroot().iter("vehicle"):
        name = veh.get("name") or ""
        if not name:
            continue
        entries[name] = RosterEntry(
            name=name,
            bsm_id=_norm_bsm_id(veh.get("bsmid")),
            org=veh.get("organization") or _org_from_name(name),
            vehicle_type=veh.get("type") or "",
            sim_id=veh.get("simulationIdentifier") or "",
            lvc=veh.get("lvcIndicator") or "",
            generate_bsms=str(veh.get("generate_bsms", "true")).lower() in {"1", "true", "yes"},
            source="xml",
        )
    return entries


class ScenarioRoster:
    def __init__(self, xml_path: str = "", local_org: str = "PATH", reload_interval_s: float = 10.0,
                 dt_scenario_config: str = DEFAULT_DT_SCENARIO_CONFIG, dt_repo: str = DEFAULT_DT_REPO):
        self._explicit = xml_path
        self._dt_scenario_config = dt_scenario_config
        self._dt_repo = dt_repo
        self.local_org = local_org
        self._reload_interval = reload_interval_s
        self._lock = threading.Lock()
        self._entries: Dict[str, RosterEntry] = {}
        self._live: Dict[str, RosterEntry] = {}
        self._path: Optional[str] = None
        self._mtime: float = 0.0
        self._last_check: float = 0.0
        self.scenario_name: str = ""
        self.reload(force=True)

    # ── XML ──

    def reload(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._last_check < self._reload_interval:
            return False
        self._last_check = now
        path = resolve_scenario_xml(self._explicit, self._dt_scenario_config, self._dt_repo)
        if path is None:
            if self._path is not None:
                logger.warning("Scenario XML no longer found; keeping the last roster (%s)", self._path)
            return False
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return False
        if not force and path == self._path and mtime == self._mtime:
            return False
        try:
            entries = parse_scenario_xml(path)
        except Exception as exc:
            logger.warning("Scenario XML %s could not be parsed: %s", path, exc)
            return False
        with self._lock:
            self._entries = entries
            self._path = path
            self._mtime = mtime
        logger.info("Scenario roster: %d vehicles from %s", len(entries), path)
        return True

    # ── live Scenario object from the JSON stream ──

    def merge_live(self, scenario_attributes: dict) -> int:
        """Merge the ``VUG::Configuration::Scenario`` attributes seen on the stream."""
        vehicles = scenario_attributes.get("vehicles") or []
        merged = 0
        live: Dict[str, RosterEntry] = {}
        for item in vehicles:
            attrs = item.get("attributes", item) if isinstance(item, dict) else {}
            name = attrs.get("identifier") or ""
            if not name:
                continue
            live[name] = RosterEntry(
                name=name,
                bsm_id=_norm_bsm_id(attrs.get("basicSafetyMessageIdentifier")),
                org=attrs.get("owner") or _org_from_name(name),
                vehicle_type=attrs.get("type") or "",
                sim_id=attrs.get("simulationIdentifier") or "",
                lvc=str(attrs.get("lvcIndicator") or "").split("_")[-1],
                generate_bsms=True,
                source="live",
            )
            merged += 1
        with self._lock:
            self._live = live
            self.scenario_name = scenario_attributes.get("identifier") or self.scenario_name
        return merged

    # ── queries ──

    def get(self, name: str) -> Optional[RosterEntry]:
        self.reload()
        with self._lock:
            entry = self._entries.get(name)
            live = self._live.get(name)
        if entry is None:
            return live
        if live is not None and not entry.bsm_id:
            entry.bsm_id = live.bsm_id
        return entry

    def names(self) -> list:
        self.reload()
        with self._lock:
            return sorted(set(self._entries) | set(self._live))

    def by_bsm_id(self, bsm_id: str) -> Optional[RosterEntry]:
        wanted = _norm_bsm_id(bsm_id)
        if not wanted or set(wanted) == {"0"}:
            return None
        self.reload()
        with self._lock:
            for table in (self._entries, self._live):
                for entry in table.values():
                    if entry.bsm_id == wanted:
                        return entry
        return None

    def is_local(self, entry: Optional[RosterEntry]) -> bool:
        return bool(entry) and entry.org.upper() == self.local_org.upper()

    def status(self) -> dict:
        with self._lock:
            return {
                "file": self._path,
                "vehicles": len(set(self._entries) | set(self._live)),
                "live_vehicles": len(self._live),
                "local_org": self.local_org,
                "scenario": self.scenario_name,
            }
