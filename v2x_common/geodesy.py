"""WGS-84 and OpenDRIVE transverse-Mercator conversion helpers.

The Richmond OpenDRIVE map declares a WGS-84 transverse-Mercator CRS. These
helpers implement the bounded local series directly so both Python runtimes use
one projection without requiring pyproj in the CARLA 3.10 environment.
"""

from dataclasses import dataclass
import math
import shlex
import xml.etree.ElementTree as ET

WGS84_A = 6_378_137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
WGS84_EP2 = WGS84_E2 / (1.0 - WGS84_E2)


class GeodesyError(ValueError):
    pass


def _meridional_arc(latitude_rad):
    e2 = WGS84_E2
    e4 = e2 * e2
    e6 = e4 * e2
    return WGS84_A * (
        (1.0 - e2 / 4.0 - 3.0 * e4 / 64.0 - 5.0 * e6 / 256.0)
        * latitude_rad
        - (3.0 * e2 / 8.0 + 3.0 * e4 / 32.0 + 45.0 * e6 / 1024.0)
        * math.sin(2.0 * latitude_rad)
        + (15.0 * e4 / 256.0 + 45.0 * e6 / 1024.0)
        * math.sin(4.0 * latitude_rad)
        - 35.0 * e6 / 3072.0 * math.sin(6.0 * latitude_rad)
    )


@dataclass(frozen=True)
class TransverseMercator:
    latitude_of_origin_deg: float
    central_meridian_deg: float
    scale_factor: float = 1.0
    false_easting_m: float = 0.0
    false_northing_m: float = 0.0

    @classmethod
    def from_proj_string(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise GeodesyError("map georeference is missing")
        parameters = {}
        for token in shlex.split(value):
            if not token.startswith("+"):
                continue
            key, separator, raw = token[1:].partition("=")
            parameters[key] = raw if separator else True
        if parameters.get("proj") != "tmerc":
            raise GeodesyError("only transverse-Mercator georeferences are supported")
        if parameters.get("datum", "WGS84").upper() != "WGS84":
            raise GeodesyError("only the WGS84 datum is supported")
        if parameters.get("units", "m") != "m":
            raise GeodesyError("map georeference units must be metres")
        try:
            projection = cls(
                latitude_of_origin_deg=float(parameters["lat_0"]),
                central_meridian_deg=float(parameters["lon_0"]),
                scale_factor=float(parameters.get("k", parameters.get("k_0", 1.0))),
                false_easting_m=float(parameters.get("x_0", 0.0)),
                false_northing_m=float(parameters.get("y_0", 0.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GeodesyError("map georeference parameters are incomplete") from exc
        if (
            not all(
                math.isfinite(value)
                for value in (
                    projection.latitude_of_origin_deg,
                    projection.central_meridian_deg,
                    projection.scale_factor,
                    projection.false_easting_m,
                    projection.false_northing_m,
                )
            )
            or projection.scale_factor <= 0.0
        ):
            raise GeodesyError("map georeference parameters are invalid")
        return projection

    def forward(self, latitude_deg, longitude_deg):
        if not math.isfinite(latitude_deg) or not math.isfinite(longitude_deg):
            raise GeodesyError("latitude/longitude must be finite")
        latitude = math.radians(latitude_deg)
        longitude = math.radians(longitude_deg)
        latitude0 = math.radians(self.latitude_of_origin_deg)
        longitude0 = math.radians(self.central_meridian_deg)
        delta_longitude = (longitude - longitude0 + math.pi) % (2.0 * math.pi) - math.pi
        sin_latitude = math.sin(latitude)
        cos_latitude = math.cos(latitude)
        tangent = math.tan(latitude)
        n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_latitude**2)
        t = tangent**2
        c = WGS84_EP2 * cos_latitude**2
        a = cos_latitude * delta_longitude
        meridian = _meridional_arc(latitude)
        meridian0 = _meridional_arc(latitude0)
        easting = self.false_easting_m + self.scale_factor * n * (
            a
            + (1.0 - t + c) * a**3 / 6.0
            + (5.0 - 18.0 * t + t**2 + 72.0 * c - 58.0 * WGS84_EP2)
            * a**5
            / 120.0
        )
        northing = self.false_northing_m + self.scale_factor * (
            meridian
            - meridian0
            + n
            * tangent
            * (
                a**2 / 2.0
                + (5.0 - t + 9.0 * c + 4.0 * c**2) * a**4 / 24.0
                + (
                    61.0
                    - 58.0 * t
                    + t**2
                    + 600.0 * c
                    - 330.0 * WGS84_EP2
                )
                * a**6
                / 720.0
            )
        )
        return easting, northing

    def inverse(self, easting_m, northing_m):
        if not math.isfinite(easting_m) or not math.isfinite(northing_m):
            raise GeodesyError("projected coordinates must be finite")
        latitude0 = math.radians(self.latitude_of_origin_deg)
        longitude0 = math.radians(self.central_meridian_deg)
        meridian0 = _meridional_arc(latitude0)
        meridian = meridian0 + (
            northing_m - self.false_northing_m
        ) / self.scale_factor
        e2 = WGS84_E2
        e4 = e2 * e2
        e6 = e4 * e2
        mu = meridian / (
            WGS84_A * (1.0 - e2 / 4.0 - 3.0 * e4 / 64.0 - 5.0 * e6 / 256.0)
        )
        e1 = (1.0 - math.sqrt(1.0 - e2)) / (1.0 + math.sqrt(1.0 - e2))
        footprint = (
            mu
            + (3.0 * e1 / 2.0 - 27.0 * e1**3 / 32.0) * math.sin(2.0 * mu)
            + (21.0 * e1**2 / 16.0 - 55.0 * e1**4 / 32.0)
            * math.sin(4.0 * mu)
            + 151.0 * e1**3 / 96.0 * math.sin(6.0 * mu)
            + 1097.0 * e1**4 / 512.0 * math.sin(8.0 * mu)
        )
        sin_footprint = math.sin(footprint)
        cos_footprint = math.cos(footprint)
        tangent = math.tan(footprint)
        n1 = WGS84_A / math.sqrt(1.0 - e2 * sin_footprint**2)
        r1 = WGS84_A * (1.0 - e2) / (
            1.0 - e2 * sin_footprint**2
        ) ** 1.5
        t1 = tangent**2
        c1 = WGS84_EP2 * cos_footprint**2
        d = (easting_m - self.false_easting_m) / (n1 * self.scale_factor)
        latitude = footprint - (n1 * tangent / r1) * (
            d**2 / 2.0
            - (5.0 + 3.0 * t1 + 10.0 * c1 - 4.0 * c1**2 - 9.0 * WGS84_EP2)
            * d**4
            / 24.0
            + (
                61.0
                + 90.0 * t1
                + 298.0 * c1
                + 45.0 * t1**2
                - 252.0 * WGS84_EP2
                - 3.0 * c1**2
            )
            * d**6
            / 720.0
        )
        longitude = longitude0 + (
            d
            - (1.0 + 2.0 * t1 + c1) * d**3 / 6.0
            + (
                5.0
                - 2.0 * c1
                + 28.0 * t1
                - 3.0 * c1**2
                + 8.0 * WGS84_EP2
                + 24.0 * t1**2
            )
            * d**5
            / 120.0
        ) / cos_footprint
        return math.degrees(latitude), math.degrees(longitude)


def extract_opendrive_georeference(opendrive):
    """Return the one authoritative, non-empty OpenDRIVE geoReference.

    A first-match text search is unsafe here: a document containing a second,
    conflicting declaration would silently select whichever element happened
    to appear first.  Parse the complete document and require cardinality one.
    CDATA is exposed as normal element text by ElementTree.
    """
    if not isinstance(opendrive, str):
        raise GeodesyError("OpenDRIVE content is missing")
    try:
        root = ET.fromstring(opendrive)
    except ET.ParseError as exc:
        raise GeodesyError("OpenDRIVE document is malformed") from exc
    declarations = [
        element
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "geoReference"
    ]
    if len(declarations) != 1:
        raise GeodesyError(
            "OpenDRIVE must contain exactly one authoritative georeference"
        )
    declaration = declarations[0]
    if list(declaration):
        raise GeodesyError("OpenDRIVE georeference contains nested markup")
    value = (declaration.text or "").strip()
    if not value:
        raise GeodesyError("OpenDRIVE georeference is empty")
    return value


def local_xz_to_geodetic(
    x_right_m,
    z_forward_m,
    origin_latitude_deg,
    origin_longitude_deg,
    heading_deg,
    projection,
):
    """Map one camera-local ground point into WGS-84 through the map CRS."""
    if isinstance(projection, str):
        projection = TransverseMercator.from_proj_string(projection)
    if not isinstance(projection, TransverseMercator):
        raise GeodesyError("an explicit transverse-Mercator projection is required")
    heading = math.radians(heading_deg)
    east = z_forward_m * math.sin(heading) + x_right_m * math.cos(heading)
    north = z_forward_m * math.cos(heading) - x_right_m * math.sin(heading)
    origin_east, origin_north = projection.forward(
        origin_latitude_deg, origin_longitude_deg
    )
    return projection.inverse(origin_east + east, origin_north + north)
