# Vendored CARLA `agents` package

Copied unchanged from the CARLA 0.10.0 PythonAPI shipped inside the RFS
simulator image:

- image: `ghcr.io/simforgeinc/carla-rr-maps:0.10.0`
- digest: `sha256:bf0c4a057ca8049bb1d82dbb337c3adbcaae395cc9dd7074186a6cc441020e44`
- path in the image: `/home/carla/PythonAPI/carla/agents`
- copied: 2026-10-18

License: MIT, Copyright (c) 2018-2020 Computer Vision Center (CVC) at the
Universitat Autonoma de Barcelona (UAB) and Intel Labs (see the file
headers). The `carla` wheel does not include this package, which is why it
lives here; `digital_twin_bridge/v2x_behaviors/controllers.py` subclasses
`BasicAgent` and uses `GlobalRoutePlanner` / `LocalPlanner`.

Dependencies: `networkx`, `shapely`, `numpy` (see `requirements.txt`).

Local modifications: none. Behaviour differences are implemented in the
subclass, so the package can be refreshed from a newer image by copying it
over this directory again.
