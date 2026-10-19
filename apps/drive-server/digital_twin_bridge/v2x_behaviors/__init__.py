"""
V2X-reactive driving models.

Pure modules (no CARLA import, unit tested with plain numbers):
``bsm_types``, ``profiles``, ``geometry``, ``erv_tracker``, ``civilian_fsm``,
``erv_fsm``.  CARLA-bound modules: ``road_context``, ``route_planner``,
``controllers``, ``feed_sources``, ``fleet``.

The CARLA ``agents`` package is vendored next to ``digital_twin_bridge``
(``apps/drive-server/agents``); make sure that directory is importable no
matter which working directory the server was launched from.
"""

import os as _os
import sys as _sys

_SERVER_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _os.path.isdir(_os.path.join(_SERVER_ROOT, "agents")) and _SERVER_ROOT not in _sys.path:
    _sys.path.append(_SERVER_ROOT)
