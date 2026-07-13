#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

PYTHON_BIN=${KITTY_PYTHON:-}

is_supported_python() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

if [ -z "$PYTHON_BIN" ]; then
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && is_supported_python "$candidate"; then
      PYTHON_BIN=$candidate
      break
    fi
  done
fi

if [ -z "$PYTHON_BIN" ] && [ -x .venv/bin/python ] && is_supported_python .venv/bin/python; then
  PYTHON_BIN=.venv/bin/python
fi

if [ -z "$PYTHON_BIN" ] || ! is_supported_python "$PYTHON_BIN"; then
  echo "Kitty requires Python 3.11 or newer."
  echo "On macOS, run:"
  echo "  brew install python@3.12"
  echo "  KITTY_PYTHON=\"\$(brew --prefix python@3.12)/bin/python3.12\" sh scripts/bootstrap-local.sh"
  exit 1
fi

if [ -x .venv/bin/python ] && ! is_supported_python .venv/bin/python; then
  backup=".venv-py39-bak-$(date +%Y%m%d%H%M%S)"
  echo "Existing .venv uses an unsupported Python. Moving it to $backup"
  mv .venv "$backup"
fi

if [ ! -x .venv/bin/python ]; then
  "$PYTHON_BIN" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install .
.venv/bin/kitty --once hello

echo "Kitty local environment is ready."
