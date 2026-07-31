"""
Geographic utility functions for the Digital Twin Camera Bridge.

Handles conversions between GPS (WGS-84) coordinates and CARLA's UE4
coordinate system, correcting for the left-handed Y-axis inversion.
"""

import math
import logging
import hashlib
from pathlib import Path
import sys
from typing import List, Tuple

import carla

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from v2x_common.geodesy import (  # noqa: E402
    GeodesyError,
    TransverseMercator,
    extract_opendrive_georeference,
)

logger = logging.getLogger(__name__)
_MAP_PROJECTION_CACHE = {}
MAX_STRICT_MAP_ORIGIN_ERROR_M = 0.5


def _projection_for_map(carla_map):
    """Return the map-declared projection, cached by exact georef content."""
    origin = carla_map.transform_to_geolocation(carla.Location())
    map_name = str(getattr(carla_map, "name", "unknown"))
    opendrive_sha256 = None
    georeference_sha256 = None
    try:
        opendrive = carla_map.to_opendrive()
        if not isinstance(opendrive, str) or not opendrive:
            raise GeodesyError("OpenDRIVE document is empty")
        opendrive_sha256 = hashlib.sha256(opendrive.encode("utf-8")).hexdigest()
        georeference = extract_opendrive_georeference(opendrive)
        georeference_sha256 = hashlib.sha256(
            georeference.encode("utf-8")
        ).hexdigest()
        key = (
            map_name,
            opendrive_sha256,
            georeference_sha256,
        )
        cached = _MAP_PROJECTION_CACHE.get(key)
        if cached is not None:
            return cached
        projection = TransverseMercator.from_proj_string(georeference)
        source = "opendrive_georeference"
    except (AttributeError, GeodesyError, RuntimeError) as exc:
        # Test doubles and legacy maps may not expose OpenDRIVE. Keep one
        # WGS-84 implementation rather than reintroducing degree constants,
        # but mark the missing map declaration loudly for acceptance evidence.
        projection = TransverseMercator.from_proj_string(
            f"+proj=tmerc +lat_0={float(origin.latitude):.15g} "
            f"+lon_0={float(origin.longitude):.15g} +k=1 +x_0=0 +y_0=0 "
            "+datum=WGS84 +units=m +no_defs"
        )
        source = "origin_centered_fallback"
        key = (
            map_name,
            "fallback",
            opendrive_sha256,
            round(float(origin.latitude), 12),
            round(float(origin.longitude), 12),
        )
        logger.warning(
            "Map %s lacks a usable OpenDRIVE georeference: %s",
            map_name,
            exc,
        )
    result = (
        projection,
        source,
        {
            "map_name": map_name,
            "opendrive_sha256": opendrive_sha256,
            "georeference_sha256": georeference_sha256,
        },
    )
    _MAP_PROJECTION_CACHE[key] = result
    return result


def map_projection_with_provenance(
    carla_map, *, require_opendrive_georeference=False
):
    """Return projection plus explicit provenance for acceptance callers.

    RR/CARLA 0.10 has no inverse geolocation API, so acceptance must use the
    map's actual OpenDRIVE projection.  The origin-centred construction remains
    available only for diagnostics and test doubles.  Strict mode also checks
    that the declared projection agrees with CARLA's world-origin geolocation;
    a syntactically valid declaration for another map is not acceptable.
    """
    projection, source, content_identity = _projection_for_map(carla_map)
    origin = carla_map.transform_to_geolocation(carla.Location())
    origin_error_m = None
    if source == "opendrive_georeference":
        origin_easting, origin_northing = projection.forward(
            float(origin.latitude), float(origin.longitude)
        )
        origin_error_m = math.hypot(origin_easting, origin_northing)
    if require_opendrive_georeference:
        if source != "opendrive_georeference":
            raise GeodesyError(
                "strict projection requires a usable OpenDRIVE georeference"
            )
        if (
            origin_error_m is None
            or not math.isfinite(origin_error_m)
            or origin_error_m > MAX_STRICT_MAP_ORIGIN_ERROR_M
        ):
            raise GeodesyError(
                "OpenDRIVE georeference disagrees with the CARLA map origin"
            )
    return projection, {
        "source": source,
        "strict": bool(require_opendrive_georeference),
        "map_origin_error_m": origin_error_m,
        **content_identity,
    }


def lateral_shift(transform: carla.Transform, shift: float) -> carla.Location:
    """Compute a lateral shift of a transform's forward vector.

    Rotates the transform's yaw by 90 degrees and moves ``shift`` metres
    along the resulting forward vector.  This is the standard technique
    used in CARLA's ``no_rendering_mode.py`` for computing lane edges.

    Args:
        transform: The reference transform (will be mutated -- pass a copy
            if you need to preserve the original).
        shift: Distance in metres.  Positive values shift to the right
            of the original forward direction.

    Returns:
        A :class:`carla.Location` offset from the original position.
    """
    transform.rotation.yaw += 90
    return transform.location + shift * transform.get_forward_vector()


def extract_road_network_gps(carla_map: carla.Map) -> List[List[List[float]]]:
    """Extract road edges as GPS polylines suitable for map rendering.

    Each polyline is a list of ``[longitude, latitude]`` pairs.  CARLA 0.9
    needs its historical UE4 latitude mirror, while CARLA 0.10 already emits
    correct WGS-84 coordinates and must be left unchanged.

    Args:
        carla_map: A :class:`carla.Map` obtained from the simulator.

    Returns:
        A list of polylines, each being a list of ``[lon, lat]`` pairs.
    """
    origin_geo = carla_map.transform_to_geolocation(carla.Location())
    origin_lat = origin_geo.latitude
    mirror_latitude = hasattr(carla_map, "geolocation_to_transform")

    def output_latitude(raw_latitude: float) -> float:
        if mirror_latitude:
            return 2 * origin_lat - raw_latitude
        return raw_latitude

    topology = [wp_pair[0] for wp_pair in carla_map.get_topology()]
    topology = sorted(topology, key=lambda w: w.transform.location.z)

    precision = 0.5  # metres between waypoints
    road_lines: List[List[List[float]]] = []

    for waypoint in topology:
        waypoints = [waypoint]
        nxt_list = waypoint.next(precision)
        if len(nxt_list) > 0:
            nxt = nxt_list[0]
            while nxt.road_id == waypoint.road_id:
                waypoints.append(nxt)
                nxt_list = nxt.next(precision)
                if len(nxt_list) > 0:
                    nxt = nxt_list[0]
                else:
                    break

        left_edge: List[List[float]] = []
        right_edge: List[List[float]] = []

        for w in waypoints:
            l_loc = lateral_shift(w.transform, -w.lane_width * 0.5)
            r_loc = lateral_shift(w.transform, w.lane_width * 0.5)

            if mirror_latitude:
                l_geo = carla_map.transform_to_geolocation(l_loc)
                r_geo = carla_map.transform_to_geolocation(r_loc)
                left_edge.append(
                    [l_geo.longitude, output_latitude(l_geo.latitude)]
                )
                right_edge.append(
                    [r_geo.longitude, output_latitude(r_geo.latitude)]
                )
            else:
                l_lat, l_lon = carla_to_gps(carla_map, l_loc)
                r_lat, r_lon = carla_to_gps(carla_map, r_loc)
                left_edge.append([l_lon, l_lat])
                right_edge.append([r_lon, r_lat])

        if len(left_edge) > 1:
            road_lines.append(left_edge)
            road_lines.append(right_edge)

    logger.info("Extracted %d road-edge polylines from CARLA map.", len(road_lines))
    return road_lines


def gps_to_carla(
    carla_map: carla.Map,
    lat: float,
    lon: float,
    *,
    require_opendrive_georeference: bool = False,
    return_projection_provenance: bool = False,
) -> carla.Location:
    """Convert GPS coordinates to a CARLA world Location.

    On CARLA 0.9, applies the UE4 Y-axis correction before calling
    :meth:`carla.Map.geolocation_to_transform`.  On CARLA 0.10, where that
    inverse API was removed, performs the local inverse projection from the
    map's georeference origin.

    The returned location is snapped to the nearest road surface when
    possible.

    Args:
        carla_map: The active CARLA map.
        lat: WGS-84 latitude in decimal degrees.
        lon: WGS-84 longitude in decimal degrees.

    Returns:
        A :class:`carla.Location` in CARLA world coordinates.
    """
    origin_geo = carla_map.transform_to_geolocation(carla.Location())
    origin_lat = origin_geo.latitude

    if hasattr(carla_map, "geolocation_to_transform"):
        # CARLA <= 0.9.x: geolocation_to_transform exists but mishandles the
        # UE left-handed Y-axis, so mirror latitude around the origin first.
        corrected_lat = 2 * origin_lat - lat

        geo = carla.GeoLocation(
            latitude=corrected_lat,
            longitude=lon,
            altitude=0.0,
        )
        projected = carla_map.geolocation_to_transform(geo)
        # CARLA 0.9.x returns a Transform here.  Some test doubles and older
        # PythonAPI builds expose the projected Location directly, so accept
        # both shapes at this version boundary.
        location = getattr(projected, "location", projected)
        projection_provenance = {
            "source": "carla_geolocation_to_transform",
            "strict": False,
            "map_origin_error_m": None,
        }
        if require_opendrive_georeference:
            raise GeodesyError(
                "strict projection requires RR/CARLA 0.10 OpenDRIVE provenance"
            )
    else:
        # CARLA 0.10 removed the inverse API. Invert the map's actual
        # OpenDRIVE transverse-Mercator declaration using the shared WGS-84
        # implementation; CARLA world x=easting and y=-northing.
        projection, projection_provenance = map_projection_with_provenance(
            carla_map,
            require_opendrive_georeference=require_opendrive_georeference,
        )
        easting, northing = projection.forward(float(lat), float(lon))
        location = carla.Location(
            x=easting,
            y=-northing,
            z=0.0,
        )

    # Snap Z to the road surface (coarse estimate from OpenDRIVE profile)
    wp = carla_map.get_waypoint(location, project_to_road=True)
    if wp is not None:
        location.z = wp.transform.location.z

    if return_projection_provenance:
        return location, projection_provenance
    return location


def carla_to_gps(
    carla_map: carla.Map, location: carla.Location
) -> Tuple[float, float]:
    """Convert a CARLA world Location to GPS coordinates.

    Applies the inverse of the UE4 Y-axis correction so the returned
    latitude matches real-world WGS-84.

    Args:
        carla_map: The active CARLA map.
        location: A :class:`carla.Location` in CARLA world coordinates.

    Returns:
        A ``(latitude, longitude)`` tuple in decimal degrees.
    """
    if not hasattr(carla_map, "geolocation_to_transform"):
        projection, source, _content_identity = _projection_for_map(carla_map)
        if source != "opendrive_georeference":
            geo = carla_map.transform_to_geolocation(location)
            return geo.latitude, geo.longitude
        return projection.inverse(float(location.x), -float(location.y))

    geo = carla_map.transform_to_geolocation(location)

    origin_geo = carla_map.transform_to_geolocation(carla.Location())
    origin_lat = origin_geo.latitude

    # Reverse the Y-axis mirror
    corrected_lat = 2 * origin_lat - geo.latitude

    return corrected_lat, geo.longitude


def compute_look_at_transform(
    target_location: carla.Location,
    offset_distance: float = 8.0,
    offset_height: float = 4.0,
) -> carla.Transform:
    """Compute a camera Transform that looks at a target location.

    The camera is placed ``offset_distance`` metres behind the target
    along the X-axis and ``offset_height`` metres above it.  Pitch and
    yaw are computed so the camera looks directly at the target.

    Args:
        target_location: The world location to look at.
        offset_distance: Horizontal offset in metres (default 8.0).
        offset_height: Vertical offset in metres (default 4.0).

    Returns:
        A :class:`carla.Transform` for the camera.
    """
    cam_loc = carla.Location(
        x=target_location.x - offset_distance,
        y=target_location.y,
        z=target_location.z + offset_height,
    )
    dx = target_location.x - cam_loc.x
    dy = target_location.y - cam_loc.y
    dz = target_location.z - cam_loc.z
    yaw = math.degrees(math.atan2(dy, dx))
    pitch = math.degrees(math.atan2(dz, math.sqrt(dx * dx + dy * dy)))

    return carla.Transform(cam_loc, carla.Rotation(pitch=pitch, yaw=yaw))
