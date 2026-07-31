# Bridge test environments

Run the complete bridge suite with:

```bash
scripts/test-v2x-bridge.sh
```

The runner intentionally has two mandatory lanes because the runtime contracts
are incompatible:

- ordinary bridge tests run with the production CARLA Python 3.10 environment;
- `test_register_map_to_lidar.py` runs with the deterministic Python 3.12
  environment bound by `apps/bridge/tools/map_lidar_toolchain_lock.json`.

Both lanes promote every warning to an error. The runner clears
`PYTEST_ADDOPTS` and `PYTEST_PLUGINS`, overrides configured `addopts`, disables
third-party plugin autoload, explicitly loads pytest-asyncio, and accepts no
pytest selectors, so external or positional options cannot weaken collection.
Repository `conftest.py` files and pytest's internal plugins remain active. Its
`--ignore` in the first lane does not skip coverage: the excluded registration
file is executed in full by the second lane. The second lane also fixes every
thread-control variable required by the tracked lock.

Execute the runner directly; invoking it through another shell is rejected.
The first operation resolves the runner with absolute `/usr/bin/readlink -f`;
the verifier path is derived only from that canonical directory, never from a
symlink entry directory. The isolated absolute Python verifier then reads the
parent process through `/proc` and requires exactly `/bin/bash`,
`-p`, and this tracked runner as the sole script argument; `bash -c`, source
mode, extra arguments, a different executable, or a different resolved script
path terminates the isolated runner before repository discovery or test lanes.
The runner then enumerates functions with `builtin declare -F` and rejects every
predeclared function. Pytest receives absolute root/test paths, so the runner
does not dispatch `cd` or `pwd` while selecting either lane.
Its absolute privileged-Bash shebang prevents `BASH_ENV` startup sourcing and
function import. It additionally rejects `BASH_ENV`, `ENV`, surviving
`BASH_FUNC_*` payloads, or a `PATH` that resolves `env` anywhere except
`/usr/bin/env`, then replaces `PATH` with `/usr/bin:/bin` before external work.
It rejects inherited Python startup, path, optimization, warning, debugging,
user-site, and native-loader controls. Every Python preflight and test lane then
runs under `env -i` with only the documented home, locale, canonical path,
no-user-site/safe-path controls, and lane-specific variables. Python also runs
with `-S`, preventing `site`, `.pth`, `sitecustomize`, and `usercustomize`
startup execution; each lane receives only its hard-bound package roots.

The tracked adversarial check combines hostile `--collect-only`, `--ignore`,
and `-k` values and requires the complete 690-test and 97-test lane totals:

```bash
scripts/tests/test-v2x-bridge-runner.sh
```

On the Path PC the hard-bound interpreter paths are:

```text
/home/path/V2XCarla/carla-venv-310/bin/python
/home/path/V2XCarla/geospatial-venv/bin/python
```

Those interpreter paths are hard-bound for acceptance. The runner rejects
inherited `CARLA_PYTHON` or `MAP_LIDAR_PYTHON` variables rather than executing
an untrusted override. It also rejects the wrong Python version or a missing
dependency at either canonical path. At the 2026-07-14 inspection,
`/mnt/v2x-ue5/venvs/geospatial` contained the numerical toolchain but lacked
`pypdf`, `pytest`, and `pytest-asyncio`, so it is not an acceptance test
environment.
