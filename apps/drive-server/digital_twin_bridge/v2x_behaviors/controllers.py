"""Vehicle controllers: the vendored CARLA agent adapted to the behaviours.

``V2xAgent`` is CARLA's ``BasicAgent`` (0.10.0 tuning) with three fixes the
behaviours need: the lateral offset is applied to both the lane-following
PID and the obstacle detector (the stock agent only updates one of them),
target speeds are set in m/s, and obstacle detection takes the vehicle list
the fleet already fetched instead of calling ``get_actors`` per vehicle per
tick.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def make_agent(vehicle, carla_map, grp, target_speed_mps: float = 8.0, offset_m: float = 0.0):
    """Build a V2xAgent (imports the vendored agents package lazily)."""
    from agents.navigation.basic_agent import BasicAgent

    class V2xAgent(BasicAgent):
        def __init__(self, vehicle, carla_map, grp, target_speed_kmh, offset):
            opt = {
                "target_speed": target_speed_kmh,
                "offset": offset,
                "ignore_traffic_lights": True,   # the behaviours decide at controls themselves
                "ignore_stop_signs": True,
                "base_vehicle_threshold": 6.0,
                "detection_speed_ratio": 1.0,
                "max_brake": 0.6,
                "max_throttle": 0.75,
                "max_steering": 0.8,
                "sampling_resolution": 2.0,
                "dt": 0.05,
            }
            super().__init__(vehicle, target_speed_kmh, opt_dict=opt, map_inst=carla_map, grp_inst=grp)
            self._stop_control = None
            self.last_hazard = False
            self.last_hazard_actor = None

        # ── parameters ──

        def set_lateral_offset(self, offset: float) -> None:
            if abs(offset - self._offset) < 1e-3:
                return
            self._offset = float(offset)
            self._local_planner.set_offset(float(offset))

        def lateral_offset(self) -> float:
            return self._offset

        def set_target_speed_mps(self, v: float) -> None:
            self.set_target_speed(max(0.0, float(v)) * 3.6)

        def plan_to(self, destination) -> int:
            self.set_destination(destination)
            return self.remaining()

        def set_plan(self, plan: list) -> None:
            self.set_global_plan(plan, stop_waypoint_creation=True, clean_queue=True)

        def remaining(self) -> int:
            return len(self._local_planner.get_plan())

        # ── control ──

        def run_control(self, stop: bool = False, vehicle_list=None, hard_stop: bool = False):
            """One control step; ``stop`` brakes to a halt, ``hard_stop`` holds with full brake."""
            import carla
            if hard_stop:
                return carla.VehicleControl(throttle=0.0, brake=1.0, steer=0.0, hand_brake=False)
            hazard = False
            hazard_actor = None
            if vehicle_list is not None and not self._ignore_vehicles:
                speed = self._vehicle.get_velocity().length()
                max_distance = self._base_vehicle_threshold + self._speed_ratio * speed
                try:
                    hazard, hazard_actor, _ = self._vehicle_obstacle_detected(vehicle_list, max_distance)
                except Exception as exc:
                    logger.debug("obstacle detection failed: %s", exc)
            self.last_hazard = bool(hazard)
            self.last_hazard_actor = getattr(hazard_actor, "id", None) if hazard else None
            control = self._local_planner.run_step()
            if stop or hazard:
                control.throttle = 0.0
                control.brake = max(control.brake, self._max_brake if hazard else 0.5)
                if stop and self._vehicle.get_velocity().length() < 0.3:
                    control.brake = 1.0
            return control

    return V2xAgent(vehicle, carla_map, grp, target_speed_mps * 3.6, offset_m)


def set_vehicle_lights(vehicle, mask: int, previous: Optional[int]) -> int:
    """Apply a light mask when it changed; returns the mask now applied."""
    if previous is not None and previous == mask:
        return mask
    try:
        import carla
        vehicle.set_light_state(carla.VehicleLightState(mask))
    except Exception as exc:
        logger.debug("set_light_state failed: %s", exc)
    return mask


def open_rear_doors(vehicle) -> bool:
    try:
        import carla
        door = getattr(carla, "VehicleDoor", None)
        if door is None:
            return False
        for name in ("RL", "RR"):
            which = getattr(door, name, None)
            if which is not None:
                vehicle.open_door(which)
        return True
    except Exception as exc:
        logger.debug("open_door failed: %s", exc)
        return False
