#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f requirements.txt ]]; then
  python -m pip install --disable-pip-version-check --no-input --break-system-packages -r requirements.txt
fi

python -m py_compile main.py