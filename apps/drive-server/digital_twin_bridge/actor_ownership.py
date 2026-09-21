"""Which CARLA actors the drive server may destroy (coexist branch).

The shared CARLA world also carries actors from other clients: the DT TENA
adapter's manual car (role_name PATH-M-1), Aimsun background traffic mirrored by
HIL_Tool (role_name bkg) and future scenario vehicles (PATH-BG-n).  With
PROTECT_FOREIGN_ACTORS on, startup cleanup, map-switch cleanup, the 60 s orphan
audit and the clear-non-ego action only touch actors this server created, which
are recognised by the role_name namespaces it uses.

Rules:
- vehicles and walkers: ours when role_name starts with one of DRIVE_ROLE_PREFIXES;
- a VOICES role (e.g. PATH-M-1 given to a browser session) is ours only when
  KEEP_VOICES_EGO is off, otherwise it is deliberately left in the world;
- sensors: ours when attached to one of our actors, or unattached (the camera pool);
- static props: always ours (nothing else on this host spawns props; DT draws
  work zones with debug shapes).
"""

DRIVE_ROLE_PREFIXES = ("ego_vehicle", "autopilot", "trajectory", "dynamic_geofence", "drive_")


def _role(actor) -> str:
    try:
        attrs = actor.attributes
        return str(attrs.get("role_name", "")) if attrs else ""
    except Exception:
        return ""


def is_drive_owned(actor, voices_roles=(), keep_voices: bool = True) -> bool:
    """True when the drive server may destroy ``actor`` during cleanup or audit."""
    type_id = str(getattr(actor, "type_id", "") or "")
    if type_id.startswith("sensor."):
        parent = getattr(actor, "parent", None)
        if parent is None:
            return True
        return is_drive_owned(parent, voices_roles, keep_voices)
    if type_id.startswith("static.prop."):
        return True
    role = _role(actor)
    if role and role in tuple(voices_roles):
        return not keep_voices
    return any(role.startswith(prefix) for prefix in DRIVE_ROLE_PREFIXES)
