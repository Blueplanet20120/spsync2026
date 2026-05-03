#!/usr/bin/env bash
# 调用 Python 实现（与 Windows 共用 tools/setup_local_venv.py）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1; then
    exec "$py" tools/setup_local_venv.py "$@"
  fi
done
echo "setup: need python3 on PATH" >&2
exit 1
