"""Routes, spawn poses and special manoeuvre paths on the CARLA map.

Wraps the vendored CARLA ``GlobalRoutePlanner`` (built once per map, it is
the expensive part) and builds the two paths the behaviours need that the
stock agents cannot produce: a pose a given distance ahead of / behind a
vehicle along its lane, and the opposing-lane pass path (leave the lane to
the left, drive the oncoming lane backwards with ``previous()``, come back).
"""

from __future__ import annotations

import logging
import math
import random
import threading
from typing import List, Optional, Tuple

from digital_twin_bridge.v2x_behaviors.road_context import is_driving, lane_sign, opposing_lane

logger = logging.getLogger(__name__)


def _road_option(name: str):
    from agents.navigation.local_planner import RoadOption
    return getattr(RoadOption, name)


def walk_along(wp, distance_m: float, step: float = 2.0, backwards: bool = False, min_fraction: float = 0.3):
    """The waypoint ``distance_m`` along the lane from ``wp``.

    Roads on the RFS map are short and some end in dead ends: when the lane
    runs out, the farthest waypoint reached is returned as long as at least
    ``min_fraction`` of the distance (and 10 m) was covered, else None.
    """
    cur = wp
    done = 0.0
    while done < distance_m:
        try:
            nxt = cur.previous(step) if backwards else cur.next(step)
        except Exception:
            nxt = []
        if not nxt:
            break
        cur = nxt[0]
        done += step
    if done >= distance_m or done >= max(10.0, distance_m * min_fraction):
        return cur
    return None


class RoutePlanner:
    def __init__(self, carla_map, sampling_resolution: float = 2.0, rng: Optional[random.Random] = None):
        self._map = carla_map
        self._resolution = sampling_resolution
        self._grp = None
        self._lock = threading.Lock()
        self._rng = rng or random.Random()
        self._routable = None

    @property
    def carla_map(self):
        return self._map

    def grp(self):
        """The GlobalRoutePlanner, built on first use (seconds on a large map)."""
        with self._lock:
            if self._grp is None:
                from agents.navigation.global_route_planner import GlobalRoutePlanner
                self._grp = GlobalRoutePlanner(self._map, self._resolution)
                logger.info("Route planner graph built for %s", getattr(self._map, "name", "map"))
            return self._grp

    def trace(self, start_location, end_location) -> list:
        try:
            return self.grp().trace_route(start_location, end_location)
        except Exception as exc:
            logger.debug("trace_route failed: %s", exc)
            return []

    def waypoint(self, location):
        return self._map.get_waypoint(location, project_to_road=True)

    # ── spawn poses ──

    def transform_along_lane(self, ref_location, distance_m: float, backwards: bool = False):
        """Transform on the lane through ``ref_location``, ``distance_m`` ahead (or behind)."""
        wp = self.waypoint(ref_location)
        if wp is None:
            return None
        target = walk_along(wp, distance_m, backwards=backwards)
        if target is None:
            return None
        return self._lift(target.transform)

    def snapped_transform(self, location, yaw_deg: Optional[float] = None):
        wp = self.waypoint(location)
        if wp is None:
            return None
        tf = self._lift(wp.transform)
        if yaw_deg is not None:
            tf.rotation.yaw = float(yaw_deg)
        return tf

    @staticmethod
    def _lift(transform, dz: float = 0.5):
        import carla
        loc = transform.location
        return carla.Transform(carla.Location(x=loc.x, y=loc.y, z=loc.z + dz),
                               carla.Rotation(pitch=0.0, yaw=transform.rotation.yaw, roll=0.0))

    def random_destination(self, from_location, min_m: float = 150.0, tries: int = 25, strict: bool = False):
        """A spawn-point location at least ``min_m`` away with a route from ``from_location``.

        ``strict`` returns None when no candidate has a route (the car sits on a road
        fragment with no way out); otherwise a far point is returned as a last resort.
        """
        points = list(self._map.get_spawn_points())
        self._rng.shuffle(points)
        for sp in points[:tries]:
            loc = sp.location
            if math.hypot(loc.x - from_location.x, loc.y - from_location.y) < min_m:
                continue
            if len(self.trace(from_location, loc)) >= 5:
                return loc
        if strict:
            return None
        for sp in points:
            if math.hypot(sp.location.x - from_location.x, sp.location.y - from_location.y) >= min_m / 2:
                return sp.location
        return points[0].location if points else from_location

    def routable_spawn_points(self) -> list:
        """Spawn points a route leads away from, cached per map.

        The RFS road graph has dead fragments (14 of 95 spawn points): a car placed on
        one can never plan a route, so random placement and stuck relocation avoid them.
        """
        if self._routable is None:
            points = list(self._map.get_spawn_points())
            good = [sp for sp in points
                    if self.random_destination(sp.location, tries=len(points), strict=True) is not None]
            logger.info("Routable spawn points: %d of %d", len(good), len(points))
            self._routable = good
        return list(self._routable)

    # ── manoeuvre paths ──

    def splice_to_destination(self, path: list, destination) -> list:
        """Append the route from the end of ``path`` to ``destination``."""
        if not path or destination is None:
            return list(path)
        tail = self.trace(path[-1][0].transform.location, destination)
        return list(path) + list(tail[1:] if tail else [])

    def opposing_pass_path(self, wp, pass_length_m: float, step: float = 2.0,
                           lead_in_m: float = 6.0, merge_m: float = 14.0) -> list:
        """Path that overtakes via the oncoming lane: out left, along, back right.

        Empty when the road has no opposing lane, the lane ends, or the pass
        would run through a junction.
        """
        opp = opposing_lane(wp)
        if opp is None:
            return []
        LANEFOLLOW, LEFT, RIGHT = _road_option("LANEFOLLOW"), _road_option("CHANGELANELEFT"), _road_option("CHANGELANERIGHT")
        plan = [(wp, LANEFOLLOW)]
        # a few metres in my lane first
        cur = wp
        done = 0.0
        while done < lead_in_m:
            nxt = cur.next(step)
            if not nxt:
                return []
            cur = nxt[0]
            done += step
            if getattr(cur, "is_junction", False):
                return []
            plan.append((cur, LANEFOLLOW))
        # cross to the opposing lane (its "previous" runs in my direction)
        opp = opposing_lane(cur)
        if opp is None or not is_driving(opp):
            return []
        plan.append((opp, LEFT))
        done = 0.0
        cur_opp = opp
        while done < pass_length_m:
            prv = cur_opp.previous(step)
            if not prv:
                return []
            cur_opp = prv[0]
            done += step
            if getattr(cur_opp, "is_junction", False) or lane_sign(cur_opp) == lane_sign(wp):
                return []
            plan.append((cur_opp, LANEFOLLOW))
        # back to a lane heading my way: our lane is on the opposing lane's left
        back = None
        probe = cur_opp
        for _ in range(4):
            try:
                probe = probe.get_left_lane()
            except Exception:
                probe = None
            if probe is None:
                break
            if lane_sign(probe) == lane_sign(wp) and is_driving(probe):
                back = probe
                break
        if back is None:
            return []
        plan.append((back, RIGHT))
        cur = back
        done = 0.0
        while done < merge_m:
            nxt = cur.next(step)
            if not nxt:
                break
            cur = nxt[0]
            done += step
            plan.append((cur, LANEFOLLOW))
        return plan

    def lane_change_path(self, agent, wp, direction: str, speed_mps: float) -> list:
        """A lane change path from the vendored agent, scaled with speed."""
        same = max(8.0, speed_mps * 1.0)
        change = max(15.0, speed_mps * 2.0)
        other = max(20.0, speed_mps * 2.0)
        try:
            return agent._generate_lane_change_path(wp, direction, same, other, change, True, 1, self._resolution)
        except Exception as exc:
            logger.debug("lane change path failed: %s", exc)
            return []
