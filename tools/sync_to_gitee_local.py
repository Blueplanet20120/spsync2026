#!/usr/bin/env python3
"""
**本机专用入口**：`sync_to_gitee_local.py`（菜单、克隆、路径、Windows SSH 等本地化事宜）。

与 GitHub Actions / `.github` 无绑定；云端另有调度方式，不必对齐其它行为。

行为说明：
- **pull / push / all**：均由**本脚本**编排；实际补丁、`verify_patches`、以及随后的 git 操作通过 in-process 调用
  ``tools/sync_to_gitee.py`` 中的**同一份实现**完成（**不修改**该文件；推送目标筛选在本脚本内 monkey-patch）。
- **push 目标**：菜单/CLI 可选 Gitee、Codeup 或两者（默认两者）；``.env`` 可用 ``SP_SYNC_PUSH_TARGETS=both|gitee|codeup``。
  **上游 sunnypilot 未变时**：交互询问是否强制推送，**默认不推**；确认后 FORCE_SYNC pull（必要时空提交）再 push。
  上游有更新则正常 pull + push。pull 阶段已通过 ``verify_patches`` 时，push 不再重复校验。
  本机 **push 默认** ``SYNC_GITEE_SINGLE_COMMIT=1``（与 CI Codeup 单提交压扁一致）。
- **TTY**：默认 **安静模式**（``SYNC_LOCAL_QUIET=1``）：隐藏 git commit 海量 ``create mode``、push 枚举对象等；推送时显示
  「正在强制推送到 Gitee/Codeup…」。需完整 git 输出时设 ``SYNC_LOCAL_QUIET=0``（可同时 ``SYNC_LOCAL_GIT_PROGRESS=1``）。
- **默认 workdir**：若仓库根已有 `.git` 则用根目录；否则用 `./sunnypilot`。**若两者皆无**，首次会自动
  ``git clone``：**默认从 GitHub** ``https://github.com/sunnypilot/sunnypilot.git`` **的 staging**（含子模块浅克隆）。
  **已有完整 clone 时**：pull/all 前本脚本会先 ``git fetch upstream staging`` 并尝试 fast-forward，再交给共用脚本打补丁（子进程仍设 ``SP_SYNC_SOURCE=local``，upstream 只 fetch 分支、不扫全 tag）。
  push 会推送到共用脚本内**已启用**的远端（Gitee / Codeup，取决于项目根 ``.env`` 凭据与 impl 配置）。
  ``.env`` 固定在本脚本所在项目根（``tools/`` 的上一级，如 ``f:\\sunnypilot\\.env``），不会读 ``sunnypilot_cn_github/.env``。
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
import shutil
import subprocess
import sys
import tempfile
import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

# 本地无仓库时自动 clone：默认 GitHub staging（含子模块）；后续 pull/push 由本脚本编排，补丁与默认 Gitee 远端在共用模块内
_DEFAULT_CLONE_URL = "https://github.com/sunnypilot/sunnypilot.git"
_DEFAULT_CLONE_BRANCH = "staging"
_DEFAULT_UPSTREAM_URL = _DEFAULT_CLONE_URL

# 同一进程内对同一 workdir 只做一次 push 前加深，避免 all→push 重复 unshallow
_push_depth_prep_done: set[str] = set()

_DEFAULT_DEP_MIRROR_OWNER = "xc2026"


def _truthy(s: str | None) -> bool:
  return bool((s or "").strip().lower() in ("1", "true", "yes", "y", "on"))


def _dep_mirror_plan(env: dict[str, str]) -> list[tuple[str, str, str]]:
  """
  Returns list of (label, upstream_url, gitee_dest_url).

  Env overrides:
  - DEP_MIRROR_OWNER: gitee owner/org (default: xc2026)
  - DEP_MIRROR_USE_HTTPS=1: use https://gitee.com/owner/repo.git (default uses SSH git@gitee.com:owner/repo.git)
  - DEP_MIRROR_DEST_<LABEL>: override a single dest git URL, where <LABEL> is uppercased and non-alnum replaced with '_'
  """
  owner = (env.get("DEP_MIRROR_OWNER") or "").strip() or _DEFAULT_DEP_MIRROR_OWNER
  use_https = _truthy(env.get("DEP_MIRROR_USE_HTTPS"))

  def dest(repo: str) -> str:
    return f"https://gitee.com/{owner}/{repo}.git" if use_https else f"git@gitee.com:{owner}/{repo}.git"

  repos: list[tuple[str, str, str]] = [
    ("opendbc", "https://github.com/sunnypilot/opendbc.git", dest("opendbc")),
    ("msgq", "https://github.com/commaai/msgq.git", dest("msgq")),
    ("tinygrad", "https://github.com/sunnypilot/tinygrad.git", dest("tinygrad")),
    ("neural_network_data", "https://github.com/sunnypilot/neural-network-data.git", dest("neural_network_data")),
    ("panda", "https://github.com/sunnyhaibin/panda.git", dest("panda")),
    ("rednose", "https://github.com/commaai/rednose.git", dest("rednose")),
    ("teleoprtc", "https://github.com/commaai/teleoprtc.git", dest("teleoprtc")),
    ("dependencies", "https://github.com/commaai/dependencies.git", dest("dependencies")),
    ("Catch2", "https://github.com/catchorg/Catch2.git", dest("Catch2")),
    ("sunnypilot-models", "https://github.com/sunnypilot/sunnypilot-models.git", dest("sunnypilot-models")),
  ]

  out: list[tuple[str, str, str]] = []
  for label, up, de in repos:
    key = "DEP_MIRROR_DEST_" + re.sub(r"[^A-Z0-9]+", "_", label.upper()).strip("_")
    de2 = (env.get(key) or "").strip().strip('"').strip("'") or de
    out.append((label, up, de2))
  return out


def _resolve_git_executable(env: dict[str, str]) -> str:
  """Windows 上 Git 常装在本机但未加入当前 shell 的 PATH。"""
  override = (env.get("SP_SYNC_GIT") or env.get("GIT_EXECUTABLE") or "").strip().strip('"').strip("'")
  if override:
    p = Path(override).expanduser()
    if not p.is_file():
      raise FileNotFoundError(f"SP_SYNC_GIT/GIT_EXECUTABLE 不存在: {p}")
    return str(p)
  found = shutil.which("git", path=env.get("PATH"))
  if found:
    return found
  if os.name == "nt":
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for cand in (
      Path(pf) / "Git" / "cmd" / "git.exe",
      Path(pfx) / "Git" / "cmd" / "git.exe",
      Path(r"C:\Program Files\Git\cmd\git.exe"),
      Path(r"C:\Program Files (x86)\Git\cmd\git.exe"),
    ):
      if cand.is_file():
        return str(cand)
  raise FileNotFoundError(
    "未找到 git 命令。请安装 Git for Windows 并加入 PATH，"
    "或在 .env 设置 SP_SYNC_GIT=C:\\Program Files\\Git\\cmd\\git.exe"
  )


def _git_env(env: dict[str, str]) -> dict[str, str]:
  """把 git.exe 所在目录 prepend 到 PATH，供本脚本与子进程共用。"""
  out = dict(env)
  try:
    git_exe = _resolve_git_executable(out)
  except FileNotFoundError:
    return out
  git_dir = str(Path(git_exe).parent)
  sep = ";" if os.name == "nt" else ":"
  path = out.get("PATH", "")
  try:
    git_resolved = Path(git_dir).resolve()
    already = any(Path(p).resolve() == git_resolved for p in path.split(sep) if p.strip())
  except OSError:
    already = git_dir in path
  if not already:
    out["PATH"] = git_dir + (sep if path else "") + path
  return out


def _git(args: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
  base = _git_env(env if env is not None else dict(os.environ))
  exe = _resolve_git_executable(base)
  p = subprocess.run(
    [exe] + args,
    cwd=str(cwd) if cwd else None,
    env=base,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
  )
  if p.returncode != 0:
    raise RuntimeError(f"git {' '.join(args)} failed (exit {p.returncode})\n{p.stdout}")


def _sync_dependency_mirrors(repo_root: Path) -> None:
  """
  Mirror-sync dependency repos to Gitee.

  Selection:
  - DEP_MIRROR_ONLY: comma-separated labels (e.g. "opendbc,msgq,tinygrad"). If set, only those run.
  - DEP_MIRROR_SKIP: comma-separated labels to skip.

  Notes:
  - Gitee rejects pushing hidden refs like refs/pull/*, so we only sync refs/heads/* and refs/tags/* by default.
  """
  env = _local_env_with_dotenv(repo_root)
  git_env = _git_env(env)
  plan = _dep_mirror_plan(env)

  only = {x.strip() for x in (env.get("DEP_MIRROR_ONLY") or "").split(",") if x.strip()}
  skip = {x.strip() for x in (env.get("DEP_MIRROR_SKIP") or "").split(",") if x.strip()}

  def want(label: str) -> bool:
    if only and label not in only:
      return False
    if label in skip:
      return False
    return True

  selected = [(l, u, d) for (l, u, d) in plan if want(l)]
  if not selected:
    print("[warn] 未选择任何依赖仓库（DEP_MIRROR_ONLY/DEP_MIRROR_SKIP 过滤后为空）。")
    return

  print("[step] 同步依赖镜像到 Gitee（branches + tags）。")
  print("       可用 DEP_MIRROR_OWNER/DEP_MIRROR_USE_HTTPS/DEP_MIRROR_ONLY/DEP_MIRROR_SKIP 覆盖行为。")

  cache_dir = (repo_root / ".sp-sync-cache" / "dep-mirrors").resolve()
  cache_dir.mkdir(parents=True, exist_ok=True)

  for label, upstream_url, dest_url in selected:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label)
    mirror_dir = cache_dir / f"{safe}.git"
    print(f"\n[dep] {label}")
    print(f"      upstream: {upstream_url}")
    print(f"      dest:     {dest_url}")

    if not (mirror_dir / "config").is_file():
      if mirror_dir.exists():
        raise RuntimeError(f"缓存目录异常（缺少 config）：{mirror_dir}")
      _git(["clone", "--mirror", upstream_url, str(mirror_dir)], env=git_env)

    _git(["remote", "set-url", "origin", upstream_url], cwd=mirror_dir, env=git_env)
    try:
      _git(["remote", "add", "gitee", dest_url], cwd=mirror_dir, env=git_env)
    except RuntimeError:
      _git(["remote", "set-url", "gitee", dest_url], cwd=mirror_dir, env=git_env)

    # Fetch only branches/tags (avoid PR refs that Gitee rejects).
    _git(["fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"], cwd=mirror_dir, env=git_env)

    # Push branches + tags, and prune removed ones on destination.
    # Use --force to keep it mirror-like for heads/tags.
    _git(
      [
        "push",
        "--prune",
        "--force",
        "gitee",
        "+refs/heads/*:refs/heads/*",
        "+refs/tags/*:refs/tags/*",
      ],
      cwd=mirror_dir,
      env=git_env,
    )

  print("\n[ok] 依赖镜像同步完成。\n")


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

  full = _truthy(env.get("SP_SYNC_FULL_CLONE", "1"))
  cmd = ["git", "clone", "--branch", branch, "--single-branch"]
  if full:
    cmd += ["--recurse-submodules"]
  else:
    cmd += ["--depth", "1", "--recurse-submodules", "--shallow-submodules"]
  cmd += [url, str(nested)]
  print(
    f"[setup] 未找到本地仓库，从上游克隆 staging"
    f"{'（完整历史，便于 push）' if full else '（浅克隆）'}"
    f"：{url} → {nested}\n"
    "       之后在菜单选 pull 打补丁、选 push：由本脚本拉起子进程完成。"
  )
  git_env = _git_env(env)
  try:
    git_exe = _resolve_git_executable(git_env)
  except FileNotFoundError as e:
    raise SystemExit(f"[错误] {e}") from e
  cmd[0] = git_exe
  try:
    subprocess.run(cmd, env=git_env, check=True)
  except FileNotFoundError as e:
    raise SystemExit(f"[错误] {e}") from e
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


def _workdir_from_argv(repo_root: Path, argv: list[str]) -> Path:
  for i, a in enumerate(argv):
    if a == "--workdir" and i + 1 < len(argv):
      return Path(argv[i + 1]).expanduser().resolve()
    if a.startswith("--workdir="):
      return Path(a.split("=", 1)[1]).expanduser().resolve()
  return _resolve_git_workdir(repo_root)


def _parse_action_from_argv(argv: list[str]) -> str | None:
  for i, a in enumerate(argv):
    if a == "--action" and i + 1 < len(argv):
      return argv[i + 1].strip().lower()
    if a.startswith("--action="):
      return a.split("=", 1)[1].strip().lower()
  return None


def _strip_action_argv(argv: list[str]) -> list[str]:
  out: list[str] = []
  i = 0
  while i < len(argv):
    a = argv[i]
    if a == "--action" and i + 1 < len(argv):
      i += 2
      continue
    if a.startswith("--action="):
      i += 1
      continue
    out.append(a)
    i += 1
  return out


def _argv_with_action(argv: list[str], action: str) -> list[str]:
  base = _strip_action_argv(argv)
  return ["--action", action] + base


_DEFAULT_PUSH_TARGETS: frozenset[str] = frozenset({"gitee", "codeup"})


def _normalize_push_targets(raw: str) -> set[str]:
  s = (raw or "both").strip().lower().strip('"').strip("'")
  if s in ("both", "all", "3", "gitee,codeup", "codeup,gitee", "gitee+codeup"):
    return set(_DEFAULT_PUSH_TARGETS)
  if s in ("gitee", "1", "origin"):
    return {"gitee"}
  if s in ("codeup", "2", "code", "aliyun"):
    return {"codeup"}
  raise SystemExit(f"[错误] 无效推送目标 {raw!r}（可用 gitee / codeup / both）")


def _push_targets_from_env(env: dict[str, str]) -> set[str]:
  return _normalize_push_targets(env.get("SP_SYNC_PUSH_TARGETS") or "both")


def _format_push_targets(targets: set[str]) -> str:
  if targets >= _DEFAULT_PUSH_TARGETS:
    return "Gitee + Codeup"
  if targets == {"gitee"}:
    return "Gitee"
  if targets == {"codeup"}:
    return "Codeup"
  return ", ".join(sorted(targets))


def _parse_and_strip_push_targets_argv(
  argv: list[str],
  env: dict[str, str],
  *,
  default_from_env: bool = True,
) -> tuple[list[str], set[str] | None]:
  """剥离本脚本专用 ``--push-targets``；未在 CLI 指定时可选从 env 读默认。"""
  targets: set[str] | None = None
  out: list[str] = []
  i = 0
  while i < len(argv):
    a = argv[i]
    if a == "--push-targets" and i + 1 < len(argv):
      targets = _normalize_push_targets(argv[i + 1])
      i += 2
      continue
    if a.startswith("--push-targets="):
      targets = _normalize_push_targets(a.split("=", 1)[1])
      i += 1
      continue
    out.append(a)
    i += 1
  if targets is None and default_from_env:
    targets = _push_targets_from_env(env)
  return out, targets


def _parse_and_strip_force_push_argv(argv: list[str]) -> tuple[list[str], bool | None]:
  """剥离 ``--force-push`` / ``--no-force-push``；未指定时返回 None（由交互或 env 决定）。"""
  force: bool | None = None
  out: list[str] = []
  i = 0
  while i < len(argv):
    a = argv[i]
    if a == "--force-push":
      force = True
      i += 1
      continue
    if a == "--no-force-push":
      force = False
      i += 1
      continue
    if a.startswith("--force-push="):
      force = _truthy(a.split("=", 1)[1])
      i += 1
      continue
    out.append(a)
    i += 1
  return out, force


def _sync_branch_name(env: dict[str, str]) -> str:
  return (env.get("SP_SYNC_CLONE_BRANCH") or _DEFAULT_CLONE_BRANCH).strip().strip('"').strip("'")


def _local_canonical_commit_sha(sha: str | None, workdir: Path, env: dict[str, str]) -> str | None:
  """与共用脚本 canonical_commit_sha 同义，但走本机 _git_output（Windows 可找到 git.exe）。"""
  if not sha:
    return None
  s = sha.strip().lower()
  if not re.fullmatch(r"[0-9a-f]{7,40}", s):
    return None
  try:
    return _git_output(["rev-parse", "--verify", f"{s}^{{commit}}"], workdir, env).strip().lower()
  except RuntimeError:
    return s if len(s) == 40 else None


def _upstream_unchanged_vs_gitee(repo_root: Path, argv: list[str]) -> tuple[str, bool]:
  """
  对比 upstream/staging 与 Gitee 最新提交里记录的 upstream SHA。
  返回 (upstream_sha, unchanged)；无法判断时 unchanged=False。
  """
  workdir = _workdir_from_argv(repo_root, argv)
  if not (workdir / ".git").is_dir():
    return "", False
  env = _git_env(_local_env_with_dotenv(repo_root))
  branch = _sync_branch_name(env)
  impl = _load_impl(repo_root)
  try:
    _git_run(["fetch", "upstream", branch, "--prune"], workdir, env, stream=False)
    upstream_sha = _git_output(["rev-parse", f"upstream/{branch}"], workdir, env).strip().lower()
  except RuntimeError:
    return "", False

  recorded_sha: str | None = None
  try:
    _git_run(["fetch", "--depth=1", "origin", branch], workdir, env, stream=False)
    body = _git_output(["log", "-1", "--format=%B", "FETCH_HEAD"], workdir, env)
    recorded_sha = impl.parse_recorded_upstream_sha(body, branch)
  except RuntimeError:
    recorded_sha = None

  recorded_canon = _local_canonical_commit_sha(recorded_sha, workdir, env)
  upstream_canon = _local_canonical_commit_sha(upstream_sha, workdir, env) or upstream_sha.strip().lower()
  if recorded_canon is None:
    return upstream_sha, False
  return upstream_sha, recorded_canon == upstream_canon


def _prompt_force_push_when_upstream_unchanged(upstream_sha: str, branch: str) -> bool:
  short = upstream_sha[:7] if upstream_sha else "?"
  print(f"\n[local] 上游 sunnypilot/{branch} 未变化（{short}），Gitee 记录已与 upstream 一致。")
  print("  是否仍强制重新打补丁并推送？（会空提交或重推，刷新远端时间）")
  ans = input("强制推送？[1/N]: ").strip().lower()
  return ans in ("1", "y", "yes")


def _resolve_force_push_on_unchanged(
  repo_root: Path,
  argv: list[str],
  upstream_sha: str,
  *,
  force_push: bool | None,
) -> bool:
  """上游未变时是否强制推送；默认 False。"""
  if force_push is True:
    return True
  if force_push is False:
    return False
  env = _local_env_with_dotenv(repo_root)
  if _truthy(env.get("SP_SYNC_FORCE_PUSH")):
    return True
  if is_tty():
    return _prompt_force_push_when_upstream_unchanged(upstream_sha, _sync_branch_name(env))
  return False


def _prompt_push_targets() -> set[str]:
  if not is_tty():
    return set(_DEFAULT_PUSH_TARGETS)
  print("\n推送目标：")
  print("  1) 仅 Gitee")
  print("  2) 仅 Codeup")
  print("  3) Gitee + Codeup（默认）")
  choice = input("请选择 [1-3，回车=3]: ").strip()
  if choice in ("", "3"):
    return set(_DEFAULT_PUSH_TARGETS)
  if choice == "1":
    return {"gitee"}
  if choice == "2":
    return {"codeup"}
  print("无效选择，使用默认：Gitee + Codeup")
  return set(_DEFAULT_PUSH_TARGETS)


def _patch_impl_push_targets(impl, targets: set[str]) -> None:
  """本地限定推送端，不修改 sync_to_gitee.py 源码。"""
  if not targets or targets >= _DEFAULT_PUSH_TARGETS:
    return
  orig = impl.enabled_main_repo_push_sources
  want = frozenset(targets)

  def _filtered():
    return [s for s in orig() if s.id in want]

  impl.enabled_main_repo_push_sources = _filtered  # type: ignore[method-assign]


# pull 已 verify 后，push_branch 内 log+verify 各一次；本地 prep 完成后跳过 push 侧重复。
_push_verify_skip_armed = False
_last_patch_all_total = 0


def _local_arm_skip_next_push_verify() -> None:
  global _push_verify_skip_armed
  _push_verify_skip_armed = True


def _should_skip_push_verify() -> bool:
  return _push_verify_skip_armed


def _clear_push_verify_skip() -> None:
  global _push_verify_skip_armed
  _push_verify_skip_armed = False


def _verify_failure_count(exc: BaseException) -> int:
  if not isinstance(exc, RuntimeError):
    return 1
  msg = str(exc)
  if "verify_patches 失败" not in msg:
    return 1
  n = sum(1 for ln in msg.splitlines() if ln.strip().startswith("-"))
  return max(n, 1)


def _format_verify_pass_line() -> str:
  n = _last_patch_all_total
  return f"[local] 校验补丁通过，共打补丁{n}个，成功{n}个"


def _format_verify_fail_line(fail_n: int) -> str:
  n = _last_patch_all_total
  return f"[local] 校验补丁未通过，共打补丁{n}个，失败{fail_n}个"


def _resolve_impl_path(repo_root: Path, env: dict[str, str]) -> Path:
  """
  共用补丁脚本路径。默认 ``{项目根}/tools/sync_to_gitee.py``（REPO_ROOT 与 .env 同目录）。
  仅当 .env / 环境显式设置 ``SP_SYNC_IMPL`` 时使用其它路径。
  """
  override = (env.get("SP_SYNC_IMPL") or "").strip().strip('"').strip("'")
  if override:
    p = Path(override).expanduser().resolve()
    if not p.is_file():
      raise SystemExit(f"[错误] SP_SYNC_IMPL 不存在: {p}")
    return p
  default = (_project_root(repo_root) / "tools" / "sync_to_gitee.py").resolve()
  if not default.is_file():
    sibling = (_project_root(repo_root) / "sunnypilot_cn_github" / "tools" / "sync_to_gitee.py").resolve()
    if sibling.is_file():
      return sibling
  return default


def _git_output(args: list[str], cwd: Path, env: dict[str, str]) -> str:
  git_env = _git_env(env)
  exe = _resolve_git_executable(git_env)
  p = subprocess.run(
    [exe] + args,
    cwd=str(cwd),
    env=git_env,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
  )
  if p.returncode != 0:
    raise RuntimeError(f"git {' '.join(args)} failed (exit {p.returncode})\n{p.stdout}")
  return p.stdout


def _git_run(args: list[str], cwd: Path, env: dict[str, str], *, stream: bool = False) -> None:
  git_env = _git_env(env)
  exe = _resolve_git_executable(git_env)
  cmd = [exe] + args
  if stream:
    rc = subprocess.run(cmd, cwd=str(cwd), env=git_env).returncode
    if rc != 0:
      raise RuntimeError(f"git {' '.join(args)} failed (exit {rc})")
    return
  _git_output(args, cwd, env)


def _is_shallow_clone(workdir: Path) -> bool:
  shallow = workdir / ".git" / "shallow"
  return shallow.is_file() and bool(shallow.read_text(encoding="utf-8", errors="ignore").strip())


def _local_incremental_upstream_sync(workdir: Path, env: dict[str, str], *, stream: bool = False) -> None:
  """
  本地已有源码时：预 fetch upstream staging，非浅克隆则 fast-forward 到 upstream/staging。
  补丁与 commit 仍由子进程 sync_to_gitee.py 完成（与云端逻辑一致）。
  """
  if _truthy(env.get("SYNC_LOCAL_SKIP_PREP")):
    return
  if not (workdir / ".git").is_dir():
    return

  branch = (env.get("SP_SYNC_CLONE_BRANCH") or "").strip().strip('"').strip("'") or _DEFAULT_CLONE_BRANCH
  upstream = (env.get("SP_SYNC_UPSTREAM") or env.get("UPSTREAM") or "").strip().strip('"').strip("'") or _DEFAULT_UPSTREAM_URL

  git_env = dict(env)
  git_env.setdefault("GIT_TERMINAL_PROMPT", "0")

  remotes = _git_output(["remote"], workdir, git_env).splitlines()
  if "upstream" not in remotes:
    _git_run(["remote", "add", "upstream", upstream], workdir, git_env, stream=stream)
  else:
    _git_run(["remote", "set-url", "upstream", upstream], workdir, git_env, stream=stream)

  fetch_args = ["fetch", "upstream", branch, "--prune"]
  if _truthy(env.get("SYNC_LOCAL_GIT_PROGRESS", "1")) and stream:
    fetch_args = ["fetch", "--progress", "upstream", branch, "--prune"]
  print(f"[local] git {' '.join(fetch_args)}  ({workdir})")
  _git_run(fetch_args, workdir, git_env, stream=stream)

  if _is_shallow_clone(workdir):
    print(f"[local] 浅克隆：已预 fetch upstream/{branch}，基线切换与补丁由共用脚本完成。")
    return

  try:
    head = _git_output(["rev-parse", "--abbrev-ref", "HEAD"], workdir, git_env).strip()
  except RuntimeError:
    head = ""

  if head != branch:
    try:
      _git_run(["checkout", branch], workdir, git_env, stream=stream)
    except RuntimeError:
      _git_run(["checkout", "-B", branch, f"upstream/{branch}"], workdir, git_env, stream=stream)
      print(f"[local] 已检出 {branch} @ upstream/{branch}")
      return

  try:
    _git_run(["merge", "--ff-only", f"upstream/{branch}"], workdir, git_env, stream=stream)
    print(f"[local] 已 fast-forward {branch} ← upstream/{branch}")
  except RuntimeError:
    _git_run(["reset", "--hard", f"upstream/{branch}"], workdir, git_env, stream=stream)
    print(f"[local] merge 非 fast-forward，已 reset --hard 到 upstream/{branch}")


def _maybe_local_prep_before_push(repo_root: Path, argv: list[str], *, stream: bool = False) -> None:
  if _parse_action_from_argv(argv) != "push":
    return
  env = _local_env_with_dotenv(repo_root)
  workdir = _workdir_from_argv(repo_root, argv)
  if not (workdir / ".git").is_dir():
    return
  if not _is_shallow_clone(workdir):
    return
  key = str(workdir.resolve())
  if key in _push_depth_prep_done:
    return
  _push_depth_prep_done.add(key)

  git_env = _git_env(env)
  br = (env.get("SP_SYNC_CLONE_BRANCH") or _DEFAULT_CLONE_BRANCH).strip().strip('"')
  progress = ["--progress"] if _truthy(env.get("SYNC_LOCAL_GIT_PROGRESS", "1")) and stream else []
  print(
    f"[local] 浅克隆：仅加深 upstream/{br}（避免 fetch --unshallow 拉取全部分支/2GB+）"
  )
  try:
    _git_run(["fetch"] + progress + ["--deepen", "500000", "upstream", br], workdir, git_env, stream=stream)
  except RuntimeError as e:
    print(f"[warn] --deepen 失败，尝试仅对 {br} unshallow：{e}")
    try:
      _git_run(["fetch"] + progress + ["--unshallow", "upstream", br], workdir, git_env, stream=stream)
    except RuntimeError as e2:
      print(f"[warn] 仍无法取消浅克隆（push 可能遭 shallow update 拒绝）：{e2}")
      print("       可删除 sunnypilot 目录后重跑（默认 SP_SYNC_FULL_CLONE=1 完整克隆）。")
      return
  if _is_shallow_clone(workdir):
    print("[warn] fetch 后仍为浅克隆；若 push 失败，请删除 workdir 后让脚本完整 re-clone。")


def _maybe_local_prep_before_impl(repo_root: Path, argv: list[str], *, stream: bool = False) -> None:
  action = _parse_action_from_argv(argv)
  if action not in ("pull", "all"):
    return
  env = _local_env_with_dotenv(repo_root)
  workdir = _workdir_from_argv(repo_root, argv)
  try:
    _local_incremental_upstream_sync(workdir, env, stream=stream)
  except (RuntimeError, FileNotFoundError) as e:
    print(f"[warn] 本地 upstream 预同步失败（将继续调用共用脚本）: {e}")


@contextmanager
def _force_sync_env():
  old = os.environ.get("FORCE_SYNC")
  os.environ["FORCE_SYNC"] = "1"
  try:
    yield
  finally:
    if old is None:
      os.environ.pop("FORCE_SYNC", None)
    else:
      os.environ["FORCE_SYNC"] = old


def _run_force_sync_pull(repo_root: Path, argv: list[str], *, stream: bool = True) -> int:
  """
  本地推送前必做：重新 checkout + 打补丁 + 提交。
  共用脚本在补丁无改动时会 ``commit --allow-empty``，保证 push 有新 SHA、云端可见更新。
  """
  print(
    "[local] FORCE_SYNC pull：重新 checkout + 打补丁 + 提交"
    "（无文件改动则空提交，避免 Everything up-to-date）…"
  )
  pull_argv = _argv_with_action(_strip_action_argv(argv), "pull")
  with _force_sync_env():
    return _run_impl_actions(repo_root, pull_argv, stream=stream)


def _run_impl_actions(
  repo_root: Path,
  argv: list[str],
  *,
  stream: bool = True,
  push_targets: set[str] | None = None,
  force_push: bool | None = None,
) -> int:
  _maybe_local_prep_before_impl(repo_root, argv, stream=stream)
  return _run_impl(
    repo_root,
    argv,
    stream=stream,
    push_targets=push_targets,
    force_push=force_push,
  )


def _run_local_all(
  repo_root: Path,
  argv: list[str],
  *,
  stream: bool = True,
  push_targets: set[str] | None = None,
  force_push: bool | None = None,
) -> int:
  """
  本地「一键」：pull + push。
  pull 在上游未变时会 skip；push 前若上游仍未变则询问是否强制推送（默认不推）。
  """
  pull_argv = _argv_with_action(argv, "pull")
  push_argv = _argv_with_action(argv, "push")
  resolved = push_targets or _push_targets_from_env(_local_env_with_dotenv(repo_root))
  print(f"[local] all：pull + push → {_format_push_targets(resolved)}")
  rc_pull = _run_impl_actions(repo_root, pull_argv, stream=stream)
  if rc_pull != 0:
    print(f"[warn] pull 阶段失败（退出码 {rc_pull}），已跳过 push")
    return rc_pull
  return _run_impl(
    repo_root,
    push_argv,
    stream=stream,
    push_targets=resolved,
    force_push=force_push,
  )


def _prepare_local_push(
  repo_root: Path,
  args: list[str],
  *,
  force_push: bool | None,
) -> int | None:
  """
  push 前本地编排：上游未变则询问/跳过；上游有变或未国内化则 pull（含 verify）。
  返回 None 表示可继续调用 impl push（不再重复 verify）；返回 int 为应直接退出的 exit code。
  """
  _maybe_local_prep_before_push(repo_root, args, stream=True)
  env = _local_env_with_dotenv(repo_root)
  branch = _sync_branch_name(env)
  upstream_sha, unchanged = _upstream_unchanged_vs_gitee(repo_root, args)

  if unchanged:
    if not _resolve_force_push_on_unchanged(repo_root, args, upstream_sha, force_push=force_push):
      short = upstream_sha[:7] if upstream_sha else "?"
      print(f"[skip] 上游 sunnypilot/{branch} 未变（{short}），已跳过 push。")
      print("       强制推送：交互输入 1，或 --force-push / SP_SYNC_FORCE_PUSH=1")
      return 0
    rc = _run_force_sync_pull(repo_root, args, stream=True)
    if rc != 0:
      return rc
  else:
    if _workdir_needs_cn_patch(repo_root, args):
      with _force_sync_env():
        rc = _run_impl_actions(repo_root, _argv_with_action(_strip_action_argv(args), "pull"), stream=True)
    else:
      rc = _run_impl_actions(
        repo_root,
        _argv_with_action(_strip_action_argv(args), "pull"),
        stream=True,
      )
    if rc != 0:
      return rc

  if _workdir_needs_cn_patch(repo_root, args):
    print("[error] pull 后仍未通过 verify_patches，无法 push")
    return 1
  return None


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


def _resolve_sp_cn_key_path() -> str | None:
  """当前进程环境中 sp-cn 私钥文件（.env 物化后的绝对路径）。"""
  for name in ("ALIYUN_SSH_KEY", "COMMA_KEY"):
    raw = (os.environ.get(name) or "").strip().strip('"')
    if raw:
      p = Path(raw).expanduser()
      if p.is_file():
        return p.resolve().as_posix()
  return None


def _patch_impl_codeup_ssh(impl) -> None:
  """
  Codeup 推送使用 .env 物化的 sp-cn 私钥。
  Windows 上 ``ssh -i F:\\path`` 须正斜杠 + 引号；Gitee 仍用共用脚本默认 SSH（ssh-agent / 系统配置）。
  """
  default_key = getattr(impl, "ALIYUN_SSH_KEY_DEFAULT", "~/.ssh/sp-cn")
  orig_aliyun = impl.aliyun_git_push_env

  def _key_posix() -> str | None:
    k = _resolve_sp_cn_key_path()
    if k:
      return k
    p = Path(default_key).expanduser()
    return p.resolve().as_posix() if p.is_file() else None

  def aliyun_git_push_env(base_env: dict[str, str]) -> dict[str, str]:
    key = _key_posix()
    if key and os.name == "nt":
      out = dict(base_env)
      out["GIT_SSH_COMMAND"] = f'ssh -i "{key}" -o StrictHostKeyChecking=no -o BatchMode=yes'
      out["GIT_LFS_SKIP_PUSH"] = "1"
      return out
    out = orig_aliyun(base_env)
    out["GIT_LFS_SKIP_PUSH"] = "1"
    return out

  impl.aliyun_git_push_env = aliyun_git_push_env  # type: ignore[method-assign]


def _local_quiet_enabled(env: dict[str, str] | None = None) -> bool:
  e = env if env is not None else os.environ
  return _truthy((e.get("SYNC_LOCAL_QUIET") or "1"))


def _push_target_label_from_git_cmd(cmd: list[str]) -> str | None:
  if len(cmd) < 4 or cmd[0] != "git" or cmd[1] != "push":
    return None
  if "origin" in cmd:
    return "Gitee"
  if "aliyun" in cmd:
    return "Codeup"
  return None


def _git_subcommand(cmd: list[str]) -> str | None:
  """``git -c … commit`` 等带全局选项时，子命令不在 ``cmd[1]``。"""
  if len(cmd) < 2 or cmd[0] != "git":
    return None
  i = 1
  while i < len(cmd):
    tok = cmd[i]
    if tok in ("-C", "--git-dir", "--work-tree", "-c"):
      i += 2
      continue
    if tok.startswith("-"):
      i += 1
      continue
    return tok
    i += 1
  return None


# git commit 摘要行常以两空格缩进：`` create mode 100644 …``
_QUIET_DROP_LINE_RES: tuple[re.Pattern[str], ...] = (
  re.compile(r"^\s*create mode \d+"),
  re.compile(r"^\s*delete mode \d+"),
  re.compile(r"^\s*rename (from|to)"),
  re.compile(r"^\s*copy (from|to)"),
  re.compile(r"^Enumerating objects:"),
  re.compile(r"^Counting objects:"),
  re.compile(r"^Compressing objects:"),
  re.compile(r"^Writing objects:"),
  re.compile(r"^Total \d+ \(delta"),
  re.compile(r"^remote: Powered by"),
  re.compile(r"^remote: Set trace flag"),
  re.compile(r"^branch '.*' set up to track"),
  re.compile(r"^From https?://"),
  re.compile(r"^\s\*\sbranch"),
  re.compile(r"^remote: Total \d+"),
  re.compile(r"^To (gitee\.com|codeup\.aliyun\.com)"),
)


def _should_drop_quiet_line(line: str) -> bool:
  s = line.rstrip("\r\n")
  if not s.strip():
    return False
  if re.match(r"^\[(local|skip|force|warn|error|ok|dep|step)\]", s):
    return False
  if s.startswith("fad75e") or re.fullmatch(r"[0-9a-f]{7,40}", s.strip()):
    return True
  if s.startswith("【sp-sync 本地】"):
    return True
  if s.startswith("  ·"):
    return True
  for pat in _QUIET_DROP_LINE_RES:
    if pat.search(s):
      return True
  if re.match(r"^\[\d{4}-\d{2}-\d{2} .+\] \[(config|git|push|staging)\]", s):
    return True
  return False


_QUIET_STDIO_BUF_MAX = 4096


class _LocalQuietStdout:
  """过滤共用脚本/ git 的冗长行；保留 [local]/[skip]/[error]/交互提示等。"""

  def __init__(self, target):
    self._target = target
    self._buf = ""

  def write(self, s: str) -> int:
    if not s:
      return 0
    self._buf += s
    while "\n" in self._buf:
      line, self._buf = self._buf.split("\n", 1)
      if not _should_drop_quiet_line(line):
        self._target.write(line + "\n")
    if len(self._buf) > _QUIET_STDIO_BUF_MAX:
      if not _should_drop_quiet_line(self._buf):
        self._target.write(self._buf)
      self._buf = ""
    return len(s)

  def flush(self) -> None:
    if self._buf:
      if not _should_drop_quiet_line(self._buf):
        self._target.write(self._buf)
      self._buf = ""
    self._target.flush()

  def isatty(self) -> bool:
    try:
      return self._target.isatty()
    except Exception:
      return False

  def __getattr__(self, name: str):
    return getattr(self._target, name)


@contextmanager
def _local_filtered_stdio_if_quiet():
  if not _local_quiet_enabled():
    yield
    return
  old = sys.stdout
  sys.stdout = _LocalQuietStdout(old)  # type: ignore[assignment]
  try:
    yield
  finally:
    sys.stdout = old


def _patch_impl_quiet_local(impl) -> None:
  """捕获冗长 git 输出；推送/补丁阶段改为人读友好的 [local] 提示。"""
  orig_run = impl.run
  orig_log = impl.log

  def run(
    cmd: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    *,
    stream: bool | None = None,
    timeout_s: int | None = None,
  ) -> str:
    cmd = list(cmd)
    quiet = _local_quiet_enabled(env)
    push_label = _push_target_label_from_git_cmd(cmd) if quiet else None

    if quiet:
      if push_label:
        print(f"[local] 正在强制推送到 {push_label}…", flush=True)
        stream = False
      elif len(cmd) >= 2 and cmd[0] == "git":
        verb = _git_subcommand(cmd) or ""
        if verb in (
          "commit", "checkout", "add", "reset", "clean", "merge", "remote",
          "submodule", "rev-parse", "show-ref", "log", "status", "branch",
        ):
          stream = False
        elif verb == "fetch":
          stream = False
        elif verb == "push":
          stream = False
      elif stream is None:
        stream = False

    out = orig_run(cmd, cwd, env, stream=stream, timeout_s=timeout_s)

    if quiet and push_label:
      summary = ""
      for line in out.splitlines():
        t = line.strip()
        if "forced update" in t or ("->" in t and "staging" in t):
          summary = t
          break
        if "Everything up-to-date" in t:
          summary = "Everything up-to-date"
          break
      if summary:
        print(f"[local] {push_label} 完成：{summary}", flush=True)
      else:
        print(f"[local] {push_label} 推送完成", flush=True)
    return out

  def log(stage: str, msg: str) -> None:
    if not _local_quiet_enabled():
      orig_log(stage, msg)
      return
    if stage == "config":
      return
    if stage == "git" and "fetch upstream" in msg:
      print("[local] 正在 fetch upstream…", flush=True)
      return
    if stage == "staging":
      if msg.startswith("apply patches"):
        print("[local] 正在打国内化补丁…", flush=True)
        return
      if msg.startswith("verify patches"):
        if _should_skip_push_verify():
          return
        return
      if msg.startswith("patch summary:"):
        body = msg.replace("patch summary: ", "补丁：", 1)
        print(f"[local] {body}", flush=True)
        return
    if stage == "push":
      if "squashed to single commit" in msg:
        print("[local] 已压成单提交（SYNC_GITEE_SINGLE_COMMIT）", flush=True)
        return
      if " pushed" in msg:
        return
    orig_log(stage, msg)

  impl.run = run  # type: ignore[method-assign]
  impl.log = log  # type: ignore[method-assign]


def _patch_impl_local_patch_verify(impl) -> None:
  """记录 patch_all 数量；verify 完成后输出本地摘要；pull 后跳过 push 侧重复 verify。"""
  global _last_patch_all_total
  orig_patch_all = impl.patch_all
  orig_verify = impl.verify_patches

  def patch_all(root):
    global _last_patch_all_total
    results = orig_patch_all(root)
    _last_patch_all_total = len(results)
    return results

  def verify_patches(root):
    if _should_skip_push_verify():
      _clear_push_verify_skip()
      return
    try:
      orig_verify(root)
    except RuntimeError as e:
      if _last_patch_all_total > 0:
        print(_format_verify_fail_line(_verify_failure_count(e)), flush=True)
      raise
    if _last_patch_all_total > 0:
      print(_format_verify_pass_line(), flush=True)

  impl.patch_all = patch_all  # type: ignore[method-assign]
  impl.verify_patches = verify_patches  # type: ignore[method-assign]
  impl._verify_patches_unwrapped = orig_verify  # type: ignore[attr-defined]


def _load_impl_from_path(impl_path: Path):
  import importlib.util
  spec = importlib.util.spec_from_file_location("sync_to_gitee_impl", impl_path)
  if spec is None or spec.loader is None:
    raise RuntimeError(f"无法加载脚本模块: {impl_path}")
  impl = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(impl)  # type: ignore[attr-defined]
  _patch_impl_remote_transport_for_windows(impl)
  _patch_impl_codeup_ssh(impl)
  _patch_impl_quiet_local(impl)
  _patch_impl_local_patch_verify(impl)
  return impl


def _project_root(repo_root: Path) -> Path:
  """sp-sync 项目根（``f:\\sunnypilot``），``.env`` 固定在此，与 workdir/sunnypilot 源码树无关。"""
  return repo_root.resolve()


def _dotenv_paths(project_root: Path) -> list[Path]:
  paths: list[Path] = []
  primary = project_root / ".env"
  if primary.is_file():
    paths.append(primary)
  return paths


def _dotenv_unquoted_value(rest: str) -> str:
  val = rest.strip()
  if "#" in val:
    val = val.split("#", 1)[0].rstrip()
  return val.strip('"').strip("'")


def _parse_dotenv_file(path: Path) -> dict[str, str]:
  """解析 .env；支持 ``KEY="多行值"``（如 sp-cn 私钥块）；同行 ``KEY="v" # c`` 不误吞后续行。"""
  text = path.read_text(encoding="utf-8", errors="ignore")
  out: dict[str, str] = {}
  i = 0
  lines = text.splitlines()
  while i < len(lines):
    raw = lines[i]
    line = raw.strip()
    i += 1
    if not line or line.startswith("#") or "=" not in line:
      continue
    key, rest = line.split("=", 1)
    key = key.strip()
    rest = rest.strip()
    if not key:
      continue
    if rest.startswith('"'):
      close = rest.find('"', 1)
      if close != -1:
        out[key] = rest[1:close]
        continue
      buf = [rest[1:]]
      while i < len(lines):
        seg = lines[i]
        i += 1
        if seg.rstrip().endswith('"'):
          buf.append(seg.rstrip()[:-1])
          break
        buf.append(seg)
      out[key] = "\n".join(buf)
      continue
    if rest.startswith("'"):
      close = rest.find("'", 1)
      if close != -1:
        out[key] = rest[1:close]
        continue
      buf = [rest[1:]]
      while i < len(lines):
        seg = lines[i]
        i += 1
        if seg.rstrip().endswith("'"):
          buf.append(seg.rstrip()[:-1])
          break
        buf.append(seg)
      out[key] = "\n".join(buf)
      continue
    out[key] = _dotenv_unquoted_value(rest)
  return out


def _parse_dotenv(project_root: Path) -> dict[str, str]:
  merged: dict[str, str] = {}
  for p in _dotenv_paths(project_root):
    merged.update(_parse_dotenv_file(p))
  return merged


def _materialize_sp_cn_key(project_root: Path, dotenv: dict[str, str]) -> Path | None:
  """将 .env 内联 ``sp-cn`` 私钥写入项目根 ``sp-cn`` 文件，供 Codeup SSH / comma 使用。"""
  raw = (dotenv.get("sp-cn") or dotenv.get("SP_CN_KEY") or "").strip()
  if not raw or "BEGIN" not in raw:
    return None
  key_path = (project_root / "sp-cn").resolve()
  if key_path.is_file():
    try:
      if key_path.read_text(encoding="utf-8", errors="ignore").strip() == raw.strip():
        return key_path
    except OSError:
      pass
  key_path.write_text(raw if raw.endswith("\n") else raw + "\n", encoding="utf-8", newline="\n")
  try:
    key_path.chmod(0o600)
  except OSError:
    pass
  return key_path


def _apply_project_secrets(project_root: Path, env: dict[str, str]) -> dict[str, str]:
  """从项目根 .env 强制注入凭据（覆盖空环境变量；子进程 impl 的 REPO_ROOT 可能不是项目根）。"""
  out = dict(env)
  dotenv = _parse_dotenv(project_root)
  for k, v in dotenv.items():
    if v:
      out[k] = v

  token = (dotenv.get("sp-cn-token") or dotenv.get("SP_CN_TOKEN") or "").strip().strip('"')
  if token:
    out["sp-cn-token"] = token
    out["SP_CN_TOKEN"] = token

  gitee = (dotenv.get("GITEE_TOKEN") or "").strip().strip('"')
  if gitee:
    out["GITEE_TOKEN"] = gitee

  key_path = _materialize_sp_cn_key(project_root, dotenv)
  if key_path is not None:
    # OpenSSH -i 在 Windows 上需正斜杠路径；由 _patch_impl_git_ssh_for_windows 加引号
    out["ALIYUN_SSH_KEY"] = str(key_path.resolve())
    out["COMMA_KEY"] = str(key_path.resolve())
    out["ALIYUN_PUSH_SSH"] = "1"  # Codeup 与 Gitee 一致走 SSH + sp-cn 私钥，不用 HTTPS 令牌

  out["SP_SYNC_DOTENV_ROOT"] = str(project_root)
  return out


def _local_env_with_dotenv(repo_root: Path) -> dict[str, str]:
  project = _project_root(repo_root)
  return _apply_project_secrets(project, os.environ.copy())


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
  env = _local_env_with_dotenv(repo_root)
  impl_path = _resolve_impl_path(repo_root, env)
  return _load_impl_from_path(impl_path)


def _workdir_needs_cn_patch(repo_root: Path, argv: list[str]) -> bool:
  """本地 workdir 是否尚未应用国内化补丁（verify_patches 未通过）。"""
  workdir = _workdir_from_argv(repo_root, argv)
  if not (workdir / ".git").is_dir():
    return False
  try:
    impl = _load_impl(repo_root)
    verify_fn = getattr(impl, "_verify_patches_unwrapped", impl.verify_patches)
    verify_fn(workdir)
    return False
  except RuntimeError:
    return True
  except Exception:
    return False


def _run_impl(
  repo_root: Path,
  args: list[str],
  stream: bool = True,
  *,
  push_targets: set[str] | None = None,
  force_push: bool | None = None,
) -> int:
  del stream
  action = _parse_action_from_argv(args)
  env = _git_env(_local_env_with_dotenv(repo_root))
  resolved_push_targets = push_targets or _push_targets_from_env(env)

  if action == "push":
    prep_rc = _prepare_local_push(repo_root, args, force_push=force_push)
    if prep_rc is not None:
      return prep_rc
    print(f"[local] 推送目标：{_format_push_targets(resolved_push_targets)}")

  impl_path = _resolve_impl_path(repo_root, env)
  env.setdefault("GIT_TERMINAL_PROMPT", "0")
  env.setdefault("SP_SYNC_SOURCE", "local")
  env.setdefault("SYNC_LOCAL_QUIET", "1")
  if _local_quiet_enabled(env):
    env.setdefault("SYNC_LOCAL_GIT_PROGRESS", "0")
  else:
    env.setdefault("SYNC_LOCAL_GIT_PROGRESS", "1")
  if action == "push":
    env.setdefault("SYNC_GITEE_SINGLE_COMMIT", "1")
  if action == "pull" and _workdir_needs_cn_patch(repo_root, args):
    env["FORCE_SYNC"] = "1"
    print("[local] 本地尚未国内化，pull 将 FORCE_SYNC=1（即使 upstream SHA 未变）")

  old_environ = os.environ.copy()
  old_argv = sys.argv[:]
  try:
    os.environ.clear()
    os.environ.update(env)
    sys.argv = [str(impl_path)] + args
    impl = _load_impl_from_path(impl_path)
    if action == "push":
      _patch_impl_push_targets(impl, resolved_push_targets)
      _local_arm_skip_next_push_verify()
    try:
      with _local_filtered_stdio_if_quiet():
        impl.main()
    finally:
      if action == "push":
        _clear_push_verify_skip()
    return 0
  except SystemExit as e:
    code = e.code
    if code is None:
      return 0
    if isinstance(code, int):
      return code
    try:
      return int(code)
    except (TypeError, ValueError):
      return 1
  finally:
    os.environ.clear()
    os.environ.update(old_environ)
    sys.argv = old_argv


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
  project = _project_root(repo_root)
  dotenv = _parse_dotenv(project)
  env = _apply_project_secrets(project, os.environ.copy())
  if os.name == "nt":
    try:
      os.system("title sp-sync (local)")
    except Exception:
      pass

  try:
    workdir = _workdir_from_argv(repo_root, _inject_workdir_argv(repo_root, list(sys.argv[1:])))
  except Exception:
    workdir = repo_root / "sunnypilot"
  try:
    default_targets = _format_push_targets(_push_targets_from_env(env))
  except Exception:
    default_targets = _format_push_targets(set(_DEFAULT_PUSH_TARGETS))

  while True:
    print("\n=== sp-sync (local) 菜单 ===")
    print(f"workdir: {workdir}")
    print(f"默认推送目标: {default_targets}（可用 .env: SP_SYNC_PUSH_TARGETS=both|gitee|codeup）")
    print("1) pull：预 fetch upstream staging + 打补丁/校验（未国内化时自动 FORCE_SYNC，不推送）")
    print("2) push：推送 staging（上游未变默认跳过，可选强制；Gitee/Codeup/两者）")
    print("3) all：pull + push（上游未变时 push 同 2，默认不强制推）")
    print("4) mapd release：仅同步 mapd release（需要 GITEE_TOKEN）")
    print("5) installer：在 comma 设备编译 staging installer 并发布到 sp-cn_install（含 release）")
    print("6) deps mirrors：同步子依赖仓库镜像到 Gitee（opendbc/msgq/tinygrad/...）")
    print("0) 退出\n")
    choice = input("请选择操作 [0-6]: ").strip()

    if choice == "0":
      return 0
    if choice in ("1", "2", "3"):
      base = _inject_workdir_argv(repo_root, list(sys.argv[1:]))
      base = _ensure_local_defaults(repo_root, _strip_action_argv(base))
      push_targets: set[str] | None = None
      if choice in ("2", "3"):
        push_targets = _prompt_push_targets()
      if choice == "3":
        rc = _run_local_all(repo_root, base, stream=True, push_targets=push_targets)
      else:
        action = {"1": "pull", "2": "push"}[choice]
        rc = _run_impl_actions(
          repo_root,
          _argv_with_action(base, action),
          stream=True,
          push_targets=push_targets if choice == "2" else None,
        )
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
    if choice == "6":
      _sync_dependency_mirrors(repo_root)
      continue

    print("无效选择，请输入 0-6。")


def main(argv: Optional[list[str]] = None) -> None:
  repo_root = Path(__file__).resolve().parents[1]
  if argv is None:
    _maybe_reexec_with_local_venv(repo_root)

  argv = list(sys.argv[1:] if argv is None else argv)
  argv = _inject_workdir_argv(repo_root, argv)

  # If no explicit --action provided, show local menu（菜单内发起子进程时再注入本机默认参数）。
  if "--action" not in argv and is_tty():
    raise SystemExit(_menu(repo_root))

  # Local-only action: sync dependency mirrors (do not call shared impl)
  for i, a in enumerate(argv):
    if a == "--action" and i + 1 < len(argv):
      act = argv[i + 1].strip().lower()
      if act in ("sync-deps", "sync_deps", "deps", "dep-mirrors", "dep_mirrors"):
        _sync_dependency_mirrors(repo_root)
        raise SystemExit(0)
      break
    if a.startswith("--action="):
      act = a.split("=", 1)[1].strip().lower()
      if act in ("sync-deps", "sync_deps", "deps", "dep-mirrors", "dep_mirrors"):
        _sync_dependency_mirrors(repo_root)
        raise SystemExit(0)
      break

  argv = _ensure_local_defaults(repo_root, argv)
  env = _local_env_with_dotenv(repo_root)
  action = _parse_action_from_argv(argv)
  push_targets: set[str] | None = None
  force_push: bool | None = None
  if action in ("push", "all"):
    argv, force_push = _parse_and_strip_force_push_argv(argv)
    argv, push_targets = _parse_and_strip_push_targets_argv(argv, env, default_from_env=True)
  if action == "all":
    raise SystemExit(
      _run_local_all(
        repo_root,
        _strip_action_argv(argv),
        stream=True,
        push_targets=push_targets,
        force_push=force_push,
      )
    )
  raise SystemExit(
    _run_impl_actions(
      repo_root,
      argv,
      stream=True,
      push_targets=push_targets if action == "push" else None,
      force_push=force_push if action == "push" else None,
    )
  )


if __name__ == "__main__":
  main()

