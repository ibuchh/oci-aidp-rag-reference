#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
check_python() {
  .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "This pinned app requires Python 3.12 or newer. Set POC_PYTHON and recreate the app virtual environment.")'
}
if command -v uv >/dev/null 2>&1; then
  export UV_CACHE_DIR="$PWD/.runtime/uv-cache"
  uv venv --allow-existing .venv --python "${POC_PYTHON:-python3}"
  check_python
  uv pip sync --python .venv/bin/python requirements.txt
else
  "${POC_PYTHON:-python3}" -m venv .venv
  check_python
  .venv/bin/python -m pip install -r requirements.txt
fi
printf '\nReady. Start with ./run-local.sh --poc-config ../../poc-config.json\n'
