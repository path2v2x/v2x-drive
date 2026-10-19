"""SAE J2735 (2024) values the driving models interpret (pure constants).

The viewer's codec renders enumerations as their ASN.1 names ("inUse"); the
raw UDP path may carry integers.  Helpers accept both.
"""

from __future__ import annotations

# SirenInUse
SIREN_UNAVAILABLE = 0
SIREN_NOT_IN_USE = 1
SIREN_IN_USE = 2

# LightbarInUse
LIGHTBAR_UNAVAILABLE = 0
LIGHTBAR_NOT_IN_USE = 1
LIGHTBAR_IN_USE = 2
LIGHTBAR_YELLOW_CAUTION = 3

# ResponseType
RESPONSE_NOT_IN_USE = 0
RESPONSE_EMERGENCY = 1
RESPONSE_NON_EMERGENCY = 2
RESPONSE_PURSUIT = 3
RESPONSE_STATIONARY = 4
RESPONSE_SLOW_MOVING = 5
RESPONSE_STOP_AND_GO = 6

RESPONSE_NAMES = {
    RESPONSE_NOT_IN_USE: "notInUseOrNotEquipped",
    RESPONSE_EMERGENCY: "emergency",
    RESPONSE_NON_EMERGENCY: "nonEmergency",
    RESPONSE_PURSUIT: "pursuit",
    RESPONSE_STATIONARY: "stationary",
    RESPONSE_SLOW_MOVING: "slowMoving",
    RESPONSE_STOP_AND_GO: "stopAndGoMovement",
}

# BasicVehicleRole (subset)
ROLE_BASIC = 0
ROLE_ROAD_RESCUE = 5
ROLE_EMERGENCY = 6
ROLE_SAFETY_CAR = 7
ROLE_POLICE = 12
ROLE_FIRE = 13
ROLE_AMBULANCE = 14

ROLE_NAMES = {
    ROLE_BASIC: "basicVehicle",
    ROLE_ROAD_RESCUE: "roadRescue",
    ROLE_EMERGENCY: "emergency",
    ROLE_SAFETY_CAR: "safetyCar",
    ROLE_POLICE: "police",
    ROLE_FIRE: "fire",
    ROLE_AMBULANCE: "ambulance",
}

EMERGENCY_ROLES = {"roadRescue", "emergency", "safetyCar", "police", "fire", "ambulance"}

# CARLA vehicle light bits used for the emergency light bar / siren signalling
# (the DT adapter maps Special1 to sirenUse/lightsUse inUse).
LIGHT_NONE = 0
LIGHT_POSITION = 1 << 0
LIGHT_LOW_BEAM = 1 << 1
LIGHT_HIGH_BEAM = 1 << 2
LIGHT_BRAKE = 1 << 3
LIGHT_RIGHT_BLINKER = 1 << 4
LIGHT_LEFT_BLINKER = 1 << 5
LIGHT_REVERSE = 1 << 6
LIGHT_FOG = 1 << 7
LIGHT_INTERIOR = 1 << 8
LIGHT_SPECIAL1 = 1 << 9
LIGHT_SPECIAL2 = 1 << 10


def _enum_in(value, names: set, numbers: set) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) in numbers
    return str(value) in names


def siren_active(value) -> bool:
    """True when a BSM Part II sirenUse says the siren is in use."""
    return _enum_in(value, {"inUse"}, {SIREN_IN_USE})


def lights_active(value) -> bool:
    """True when a BSM Part II lightsUse says the light bar is in use."""
    return _enum_in(value, {"inUse", "yellowCautionLights"}, {LIGHTBAR_IN_USE, LIGHTBAR_YELLOW_CAUTION})


def response_stationary(value) -> bool:
    return _enum_in(value, {"stationary"}, {RESPONSE_STATIONARY})


def role_is_emergency(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) in {ROLE_ROAD_RESCUE, ROLE_EMERGENCY, ROLE_SAFETY_CAR, ROLE_POLICE, ROLE_FIRE, ROLE_AMBULANCE}
    return str(value) in EMERGENCY_ROLES


def role_name(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ROLE_NAMES.get(int(value), str(int(value)))
    return str(value or "")


def light_mask(lights: bool = False, siren: bool = False, left: bool = False, right: bool = False,
               hazard: bool = False, brake: bool = False, low_beam: bool = False) -> int:
    """Compose a CARLA VehicleLightState bit mask."""
    mask = LIGHT_NONE
    if lights:
        mask |= LIGHT_SPECIAL1
    if siren:
        mask |= LIGHT_SPECIAL1 | LIGHT_SPECIAL2
    if left or hazard:
        mask |= LIGHT_LEFT_BLINKER
    if right or hazard:
        mask |= LIGHT_RIGHT_BLINKER
    if brake:
        mask |= LIGHT_BRAKE
    if low_beam:
        mask |= LIGHT_LOW_BEAM
    return mask
