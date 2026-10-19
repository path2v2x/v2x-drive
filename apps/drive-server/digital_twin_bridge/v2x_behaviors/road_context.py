"""CARLA map queries for the driving models: lane geometry, junctions, controls.

Everything here works on ``carla.Waypoint`` objects, which the client
computes locally from the OpenDRIVE map (no server round trips).  The
functions accept any object with the waypoint attributes so the unit tests
can use plain stand-ins.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from digital_twin_bridge.v2x_behaviors.geometry import INF

STOP_SIGN_TYPES = ("206",)                 # OpenDRIVE signal type for a stop sign
SIGNAL_TYPES = ("1000001", "1000002")      # traffic signals


def _lane_type_name(wp) -> str:
    try:
        return str(wp.lane_type).split(".")[-1]
    except Exception:
        return ""


def is_driving(wp) -> bool:
    return wp is not None and _lane_type_name(wp) == "Driving"


def lane_sign(wp) -> int:
    return 1 if wp.lane_id > 0 else -1


def same_road_same_direction(a, b) -> bool:
    return a.road_id == b.road_id and lane_sign(a) == lane_sign(b)


def along_track(wp) -> float:
    """A coordinate that increases in the lane's driving direction (OpenDRIVE s)."""
    return -wp.s if wp.lane_id > 0 else wp.s


def along_gap(a, b) -> float:
    """How far ahead ``b`` is of ``a`` along the road (both on the same road and direction)."""
    return along_track(b) - along_track(a)


def count_same_direction_lanes(wp) -> int:
    count = 1
    sign = lane_sign(wp)
    for step in ("get_left_lane", "get_right_lane"):
        cur = wp
        for _ in range(6):
            try:
                cur = getattr(cur, step)()
            except Exception:
                cur = None
            if cur is None or not is_driving(cur) or lane_sign(cur) != sign:
                break
            count += 1
    return count


def opposing_lane(wp):
    """The nearest driving lane heading the other way (left of us), or None."""
    cur = wp
    sign = lane_sign(wp)
    for _ in range(6):
        try:
            cur = cur.get_left_lane()
        except Exception:
            return None
        if cur is None:
            return None
        if lane_sign(cur) != sign:
            return cur if is_driving(cur) else None
    return None


def right_shoulder_width(wp) -> float:
    try:
        right = wp.get_right_lane()
    except Exception:
        return 0.0
    if right is None:
        return 0.0
    if _lane_type_name(right) in ("Shoulder", "Parking", "Border"):
        return float(getattr(right, "lane_width", 0.0) or 0.0)
    return 0.0


def distance_to_junction(wp, max_m: float = 200.0, step: float = 2.0) -> Tuple[float, Optional[object]]:
    """Distance along the lane to the next junction waypoint (inf when none within ``max_m``)."""
    if getattr(wp, "is_junction", False):
        return 0.0, wp
    dist = 0.0
    cur = wp
    while dist < max_m:
        try:
            nxt = cur.next(step)
        except Exception:
            return INF, None
        if not nxt:
            return INF, None
        cur = nxt[0]
        dist += step
        if getattr(cur, "is_junction", False):
            return dist, cur
    return INF, None


def junction_control(wp, dist_m: float) -> str:
    """'stop' | 'signal' | 'none' for the junction ``dist_m`` ahead, from OpenDRIVE landmarks."""
    if dist_m == INF:
        return "none"
    try:
        landmarks = wp.get_landmarks(dist_m + 8.0, False)
    except Exception:
        return "none"
    kinds = set()
    for lm in landmarks or []:
        t = str(getattr(lm, "type", ""))
        if t in STOP_SIGN_TYPES:
            kinds.add("stop")
        elif t in SIGNAL_TYPES:
            kinds.add("signal")
    if "stop" in kinds:
        return "stop"
    if "signal" in kinds:
        return "signal"
    return "none"


def lane_polyline_ahead(wp, length_m: float, step: float = 2.0) -> List[Tuple[float, float]]:
    pts = [(wp.transform.location.x, wp.transform.location.y)]
    cur = wp
    dist = 0.0
    while dist < length_m:
        try:
            nxt = cur.next(step)
        except Exception:
            break
        if not nxt:
            break
        cur = nxt[0]
        dist += step
        pts.append((cur.transform.location.x, cur.transform.location.y))
    return pts


@dataclass
class LaneInfo:
    road_id: int
    lane_id: int
    lane_width_m: float
    is_junction: bool
    same_dir_lanes: int
    two_way: bool
    shoulder_m: float
    dist_to_junction_m: float
    junction_control: str
    yaw_deg: float


def lane_info(wp, with_control: bool = True) -> LaneInfo:
    dist, _ = distance_to_junction(wp)
    return LaneInfo(
        road_id=int(wp.road_id),
        lane_id=int(wp.lane_id),
        lane_width_m=float(getattr(wp, "lane_width", 3.5) or 3.5),
        is_junction=bool(getattr(wp, "is_junction", False)),
        same_dir_lanes=count_same_direction_lanes(wp),
        two_way=opposing_lane(wp) is not None,
        shoulder_m=right_shoulder_width(wp),
        dist_to_junction_m=dist,
        junction_control=junction_control(wp, dist) if with_control else "none",
        yaw_deg=float(wp.transform.rotation.yaw),
    )


def lateral_in_lane(wp, x: float, y: float) -> float:
    """Signed lateral offset (right positive) of a point from the lane center at ``wp``."""
    loc = wp.transform.location
    yaw = math.radians(wp.transform.rotation.yaw)
    dx, dy = x - loc.x, y - loc.y
    return -math.sin(yaw) * dx + math.cos(yaw) * dy
