"""
Export and upload the CARLA road network as a JSON file.

Used to provide the front-end map layer with road geometry without
requiring a live CARLA connection from the browser.
"""

import json
import os
import re
import logging
from typing import Any, Dict, List, Optional

import carla

from digital_twin_bridge.carla_connection import CarlaConnection
from digital_twin_bridge.geo_utils import extract_road_network_gps

logger = logging.getLogger(__name__)


class MapDataExporter:
    """Extracts the CARLA road network and geo-reference metadata,
    then exports it as JSON for consumption by the web front-end.
    """

    def __init__(self, connection: CarlaConnection) -> None:
        self._conn = connection

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    def export_road_network(self) -> List[List[List[float]]]:
        """Return the road network as a list of GPS polylines.

        Each polyline is a list of ``[longitude, latitude]`` pairs with
        the UE4 Y-axis correction already applied.
        """
        return extract_road_network_gps(self._conn.carla_map)

    def _extract_geo_ref(self) -> Dict[str, Any]:
        """Gather geo-reference metadata from the CARLA map."""
        carla_map = self._conn.carla_map

        origin_geo = carla_map.transform_to_geolocation(carla.Location())
        geo_info: Dict[str, Any] = {
            "map_name": carla_map.name,
            "origin_lat": origin_geo.latitude,
            "origin_lon": origin_geo.longitude,
            "origin_alt": origin_geo.altitude,
        }

        # Try to extract the PROJ4 string from the OpenDRIVE XML
        try:
            odr_xml = carla_map.to_opendrive()
            match = re.search(
                r"<geoReference>\s*<!\[CDATA\[(.*?)\]\]>\s*</geoReference>",
                odr_xml,
                re.DOTALL,
            )
            if match:
                geo_info["proj_string"] = match.group(1).strip()
            else:
                match = re.search(
                    r"<geoReference>(.*?)</geoReference>",
                    odr_xml,
                    re.DOTALL,
                )
                geo_info["proj_string"] = (
                    match.group(1).strip() if match else ""
                )
        except Exception:
            geo_info["proj_string"] = ""

        return geo_info

    # ------------------------------------------------------------------
    # Export to file
    # ------------------------------------------------------------------

    def export_to_json(self, filepath: str) -> Dict[str, Any]:
        """Export road network and geo-reference to a JSON file.

        Args:
            filepath: Destination path for the JSON file.  Parent
                directories are created automatically.

        Returns:
            The payload dict that was written.
        """
        logger.info("Extracting road network for JSON export ...")
        road_lines = self.export_road_network()
        geo_ref = self._extract_geo_ref()

        payload: Dict[str, Any] = {
            "geo_ref": geo_ref,
            "road_network": road_lines,
        }

        dirpath = os.path.dirname(filepath)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        logger.info(
            "Map data exported: %s (%d polylines).",
            os.path.abspath(filepath),
            len(road_lines),
        )
        return payload
