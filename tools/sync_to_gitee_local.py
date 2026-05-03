#!/usr/bin/env python3
"""
**本机专用入口**：`sync_to_gitee_local.py`（菜单、克隆、路径、Windows SSH 等本地化事宜）。

与 GitHub Actions / `.github` 无绑定；云端另有调度方式，不必对齐其它行为。

行为说明：
- **pull / push / all**：均由**本脚本**编排；实际补丁、`verify_patches`、以及随后的 git 操作通过子进程调用
  ``tools/sync_to_gitee.py`` 中的**同一份实现**完成——**仅补丁相关逻辑**与云端共用（单一维护点），其它 CI 语义不必理会。
- **TTY**：交互终端下透传 stdout/stderr，便于 `git fetch` 等显示进度。
- **默认 workdir**：若仓库根已有 `.git` 则用根目录；否则用 `./sunnypilot`。**若两者皆无**，首次会自动
  ``git clone``：**默认从 GitHub** ``https://github.com/sunnypilot/sunnypilot.git`` **的 staging**（含子模块浅克隆）。
  之后在本机选 **pull** 打国内化补丁、选 **push** **强推到 Gitee** ``xc2026/sunnypilot_cn``（默认目标由共用补丁模块内的配置决定）。
  可用 ``SP_SYNC_CLONE_URL`` / ``SP_SYNC_CLONE_BRANCH`` 或 ``.env`` 覆盖克隆地址与分支。
- **选项 5（comma 上编 installer）**：远端设备上的 `installer.cc` 需单独打补丁；内嵌逻辑须与共用补丁模块里的
  ``patch_installer_urls`` **保持同步**（见 ``tools/sync_to_gitee.py`` 与下方 heredoc）。

职责划分：**本脚本** = 本地化编排；**``tools/sync_to_gitee.py``** = 补丁实现载体（亦被 CI 调用），不要在文案里把它说成「云端替你推送」——推送是你在本机选菜单/命令后，由本脚本拉起子进程完成的。

用法（在仓库根；建议先 ``setup_local_venv.cmd`` 或 ``python tools/setup_local_venv.py``，再激活 ``.venv``）：
  python tools/sync_to_gitee_local.py --action pull
  python sp_sync.py --action pull
  Git Bash 也可用 ./sp-sync（同上）。

本地化差异（仍共用补丁逻辑）：
- SP_SYNC_SOURCE=local：上游默认只 `git fetch upstream <SYNC_BRANCHES…> --prune`，不扫全仓库 tag，
  减少无关传输；对象仍是按需增量（并非每次整仓重下）。要与 CI 完全一致则设 SYNC_FULL_UPSTREAM_FETCH=1。
- SYNC_LOCAL_GIT_PROGRESS=1：fetch 附加 --progress；长耗时 git 直连终端。可用 SYNC_LOCAL_GIT_PROGRESS=0 关闭。
- 仍需 fetch（不可跳过）：要用远端分支 HEAD 判断是否该打补丁；跳过会与 Gitee/CI 脱节。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import datetime
from pathlib import Path
from typing import Optional

# 本地无仓库时自动 clone：默认 GitHub staging（含子模块）；后续 pull/push 由本脚本编排，补丁与默认 Gitee 远端在共用模块内
_DEFAULT_CLONE_URL = "https://github.com/sunnypilot/sunnypilot.git"
_DEFAULT_CLONE_BRANCH = "staging"


def _dir_nonempty(path: Path) -> bool:
  if not path.is_dir():
    return False
  return any(path.iterdir())


def _clone_sunnypilot(repo_root: Path, nested: Path) -> None:
  env = _local_env_with_dotenv(repo_root)
  url = (env.get("SP_SYNC_CLONE_URL") or "").strip().strip('"').strip("'") or _DEFAULT_CLONE_URL
  branch = (env.get("SP_SYNC_CLONE_BRANCH") or "").strip().strip('"').strip("'") or _DEFAULT_CLONE_BRANCH

  if nested.exists():
    if (nested / ".git").is_dir():
      return
    if _dir_nonempty(nested):
      raise SystemExit(
        f"[错误] 目录已存在但不是 git 仓库：{nested}\n"
        "请删除、改名该文件夹，或手动 clone 后再运行。"
      )

  cmd = [
    "git",
    "clone",
    "--branch",
    branch,
    "--single-branch",
    "--depth",
    "1",
    "--recurse-submodules",
    "--shallow-submodules",
    url,
    str(nested),
  ]
  print(
    f"[setup] 未找到本地仓库，从上游克隆 staging + 子模块（浅）：{url} → {nested}\n"
    "       之后在菜单选 pull 打补丁、选 push：由本脚本拉起子进程完成（默认强推 Gitee xc2026/sunnypilot_cn）。"
  )
  try:
    subprocess.run(cmd, env=os.environ.copy(), check=True)
  except FileNotFoundError as e:
    raise SystemExit("[错误] 未找到 git 命令，请先安装 Git 并加入 PATH。") from e
  except subprocess.CalledProcessError as e:
    raise SystemExit(f"[错误] git clone 失败（退出码 {e.returncode}）。请检查网络与仓库 URL。") from e


def _resolve_git_workdir(repo_root: Path) -> Path:
  """与 sync_to_gitee.default_workdir 一致：优先扁平仓库根，否则 ./sunnypilot；若无则自动 clone。"""
  if (repo_root / ".git").is_dir():
    return repo_root
  nested = repo_root / "sunnypilot"
  if (nested / ".git").is_dir():
    return nested
  _clone_sunnypilot(repo_root, nested)
  if not (nested / ".git").is_dir():
    raise SystemExit(f"[错误] clone 后仍未找到 .git：{nested}")
  return nested


def _inject_workdir_argv(repo_root: Path, argv: list[str]) -> list[str]:
  if _argv_has_long_opt(argv, "--workdir"):
    return argv
  wd = _resolve_git_workdir(repo_root)
  return ["--workdir", str(wd)] + argv


def _ssh_user_known_hosts_opt() -> str:
  """OpenSSH：Windows 使用 NUL，Unix 使用 /dev/null（与 sync_to_gitee.py 中远端逻辑无关，仅本机 SSH/SCP）。"""
  return "UserKnownHostsFile=NUL" if os.name == "nt" else "UserKnownHostsFile=/dev/null"


def _patch_impl_remote_transport_for_windows(impl) -> None:
  """覆盖已加载模块上的 run_ssh / scp_from，避免 Win32 OpenSSH 不识别 /dev/null。"""
  if os.name != "nt":
    return

  import subprocess

  kh = _ssh_user_known_hosts_opt()

  def run_ssh(host: str, user: str, key_path: str, remote_cmd: str, timeout_s: int = 3600) -> str:
    key = Path(key_path)
    if not key.exists():
      raise RuntimeError(f"未找到 comma SSH 私钥: {key}")
    cmd = [
      "ssh",
      "-i",
      str(key),
      "-p",
      "22",
      "-o",
      "StrictHostKeyChecking=no",
      "-o",
      kh,
      f"{user}@{host}",
      remote_cmd,
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout_s)
    if p.returncode != 0:
      raise RuntimeError(f"SSH 命令失败: {user}@{host}\n{p.stdout}")
    return p.stdout

  def scp_from(host: str, user: str, key_path: str, remote_path: str, local_path: Path, timeout_s: int = 600) -> None:
    key = Path(key_path)
    if not key.exists():
      raise RuntimeError(f"未找到 comma SSH 私钥: {key}")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
      "scp",
      "-i",
      str(key),
      "-P",
      "22",
      "-o",
      "StrictHostKeyChecking=no",
      "-o",
      kh,
      f"{user}@{host}:{remote_path}",
      str(local_path),
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout_s)
    if p.returncode != 0:
      raise RuntimeError(f"SCP 下载失败: {remote_path} -> {local_path}\n{p.stdout}")

  impl.run_ssh = run_ssh  # type: ignore[method-assign]
  impl.scp_from = scp_from  # type: ignore[method-assign]


def _local_env_with_dotenv(repo_root: Path) -> dict[str, str]:
  env = os.environ.copy()
  for k, v in _parse_dotenv(repo_root).items():
    env.setdefault(k, v)
  return env


def _argv_has_long_opt(argv: list[str], flag: str) -> bool:
  if flag in argv:
    return True
  eq = flag + "="
  return any(a.startswith(eq) for a in argv)


def _ensure_local_defaults(repo_root: Path, argv: list[str]) -> list[str]:
  """
  在未显式传入时，为本机补上 `--comma-key`、`--installer-outdir`，避免沿用 CI 脚本里写死的 Linux 默认路径。
  优先级：命令行已有参数 > 环境变量 / .env（COMMA_KEY、INSTALLER_OUTDIR）> 仓库根下的 sp-cn / 仓库根目录。
  """
  env = _local_env_with_dotenv(repo_root)
  prefix: list[str] = []

  if not _argv_has_long_opt(argv, "--comma-key"):
    key = (env.get("COMMA_KEY") or "").strip().strip('"').strip("'")
    if not key:
      key = str((repo_root / "sp-cn").resolve())
    prefix.extend(["--comma-key", key])

  if not _argv_has_long_opt(argv, "--installer-outdir"):
    out = (env.get("INSTALLER_OUTDIR") or "").strip().strip('"').strip("'")
    if not out:
      out = str(repo_root.resolve())
    prefix.extend(["--installer-outdir", out])

  return prefix + argv


def _venv_python(repo_root: Path) -> Optional[Path]:
  """项目根目录下由 setup_local_venv 创建的虚拟环境解释器。"""
  if os.name == "nt":
    p = repo_root / ".venv" / "Scripts" / "python.exe"
  else:
    p = repo_root / ".venv" / "bin" / "python"
    if not p.is_file():
      p3 = repo_root / ".venv" / "bin" / "python3"
      if p3.is_file():
        p = p3
  return p if p.is_file() else None


def _maybe_reexec_with_local_venv(repo_root: Path) -> None:
  """
  若存在 .venv 且当前不是该解释器，则用 .venv 重启本脚本（避免系统自带的旧 Python 缺 zoneinfo）。
  设 SP_SYNC_NO_VENV_REEXEC=1 可跳过。
  """
  if (os.environ.get("SP_SYNC_NO_VENV_REEXEC") or "").strip():
    return
  vpy = _venv_python(repo_root)
  if vpy is None:
    return
  try:
    if Path(sys.executable).resolve() == vpy.resolve():
      return
  except OSError:
    return
  local_py = repo_root / "tools" / "sync_to_gitee_local.py"
  rc = subprocess.run([str(vpy), str(local_py)] + sys.argv[1:], env=os.environ.copy()).returncode
  raise SystemExit(rc)


def is_tty() -> bool:
  try:
    return sys.stdout.isatty() and sys.stdin.isatty()
  except Exception:
    return False


def _load_impl(repo_root: Path):
  # Load `tools/sync_to_gitee.py` as a module (tools/ isn't a package).
  import importlib.util
  impl_path = repo_root / "tools" / "sync_to_gitee.py"
  spec = importlib.util.spec_from_file_location("sync_to_gitee_impl", impl_path)
  if spec is None or spec.loader is None:
    raise RuntimeError(f"无法加载脚本模块: {impl_path}")
  impl = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(impl)  # type: ignore[attr-defined]
  return impl


def _run_impl(repo_root: Path, args: list[str], stream: bool = True) -> int:
  impl_path = repo_root / "tools" / "sync_to_gitee.py"
  cmd = [sys.executable, str(impl_path)] + args
  env = os.environ.copy()
  env.setdefault("GIT_TERMINAL_PROMPT", "0")
  # 与裸跑 sync_to_gitee.py 区分：子进程内可读 SP_SYNC_SOURCE / SYNC_LOCAL_GIT_PROGRESS
  env.setdefault("SP_SYNC_SOURCE", "local")
  env.setdefault("SYNC_LOCAL_GIT_PROGRESS", "1")
  if stream and is_tty() and not env.get("SYNC_TO_GITEE_NO_STREAM", "").strip():
    return subprocess.run(cmd, env=env).returncode
  p = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
  if p.stdout:
    sys.stdout.write(p.stdout)
  return p.returncode


def _parse_dotenv(repo_root: Path) -> dict[str, str]:
  dotenv = repo_root / ".env"
  if not dotenv.exists():
    return {}
  out: dict[str, str] = {}
  for line in dotenv.read_text(encoding="utf-8", errors="ignore").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
      continue
    k, v = line.split("=", 1)
    out[k.strip()] = v.strip().strip('"').strip("'")
  return out


def _gitee_owner_repo_from_url(url: str) -> tuple[str, str]:
  m = re.fullmatch(r"https?://gitee\.com/([^/]+)/([^/]+?)(?:\.git)?/?", url.strip())
  if not m:
    raise RuntimeError(f"不支持的 Gitee 仓库 URL: {url}")
  return m.group(1), m.group(2)


def _build_installer_on_comma_via_openpilot(impl, host: str, user: str, key_path: str, out_dir: Path) -> Path:
  """
  在 comma 设备上使用 commaai/openpilot 的 SCons 构建 installer_openpilot_staging。
  产物会被 SCP 回本机 out_dir/installer_openpilot_staging。
  """
  impl.run_ssh(host, user, key_path, "echo connected && uname -m && test -f /TICI && echo HAS_/TICI || echo NO_/TICI", timeout_s=30)

  remote_root = "/data/tmp/sp_build/openpilot_installer"
  target = "selfdrive/ui/installer/installers/installer_openpilot_staging"
  openpilot_upstream = "https://github.com/commaai/openpilot.git"

  remote_cmd = rf"""
set -euo pipefail
sudo -n true >/dev/null 2>&1 || true
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y >/dev/null
sudo apt-get install -y scons git python3 python3-pip pkg-config >/dev/null

mkdir -p /data/tmp/sp_build
cd /data/tmp/sp_build
if [ ! -d "openpilot_installer/.git" ]; then
  rm -rf openpilot_installer
  git clone --depth=1 --recurse-submodules --shallow-submodules "{openpilot_upstream}" openpilot_installer
fi

cd "{remote_root}"

# Ensure required submodules/tools exist (e.g. rednose_filter tool module)
git submodule sync --recursive || true
GIT_TERMINAL_PROMPT=0 git submodule update --init --recursive --depth=1

# Prepare python venv + dependencies for SCons (openpilot SConstruct imports deps modules like bzip2/eigen/...)
mkdir -p /data/tmp/uv-cache /data/tmp/uv-tmp
export UV_CACHE_DIR=/data/tmp/uv-cache
export TMPDIR=/data/tmp/uv-tmp
python3 -m pip install -U uv >/dev/null 2>&1 || true
# Create/refresh venv idempotently
if [ -d ".venv" ]; then
  uv venv --clear --python python3 .venv >/dev/null || true
else
  uv venv --python python3 .venv >/dev/null
fi
set +u
. .venv/bin/activate
set -u
uv sync --frozen --no-dev

# Patch installer.cc（策略与 tools/sync_to_gitee.py::patch_installer_urls 一致；修改时请两处同步）
python3 - <<'PY'
from pathlib import Path

def _with_git_url_variants(url):
  u = url.strip()
  out = [u]
  if u.endswith(".git"):
    out.append(u[:-4])
  else:
    out.append(u + ".git")
  if u.startswith("https://github.com/"):
    out.append("http://" + u[8:])
  if u.startswith("http://github.com/"):
    out.append("https://" + u[7:])
  seen = set()
  uniq = []
  for x in out:
    if x not in seen:
      seen.add(x)
      uniq.append(x)
  return uniq

def replace_first_alias(s, old_candidates, new):
  for o in old_candidates:
    if o in s:
      return s.replace(o, new)
  return s

def apply_text_replacement_rows(s, rows, path_obj, require_all=False, skip_row_when_new_present=True):
  missing = []
  for i, (olds, new) in enumerate(rows):
    if skip_row_when_new_present and new and new in s:
      continue
    before = s
    s = replace_first_alias(s, olds, new)
    if s == before and require_all:
      missing.append("  行 %d: 未匹配任一候选（首项 %r…）" % (i + 1, olds[0]))
  if missing and require_all:
    raise RuntimeError("%s: 弹性替换未完全命中：\n%s" % (path_obj, "\n".join(missing)))
  return s

p = Path("selfdrive/ui/installer/installer.cc")
if not p.exists():
  raise SystemExit("missing selfdrive/ui/installer/installer.cc")
s = p.read_text(encoding="utf-8")
s2 = apply_text_replacement_rows(
  s,
  [
    (_with_git_url_variants("https://github.com/commaai/openpilot.git"), "https://gitee.com/xc2026/sunnypilot_cn.git"),
    (
      [
        '#define GIT_SSH_URL "git@github.com:commaai/openpilot.git"',
        "#define GIT_SSH_URL 'git@github.com:commaai/openpilot.git'",
        '#define GIT_SSH_URL "git@github.com:commaai/openpilot"',
      ],
      '#define GIT_SSH_URL "git@gitee.com:xc2026/sunnypilot_cn.git"',
    ),
  ],
  p,
  require_all=True,
)
if s2 != s:
  p.write_text(s2, encoding="utf-8")
PY

# Ensure staging installer target exists in selfdrive/ui/SConscript
python3 - <<'PY'
from pathlib import Path
p = Path("selfdrive/ui/SConscript")
if not p.exists():
  raise SystemExit("missing selfdrive/ui/SConscript (unexpected)")
s = p.read_text(encoding="utf-8")
# Fix possible previous bad insertion that wrote literal "\n" text into file.
# Replace any backslash-n sequences with real newlines, then always write back if changed.
s2 = s.replace("\\\\n", "\n").replace("\\n", "\n")
if s2 != s:
  p.write_text(s2, encoding="utf-8")
  s = s2
needle = '("openpilot_staging", "staging"),'
if needle in s:
  raise SystemExit(0)
else:
  lines = s.splitlines(True)
  out = []
  inserted = False
  for ln in lines:
    out.append(ln)
    if not inserted and '("openpilot_test"' in ln:
      out.append("    " + needle + "\n")
      inserted = True
  if not inserted:
    for i, ln in enumerate(out):
      if '("openpilot",' in ln:
        out.insert(i+1, "    " + needle + "\n")
        inserted = True
        break
  if not inserted:
    raise SystemExit("failed to patch installers list in SConscript")
  p.write_text("".join(out), encoding="utf-8")
PY

scons -j"$(nproc)" "{target}"
ls -la selfdrive/ui/installer/installers | head -n 50
file "{target}"
"""

  impl.run_ssh(host, user, key_path, remote_cmd, timeout_s=3 * 3600)
  local_out = out_dir / "installer_openpilot_staging"
  impl.scp_from(host, user, key_path, f"{remote_root}/{target}", local_out)
  local_out.chmod(0o755)
  return local_out


def _menu(repo_root: Path) -> int:
  impl = _load_impl(repo_root)
  _patch_impl_remote_transport_for_windows(impl)
  dotenv = _parse_dotenv(repo_root)
  env = os.environ
  for k, v in dotenv.items():
    env.setdefault(k, v)

  while True:
    print("\n=== sp-sync (local) 菜单 ===")
    print("1) pull：拉取 upstream + 仅对 staging 应用补丁 + (可选)更新子模块（不推送）")
    print("2) push：推送本地 staging 到 Gitee（强推）")
    print("3) all：一键执行（1 + 2）（补丁/推送走本脚本调用的共用补丁实现）")
    print("4) mapd release：仅同步 mapd release（需要 GITEE_TOKEN）")
    print("5) installer：在 comma 设备编译 staging installer 并发布到 sp-cn_install（含 release）")
    print("0) 退出\n")
    choice = input("请选择操作 [0-5]: ").strip()

    if choice == "0":
      return 0
    if choice in ("1", "2", "3"):
      action = {"1": "pull", "2": "push", "3": "all"}[choice]
      base = _inject_workdir_argv(repo_root, list(sys.argv[1:]))
      child_argv = _ensure_local_defaults(repo_root, ["--action", action] + base)
      rc = _run_impl(repo_root, child_argv, stream=True)
      if rc != 0:
        print(f"[warn] 补丁/同步子进程退出码 {rc}\n")
      else:
        print("[ok] 本次操作已完成。\n")
      continue
    if choice == "4":
      token = env.get("GITEE_TOKEN", "").strip().strip('"')
      if not token:
        print("[warn] 未设置 GITEE_TOKEN，无法同步 mapd release。")
        continue
      tag_env = env.get("MAPD_TAG", "latest")
      print(f"[step] 同步 mapd release（MAPD_TAG={tag_env}）")
      impl.sync_mapd_release(token, tag_env)
      print("[ok] mapd release 同步完成。\n")
      continue
    if choice == "5":
      out_dir = Path(env.get("INSTALLER_OUTDIR", str(repo_root))).expanduser().resolve()
      host = env.get("COMMA_HOST", "10.90.1.231")
      user = env.get("COMMA_USER", "comma")
      key_path = env.get("COMMA_KEY", str(repo_root / "sp-cn"))
      installer_repo = env.get("INSTALLER_REPO", "https://gitee.com/xc2026/sp-cn_install.git")
      installer_branch = env.get("INSTALLER_REPO_BRANCH", "master")

      print(f"[step] 连接 comma：{user}@{host}")
      out = _build_installer_on_comma_via_openpilot(impl, host, user, key_path, out_dir)

      print(f"[step] 同步二进制到仓库：{installer_repo} ({installer_branch})")
      impl.sync_installer_to_repo(out, installer_repo, branch=installer_branch)

      token = env.get("GITEE_TOKEN", "").strip().strip('"')
      if not token:
        print("[warn] 未设置 GITEE_TOKEN，跳过发布 Release。\n")
        continue

      print("[step] 发布 Gitee Release（tag=YYYYMMDDHHMM）并上传 installer_openpilot")
      tag = impl.publish_installer_release(token, installer_repo, out)
      print(f"[ok] 已发布 Release tag={tag}\n")
      continue

    print("无效选择，请输入 0-5。")


def main(argv: Optional[list[str]] = None) -> None:
  repo_root = Path(__file__).resolve().parents[1]
  if argv is None:
    _maybe_reexec_with_local_venv(repo_root)

  argv = list(sys.argv[1:] if argv is None else argv)
  argv = _inject_workdir_argv(repo_root, argv)

  # If no explicit --action provided, show local menu（菜单内发起子进程时再注入本机默认参数）。
  if "--action" not in argv and is_tty():
    raise SystemExit(_menu(repo_root))

  argv = _ensure_local_defaults(repo_root, argv)
  raise SystemExit(_run_impl(repo_root, argv, stream=True))


if __name__ == "__main__":
  main()

