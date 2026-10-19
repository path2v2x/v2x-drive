"""Plain 2-D geometry for the driving models (CARLA frame, no CARLA import).

CARLA's frame: x forward of yaw 0, yaw increases clockwise seen from above,
the "right" vector of a pose with yaw ``a`` is ``(-sin a, cos a)``.  Lateral
values here are positive to the right, along-track values positive ahead.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence, Tuple

INF = float("inf")


def wrap_deg(angle: float) -> float:
    """Wrap an angle to (-180, 180]."""
    a = (float(angle) + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


def heading_diff_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two headings in degrees."""
    return abs(wrap_deg(a - b))


def same_direction(yaw_a: float, yaw_b: float, tol_deg: float = 45.0) -> bool:
    return heading_diff_deg(yaw_a, yaw_b) <= tol_deg


def distance(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(bx - ax, by - ay)


def relative_position(ref_x: float, ref_y: float, ref_yaw_deg: float, x: float, y: float) -> Tuple[float, float]:
    """(along, lateral) of a point in the frame of a pose: along ahead, lateral right."""
    yaw = math.radians(ref_yaw_deg)
    dx, dy = x - ref_x, y - ref_y
    along = math.cos(yaw) * dx + math.sin(yaw) * dy
    lateral = -math.sin(yaw) * dx + math.cos(yaw) * dy
    return along, lateral


def is_behind(me_x: float, me_y: float, me_yaw: float, other_x: float, other_y: float, other_yaw: float,
              max_lateral_m: float = 8.0, heading_tol_deg: float = 45.0) -> Tuple[bool, float, float]:
    """Is ``other`` behind me on my road and heading my way?

    Returns ``(behind, gap_m, lateral_m)`` where ``gap_m`` is positive when
    the other vehicle is behind (negative: ahead) and ``lateral_m`` is its
    offset in my frame (positive right).
    """
    along, lateral = relative_position(me_x, me_y, me_yaw, other_x, other_y)
    behind = along < 0 and abs(lateral) <= max_lateral_m and same_direction(me_yaw, other_yaw, heading_tol_deg)
    return behind, -along, lateral


def time_to_pass(gap_m: float, erv_speed_mps: float, my_speed_mps: float) -> float:
    """Seconds until a vehicle ``gap_m`` behind reaches me (inf when not closing)."""
    if gap_m <= 0:
        return 0.0
    closing = erv_speed_mps - my_speed_mps
    if closing <= 0.1:
        return INF
    return gap_m / closing


def time_to_collision(gap_m: float, closing_speed_mps: float) -> float:
    if gap_m <= 0:
        return 0.0
    if closing_speed_mps <= 0.05:
        return INF
    return gap_m / closing_speed_mps


def brake_distance(speed_mps: float, decel_mps2: float) -> float:
    if decel_mps2 <= 0:
        return INF
    return speed_mps * speed_mps / (2.0 * decel_mps2)


def pull_over_offset(lane_width_m: float, half_width_m: float, shoulder_m: float = 0.0,
                     edge_margin_m: float = 0.3, shoulder_use_m: float = 1.0) -> Optional[float]:
    """Lateral offset (right, metres) that parks the car at the right edge.

    ``None`` when the lane is too narrow to move over meaningfully (the car
    then stops in its lane).
    """
    inside = lane_width_m / 2.0 - half_width_m - edge_margin_m
    if inside < 0.2:
        return None
    return round(inside + max(0.0, min(shoulder_m, shoulder_use_m)), 2)


def car_following_speed(gap_m: Optional[float], lead_speed_mps: Optional[float], desired_speed_mps: float,
                        min_gap_m: float = 8.0, time_headway_s: float = 1.5) -> float:
    """Target speed that keeps a safe gap behind a lead vehicle (simple linear policy)."""
    if gap_m is None or lead_speed_mps is None:
        return desired_speed_mps
    if gap_m <= min_gap_m:
        return max(0.0, min(desired_speed_mps, lead_speed_mps - 1.0))
    allowed = lead_speed_mps + (gap_m - min_gap_m) / time_headway_s
    return max(0.0, min(desired_speed_mps, allowed))


def compass_to_carla_yaw(heading_deg: float) -> float:
    """J2735 heading (0 = north, clockwise) to a CARLA yaw (x east, y south)."""
    h = math.radians(heading_deg)
    return math.degrees(math.atan2(-math.cos(h), math.sin(h)))


def carla_yaw_to_compass(yaw_deg: float) -> float:
    yaw = math.radians(yaw_deg)
    east, north = math.cos(yaw), -math.sin(yaw)
    return math.degrees(math.atan2(east, north)) % 360.0


def polyline_length(points: Sequence[Tuple[float, float]]) -> float:
    total = 0.0
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        total += math.hypot(bx - ax, by - ay)
    return total


def project_on_polyline(points: Sequence[Tuple[float, float]], x: float, y: float) -> Tuple[float, float, int]:
    """Project a point on a polyline: ``(s_along, lateral_right, segment_index)``.

    Lateral is signed with respect to the segment direction (positive right).
    """
    best = (INF, 0.0, 0.0, 0)
    s_acc = 0.0
    for i, ((ax, ay), (bx, by)) in enumerate(zip(points, points[1:])):
        vx, vy = bx - ax, by - ay
        seg_len = math.hypot(vx, vy)
        if seg_len < 1e-6:
            continue
        t = ((x - ax) * vx + (y - ay) * vy) / (seg_len * seg_len)
        t = max(0.0, min(1.0, t))
        px, py = ax + t * vx, ay + t * vy
        d = math.hypot(x - px, y - py)
        if d < best[0]:
            lateral = (-vy * (x - ax) + vx * (y - ay)) / seg_len
            best = (d, s_acc + t * seg_len, lateral, i)
        s_acc += seg_len
    return best[1], best[2], best[3]


def point_along_polyline(points: Sequence[Tuple[float, float]], s: float) -> Tuple[float, float]:
    """The point ``s`` metres along a polyline (clamped to its ends)."""
    if not points:
        raise ValueError("empty polyline")
    if s <= 0:
        return points[0]
    acc = 0.0
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        seg = math.hypot(bx - ax, by - ay)
        if acc + seg >= s:
            t = (s - acc) / seg if seg > 0 else 0.0
            return ax + t * (bx - ax), ay + t * (by - ay)
        acc += seg
    return points[-1]


def min_pairwise_distance(positions: Iterable[Tuple[float, float]]) -> float:
    pts = list(positions)
    best = INF
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            best = min(best, math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]))
    return best
