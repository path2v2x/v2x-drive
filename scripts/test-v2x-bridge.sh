#!/bin/bash -p
canonical_runner="$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" \
  || /bin/kill -KILL "$$"
/usr/bin/env -i PATH=/usr/bin:/bin /usr/bin/python3 -I \
  "${canonical_runner%/*}/verify-v2x-bridge-runner-process.py" \
  || /bin/kill -KILL "$$"
set -euo pipefail

if [[ $- != *p* ]]; then
  echo "runner must be executed directly with privileged Bash startup isolation" >&2
  exit 2
fi
if [[ -n $(builtin declare -F) ]]; then
  echo "predeclared shell functions are not accepted" >&2
  exit 2
fi
if [[ -n ${BASH_ENV:-} || -n ${ENV:-} ]]; then
  echo "BASH_ENV and ENV startup hooks are not accepted" >&2
  exit 2
fi
if [[ "$(type -P env || true)" != "/usr/bin/env" ]]; then
  echo "PATH resolves env outside the trusted system location" >&2
  exit 2
fi
PATH=/usr/bin:/bin
export PATH
if /usr/bin/grep -zq '^BASH_FUNC_' "/proc/$$/environ"; then
  echo "inherited shell functions are not accepted" >&2
  exit 2
fi
unsafe_environment=(
  PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONINSPECT PYTHONWARNINGS
  PYTHONBREAKPOINT PYTHONSAFEPATH PYTHONOPTIMIZE PYTHONUSERBASE
  LD_PRELOAD LD_LIBRARY_PATH
)
for variable in "${unsafe_environment[@]}"; do
  if [[ -n ${!variable:-} ]]; then
    echo "inherited Python and loader controls are not accepted: $variable" >&2
    exit 2
  fi
done

if (( $# != 0 )); then
  echo "usage: $0" >&2
  echo "This runner does not accept pytest selectors; both complete lanes are mandatory." >&2
  exit 2
fi

runner_path="$canonical_runner"
runner_suffix="/scripts/test-v2x-bridge.sh"
if [[ $runner_path != *"$runner_suffix" ]]; then
  echo "runner path does not match the tracked repository layout" >&2
  exit 2
fi
repo_root="${runner_path%"$runner_suffix"}"
bridge_dir="$repo_root/apps/bridge"
carla_python="/home/path/V2XCarla/carla-venv-310/bin/python"
map_lidar_python="/home/path/V2XCarla/geospatial-venv/bin/python"
carla_packages="/home/path/V2XCarla/carla-venv-310/lib/python3.10/site-packages"
map_lidar_packages="/home/path/V2XCarla/geospatial-venv/lib/python3.12/site-packages:/home/path/.local/lib/python3.12/site-packages:/usr/local/lib/python3.12/dist-packages:/usr/lib/python3/dist-packages"

if [[ -v CARLA_PYTHON || -v MAP_LIDAR_PYTHON ]]; then
  echo "CARLA_PYTHON and MAP_LIDAR_PYTHON overrides are not accepted" >&2
  exit 2
fi

for python_bin in "$carla_python" "$map_lidar_python"; do
  if [[ ! -x "$python_bin" ]]; then
    echo "required Python interpreter is not executable: $python_bin" >&2
    exit 1
  fi
done

/usr/bin/env -i \
  HOME=/home/path \
  LANG=C.UTF-8 \
  PATH=/usr/bin:/bin \
  PYTHONNOUSERSITE=1 \
  PYTHONPATH="$carla_packages" \
  PYTHONSAFEPATH=1 \
  "$carla_python" -S - <<'PY'
import importlib.util
import sys
from importlib.metadata import version

if sys.version_info[:2] != (3, 10):
    raise SystemExit(f"CARLA bridge tests require Python 3.10, got {sys.version.split()[0]}")
for module in ("carla", "cv2", "pytest", "pytest_asyncio"):
    if importlib.util.find_spec(module) is None:
        raise SystemExit(f"CARLA bridge test dependency is missing: {module}")
if version("pytest-asyncio") != "1.4.0":
    raise SystemExit("CARLA bridge tests require pytest-asyncio 1.4.0")
PY

/usr/bin/env -i \
  HOME=/home/path \
  LANG=C.UTF-8 \
  PATH=/usr/bin:/bin \
  PYTHONNOUSERSITE=1 \
  PYTHONPATH="$map_lidar_packages" \
  PYTHONSAFEPATH=1 \
  "$map_lidar_python" -S - <<'PY'
import importlib.util
import sys
from importlib.metadata import version

if sys.version_info[:2] != (3, 12):
    raise SystemExit(
        f"map/LiDAR registration tests require Python 3.12, got {sys.version.split()[0]}"
    )
for module in (
    "carla", "cryptography", "laspy", "numpy", "PIL", "pypdf", "pyproj",
    "pytest", "pytest_asyncio", "scipy",
):
    if importlib.util.find_spec(module) is None:
        raise SystemExit(f"map/LiDAR test dependency is missing: {module}")
if version("pytest-asyncio") != "1.4.0":
    raise SystemExit("map/LiDAR tests require pytest-asyncio 1.4.0")
PY

echo "[bridge] CARLA Python 3.10 lane"
(
  /usr/bin/env -i \
    HOME=/home/path \
    LANG=C.UTF-8 \
    PATH=/usr/bin:/bin \
    PYTHONWARNINGS=error \
    PYTHONNOUSERSITE=1 \
    PYTHONPATH="$bridge_dir:$carla_packages" \
    PYTHONSAFEPATH=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    "$carla_python" -S -m pytest \
      -o addopts= \
      -W error \
      -p pytest_asyncio.plugin \
      --rootdir="$bridge_dir" \
      "$bridge_dir/tests" \
      --ignore="$bridge_dir/tests/test_register_map_to_lidar.py"
)

echo "[bridge] pinned map/LiDAR Python 3.12 lane"
(
  /usr/bin/env -i \
    HOME=/home/path \
    LANG=C.UTF-8 \
    PATH=/usr/bin:/bin \
    PYTHONWARNINGS=error \
    PYTHONNOUSERSITE=1 \
    PYTHONPATH="$bridge_dir:$map_lidar_packages" \
    PYTHONSAFEPATH=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_CORETYPE=Haswell \
    OPENBLAS_NUM_THREADS=1 \
    VECLIB_MAXIMUM_THREADS=1 \
    "$map_lidar_python" -S -m pytest \
      -o addopts= \
      -W error \
      -p pytest_asyncio.plugin \
      --rootdir="$bridge_dir" \
      "$bridge_dir/tests/test_register_map_to_lidar.py"
)
