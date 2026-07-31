"""
Background poller for the V2X detection REST API.

Periodically fetches recent detections, converts their GPS coordinates to
CARLA world locations, and upserts them into the shared ObjectRegistry.
"""

import logging
import threading
from typing import Optional

import requests
import carla

from digital_twin_bridge.config import Config
from digital_twin_bridge.object_registry import ObjectRegistry
from digital_twin_bridge.geo_utils import gps_to_carla

logger = logging.getLogger(__name__)


class V2XPoller:
    """Polls the V2X REST API and feeds detections into an
    :class:`ObjectRegistry`.

    The poller runs in a daemon thread so it does not prevent the process
    from exiting.  Call :meth:`start` to begin polling and :meth:`stop`
    to request a graceful shutdown.
    """

    def __init__(
        self,
        config: Config,
        registry: ObjectRegistry,
        carla_map: Optional[carla.Map],
    ) -> None:
        self._config = config
        self._registry = registry
        self._carla_map = carla_map
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _prune_stale(self) -> int:
        """Expire old registry entries even when an API page is empty/down."""
        return self._registry.remove_stale(
            max_age_seconds=max(0.0, float(self._config.V2X_STALE_SECONDS))
        )

    def poll_once(self) -> int:
        """Execute a single poll cycle.

        Fetches detections from the V2X API, resolves their GPS
        coordinates to CARLA world locations, and updates the registry.

        Returns:
            The number of detections successfully processed.
        """
        try:
            resp = requests.get(
                self._config.V2X_API_URL,
                params={"limit": self._config.V2X_LIMIT},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error("V2X API request failed: %s", exc)
            self._prune_stale()
            return 0

        if not isinstance(data, dict):
            logger.error("V2X API returned a non-object response.")
            self._prune_stale()
            return 0

        items = data.get("items", [])
        if not isinstance(items, list):
            logger.error("V2X API response 'items' is not a list.")
            self._prune_stale()
            return 0
        if not items:
            stale_objects = self._prune_stale()
            logger.debug(
                "V2X API returned 0 detections; expired %d stale objects.",
                stale_objects,
            )
            return 0

        # Update the registry (this handles upserting)
        self._registry.update_from_v2x(items)

        # Resolve CARLA locations for objects that need it.  A state-only
        # registry deliberately passes ``None`` here: publishing detections
        # does not require or authorize spawning/moving CARLA actors.
        # Note: Prop spawning is handled on the main thread (see __main__.py)
        # because CARLA actor operations are not thread-safe.
        resolved = 0
        actor_owned = 0
        for item in (items if self._carla_map is not None else []):
            gps = item.get("gps_location", {})
            lat = gps.get("latitude")
            lon = gps.get("longitude")
            base_id = item.get("object_id", "")
            if lat is None or lon is None or not base_id:
                continue

            lat_f = float(lat)
            lon_f = float(lon)
            uid = ObjectRegistry._make_unique_id(base_id, lat_f, lon_f)
            obj = self._registry.get_by_id(uid)
            if obj is None:
                continue

            # Only resolve location if the object doesn't already have
            # a spawned actor (the actor's live location is more accurate)
            if obj.carla_actor_id is not None:
                actor_owned += 1
                continue

            try:
                location = gps_to_carla(self._carla_map, lat_f, lon_f)
                obj.carla_location = location
                resolved += 1
            except Exception:
                logger.warning(
                    "Failed to resolve CARLA location for object %s.",
                    uid,
                    exc_info=True,
                )

        # Clean up objects that haven't been seen in a while
        stale_objects = self._prune_stale()
        # Stale actor destruction is handled on the main thread

        logger.info(
            "V2X poll: %d items fetched, %d locations resolved, "
            "%d actor-owned, %d tracked.",
            len(items),
            resolved,
            actor_owned,
            self._registry.count,
        )
        return len(items)

    def start(self) -> None:
        """Start the background polling thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("V2X poller is already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="v2x-poller", daemon=True
        )
        self._thread.start()
        logger.info(
            "V2X poller started (interval=%.1fs, limit=%d).",
            self._config.V2X_POLL_INTERVAL,
            self._config.V2X_LIMIT,
        )

    def stop(self) -> None:
        """Signal the polling thread to stop and wait for it to finish."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._config.V2X_POLL_INTERVAL + 5)
            if self._thread.is_alive():
                logger.warning("V2X poller thread did not terminate in time.")
            else:
                logger.info("V2X poller stopped.")
        self._thread = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Main loop executed in the background thread."""
        logger.debug("V2X poller thread started.")
        # Perform an initial poll immediately
        self.poll_once()

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._config.V2X_POLL_INTERVAL)
            if self._stop_event.is_set():
                break
            try:
                self.poll_once()
            except Exception:
                logger.error(
                    "Unhandled exception in V2X poller loop.", exc_info=True
                )
        logger.debug("V2X poller thread exiting.")
