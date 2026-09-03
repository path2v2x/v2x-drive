"""
Local publisher -- writes state.json, map data and object snapshots to the
directory nginx serves at the site's data base URL (``/data`` by default).

The web dashboard polls ``api/state.json``; the map overlay reads
``api/map-data.json``; snapshot URLs point at ``snapshots/<id>/latest.jpg``.
Files are written atomically (temp file + rename) so readers never see a
partial JSON document.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List

from digital_twin_bridge.config import Config

logger = logging.getLogger(__name__)


def _write_atomic(path: str, data: bytes) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".publish-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


class StatePublisher:
    """Publishes dashboard data files under ``config.PUBLISH_DIR``."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._root = config.PUBLISH_DIR
        self._base_url = config.PUBLISH_BASE_URL.rstrip("/")
        os.makedirs(os.path.join(self._root, "api"), exist_ok=True)
        os.makedirs(os.path.join(self._root, "snapshots"), exist_ok=True)

    def describe(self) -> str:
        return f"{self._root} (served at {self._base_url}/)"

    def publish_snapshot(
        self,
        object_id: str,
        jpeg_bytes: bytes,
        metadata: Dict[str, str],
    ) -> str:
        """Write a JPEG snapshot and return the URL stored in state.json."""
        safe_id = object_id.replace("/", "_").replace("..", "_")
        relative = f"snapshots/{safe_id}/latest.jpg"
        _write_atomic(os.path.join(self._root, relative), jpeg_bytes)
        _write_atomic(
            os.path.join(self._root, f"snapshots/{safe_id}/latest.json"),
            json.dumps({k: str(v) for k, v in metadata.items()}).encode(),
        )
        return f"{self._base_url}/{relative}"

    def publish_state(
        self,
        objects: List[Dict[str, Any]],
        bridge_status: Dict[str, Any],
    ) -> None:
        state = {
            "objects": objects,
            "bridge_status": bridge_status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _write_atomic(os.path.join(self._root, "api", "state.json"), json.dumps(state).encode())
        except OSError as exc:
            logger.error("Failed to publish state.json: %s", exc)

    def publish_map_data(self, map_data: Dict[str, Any]) -> None:
        try:
            _write_atomic(
                os.path.join(self._root, "api", "map-data.json"),
                json.dumps(map_data).encode(),
            )
            logger.info("Published map data to %s", self._root)
        except OSError as exc:
            logger.error("Failed to publish map data: %s", exc)
