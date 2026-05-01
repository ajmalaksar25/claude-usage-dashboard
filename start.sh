#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating virtualenv..."
  "$PYTHON" -m venv .venv
  ".venv/bin/python" -m pip install --upgrade pip >/dev/null
  ".venv/bin/python" -m pip install -r requirements.txt
fi

exec ".venv/bin/python" dashboard.py "$@"
