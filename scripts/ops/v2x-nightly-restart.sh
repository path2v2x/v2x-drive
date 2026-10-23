#!/usr/bin/env bash
# Nightly preventive restart of the V2X drive stack (guards against slow
# degradation in long-running UE5/CARLA sessions). Skipped if a drive
# session is currently connected.
set -u
if ss -Htn state established '( sport = :8765 )' | grep -q .; then
    echo "nightly-restart: active drive session detected; skipping restart"
    exit 0
fi
# a DT session (TENA adapter) or a background-traffic bridge keeps actors in the world that
# a CARLA restart would destroy, and the adapter never re-binds them: leave it alone
if pgrep -f "bin/CARLAtenaAdapte[r]" >/dev/null || docker ps --format '{{.Names}}' | grep -qxE 'dt-core|feed-bridge|sumo-bg'; then
    echo "nightly-restart: DT session or slot-car bridge running; skipping restart"
    exit 0
fi
echo "nightly-restart: restarting CARLA container and drive service"
docker restart carla-rr-maps
systemctl restart v2x-drive.service
