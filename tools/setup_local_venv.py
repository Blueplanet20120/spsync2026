#!/usr/bin/env python3
"""
在仓库根目录创建 .venv（Python 3.11+）。推荐激活后用 ``python tools/sync_to_gitee_local.py`` 或 ``python sp_sync.py``。

说明：根目录的 .env 是密钥文件，不能与同名文件夹并存；虚拟环境目录惯例为 .venv。

用法（在仓库根）：
  python tools/setup_local_venv.py
  py -3 tools/setup_local_venv.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = REPO_ROOT / ".venv"
REQ_FILE = REPO_ROOT / "tools" / "requirements-sync.txt"

_WIN_PY_TAGS = ("-3.14", "-3.13", "-3.12", "-3.11")
_UNIX_PY_NAMES = ("python3.14", "python3.13", "python3.12", "python3.11", "python3")


def _quiet_run(argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
  kw: dict = {
    "stdout": subprocess.DEVNULL,
    "stderr": subprocess.DEVNULL,
    "text": True,
  }
  if env is not None:
    kw["env"] = env
  if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
    kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
  return subprocess.run(argv, **kw)


def _version_ok(exe: str) -> bool:
  try:
    r = _quiet_run([exe, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"])
    return r.returncode == 0
  except OSError:
    return False


def _pick_windows_creator() -> list[str] | None:
  try:
    _quiet_run(["py", "-c", "import sys"])
  except OSError:
    return None
  for tag in _WIN_PY_TAGS:
    r = _quiet_run(["py", tag, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"])
    if r.returncode == 0:
      return ["py", tag]
  return None


def _pick_unix_creator() -> list[str] | None:
  for name in _UNIX_PY_NAMES:
    exe = shutil.which(name)
    if exe and _version_ok(exe):
      return [exe]
  return None


def _venv_python() -> Path:
  if sys.platform == "win32":
    return VENV_DIR / "Scripts" / "python.exe"
  return VENV_DIR / "bin" / "python"


def main() -> int:
  os.chdir(REPO_ROOT)

  if VENV_DIR.is_dir():
    print("setup: .venv already exists; skip (delete folder to recreate).")
    return 0

  creator: list[str] | None
  if sys.platform == "win32":
    creator = _pick_windows_creator()
    if creator is None:
      print("setup: need Python 3.11+ with py launcher on PATH (install from python.org).", file=sys.stderr)
      return 1
  else:
    creator = _pick_unix_creator()
    if creator is None:
      print("setup: need python3.11+ on PATH.", file=sys.stderr)
      return 1

  print(f"setup: creating {VENV_DIR} with {' '.join(creator)}")
  try:
    subprocess.run(creator + ["-m", "venv", str(VENV_DIR)], check=True)
  except subprocess.CalledProcessError:
    return 1

  vpy = _venv_python()
  if not vpy.is_file():
    print(f"setup: missing venv interpreter: {vpy}", file=sys.stderr)
    return 1

  subprocess.run([str(vpy), "-m", "pip", "install", "--upgrade", "pip"], check=False)
  if REQ_FILE.is_file():
    subprocess.run([str(vpy), "-m", "pip", "install", "-r", str(REQ_FILE)], check=False)

  ver = subprocess.run(
    [str(vpy), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
    capture_output=True,
    text=True,
    check=True,
  ).stdout.strip()
  print(f"setup: done. Python {ver}")
  print("setup: run e.g. python tools/sync_to_gitee_local.py or python sp_sync.py")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
