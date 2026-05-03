#!/usr/bin/env python3
"""仓库根目录捷径：等同于 ``python tools/sync_to_gitee_local.py``。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
  root = Path(__file__).resolve().parent
  script = root / "tools" / "sync_to_gitee_local.py"
  if not script.is_file():
    print(f"missing {script}", file=sys.stderr)
    return 1
  return subprocess.run([sys.executable, str(script), *sys.argv[1:]]).returncode


if __name__ == "__main__":
  raise SystemExit(main())
