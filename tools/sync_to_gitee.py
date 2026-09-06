#!/usr/bin/env python3
"""
工作区级（/home/perfume/sp）同步脚本：

- 拉取上游（git fetch upstream）；仅对 upstream/staging 打补丁、校验、推送 Gitee
- 上游 master 不参与补丁与推送（设备/comma 使用 staging；避免重复大包推送与无关更新检测）
- 应用国内化补丁（幂等）
- 更新子模块（包含子依赖）
- 可选（CLI 开关；云端 Actions 不使用）：``--build-installer``、``--sync-mapd-release``（需 GITEE_TOKEN）。
  mapd / 远端 installer 交互更适合在本机用 ``tools/sync_to_gitee_local.py``；本脚本内菜单不含这两项。
- 提交并推送到你的 Gitee：staging
- GitHub Actions 推送成功后默认打附注 tag ``Beta-{上游短SHA}-{北京时间 YYYYMMDD-HHMMSS}``，可选 ``stable-…``。说明（tag message）仍为上游 staging 包装提交里的 ``master commit`` 主题。``SYNC_SKIP_ARCHIVE_TAG=1`` 可关闭；``SYNC_ARCHIVE_TAG_KIND`` / ``--archive-tag-kind`` 指定种类。

用法示例：
  python3 tools/sync_to_gitee.py --action all                  # CI / 一键同步（常用）
  python3 tools/sync_to_gitee.py                               # 交互菜单：仅 1~3（pull / push / all）
  python3 tools/sync_to_gitee.py --action verify-tinygrad-models  # 校验 tinygrad_repo 与 models JSON ref
  python3 tools/sync_to_gitee.py --sync-mapd-release           # 仅脚本/自动化调用 mapd（非菜单）
  python3 tools/sync_to_gitee.py --build-installer             # 仅脚本调用 installer（非菜单）
"""

import argparse
import ast
import compileall
import json
import os
import py_compile
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import datetime
import time
import urllib.error
import urllib.request
import traceback
from urllib.parse import quote
from pathlib import Path
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]

# 仅同步 staging：与本仓库 CI/设备约定一致；更新有无只看 staging 相对 Gitee 的记录。
SYNC_BRANCHES: tuple[str, ...] = ("staging",)


def upstream_fetch_argv() -> tuple[list[str], str]:
  """
  上游首次 fetch 命令与日志说明。
  - CI / 显式全量：git fetch upstream --prune --tags（与历史一致）。
  - 本地（SP_SYNC_SOURCE=local）：只 fetch SYNC_BRANCHES + --prune，不主动拉全仓库 tag，
    减少传输（objects 仍按需增量获取，非「整仓重下」）。
  本地若需要与 CI 完全同款抓 tag：环境变量 SYNC_FULL_UPSTREAM_FETCH=1。
  """
  force_full = (os.environ.get("SYNC_FULL_UPSTREAM_FETCH") or "").strip().lower() in ("1", "true", "yes", "on", "y")
  is_local = (os.environ.get("SP_SYNC_SOURCE") or "").strip().lower() == "local"
  if is_local and not force_full:
    br = list(SYNC_BRANCHES)
    cmd = ["git", "fetch", "upstream"] + br + ["--prune"]
    hint = " ".join(br)
    label = (
      f"fetch upstream {hint} --prune（本地精简：不拉全部 tag；"
      "与上游差异仍为增量对象；全量同 CI 请设 SYNC_FULL_UPSTREAM_FETCH=1）"
    )
    return cmd, label
  return (
    ["git", "fetch", "upstream", "--prune", "--tags"],
    "fetch upstream --prune --tags",
  )


def default_workdir(repo_root: Path) -> Path:
  """
  兼容两种布局：
  - 扁平仓库：repo_root/.git 存在（CI/GitHub 仓库推荐形态）
  - 工作区套仓库：repo_root/sunnypilot/.git 存在（你本地当前形态）
  """
  if (repo_root / ".git").exists():
    return repo_root
  nested = repo_root / "sunnypilot"
  if (nested / ".git").exists():
    return nested
  return repo_root


WORKDIR_DEFAULT = str(default_workdir(REPO_ROOT))
UPSTREAM_DEFAULT = "https://github.com/sunnypilot/sunnypilot.git"

COMMA_HOST_DEFAULT = "10.90.1.231"
COMMA_USER_DEFAULT = "comma"
COMMA_SSH_KEY_DEFAULT = str(Path("/home/perfume/sp/sp-cn"))
GITEE_OWNER = "xc2026"
# 离线/ensure_tinygrad 回退 pin；对齐与 verify 优先读 models JSON tinygrad_ref（见 _resolve_tinygrad_models_ref）
TINYGRAD_MODELS_REF = "ac1632ab966c77ba96a7048b893a30f1a714dc87"
TINYGRAD_UPSTREAM_URL = "https://github.com/sunnypilot/tinygrad.git"
MAPD_REPO = "openpilot-mapd"
MAPD_UPSTREAM = "pfeiferj/openpilot-mapd"

INSTALLER_REPO_DEFAULT = f"https://gitee.com/{GITEE_OWNER}/sp-cn_install.git"

ALIYUN_SSH_KEY_DEFAULT = "~/.ssh/sp-cn"
SP_CN_TOKEN_ENV = "sp-cn-token"

# 设备/安装器静态补丁默认 Gitee；车端 OTA 实际主仓由 cn_main_repo_route 按私钥运行时选择。
MAIN_REPO_DEVICE_DEFAULT = "gitee"  # "gitee" | "codeup"，也可用 MAIN_REPO_DEVICE 覆盖（仅静态补丁如 setup.sh）


def gitee_https_repo(repo: str, *, owner: str | None = None) -> str:
  """https://gitee.com/{owner}/{repo}（repo 可含 .git 后缀）。"""
  o = owner or GITEE_OWNER
  return f"https://gitee.com/{o}/{repo}"


def gitee_git_ssh_repo(repo: str, *, owner: str | None = None) -> str:
  """git@gitee.com:{owner}/{repo}（无 .git 时自动追加）。"""
  o = owner or GITEE_OWNER
  name = repo if repo.endswith(".git") else f"{repo}.git"
  return f"git@gitee.com:{o}/{name}"


def gitee_raw_repo(repo: str, branch: str = "master", *, owner: str | None = None) -> str:
  return f"{gitee_https_repo(repo, owner=owner)}/raw/{branch}/"


def gitee_models_raw_gh_pages(*, branch: str = "gh-pages", owner: str | None = None) -> str:
  """sunnypilot-models 的 JSON 在 gh-pages 分支，非 master。"""
  return f"{gitee_https_repo('sunnypilot-models', owner=owner)}/raw/{branch}/"


def _extract_model_json_basename(url: str) -> str:
  """从 MODEL_URL 或类似路径取出 JSON 文件名。"""
  tail = url.rsplit("/", 1)[-1].split("?")[0].split("#")[0].strip()
  return tail if tail.endswith(".json") else "driving_models_v17.json"


def _expected_models_json_url(json_name: str | None = None) -> str:
  name = json_name or "driving_models_v17.json"
  return f"{gitee_models_raw_gh_pages()}docs/{name}"


_MODELS_JSON_VER_RE = re.compile(
  r"^(driving_models(?:_chestnut|_usbgpu)?)_v(\d+)\.json$",
  re.IGNORECASE,
)


def _models_json_version_fallbacks(json_name: str) -> list[str]:
  """同前缀、版本号递减（含自身），供 Gitee 镜像尚未跟上 GitHub 时回退。"""
  name = (json_name or "").strip()
  out: list[str] = []
  if name:
    out.append(name)
  m = _MODELS_JSON_VER_RE.match(name)
  if m:
    prefix, ver = m.group(1), int(m.group(2))
    for v in range(ver - 1, 16, -1):
      out.append(f"{prefix}_v{v}.json")
  seen: set[str] = set()
  uniq: list[str] = []
  for n in out:
    k = n.lower()
    if k not in seen:
      seen.add(k)
      uniq.append(n)
  return uniq


_HTTP_OK_CACHE: dict[str, int] = {}


def _http_url_status(url: str) -> int | None:
  """HEAD 状态码。仅缓存 2xx；404 不缓存，避免菜单 6 同步后同进程仍当成缺失。"""
  hit = _HTTP_OK_CACHE.get(url)
  if hit is not None:
    return hit
  try:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "spsync-models-json/1"})
    with urllib.request.urlopen(req, timeout=12) as resp:
      st = int(getattr(resp, "status", 200) or 200)
  except urllib.error.HTTPError as e:
    st = int(e.code)
  except (urllib.error.URLError, TimeoutError, OSError):
    return None
  if 200 <= st < 300:
    _HTTP_OK_CACHE[url] = st
  return st


def _resolve_gitee_models_json_url(json_name: str) -> str:
  """
  把上游 JSON 文件名映射到 Gitee raw。
  若 Gitee 还没有该版本（404），改用同系列已有的较低版本，避免车上清单 404。
  """
  names = _models_json_version_fallbacks(json_name)
  if not names:
    return _expected_models_json_url(json_name)
  wanted = _expected_models_json_url(names[0])
  st = _http_url_status(wanted)
  if st is None or (st is not None and 200 <= st < 300):
    return wanted
  if st != 404:
    return wanted
  for alt in names[1:]:
    url = _expected_models_json_url(alt)
    alt_st = _http_url_status(url)
    if alt_st is not None and 200 <= alt_st < 300:
      log(
        "warn",
        f"Gitee 尚无 {names[0]}（HTTP {st}），清单暂用 {alt}；请菜单 6 同步 sunnypilot-models 后再 pull",
      )
      return url
  return wanted


def _fix_models_json_url_typos(url: str) -> str:
  """修正 Gitee models JSON 的已知错误 raw 路径（幂等）。"""
  gh = gitee_models_raw_gh_pages()
  repo = gitee_https_repo("sunnypilot-models")
  for wrong in (
    f"{repo}/raw/master/refs/heads/gh-pages/",
    f"{repo}/raw/master/gh-pages/",
    f"{repo}/raw/refs/heads/gh-pages/",
  ):
    if wrong in url:
      url = url.replace(wrong, gh)
  return url


_GIT_SHA40 = re.compile(r"^[0-9a-f]{40}$")


def _git_sha40(value: str | None) -> str | None:
  if not value:
    return None
  v = value.strip().lower()
  return v if _GIT_SHA40.fullmatch(v) else None


def gitee_sp_cn_installer_openpilot_url() -> str:
  return f"{gitee_https_repo('sp-cn_install')}/raw/master/installer_openpilot"


def gitee_insteadof_rules() -> list[tuple[str, str]]:
  """updated.py ensure_url_insteadof 的 (url_key, instead_of) 对。"""
  o = GITEE_OWNER
  return [
    (f"url.https://gitee.com/{o}/.insteadof", "https://github.com/"),
    (f"url.https://gitee.com/{o}/.insteadof", "https://raw.githubusercontent.com/"),
    (f"url.git@gitee.com:{o}/.insteadof", "git@github.com:"),
    (f"url.git@gitee.com:{o}/.insteadof", "ssh://git@github.com/"),
  ]


def _render_gitee_insteadof_ensure_lines() -> str:
  return "\n".join(
    f'  ensure_url_insteadof("{url_key}", "{instead_of}")' for url_key, instead_of in gitee_insteadof_rules()
  )


@dataclass(frozen=True)
class MainRepoSource:
  """主仓库 sunnypilot_cn 的一个发布端（Gitee 或 Codeup）。"""
  id: str
  label: str
  ssh_url: str
  https_url: str
  installer_ssh_define: str
  version_remote_key: str
  git_remote_name: str
  push_lfs_skip: bool


def main_repo_source_gitee() -> MainRepoSource:
  """主仓 · Gitee（{GITEE_OWNER}/sunnypilot_cn）。"""
  o = GITEE_OWNER
  ssh = gitee_git_ssh_repo("sunnypilot_cn")
  https = f"{gitee_https_repo('sunnypilot_cn')}.git"
  return MainRepoSource(
    id="gitee",
    label="Gitee",
    ssh_url=ssh,
    https_url=https,
    installer_ssh_define=f'#define GIT_SSH_URL "{ssh}"',
    version_remote_key=f"gitee.com/{o}/sunnypilot_cn",
    git_remote_name="origin",
    push_lfs_skip=True,
  )


def gitee_mirror_needles() -> list[str]:
  """verify_patches / 门禁：应从 GITEE_OWNER 派生的镜像 URL 片段。"""
  g = main_repo_source_gitee()
  o = GITEE_OWNER
  needles: list[str] = [
    g.https_url,
    g.ssh_url,
    g.version_remote_key,
    f"gitee.com/{o}/",
    f"git@gitee.com:{o}/",
  ]
  for repo in (
    "sunnypilot-models",
    "openpilot-mapd",
    "Catch2.git",
    "dependencies.git",
    "msgq.git",
    "opendbc.git",
    "rednose.git",
    "teleoprtc.git",
    "tinygrad.git",
    "panda.git",
    "neural_network_data.git",
    "sp-cn_install",
  ):
    needles.append(f"gitee.com/{o}/{repo}")
  needles.append(gitee_sp_cn_installer_openpilot_url())
  return list(dict.fromkeys(needles))


def main_repo_source_codeup() -> MainRepoSource:
  """主仓 · 阿里云效 Codeup。"""
  return MainRepoSource(
    id="codeup",
    label="Codeup",
    ssh_url="git@codeup.aliyun.com:6a0b0c8d706afd34aa607161/sunnypilot_cn.git",
    https_url="https://codeup.aliyun.com/6a0b0c8d706afd34aa607161/sunnypilot_cn.git",
    installer_ssh_define=(
      '#define GIT_SSH_URL "git@codeup.aliyun.com:6a0b0c8d706afd34aa607161/sunnypilot_cn.git"'
    ),
    version_remote_key="codeup.aliyun.com/6a0b0c8d706afd34aa607161/sunnypilot_cn",
    git_remote_name="aliyun",
    push_lfs_skip=True,
  )


def enabled_main_repo_push_sources() -> list[MainRepoSource]:
  """
  同步时推送到哪些主仓远端。注释掉下面某一行即关闭该端推送。
  设备从哪拉主仓由 MAIN_REPO_DEVICE_DEFAULT / MAIN_REPO_DEVICE 决定（与推送可不同）。
  """
  sources: list[MainRepoSource] = []
  sources.append(main_repo_source_gitee())   # 推 Gitee（git remote origin）
  sources.append(main_repo_source_codeup())  # 推 Codeup（git remote aliyun）
  return sources


# CI 分步推送顺序（与 workflow 一致：先 Codeup 后 Gitee）
CI_PUSH_TARGET_ORDER: tuple[str, ...] = ("codeup", "gitee")


def enabled_push_target_ids() -> set[str]:
  return {s.id for s in enabled_main_repo_push_sources()}


def push_target_timeout_s(target_id: str) -> int | None:
  if target_id == "gitee":
    raw = (os.environ.get("GITEE_PUSH_TIMEOUT_S") or "600").strip()
    try:
      return max(30, int(raw))
    except ValueError:
      return 600
  return None


def ci_push_targets_report() -> list[dict[str, object]]:
  """每项: id, label, enabled, timeout_s。"""
  enabled = enabled_push_target_ids()
  out: list[dict[str, object]] = []
  for tid in CI_PUSH_TARGET_ORDER:
    src = main_repo_source_by_id(tid)
    out.append({
      "id": tid,
      "label": src.label,
      "enabled": tid in enabled,
      "timeout_s": push_target_timeout_s(tid),
    })
  return out


def ensure_push_results(state: dict[str, object]) -> dict[str, object]:
  pr = state.get("push_results")
  if not isinstance(pr, dict):
    pr = {}
    state["push_results"] = pr
  for row in ci_push_targets_report():
    tid = str(row["id"])
    if tid not in pr or not isinstance(pr.get(tid), dict):
      pr[tid] = {
        "enabled": bool(row["enabled"]),
        "label": str(row["label"]),
        "overall": "pending",
        "branches": {},
      }
    else:
      entry = pr[tid]
      if isinstance(entry, dict):
        entry["enabled"] = bool(row["enabled"])
        entry["label"] = str(row["label"])
        entry.setdefault("branches", {})
  return pr


def record_push_branch(
  state: dict[str, object],
  target_id: str,
  branch: str,
  status: str,
  detail: str = "",
) -> None:
  pr = ensure_push_results(state)
  entry = pr.setdefault(target_id, {"enabled": True, "branches": {}, "overall": "pending"})
  if not isinstance(entry, dict):
    return
  branches = entry.setdefault("branches", {})
  if not isinstance(branches, dict):
    branches = {}
    entry["branches"] = branches
  rec: dict[str, str] = {"status": status}
  if detail:
    rec["detail"] = detail[:800]
  branches[branch] = rec


def finalize_push_target(state: dict[str, object], target_id: str) -> None:
  pr = ensure_push_results(state)
  entry = pr.get(target_id)
  if not isinstance(entry, dict):
    return
  if not entry.get("enabled", True):
    entry["overall"] = "disabled"
    return
  branches = entry.get("branches") or {}
  if not isinstance(branches, dict) or not branches:
    entry["overall"] = entry.get("overall") or "skip"
    return
  statuses = [str((b or {}).get("status", "")) for b in branches.values() if isinstance(b, dict)]
  if all(s == "ok" for s in statuses):
    entry["overall"] = "ok"
  elif any(s == "ok" for s in statuses):
    entry["overall"] = "partial"
  elif any(s == "timeout" for s in statuses):
    entry["overall"] = "timeout"
  else:
    entry["overall"] = "fail"


def _exception_push_status(exc: BaseException) -> tuple[str, str]:
  if isinstance(exc, subprocess.TimeoutExpired):
    t = exc.timeout
    return "timeout", f"命令超时（{t}s）"
  msg = str(exc)
  if "超时" in msg or "timed out" in msg.lower() or "timeout" in msg.lower():
    return "timeout", msg[:800]
  return "fail", msg[:800]


def _push_status_label(status: str) -> str:
  return {
    "ok": "成功",
    "fail": "失败",
    "timeout": "超时（已放弃）",
    "skip": "跳过",
    "disabled": "未启用推送",
    "pending": "未执行",
  }.get(status, status)


def apply_ci_step_outcome_fallback(
  state: dict[str, object],
  *,
  codeup_step: str | None = None,
  gitee_step: str | None = None,
) -> None:
  """Actions 强杀进程时 state 可能缺记录，用 step outcome 补全。"""
  if not state.get("to_push"):
    return
  branches = [str(b) for b in (state.get("branches") or list(SYNC_BRANCHES))]
  outcomes = {"codeup": codeup_step, "gitee": gitee_step}
  for tid, outcome in outcomes.items():
    if not outcome or outcome in ("success", "skipped"):
      continue
    pr = ensure_push_results(state)
    entry = pr.get(tid)
    if not isinstance(entry, dict) or not entry.get("enabled"):
      continue
    branches_map = entry.setdefault("branches", {})
    if not isinstance(branches_map, dict):
      continue
    st = "timeout" if outcome in ("cancelled", "timed_out") else "fail"
    detail = f"CI step {tid} outcome={outcome}（脚本内未写入详细 git 输出，请查 Actions 日志）"
    for b in branches:
      if b not in branches_map:
        record_push_branch(state, tid, b, st, detail)
      else:
        rec = branches_map.get(b)
        if isinstance(rec, dict) and rec.get("status") in ("fail", "timeout") and not rec.get("detail"):
          rec["detail"] = detail
    finalize_push_target(state, tid)


def update_pushed_flags_from_push_results(state: dict[str, object]) -> None:
  pr = ensure_push_results(state)
  state["pushed_gitee"] = pr.get("gitee", {}).get("overall") == "ok" if isinstance(pr.get("gitee"), dict) else False
  state["pushed_codeup"] = pr.get("codeup", {}).get("overall") == "ok" if isinstance(pr.get("codeup"), dict) else False
  state["pushed"] = bool(state.get("pushed_gitee")) or bool(state.get("pushed_codeup"))


def _sync_reason_variant(state: dict[str, object]) -> str:
  reason_tags: list[str] = list(state.get("sync_reason_tags") or [])
  if not reason_tags:
    return "upstream_only"
  uniq = set(reason_tags)
  if uniq == {"force_same"}:
    return "force_only"
  if uniq == {"upstream_delta"}:
    return "upstream_only"
  return "mixed"


def _build_sync_context_section(state: dict[str, object]) -> str:
  """upstream/force 说明、分支核对、上游提交摘要（full_ok 与 partial_ok 共用）。"""
  variant = _sync_reason_variant(state)
  if variant == "force_only":
    line_note = "说明：本次为手动 Force 强制重同步（上游 superproject 提交相对上次写入 Gitee 的记录一致，仍重跑补丁并推送）。"
  elif variant == "upstream_only":
    line_note = (
      "说明：本次检测到上游 sunnypilot 主仓库（superproject）提交与上次镜像提交信息里的 upstream-* 行不一致，因而同步推送；"
      "比较优先读 Gitee，若无记录则回退 Codeup（半成功时避免误触发）。"
      "比较的是完整 Git 对象 ID（短哈希与全哈希视为同一提交）。"
      "子模块指针变化会体现在主仓库树或 .gitmodules 的差异里；若你只看网页上的「某个依赖版本」而主仓库 commit 未变，脚本仍会认为无同步必要。"
    )
  else:
    line_note = "说明：本次同步中兼有「上游 superproject 有变化」与「强制重同步」情况，详见 Actions 日志。"
  parts = [line_note]
  notes = state.get("notify_branch_notes") or []
  if notes:
    parts.append("分支核对：\n" + "\n".join(str(x) for x in notes))
  commit_blocks = state.get("upstream_commit_blocks") or []
  if commit_blocks:
    sec_title = "上游提交说明"
    parts.append(f"---\n{sec_title}：\n\n" + "\n\n".join(str(b) for b in commit_blocks))
  return "\n\n".join(parts)


def _build_push_results_section(state: dict[str, object]) -> str:
  lines = ["推送结果：", f"· 推送版本: {_mail_channel_label(state)}"]
  for row in ci_push_targets_report():
    tid = str(row["id"])
    label = str(row["label"])
    enabled = bool(row["enabled"])
    pr = ensure_push_results(state)
    entry = pr.get(tid) if isinstance(pr.get(tid), dict) else {}
    if not enabled:
      lines.append(f"· {label} [未启用] — enabled_main_repo_push_sources 已注释，本轮不推送。")
      continue
    lines.append(f"· {label} [已启用] — 汇总：{_push_status_label(str(entry.get('overall', 'pending')))}")
    branches = entry.get("branches") or {}
    if isinstance(branches, dict) and branches:
      for br, rec in sorted(branches.items()):
        if not isinstance(rec, dict):
          continue
        st = str(rec.get("status", "pending"))
        extra = str(rec.get("detail", "")).strip()
        line = f"  - {br}: {_push_status_label(st)}"
        # 邮件里不回显命令失败的原始输出/命令行（避免噪声/潜在泄露）；需要细节请查 Actions 日志。
        if extra and st != "ok":
          line += "（详见 Actions 日志）"
        lines.append(line)
    elif entry.get("overall") in ("skip", "pending"):
      lines.append("  - （本轮无待推送分支或未执行 push step）")
  return "\n".join(lines)


@dataclass(frozen=True)
class CiNotifyPlan:
  mail_kind: str  # full_ok | partial_ok | fail | none
  notify_send: bool
  subject: str
  body: str


def build_ci_notify(
  state: dict[str, object],
  *,
  codeup_step_outcome: str | None = None,
  gitee_step_outcome: str | None = None,
) -> CiNotifyPlan:
  apply_ci_step_outcome_fallback(state, codeup_step=codeup_step_outcome, gitee_step=gitee_step_outcome)
  update_pushed_flags_from_push_results(state)

  attempted = bool(state.get("attempted"))
  to_push = bool(state.get("to_push"))
  if not attempted and not to_push:
    return CiNotifyPlan("none", False, "", "")

  ensure_push_results(state)
  enabled_rows = [r for r in ci_push_targets_report() if r["enabled"]]
  enabled_ids = [str(r["id"]) for r in enabled_rows]

  def _overall(tid: str) -> str:
    pr = state.get("push_results") or {}
    e = pr.get(tid) if isinstance(pr, dict) else None
    return str(e.get("overall", "pending")) if isinstance(e, dict) else "pending"

  ok_ids = [tid for tid in enabled_ids if _overall(tid) == "ok"]
  bad_ids = [tid for tid in enabled_ids if _overall(tid) in ("fail", "timeout", "partial")]

  if not enabled_ids:
    mail_kind = "none"
    notify_send = attempted and to_push
    summary = "sunnypilot_cn：未配置任何推送目标（请检查 enabled_main_repo_push_sources）。"
  elif ok_ids and not bad_ids:
    mail_kind = "full_ok"
    notify_send = True
    bits = [main_repo_source_by_id(t).label for t in ok_ids]
    summary = f"sunnypilot_cn → {'、'.join(bits)} 同步成功（本轮已执行补丁校验并完成推送）。"
  elif ok_ids and bad_ids:
    mail_kind = "partial_ok"
    notify_send = True
    ok_l = "、".join(main_repo_source_by_id(t).label for t in ok_ids)
    bad_parts = []
    for tid in bad_ids:
      ov = _overall(tid)
      bad_parts.append(f"{main_repo_source_by_id(tid).label}（{_push_status_label(ov)}）")
    summary = f"sunnypilot_cn 部分成功：{ok_l} 已推送；{'；'.join(bad_parts)}。"
  else:
    mail_kind = "fail"
    notify_send = bool(to_push) or attempted
    summary = "sunnypilot_cn 推送失败（所有已启用目标均未成功，详见下方推送结果）。"

  body_parts = [summary, _build_push_results_section(state)]
  # partial/full 邮件始终附带说明块（与旧版成功邮件一致）；Force 依赖 sync_reason_tags 中的 force_same
  if mail_kind in ("full_ok", "partial_ok") and (
    state.get("sync_reason_tags") or state.get("upstream_commit_blocks") or state.get("notify_branch_notes")
  ):
    body_parts.append(_build_sync_context_section(state))
  elif mail_kind == "fail" and (
    state.get("sync_reason_tags")
    or state.get("upstream_commit_blocks")
    or state.get("notify_branch_notes")
  ):
    body_parts.append(_build_sync_context_section(state))

  ch = _mail_channel_slug(state)
  subject_map = {
    "full_ok": f"[OK] sp_cn_{ch}_sync-bot",
    "partial_ok": f"[Partially OK] sp_cn_{ch}_sync-bot",
    "fail": f"[FAIL] sp_cn_{ch}_sync-bot",
  }
  subject = subject_map.get(mail_kind, "")
  body = "\n\n".join(p for p in body_parts if p)
  return CiNotifyPlan(mail_kind, notify_send, subject, body)


def write_push_targets_github_output() -> None:
  if os.environ.get("GITHUB_ACTIONS") != "true":
    for row in ci_push_targets_report():
      print(f"{row['id']}_enabled={row['enabled']}")
    return
  path = os.environ.get("GITHUB_OUTPUT")
  if not path:
    return
  with open(path, "a", encoding="utf-8") as f:
    for row in ci_push_targets_report():
      tid = str(row["id"])
      f.write(f"{tid}_enabled={'true' if row['enabled'] else 'false'}\n")


def main_repo_device_source() -> MainRepoSource:
  """设备/安装器/ setup.sh 写入的主仓拉取源。"""
  want = (os.environ.get("MAIN_REPO_DEVICE") or MAIN_REPO_DEVICE_DEFAULT).strip().lower()
  by_id = {
    main_repo_source_gitee().id: main_repo_source_gitee(),
    main_repo_source_codeup().id: main_repo_source_codeup(),
  }
  if want not in by_id:
    raise SystemExit(f"MAIN_REPO_DEVICE 无效: {want!r}（可用: {', '.join(by_id)})")
  return by_id[want]


def main_repo_source_by_id(source_id: str) -> MainRepoSource:
  for s in (main_repo_source_gitee(), main_repo_source_codeup()):
    if s.id == source_id:
      return s
  raise KeyError(source_id)


# 兼容旧常量 / workflow 参数
_gitee = main_repo_source_gitee()
_codeup = main_repo_source_codeup()
REPO_DEFAULT = _gitee.ssh_url
ALIYUN_REPO_DEFAULT = _codeup.ssh_url
ALIYUN_REPO_HTTPS = _codeup.https_url


def _maybe_git_fetch_progress(cmd: list[str]) -> list[str]:
  """由 sync_to_gitee_local 设置 SYNC_LOCAL_GIT_PROGRESS=1 时，为 git fetch 插入 --progress。"""
  raw = (os.environ.get("SYNC_LOCAL_GIT_PROGRESS") or "").strip().lower()
  if raw not in ("1", "true", "yes", "on", "y"):
    return cmd
  if len(cmd) < 2 or cmd[0] != "git" or cmd[1] != "fetch" or "--progress" in cmd:
    return cmd
  return [cmd[0], cmd[1], "--progress"] + cmd[2:]


def _should_inherit_stdio_for_long_git(cmd: list[str]) -> bool:
  """
  fetch/clone/push、submodule update|sync：进度常为 \\r 刷新；走 PIPE 按行读会长时间无输出。
  TTY 流式模式下改为子进程直连终端。
  """
  if len(cmd) < 2 or cmd[0] != "git":
    return False
  verb = cmd[1]
  if verb in ("fetch", "clone", "pull", "push"):
    return True
  if verb == "submodule" and len(cmd) > 2 and cmd[2] in ("update", "sync"):
    return True
  return False


def _subprocess_text_kwargs() -> dict[str, str]:
  """Windows 默认 GBK；git 输出常含 UTF-8/CP1252 字节，text=True 会 UnicodeDecodeError。"""
  return {"encoding": "utf-8", "errors": "replace"}


def run(
  cmd: list[str],
  cwd: str | None = None,
  env: dict[str, str] | None = None,
  *,
  stream: bool | None = None,
  timeout_s: int | None = None,
) -> str:
  """
  默认行为：
  - 交互终端（TTY）下：实时输出（避免 git fetch 等长任务“看起来没反应”）
  - CI/非交互：保持 capture（便于错误时把完整输出带回日志）
  - TTY + 长耗时 git：子进程继承当前终端 stdio（否则 PIPE 吞掉 \\r 进度）
  - timeout_s：仅非 stream 模式生效（Gitee push 限时）
  """
  cmd = _maybe_git_fetch_progress(list(cmd))
  text_kw = _subprocess_text_kwargs()

  if stream is None:
    stream = sys.stdout.isatty()

  def _timeout_expired() -> RuntimeError:
    return RuntimeError(f"命令超时（{timeout_s}s）: {' '.join(cmd)}")

  if not stream:
    try:
      p = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_s,
        **text_kw,
      )
    except subprocess.TimeoutExpired as e:
      raise _timeout_expired() from e
    if p.returncode != 0:
      raise RuntimeError(f"命令失败: {' '.join(cmd)}\n{p.stdout}")
    return p.stdout

  if _should_inherit_stdio_for_long_git(cmd):
    try:
      p = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
      raise _timeout_expired() from e
    if p.returncode != 0:
      raise RuntimeError(f"命令失败: {' '.join(cmd)}")
    return ""

  p2 = subprocess.Popen(
    cmd,
    cwd=cwd,
    env=env,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    bufsize=1,
    **text_kw,
  )
  assert p2.stdout is not None
  out_lines: list[str] = []
  try:
    for line in p2.stdout:
      sys.stdout.write(line)
      sys.stdout.flush()
      out_lines.append(line)
    rc = p2.wait(timeout=timeout_s)
  except subprocess.TimeoutExpired:
    p2.kill()
    raise _timeout_expired()
  out = "".join(out_lines)
  if rc != 0:
    raise RuntimeError(f"命令失败: {' '.join(cmd)}\n{out}")
  return out


def log(stage: str, msg: str) -> None:
  ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  print(f"[{ts}] [{stage}] {msg}")


def retry(op_name: str, fn, tries: int = 3, base_sleep_s: float = 1.0) -> any:
  last: Exception | None = None
  for i in range(1, tries + 1):
    try:
      return fn()
    except Exception as e:
      last = e
      if i >= tries:
        break
      sleep_s = base_sleep_s * (2 ** (i - 1))
      log("retry", f"{op_name} failed (attempt {i}/{tries}): {type(e).__name__}: {e}. sleep {sleep_s:.1f}s")
      time.sleep(sleep_s)
  assert last is not None
  raise last


def _env_truthy(name: str, *, default: str = "") -> bool:
  raw = (os.environ.get(name) or default).strip().lower()
  return raw in ("1", "true", "yes", "on", "y")


def squash_branch_single_commit(root: Path, branch: str, env: dict[str, str]) -> None:
  """
  Gitee 免费版单仓库约 1GB；长期推送完整 upstream 历史会超限。
  推送前将分支压成单提交（orphan），仅保留当前树与提交说明（含 upstream-{branch} SHA）。
  车端 git fetch + reset 仍可用；跳过同步仍靠提交正文里的 upstream SHA。
  注意：远端体积需偶尔在 Gitee 仓库设置执行「Git GC」回收旧对象。
  """
  run(["git", "checkout", branch], str(root), env=env)
  msg = run(["git", "log", "-1", "--format=%B"], str(root), env=env).rstrip() + "\n"
  tmp = f"__sp_squash_{branch}"
  run(["git", "checkout", "--orphan", tmp], str(root), env=env)
  run(["git", "add", "-A"], str(root), env=env)
  run(["git",
       "-c", "user.name=sunnypilot-cn-bot",
       "-c", "user.email=sunnypilot-cn-bot@local",
       "commit", "-m", msg], str(root), env=env)
  run(["git", "branch", "-M", branch], str(root), env=env)
  log("push", f"{branch}: squashed to single commit (Gitee quota); run Gitee Git GC if push still rejected")


def sp_cn_token_from_env(env: dict[str, str] | None = None) -> str:
  """个人访问令牌（PAT），用于 Codeup HTTPS 克隆（如 TICI 远程编译）。"""
  e = env if env is not None else os.environ
  # 本地 .env 用 sp-cn-token；GitHub Actions 建议 Secret 名 SP_CN_TOKEN（env 不宜含连字符）
  t = (e.get(SP_CN_TOKEN_ENV) or e.get("SP_CN_TOKEN") or "").strip().strip('"')
  if t:
    return t
  dotenv_path = REPO_ROOT / ".env"
  if dotenv_path.exists():
    dot = load_dotenv(dotenv_path)
    t = (dot.get(SP_CN_TOKEN_ENV) or dot.get("SP_CN_TOKEN") or "").strip().strip('"')
    if t:
      return t
  return ""


def ensure_sp_cn_token(env: dict[str, str], *, required: bool = False) -> None:
  t = sp_cn_token_from_env(env)
  if t:
    env.setdefault(SP_CN_TOKEN_ENV, t)
    env.setdefault("SP_CN_TOKEN", t)
    return
  if required:
    log(
      "warn",
      f"未设置 {SP_CN_TOKEN_ENV} / SP_CN_TOKEN（可在 {REPO_ROOT / '.env'} 或 GitHub Secret 配置；TICI 远程 Codeup 克隆需要）",
    )


def aliyun_ssh_key_path() -> Path:
  raw = (os.environ.get("ALIYUN_SSH_KEY") or ALIYUN_SSH_KEY_DEFAULT).strip()
  return Path(raw).expanduser()


def aliyun_git_push_env(base_env: dict[str, str]) -> dict[str, str]:
  """推送到云效（SSH）：专用密钥；与 Gitee 一致跳过 LFS 上传（OTA 不依赖主仓 LFS）。"""
  out = dict(base_env)
  key = aliyun_ssh_key_path()
  out["GIT_SSH_COMMAND"] = (
    f"ssh -i {key} -o StrictHostKeyChecking=no -o BatchMode=yes"
  )
  out["GIT_LFS_SKIP_PUSH"] = "1"
  return out


def aliyun_git_https_push_env(base_env: dict[str, str]) -> dict[str, str]:
  """推送到云效（HTTPS）：凭据嵌入 remote URL；与 Gitee 一致跳过 LFS。"""
  out = dict(base_env)
  out.pop("GIT_SSH_COMMAND", None)
  out["GIT_TERMINAL_PROMPT"] = "0"
  out["GIT_LFS_SKIP_PUSH"] = "1"
  return out


def aliyun_push_via_https(env: dict[str, str]) -> bool:
  """
  ALIYUN_PUSH_SSH=1 → 仅 SSH；ALIYUN_PUSH_HTTPS=1 → 有令牌则 HTTPS。
  默认 auto：CI/本机已有 ~/.ssh/sp-cn 时优先 SSH；仅无 SSH 私钥时用 HTTPS 令牌。
  """
  if _env_truthy("ALIYUN_PUSH_SSH"):
    return False
  if _env_truthy("ALIYUN_PUSH_HTTPS"):
    return bool(sp_cn_token_from_env(env))
  if aliyun_ssh_key_path().exists():
    return False
  return bool(sp_cn_token_from_env(env))


def _is_codeup_https_auth_error(exc: BaseException) -> bool:
  msg = str(exc)
  return (
    "Authentication failed" in msg
    or "克隆账号或密码错误" in msg
    or "Https克隆" in msg
  )


def aliyun_push_available(env: dict[str, str]) -> bool:
  if _env_truthy("SYNC_SKIP_ALIYUN_PUSH"):
    return False
  if aliyun_push_via_https(env):
    return True
  key = aliyun_ssh_key_path()
  if key.exists():
    return True
  log(
    "warn",
    f"跳过 aliyun push：无 {SP_CN_TOKEN_ENV}/SP_CN_TOKEN 且 SSH 私钥不存在 ({key})",
  )
  return False


def codeup_https_url_with_token(token: str, env: dict[str, str] | None = None) -> str:
  """
  Codeup HTTPS 克隆/推送。密码须 URL 编码（如 ! → %21）。
  用户名默认 oauth2；若云效页面给出专用克隆账号，可设环境变量 ALIYUN_CODEUP_USER。
  """
  e = env or {}
  user = (e.get("ALIYUN_CODEUP_USER") or "oauth2").strip() or "oauth2"
  pw = quote(token.strip().strip('"'), safe="")
  user_q = quote(user, safe="")
  host_path = main_repo_source_codeup().https_url.removeprefix("https://")
  return f"https://{user_q}:{pw}@{host_path}"


def run_ssh(host: str, user: str, key_path: str, remote_cmd: str, timeout_s: int = 3600) -> str:
  key = Path(key_path)
  if not key.exists():
    raise RuntimeError(f"未找到 comma SSH 私钥: {key}")
  cmd = [
    "ssh",
    "-i", str(key),
    "-p", "22",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
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
    "-i", str(key),
    "-P", "22",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    f"{user}@{host}:{remote_path}",
    str(local_path),
  ]
  p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout_s)
  if p.returncode != 0:
    raise RuntimeError(f"SCP 下载失败: {remote_path} -> {local_path}\n{p.stdout}")


def sync_installer_to_repo(installer_path: Path, repo_url: str, branch: str = "master") -> None:
  """
  将编译好的 installer 二进制同步到一个专用仓库（例如 gitee 的 sp-cn_install）。

  约定：
  - 仓库里保存一个固定文件名（comma 设备识别）：
    - installer_openpilot
  - 使用 git 提交并 push 到 branch
  """
  if not installer_path.exists():
    raise RuntimeError(f"installer 文件不存在：{installer_path}")

  # Prefer pushing over SSH even if user provided https, to avoid interactive credential prompts.
  # (clone can be https; for push, set remote url to ssh)
  repo_url = repo_url.strip()
  m = re.fullmatch(r"https?://gitee\.com/([^/]+)/([^/]+?)(?:\.git)?/?", repo_url)
  ssh_url = None
  owner = None
  name = None
  if m:
    owner, name = m.group(1), m.group(2)
    ssh_url = f"git@gitee.com:{owner}/{name}.git"
  elif repo_url.startswith("git@gitee.com:"):
    ssh_url = repo_url
    m2 = re.fullmatch(r"git@gitee\.com:([^/]+)/([^/]+?)(?:\.git)?", repo_url)
    if m2:
      owner, name = m2.group(1), m2.group(2)

  with tempfile.TemporaryDirectory(prefix="sp_installer_repo_") as td:
    td_path = Path(td)
    # clone
    run(["git", "clone", "--depth=1", "-b", branch, repo_url, str(td_path / "repo")])
    repo = td_path / "repo"

    # ensure remote push url uses ssh when possible
    if ssh_url:
      run(["git", "remote", "set-url", "--push", "origin", ssh_url], str(repo))

    # copy/rename into repo
    dst_latest = repo / "installer_openpilot"
    shutil.copy2(installer_path, dst_latest)
    dst_latest.chmod(0o755)

    # commit if changed
    run(["git", "add", "-A"], str(repo))
    status = run(["git", "status", "--porcelain"], str(repo))
    if not status.strip():
      return

    short = run(["git", "rev-parse", "--short", "HEAD"], str(repo)).strip()
    msg = f"update installer binaries (from {short})"
    run(["git",
         "-c", "user.name=sp-installer-bot",
         "-c", "user.email=sp-installer-bot@local",
         "commit", "-m", msg], str(repo))
    run(["git", "push", "-u", "origin", branch], str(repo))


def build_comma_installer_staging(host: str, user: str, key_path: str, out_dir: Path) -> Path:
  """
  在 comma (TICI/larch64) 上构建 staging installer，并拷贝到 out_dir。
  产物命名：installer_openpilot_staging
  """
  # quick connectivity check
  run_ssh(host, user, key_path, "echo connected && uname -m && test -f /TICI && echo HAS_/TICI || echo NO_/TICI", timeout_s=30)

  remote_root = "/data/tmp/sp_build/sunnypilot_cn"
  device = main_repo_device_source()
  token = sp_cn_token_from_env()
  if device.id == "codeup" and token:
    repo_url_https = codeup_https_url_with_token(token)
  else:
    repo_url_https = device.https_url
  if not token:
    log("warn", f"TICI 远程克隆未嵌入 PAT（缺少 {SP_CN_TOKEN_ENV}），将使用无鉴权 HTTPS")
  target = "selfdrive/ui/installer/installers/installer_openpilot_staging"

  remote_cmd = r"""
set -euo pipefail
HOST_OK=1

# ensure tools
sudo -n true >/dev/null 2>&1 || true
sudo apt-get update -y >/dev/null
sudo apt-get install -y scons git >/dev/null

mkdir -p /data/tmp/sp_build
cd /data/tmp/sp_build
if [ ! -d "sunnypilot_cn/.git" ]; then
  rm -rf sunnypilot_cn
  git clone --depth=1 --recurse-submodules """ + repo_url_https + r""" sunnypilot_cn
fi

cd """ + remote_root + r"""

# Rewrite submodule URLs in .gitmodules to https to avoid needing SSH keys on device
python3 - <<'PY'
from pathlib import Path
p = Path(".gitmodules")
if p.exists():
  t = p.read_text(encoding="utf-8")
  t2 = t.replace("git@gitee.com:", "https://gitee.com/")
  if t2 != t:
    p.write_text(t2, encoding="utf-8")
PY
git submodule sync --recursive
GIT_TERMINAL_PROMPT=0 git submodule update --init --recursive --depth=1

# Ensure python deps for SCons environment (uv cache must be on /data, /home is tiny overlay)
mkdir -p /data/tmp/uv-cache /data/tmp/uv-tmp
export UV_CACHE_DIR=/data/tmp/uv-cache
export TMPDIR=/data/tmp/uv-tmp
uv sync --frozen --no-dev
. .venv/bin/activate

# Ensure staging installer target exists in selfdrive/ui/SConscript
python3 - <<'PY'
from pathlib import Path
p = Path("selfdrive/ui/SConscript")
if not p.exists():
  raise SystemExit("missing selfdrive/ui/SConscript (unexpected)")
s = p.read_text(encoding="utf-8")
# fix possible previous bad insertion (literal \n)
s = s.replace("\\n", "\n")
needle = '("openpilot_staging", "staging"),'
if needle in s:
  p.write_text(s, encoding="utf-8")
  raise SystemExit(0)

# insert after openpilot_test line if present, else after openpilot line
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

# build only the staging installer
scons -j"$(nproc)" """ + target + r"""
ls -la selfdrive/ui/installer/installers | head -n 50
file """ + target + r"""
"""

  run_ssh(host, user, key_path, remote_cmd, timeout_s=3 * 3600)

  local_out = out_dir / "installer_openpilot_staging"
  scp_from(host, user, key_path, f"{remote_root}/{target}", local_out)
  # make it executable locally too
  local_out.chmod(0o755)
  return local_out


def load_dotenv(path: Path) -> dict[str, str]:
  if not path.exists():
    return {}
  out: dict[str, str] = {}
  for line in path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
      continue
    k, v = line.split("=", 1)
    k = k.strip()
    v = v.strip().strip('"').strip("'")
    out[k] = v
  return out


def prepare_git_env(root: Path) -> tuple[dict[str, str], Path | None]:
  """
  关键点：仓库如果启用了 Git LFS，但系统 PATH 里没有 git-lfs，
  git 的 hook（post-checkout 等）会直接失败并让 git checkout 返回非 0。

  这里用两层兜底：
  - 若 `.venv/bin/git-lfs` 存在，则把它 prepend 到 PATH
  - 若系统没有 git-lfs：通过临时覆盖 git 配置，禁用 LFS filter（保留指针文件，不做下载），
    避免 checkout 直接失败；同时再创建一个临时 `git-lfs` shim（exit 0）来兜底 hooks。
  """
  env = os.environ.copy()
  shim_dir: Path | None = None

  def add_git_config_override(key: str, value: str) -> None:
    # Use GIT_CONFIG_COUNT + GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n to override config.
    # This is per-process and doesn't modify the repo.
    n = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    env["GIT_CONFIG_KEY_" + str(n)] = key
    env["GIT_CONFIG_VALUE_" + str(n)] = value
    env["GIT_CONFIG_COUNT"] = str(n + 1)

  venv_git_lfs = root / ".venv" / "bin" / "git-lfs"
  if venv_git_lfs.exists():
    env["PATH"] = f"{venv_git_lfs.parent}:{env.get('PATH','')}"
    return env, None

  if shutil.which("git-lfs", path=env.get("PATH")):
    return env, None

  # No git-lfs: disable LFS filter so checkout doesn't fail.
  # Keep pointer files (no smudge/clean), which is enough for branch sync/patching.
  add_git_config_override("filter.lfs.process", "cat")
  add_git_config_override("filter.lfs.smudge", "cat")
  add_git_config_override("filter.lfs.clean", "cat")
  add_git_config_override("filter.lfs.required", "false")

  td = Path(tempfile.mkdtemp(prefix="git_lfs_shim_"))
  shim = td / "git-lfs"
  shim.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
  shim.chmod(0o755)
  env["PATH"] = f"{td}:{env.get('PATH','')}"
  return env, td


def http_json(url: str, headers: dict[str, str] | None = None) -> dict:
  def _do() -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
      return json.loads(resp.read().decode("utf-8"))
  return retry(f"http_json {url}", _do, tries=3, base_sleep_s=1.0)


def http_download(url: str, out_path: Path) -> None:
  def _do() -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "sync-to-gitee"})
    with urllib.request.urlopen(req, timeout=60) as resp, out_path.open("wb") as f:
      while True:
        chunk = resp.read(1024 * 1024)
        if not chunk:
          break
        f.write(chunk)
  return retry(f"http_download {url}", _do, tries=3, base_sleep_s=1.0)


def http_multipart_post(
  url: str,
  fields: dict[str, str],
  file_field: str,
  filename: str,
  file_bytes: bytes,
  *,
  timeout_s: int | None = None,
) -> dict:
  boundary = "----syncToGiteeBoundary7f3a1b"
  parts: list[bytes] = []
  for k, v in fields.items():
    parts.append(
      (f"--{boundary}\r\n"
       f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
       f"{v}\r\n").encode("utf-8")
    )
  parts.append(
    (f"--{boundary}\r\n"
     f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
     f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
  )
  parts.append(file_bytes)
  parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
  body = b"".join(parts)

  if timeout_s is None:
    try:
      timeout_s = int((os.environ.get("GITEE_UPLOAD_TIMEOUT_S") or "600").strip())
    except ValueError:
      timeout_s = 600
  timeout_s = max(30, timeout_s)

  def _do() -> dict:
    # Fresh Request each attempt (safer for retries after partial send / timeout).
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
      data = resp.read().decode("utf-8")
      return json.loads(data) if data else {}

  return retry(f"http_multipart_post {url}", _do, tries=5, base_sleep_s=2.0)


def ensure_line_in_tuple_block(src: str, key: str) -> str:
  if key in src:
    return src
  m = re.search(r"def\s+sunnypilot_remote\(self\)\s*->\s*bool:\s*\n\s*return\s+self\.git_normalized_origin\s+in\s+\(", src, re.M)
  if not m:
    raise RuntimeError("未找到 sunnypilot_remote 识别块")
  close_idx = src.find(")", m.end())
  if close_idx == -1:
    raise RuntimeError("未找到 sunnypilot_remote 识别块结尾")
  before = src[:close_idx].rstrip()
  after = src[close_idx:]
  if not before.endswith(","):
    before = before + ","
  before = before + f'\n                                          "{key}"'
  return before + after


def replace_or_fail(path: Path, replacements: list[tuple[str, str]]) -> None:
  s = path.read_text(encoding="utf-8")
  orig = s
  for a, b in replacements:
    if a not in s and b not in s:
      raise RuntimeError(f"{path} 未找到预期内容: {a}")
    s = s.replace(a, b)
  if s != orig:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(s, encoding="utf-8")


def write_if_changed(path: Path, new_text: str) -> bool:
  old = path.read_text(encoding="utf-8") if path.exists() else ""
  if new_text != old:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    return True
  return False


def _uses_openpilot_pkg_layout(root: Path) -> bool:
  """上游将 system/selfdrive/sunnypilot 迁入 openpilot/ 包目录后的布局检测。

  注意：国内化可能在仓库根留下 OTA 兼容 shim（如 system/hardware/tici/agnos.json），
  不能仅用「是否存在 system/」判断，否则会把新布局误判成旧布局。
  """
  if not (root / "openpilot" / "system").is_dir():
    return False
  # 旧布局根目录有完整 system/updated；仅有 hardware shim 仍算新布局
  return not (root / "system" / "updated").is_dir()


def cn_rel(root: Path, rel: str) -> str:
  """
  将历史根相对路径适配到当前仓库布局。
  - 新布局：system|selfdrive|sunnypilot → openpilot/...
  - version.py：system/version.py → openpilot/common/version.py
  """
  rel = rel.replace("\\", "/")
  while rel.startswith("./"):
    rel = rel[2:]
  rel = rel.lstrip("/")
  if rel in ("system/version.py", "openpilot/common/version.py", "openpilot/system/version.py"):
    for cand in (
      "openpilot/common/version.py",
      "system/version.py",
      "openpilot/system/version.py",
    ):
      if (root / cand).is_file():
        return cand
    return "openpilot/common/version.py" if _uses_openpilot_pkg_layout(root) else "system/version.py"
  if rel.startswith("openpilot/"):
    return rel
  if rel.startswith(("system/", "selfdrive/", "sunnypilot/")):
    if _uses_openpilot_pkg_layout(root) or (root / "openpilot" / rel).exists():
      return f"openpilot/{rel}"
    if (root / rel).exists() or not (root / "openpilot").is_dir():
      return rel
    return f"openpilot/{rel}"
  return rel


def cn_path(root: Path, rel: str) -> Path:
  return root / cn_rel(root, rel)


def replace_or_fail_changed(path: Path, replacements: list[tuple[str, str]]) -> bool:
  s = path.read_text(encoding="utf-8")
  orig = s
  for a, b in replacements:
    if a not in s and b not in s:
      raise RuntimeError(f"{path} 未找到预期内容: {a}")
    s = s.replace(a, b)
  return write_if_changed(path, s) if s != orig else False


# ---------------------------------------------------------------------------
# 弹性补丁基元：降低上游在引号、.git 后缀、少量空白/换行上的差异导致的失败率
# ---------------------------------------------------------------------------

def _with_git_url_variants(url: str) -> list[str]:
  """同一逻辑 URL 的常见字符串变体（顺序尝试，去重）。"""
  u = url.strip()
  out: list[str] = [u]
  if u.endswith(".git"):
    out.append(u[:-4])
  else:
    out.append(u + ".git")
  if u.startswith("https://github.com/"):
    out.append("http://" + u[8:])
  if u.startswith("http://github.com/"):
    out.append("https://" + u[7:])
  # 去重保序
  seen: set[str] = set()
  uniq: list[str] = []
  for x in out:
    if x not in seen:
      seen.add(x)
      uniq.append(x)
  return uniq


def replace_first_alias(s: str, old_candidates: list[str], new: str) -> str:
  """
  在 old_candidates 中按顺序找第一个真实出现在 s 里的串，做全局 replace(old, new)；
  若均不存在则原样返回（由调用方决定是否报错）。
  """
  for o in old_candidates:
    if o in s:
      return s.replace(o, new)
  return s


def apply_text_replacement_rows(
  s: str,
  rows: list[tuple[list[str], str]],
  *,
  path: Path,
  require_all: bool = False,
  skip_row_when_new_present: bool = True,
) -> str:
  """
  rows: 每行 = (若干「上游可能出现的旧串」列表, 统一新串)。
  对每行：若任旧串命中则整串替换为 new，并仅使用命中的那个旧串做 replace。
  require_all=True：每一「未完成」行都必须能从 olds 命中一次（幂等时若 new 已在文中则跳过该行）。
  skip_row_when_new_present：若整段 new 已在 s 中，视为该行已打好，跳过后续 olds 匹配。
  """
  missing: list[str] = []
  for i, (olds, new) in enumerate(rows):
    if skip_row_when_new_present and new and new in s:
      continue
    before = s
    s = replace_first_alias(s, olds, new)
    if s == before and require_all:
      missing.append(f"  行 {i+1}: 未匹配任一候选（首项 {olds[0]!r}…）")
  if missing and require_all:
    raise RuntimeError(
      f"{path}: 弹性替换未完全命中（上游可能已改 URL/宏）：\n" + "\n".join(missing)
    )
  return s


def ensure_gitee_in_sunnypilot_remote_tuple_flex(s: str, gitee_key: str) -> str:
  """
  在 version.py 的 sunnypilot_remote 元组中注入 Gitee 识别串；比单点 insert 多几种上游形态。
  """
  if gitee_key in s:
    return s
  # 原实现：def sunnypilot_remote 块
  m = re.search(
    r"(def\s+sunnypilot_remote\(self\)\s*->\s*bool:\s*\n\s*return\s+self\.git_normalized_origin\s+in\s+\()",
    s,
    re.MULTILINE,
  )
  if m:
    close_idx = s.find(")", m.end())
    if close_idx != -1:
      before = s[:close_idx].rstrip()
      after = s[close_idx:]
      if not before.endswith(","):
        before = before + ","
      before = before + f'\n                                          "{gitee_key}"'
      return before + after
  # 备用：元组跨行、return 后换行不同
  m2 = re.search(
    r"(return\s+self\.git_normalized_origin\s+in\s+\()",
    s,
    re.MULTILINE,
  )
  if m2:
    close_idx = s.find(")", m2.end())
    if close_idx != -1:
      before = s[:close_idx].rstrip()
      after = s[close_idx:]
      if not before.endswith(","):
        before = before + ","
      before = before + f'\n                                          "{gitee_key}"'
      return before + after
  raise RuntimeError("version.py: 未找到 sunnypilot_remote 元组块，无法注入 Gitee URL")


_CN_MAIN_REPO_ROUTE_SENTINEL = "CN_MAIN_REPO_ROUTE_V1"
_CN_MICI_HOME_SUFFIX_SENTINEL = "cn_mici_home_repo_suffix"
_CN_INSTALLER_ROUTE_SENTINEL = "cn_main_repo_route_installer"


def _render_cn_main_repo_route_py(gitee: MainRepoSource, codeup: MainRepoSource) -> str:
  return f'''#!/usr/bin/env python3
# {_CN_MAIN_REPO_ROUTE_SENTINEL} — sunnypilot_cn dynamic main-repo routing (private key = author flag)
import os
import subprocess
from pathlib import Path

DATA_CODEUP_KEY = "/data/ssh/id_ed25519_codeup"
CODEUP_HOST = "codeup.aliyun.com"

GITEE_HTTPS_URL = "{gitee.https_url}"
GITEE_SSH_URL = "{gitee.ssh_url}"
CODEUP_HTTPS_URL = "{codeup.https_url}"
CODEUP_SSH_URL = "{codeup.ssh_url}"
GITEE_VERSION_REMOTE_KEY = "{gitee.version_remote_key}"
CODEUP_VERSION_REMOTE_KEY = "{codeup.version_remote_key}"


def is_author_device() -> bool:
  return Path(DATA_CODEUP_KEY).is_file()


def codeup_ssh_identity_file() -> str:
  # C4 等设备 /root 常为只读；SSH 始终用 /data 下持久私钥
  return DATA_CODEUP_KEY


def ensure_codeup_ssh_key() -> None:
  data = Path(DATA_CODEUP_KEY)
  if not data.is_file():
    return
  try:
    data.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(data, 0o600)
  except OSError:
    pass


def git_ssh_command_codeup() -> str | None:
  if not is_author_device():
    return None
  ensure_codeup_ssh_key()
  key = codeup_ssh_identity_file()
  return (
    f"ssh -i {{key}} -o IdentitiesOnly=yes "
    "-o StrictHostKeyChecking=accept-new -o BatchMode=yes"
  )


def main_repo_ui_suffix() -> str:
  return " (Codeup)" if is_author_device() else " (Gitee)"


def resolve_main_repo_urls() -> tuple[str, str]:
  if is_author_device():
    return CODEUP_HTTPS_URL, CODEUP_SSH_URL
  return GITEE_HTTPS_URL, GITEE_SSH_URL


def _git_remote_get_url(cwd: str) -> str:
  try:
    return subprocess.check_output(
      ["git", "remote", "get-url", "origin"],
      cwd=cwd,
      stderr=subprocess.DEVNULL,
      encoding="utf-8",
    ).strip()
  except subprocess.CalledProcessError:
    return ""


def prepare_main_repo_git(cwd: str) -> None:
  https_url, ssh_url = resolve_main_repo_urls()
  cmd = git_ssh_command_codeup()
  if cmd:
    os.environ["GIT_SSH_COMMAND"] = cmd
  else:
    os.environ.pop("GIT_SSH_COMMAND", None)

  want = ssh_url if is_author_device() else https_url
  origin = _git_remote_get_url(cwd)
  if origin != want:
    subprocess.check_call(["git", "remote", "set-url", "origin", want], cwd=cwd)
'''


def inject_updated_check_for_update_order(s: str) -> str:
  """check_for_update：在 ls-remote 之前先 setup_git_options（含 Codeup origin/SSH）。"""
  if "inject_cn_check_for_update_order" in s:
    return s
  old = (
    "  def check_for_update(self) -> None:\n"
    "    cloudlog.info(\"checking for updates\")\n\n"
    "    excluded_branches = ('release2', 'release2-staging')\n\n"
    "    try:\n"
    "      run([\"git\", \"ls-remote\", \"origin\", \"HEAD\"], OVERLAY_MERGED)\n"
  )
  new = (
    "  def check_for_update(self) -> None:\n"
    "    cloudlog.info(\"checking for updates\")\n\n"
    "    excluded_branches = ('release2', 'release2-staging')\n\n"
    "    setup_git_options(OVERLAY_MERGED)  # inject_cn_check_for_update_order\n"
    "    try:\n"
    "      run([\"git\", \"ls-remote\", \"origin\", \"HEAD\"], OVERLAY_MERGED)\n"
  )
  if old not in s:
    if "inject_cn_check_for_update_order" in s:
      return s
    raise RuntimeError("updated.py: 未找到 check_for_update 块，无法调整 setup_git_options 顺序")
  s = s.replace(old, new, 1)
  # 去掉后面重复的 setup_git_options（已在上面调用）
  dup = (
    "    setup_git_options(OVERLAY_MERGED)\n"
    "    output = run([\"git\", \"ls-remote\", \"--heads\"], OVERLAY_MERGED)\n"
  )
  if dup in s:
    s = s.replace(dup, '    output = run(["git", "ls-remote", "--heads"], OVERLAY_MERGED)\n', 1)
  return s


def inject_updated_cn_route_hook(s: str) -> str:
  """在 setup_git_options 开头注入主仓动态路由（Update sunnypilot 检查/下载前）。"""
  if "prepare_main_repo_git(cwd)" in s:
    return s
  hook = (
    "  from openpilot.system.cn_main_repo_route import prepare_main_repo_git\n"
    "  prepare_main_repo_git(cwd)\n\n"
  )
  anchors = [
    "def setup_git_options(cwd: str) -> None:\n",
    "def setup_git_options(cwd: str):\n",
  ]
  for a in anchors:
    if a in s:
      return s.replace(a, a + hook, 1)
  raise RuntimeError("updated.py: 未找到 setup_git_options，无法注入 prepare_main_repo_git")


def inject_updated_insteadof_block_flex(s: str) -> str:
  """在 updated.py 中注入 ensure_url_insteadof；兼容上游微调 for option 循环。"""
  if "ensure_url_insteadof(" in s:
    return s
  block = f"""

  def ensure_url_insteadof(url_key: str, instead_of: str) -> None:
    \"\"\"Idempotently add a url.<base>.insteadof rewrite rule to the repo config.\"\"\"
    try:
      existing = run([\"git\", \"config\", \"--get-all\", url_key], cwd).splitlines()
    except subprocess.CalledProcessError:
      existing = []
    if instead_of in existing:
      return
    run([\"git\", \"config\", \"--add\", url_key, instead_of], cwd)

  # 国内化：将常见 GitHub 源自动重写到 Gitee 镜像（同时覆盖 https 与 ssh）。
{_render_gitee_insteadof_ensure_lines()}
"""
  anchors = [
    "for option, value in git_cfg:\n    run([\"git\", \"config\", option, value], cwd)\n",
    "for option, value in git_cfg:\n        run([\"git\", \"config\", option, value], cwd)\n",
    "for option, value in git_cfg:\n    run(['git', 'config', option, value], cwd)\n",
  ]
  for a in anchors:
    if a in s:
      return s.replace(a, a + block)
  m = re.search(
    r"(for\s+option,\s*value\s+in\s+git_cfg:\s*\n\s*run\(\[[^\]]+\],\s*cwd\)\s*\n)",
    s,
    re.MULTILINE,
  )
  if m:
    return s.replace(m.group(1), m.group(1) + block)
  raise RuntimeError("updated.py 结构变化，无法自动注入 insteadof 规则（未匹配 git_cfg 循环）")


def parse_recorded_upstream_sha(commit_body: str, branch: str) -> str | None:
  """
  约定：在提交信息中记录本次同步对应的上游分支 HEAD，例如：
    upstream-staging: <sha>
  """
  m = re.search(rf"(?m)^upstream-{re.escape(branch)}:\s*([0-9a-f]{{7,40}})\s*$", commit_body or "")
  return m.group(1) if m else None


_MASTER_COMMIT_RE = re.compile(r"(?im)master commit:?\s*([0-9a-f]{7,40})")


def _strip_github_pr_suffix(subject: str) -> str:
  return re.sub(r"\s*\(#\d+\)\s*$", "", (subject or "").strip()).strip()


def _git_commit_exists(root: Path, env: dict[str, str], spec: str) -> bool:
  try:
    run(["git", "rev-parse", "--verify", f"{spec}^{{commit}}"], str(root), env=env)
    return True
  except RuntimeError:
    return False


def _ensure_git_commit(root: Path, env: dict[str, str], spec: str) -> bool:
  if _git_commit_exists(root, env, spec):
    return True
  try:
    run(["git", "fetch", "--depth", "1", "upstream", spec], str(root), env=env)
  except RuntimeError:
    try:
      run(["git", "fetch", "upstream", spec], str(root), env=env)
    except RuntimeError:
      return False
  return _git_commit_exists(root, env, spec)


def subject_from_upstream_packaging(root: Path, env: dict[str, str], upstream_sha: str) -> str | None:
  """上游 staging 包装提交正文里的 master commit 主题（去掉 (#PR)）。"""
  if not _ensure_git_commit(root, env, upstream_sha):
    return None
  try:
    body = run(["git", "log", "-1", "--format=%B", upstream_sha], str(root), env=env)
  except RuntimeError:
    return None
  m = _MASTER_COMMIT_RE.search(body or "")
  if m and _ensure_git_commit(root, env, m.group(1)):
    try:
      subj = run(["git", "log", "-1", "--format=%s", m.group(1)], str(root), env=env).strip()
    except RuntimeError:
      subj = ""
    if subj:
      return _strip_github_pr_suffix(subj)
  first = next((ln.strip() for ln in (body or "").splitlines() if ln.strip()), "")
  return _strip_github_pr_suffix(first) if first else None


_ARCHIVE_TAG_KIND_DEFAULT = "Beta"
_ARCHIVE_TAG_KIND_STABLE = "stable"
_ARCHIVE_TAG_KIND_BETA = "Beta"


def _cn_archive_tz() -> datetime.tzinfo:
  try:
    return ZoneInfo("Asia/Shanghai")
  except Exception:
    return datetime.timezone(datetime.timedelta(hours=8))


def cn_archive_tag_stamp(now: datetime.datetime | None = None) -> str:
  """北京时间 YYYYMMDD-HHMMSS，例如 20260906-223022。"""
  dt = now
  if dt is None:
    dt = datetime.datetime.now(_cn_archive_tz())
  elif dt.tzinfo is None:
    dt = dt.replace(tzinfo=_cn_archive_tz())
  else:
    dt = dt.astimezone(_cn_archive_tz())
  return dt.strftime("%Y%m%d-%H%M%S")


def normalize_archive_tag_kind(raw: str | None) -> str:
  """stable / Beta。空、default、cn 均视为 Beta。"""
  s = (raw or "").strip()
  if s.lower() == "stable":
    return _ARCHIVE_TAG_KIND_STABLE
  return _ARCHIVE_TAG_KIND_BETA


def archive_tag_kind_from_env() -> str:
  return normalize_archive_tag_kind(
    os.environ.get("SYNC_ARCHIVE_TAG_KIND") or os.environ.get("ARCHIVE_TAG_KIND")
  )


def cn_archive_tag_base(
  kind: str,
  upstream_short: str,
  *,
  rollback: bool = False,
  now: datetime.datetime | None = None,
) -> str:
  """
  Beta / stable: {前缀}-{sha}-{北京 YYYYMMDD-HHMMSS}
  未指定或 default 一律 Beta（不再使用 cn/staging-）。
  """
  ts = cn_archive_tag_stamp(now)
  k = normalize_archive_tag_kind(kind)
  prefix = "stable" if k == _ARCHIVE_TAG_KIND_STABLE else "Beta"
  return f"{prefix}-{upstream_short}-{ts}"


def _unique_cn_archive_tag_name(root: Path, env: dict[str, str], base: str) -> str:
  name = base
  n = 0
  while True:
    try:
      run(["git", "rev-parse", "--verify", f"refs/tags/{name}"], str(root), env=env)
    except RuntimeError:
      return name
    n += 1
    name = f"{base}-{n}"


def _archive_tag_push_targets_from_state(state: dict[str, object] | None) -> set[str]:
  targets: set[str] = set()
  if not state:
    return set(enabled_push_target_ids())
  if state.get("pushed_gitee"):
    targets.add("gitee")
  if state.get("pushed_codeup"):
    targets.add("codeup")
  pr = state.get("push_results") if isinstance(state.get("push_results"), dict) else {}
  if isinstance(pr, dict):
    for tid in ("gitee", "codeup"):
      entry = pr.get(tid)
      if isinstance(entry, dict) and entry.get("overall") in ("ok", "partial"):
        targets.add(tid)
  return targets


def create_and_push_cn_archive_tag(
  root: Path,
  env: dict[str, str],
  *,
  branch: str | None = None,
  targets: set[str] | None = None,
  tag_kind: str | None = None,
) -> str | None:
  """
  在当前 HEAD 打一枚附注 tag，并推到已成功的远端。
  默认 ``Beta-{短SHA}-{北京时间到秒}``；``tag_kind=stable`` 时为 ``stable-…``。
  说明优先用上游 staging 的 master commit 主题。
  """
  branch = (branch or (SYNC_BRANCHES[0] if SYNC_BRANCHES else "staging")).strip()
  want = targets if targets is not None else set(enabled_push_target_ids())
  if not want:
    print("[skip] archive-tag: 无推送目标")
    return None
  try:
    run(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], str(root), env=env)
    run(["git", "checkout", branch], str(root), env=env)
  except RuntimeError as e:
    log("warn", f"archive-tag: 无法检出 {branch}: {e}")
    return None
  try:
    head = run(["git", "rev-parse", "--verify", "HEAD"], str(root), env=env).strip()
    body = run(["git", "log", "-1", "--format=%B"], str(root), env=env)
  except RuntimeError as e:
    log("warn", f"archive-tag: 无法读取 HEAD: {e}")
    return None
  recorded = parse_recorded_upstream_sha(body, branch)
  upstream_short = (canonical_commit_sha(recorded, root, env) or recorded or "unknown")[:7]
  subject = None
  if recorded:
    subject = subject_from_upstream_packaging(root, env, recorded)
  if not subject:
    subject = f"国内化快照：upstream={upstream_short} HEAD={head[:12]}"
  kind = normalize_archive_tag_kind(tag_kind if tag_kind is not None else archive_tag_kind_from_env())
  tag = _unique_cn_archive_tag_name(root, env, cn_archive_tag_base(kind, upstream_short))
  msg = subject if "upstream=" in subject else f"{subject}\n\nupstream={upstream_short}"
  try:
    run(
      [
        "git",
        "-c", "user.name=sunnypilot-cn-bot",
        "-c", "user.email=sunnypilot-cn-bot@local",
        "tag", "-a", tag, head, "-m", msg,
      ],
      str(root),
      env=env,
    )
  except RuntimeError as e:
    log("warn", f"archive-tag: 本地打 tag 失败: {e}")
    return None
  log("tag", f"{tag} → {head[:12]}  {subject}")
  push_env = dict(env)
  push_env["GIT_LFS_SKIP_PUSH"] = "1"
  failed = False
  if "gitee" in want:
    ge = dict(push_env)
    ge["GIT_SSH_COMMAND"] = _gitee_git_ssh_env()["GIT_SSH_COMMAND"]
    try:
      run(["git", "push", "origin", f"refs/tags/{tag}"], str(root), env=ge)
      log("tag", f"已推 Gitee {tag}")
    except RuntimeError as e:
      log("warn", f"archive-tag: 推 Gitee 失败: {e}")
      failed = True
  if "codeup" in want:
    try:
      run(["git", "push", "aliyun", f"refs/tags/{tag}"], str(root), env=aliyun_git_push_env(push_env))
      log("tag", f"已推 Codeup {tag}")
    except RuntimeError as e:
      log("warn", f"archive-tag: 推 Codeup 失败: {e}")
      failed = True
  if failed:
    log("warn", "archive-tag: 至少一端推 tag 失败（分支推送不受影响）")
  return tag


def maybe_archive_cn_tag_after_ci_push(root: Path, env: dict[str, str], state: dict[str, object] | None) -> None:
  """CI / --action all：有成功推送时默认打 tag。本地 SP_SYNC_SOURCE=local 不打（由 local 菜单处理）。"""
  if _env_truthy("SYNC_SKIP_ARCHIVE_TAG"):
    print("[skip] archive-tag: SYNC_SKIP_ARCHIVE_TAG=1")
    return
  if (os.environ.get("SP_SYNC_SOURCE") or "").strip().lower() == "local":
    return
  if os.environ.get("GITHUB_ACTIONS") != "true" and not _env_truthy("SYNC_ARCHIVE_TAG"):
    return
  if not state or not state.get("to_push"):
    print("[skip] archive-tag: 本轮无待推送分支")
    return
  if not state.get("pushed"):
    print("[skip] archive-tag: 本轮无成功推送")
    return
  want = _archive_tag_push_targets_from_state(state)
  create_and_push_cn_archive_tag(root, env, targets=want, tag_kind=archive_tag_kind_from_env())


def fetch_mirror_recorded_upstream(
  root: Path,
  env: dict[str, str],
  remote: str,
  branch: str,
) -> tuple[str | None, str | None]:
  """
  从镜像远端最新提交读取 upstream-<branch> 记录。
  返回 (recorded_sha, remote_head_sha)；fetch 失败则 (None, None)。
  """
  try:
    fetch_env = env
    if remote == "aliyun":
      if aliyun_push_via_https(env):
        fetch_env = aliyun_git_https_push_env(env)
      elif aliyun_ssh_key_path().exists():
        fetch_env = aliyun_git_push_env(env)
    run(["git", "fetch", "--depth=1", remote, branch], str(root), env=fetch_env)
    head = run(["git", "rev-parse", "FETCH_HEAD"], str(root), env=env).strip().lower()
    body = run(["git", "log", "-1", "--format=%B", "FETCH_HEAD"], str(root), env=env)
    return parse_recorded_upstream_sha(body, branch), head
  except Exception:
    return None, None


def resolve_recorded_upstream_sha(
  root: Path,
  env: dict[str, str],
  branch: str,
) -> tuple[str | None, str | None, str, str | None]:
  """
  解析「上次已同步的上游 SHA」。
  优先 Gitee(origin)；无记录时回退 Codeup(aliyun)——半成功（仅 Codeup 推上）时避免误判上游有更新。
  返回 (recorded_sha, recorded_canon, source_label, tip_head_for_note)。
  """
  recorded_sha, gitee_head = fetch_mirror_recorded_upstream(root, env, "origin", branch)
  recorded_canon = canonical_commit_sha(recorded_sha, root, env)
  if recorded_canon is not None:
    return recorded_sha, recorded_canon, "Gitee", gitee_head

  # 确保 aliyun remote 存在（即使本次未启用 Codeup 推送，也可读已成功推送的记录）
  try:
    remotes = run(["git", "remote"], str(root), env=env).splitlines()
    codeup_url = (env.get("ALIYUN_REPO_SSH") or main_repo_source_codeup().ssh_url).strip()
    if "aliyun" not in remotes:
      run(["git", "remote", "add", "aliyun", codeup_url], str(root), env=env)
    else:
      run(["git", "remote", "set-url", "aliyun", codeup_url], str(root), env=env)
  except Exception:
    pass

  codeup_sha, codeup_head = fetch_mirror_recorded_upstream(root, env, "aliyun", branch)
  codeup_canon = canonical_commit_sha(codeup_sha, root, env)
  if codeup_canon is not None:
    print(
      f"[info] {branch}: Gitee 无 upstream-{branch} 记录，改用 Codeup 记录 "
      f"({short_sha(codeup_canon) or codeup_canon[:EMAIL_SHA_LEN]})"
    )
    return codeup_sha, codeup_canon, "Codeup", codeup_head or gitee_head

  # 两边都没有 upstream-*：保留 Gitee tip 供邮件展示（与旧行为一致）
  return recorded_sha, None, "Gitee", gitee_head or codeup_head


def canonical_commit_sha(sha: str | None, root: Path, env: dict[str, str]) -> str | None:
  """
  将 7～40 位十六进制提交 ID 规范为完整小写 SHA，避免「短哈希 vs 全哈希」字符串不相等导致误判。
  不可解析时返回 None（调用方按「无记录」保守处理）。
  """
  if not sha:
    return None
  s = sha.strip().lower()
  if not re.fullmatch(r"[0-9a-f]{7,40}", s):
    return None
  try:
    # peel to commit object（annotated tag 等）
    spec = f"{s}^{{commit}}"
    return run(["git", "rev-parse", "--verify", spec], str(root), env=env).strip().lower()
  except Exception:
    return None


EMAIL_SHA_LEN = 7  # 邮件展示统一短哈希长度（与「分支核对」一致）


def short_sha(sha: str | None) -> str | None:
  if not sha:
    return None
  s = sha.strip().lower()
  if not re.fullmatch(r"[0-9a-f]{7,40}", s):
    return None
  return s[:EMAIL_SHA_LEN]


def shorten_hashes_in_text(text: str) -> str:
  """
  邮件展示层：将任何 8～40 位十六进制串缩短为 EMAIL_SHA_LEN 位，避免邮件里长短不一。
  后台对比与逻辑仍使用完整 SHA（调用方应仅在展示前使用本函数）。
  """
  if not text:
    return text

  def _repl(m: re.Match[str]) -> str:
    return m.group(1)[:EMAIL_SHA_LEN]

  return re.sub(r"(?i)\b([0-9a-f]{8,40})\b", _repl, text)


def format_email_commit_line(line: str) -> str:
  """git log 行首哈希与正文内嵌哈希统一为 EMAIL_SHA_LEN 位。"""
  line = line.strip()
  m = re.match(r"^([0-9a-f]{7,40})(\s+)(.*)$", line, re.IGNORECASE)
  if not m:
    return shorten_hashes_in_text(line)
  h = short_sha(m.group(1)) or m.group(1)[:EMAIL_SHA_LEN]
  return f"{h}{m.group(2)}{shorten_hashes_in_text(m.group(3))}"


def _git_is_shallow_repo(root: Path, env: dict[str, str]) -> bool:
  try:
    return run(["git", "rev-parse", "--is-shallow-repository"], str(root), env=env).strip() == "true"
  except Exception:
    return False


def ensure_git_objects_for_range(root: Path, env: dict[str, str], branch: str, base: str, head: str) -> None:
  """
  CI 常见浅克隆导致旧提交不可见；加深或解除 shallow，便于 git log base..head 枚举。
  """
  base = base.strip().lower()
  head = head.strip().lower()
  for _ in range(14):
    try:
      run(["git", "merge-base", base, head], str(root), env=env)
      run(["git", "rev-parse", "--verify", f"{base}^{{commit}}"], str(root), env=env)
      return
    except Exception:
      pass
    if _git_is_shallow_repo(root, env):
      try:
        run(["git", "fetch", "upstream", "--unshallow"], str(root), env=env)
        continue
      except Exception:
        pass
    try:
      run(["git", "fetch", "upstream", branch, "--deepen=500"], str(root), env=env)
    except Exception:
      break


def _email_log_tz() -> tuple[datetime.tzinfo, str]:
  tz_name = (os.environ.get("SYNC_EMAIL_LOG_TZ") or "UTC").strip()
  try:
    tz = ZoneInfo(tz_name)
  except Exception:
    tz_name = "UTC"
    tz = ZoneInfo(tz_name)
  return tz, tz_name


def _email_log_day_start_iso() -> tuple[str, str]:
  """
  邮件里「当天」的自然日起点（用于 git --since），按所选时区当日 0:00。

  默认 UTC（更接近 GitHub/CI 常见“标准时间”视角）。
  环境变量 SYNC_EMAIL_LOG_TZ（IANA）可覆盖，例如 UTC、Asia/Shanghai。
  """
  tz, tz_name = _email_log_tz()
  now = datetime.datetime.now(tz)
  start = now.replace(hour=0, minute=0, second=0, microsecond=0)
  return start.isoformat(), tz_name


def _email_lookback_hours() -> int:
  """Force 邮件回看小时数；默认 16。环境变量 SYNC_EMAIL_LOG_HOURS（1～168）。"""
  raw = (os.environ.get("SYNC_EMAIL_LOG_HOURS") or "16").strip()
  try:
    return max(1, min(int(raw), 168))
  except Exception:
    return 16


def _email_log_since_hours_iso(hours: int) -> tuple[str, str]:
  tz, tz_name = _email_log_tz()
  start = datetime.datetime.now(tz) - datetime.timedelta(hours=hours)
  return start.isoformat(), tz_name


def _email_log_max_shown() -> int:
  """邮件中最多列出几条；默认 6。环境变量 SYNC_EMAIL_LOG_MAX（1～50）。"""
  raw = (os.environ.get("SYNC_EMAIL_LOG_MAX") or "6").strip()
  try:
    return max(1, min(int(raw), 50))
  except Exception:
    return 6


def _email_staging_master_window_hours() -> int:
  """锚点提交时刻起向下回溯的小时数（锚点距现在仍在该窗口内时用）。"""
  raw = (os.environ.get("SYNC_EMAIL_STAGING_MASTER_HOURS") or "6").strip()
  try:
    return max(1, min(int(raw), 72))
  except ValueError:
    return 6


def _email_staging_master_max_old() -> int:
  """锚点距现在已超过窗口时，最多向下列出的 master 提交条数（含锚点）。"""
  raw = (os.environ.get("SYNC_EMAIL_STAGING_MASTER_MAX") or "3").strip()
  try:
    return max(1, min(int(raw), 20))
  except ValueError:
    return 3


def _mail_channel_from_state(state: dict[str, object] | None = None) -> str:
  raw = ""
  if state:
    raw = str(state.get("archive_tag_kind") or "")
  if not raw.strip():
    raw = os.environ.get("SYNC_ARCHIVE_TAG_KIND") or os.environ.get("ARCHIVE_TAG_KIND") or ""
  return normalize_archive_tag_kind(raw)


def _mail_channel_slug(state: dict[str, object] | None = None) -> str:
  return "stable" if _mail_channel_from_state(state) == _ARCHIVE_TAG_KIND_STABLE else "beta"


def _mail_channel_label(state: dict[str, object] | None = None) -> str:
  return "Stable" if _mail_channel_slug(state) == "stable" else "Beta"


def _parse_git_iso_dt(raw: str) -> datetime.datetime | None:
  s = (raw or "").strip()
  if not s:
    return None
  try:
    dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
  except ValueError:
    return None
  if dt.tzinfo is None:
    return dt.replace(tzinfo=datetime.timezone.utc)
  return dt.astimezone(datetime.timezone.utc)


def _git_commit_committer_dt(root: Path, env: dict[str, str], sha: str) -> datetime.datetime | None:
  try:
    raw = run(["git", "log", "-1", "--format=%cI", sha], str(root), env=env).strip()
  except Exception:
    return None
  return _parse_git_iso_dt(raw)


def _email_master_body_max_lines() -> int:
  """
  master 提交除 subject 外，额外附带正文（%b）的行数上限。
  0 表示仅 subject（单行）；默认 5。环境变量 SYNC_EMAIL_MASTER_BODY_MAX_LINES（0～30）。
  """
  raw = (os.environ.get("SYNC_EMAIL_MASTER_BODY_MAX_LINES") or "5").strip()
  try:
    return max(0, min(int(raw), 30))
  except Exception:
    return 5


def parse_master_commit_from_message(body: str) -> str | None:
  """从 staging 包装提交正文中解析 `master commit: <sha>`。"""
  m = re.search(r"(?im)^master\s+commit:\s*([0-9a-f]{7,40})\b", body or "")
  return m.group(1).strip().lower() if m else None


def ensure_upstream_master_for_staging_email(root: Path, env: dict[str, str]) -> None:
  """staging 邮件展开 master 指针前，尽量保证 upstream/master 提交对象可达。"""
  try:
    run(["git", "rev-parse", "--verify", "upstream/master^{commit}"], str(root), env=env)
    return
  except Exception:
    pass
  try:
    run(["git", "fetch", "upstream", "master"], str(root), env=env)
  except Exception:
    pass


def _staging_commit_display_block(root: Path, env: dict[str, str], staging_full_sha: str) -> str:
  """
  staging 摘要一行；若有 master 指针则下一行起为 master 真实改动：
  先 subject（%s），可选再附若干行正文（%b），不含列表前缀「  • 」。
  """
  staging_full_sha = staging_full_sha.strip().lower()
  try:
    short = short_sha(staging_full_sha) or staging_full_sha[:EMAIL_SHA_LEN]
    subj = shorten_hashes_in_text(run(["git", "log", "-1", "--format=%s", staging_full_sha], str(root), env=env).strip())
  except Exception:
    return f"{staging_full_sha[:EMAIL_SHA_LEN]} （无法读取 staging 提交）"
  line_a = f"{short} {subj}"
  body = ""
  try:
    body = run(["git", "log", "-1", "--format=%B", staging_full_sha], str(root), env=env)
  except Exception:
    pass
  raw_m = parse_master_commit_from_message(body)
  if not raw_m:
    return line_a
  master_full = canonical_commit_sha(raw_m, root, env)
  if not master_full:
    ensure_upstream_master_for_staging_email(root, env)
    master_full = canonical_commit_sha(raw_m, root, env)
  if not master_full:
    return f"{line_a} （master commit 对象不可解析）"
  try:
    ms = short_sha(master_full) or master_full[:EMAIL_SHA_LEN]
    msub = shorten_hashes_in_text(run(["git", "log", "-1", "--format=%s", master_full], str(root), env=env).strip())
  except Exception:
    return f"{line_a} （无法读取 master 提交）"
  parts: list[str] = [line_a, f"    → master {ms} {msub}"]
  max_body = _email_master_body_max_lines()
  if max_body > 0:
    mb_raw = ""
    try:
      mb_raw = run(["git", "log", "-1", "--format=%b", master_full], str(root), env=env)
    except Exception:
      mb_raw = ""
    taken = 0
    for ln in mb_raw.splitlines():
      s = ln.strip()
      if not s:
        continue
      if s == msub.strip():
        continue
      s = shorten_hashes_in_text(s)
      if len(s) > 220:
        s = s[:217] + "..."
      parts.append(f"       {s}")
      taken += 1
      if taken >= max_body:
        break
  return "\n".join(parts)


def _bullet_prefix_entries(entries: list[str]) -> str:
  """每条 entry 可为单行或多行（staging + master）。"""
  lines_out: list[str] = []
  for e in entries:
    e = e.strip("\n")
    if "\n" in e:
      first, rest = e.split("\n", 1)
      lines_out.append(f"  • {first}\n{rest}")
    else:
      lines_out.append(f"  • {e}")
  return "\n".join(lines_out)


def _git_log_shas_since(
  root: Path,
  env: dict[str, str],
  rev: str,
  since_iso: str,
  max_n: int,
) -> list[str]:
  try:
    out = run(
      [
        "git", "log",
        "--since", since_iso,
        "-n", str(max_n),
        "--format=%H",
        rev,
      ],
      str(root),
      env=env,
    )
  except Exception:
    return []
  return [ln.strip().lower() for ln in out.splitlines() if ln.strip()]


def _plain_commit_email_block(root: Path, env: dict[str, str], sha: str) -> str:
  """master 等普通提交：subject + 若干行正文。"""
  sha = sha.strip().lower()
  try:
    short = short_sha(sha) or sha[:EMAIL_SHA_LEN]
    subj = shorten_hashes_in_text(
      run(["git", "log", "-1", "--format=%s", sha], str(root), env=env).strip()
    )
  except Exception:
    return f"{sha[:EMAIL_SHA_LEN]} （无法读取提交）"
  parts: list[str] = [f"{short} {subj}"]
  max_body = _email_master_body_max_lines()
  if max_body <= 0:
    return parts[0]
  mb_raw = ""
  try:
    mb_raw = run(["git", "log", "-1", "--format=%b", sha], str(root), env=env)
  except Exception:
    mb_raw = ""
  taken = 0
  for ln in mb_raw.splitlines():
    s = ln.strip()
    if not s or s == subj.strip():
      continue
    s = shorten_hashes_in_text(s)
    if len(s) > 220:
      s = s[:217] + "..."
    parts.append(f"       {s}")
    taken += 1
    if taken >= max_body:
      break
  return "\n".join(parts)


def _collect_staging_synced_master_commits(
  root: Path,
  env: dict[str, str],
  staging_head_sha: str,
) -> str:
  """
  邮件用：时间锚点是 staging 包装提交（如 40d6afd 进 staging 的时刻），不是 master 那笔的时间。
  列出从包装说明里的 master commit 起向更旧（不含尚未进入 staging 的 master tip）。
  - staging 提交时间距现在 ≤ 窗口小时：以该 staging 时刻为基准，窗口内 master 全部列出。
  - 超过窗口：从 master 指针起最多向下 N 条（含指针自身）。
  """
  hours = _email_staging_master_window_hours()
  max_old = _email_staging_master_max_old()
  walk_cap = 80
  sha = (staging_head_sha or "").strip().lower()
  if len(sha) < 7:
    return "（无有效 staging HEAD，无法列出已同步的 master 提交。）"

  ensure_upstream_master_for_staging_email(root, env)
  try:
    run(["git", "rev-parse", "--verify", f"{sha}^{{commit}}"], str(root), env=env)
  except Exception:
    return f"（无法读取 staging HEAD {short_sha(sha) or sha[:EMAIL_SHA_LEN]}。）"

  staging_dt = _git_commit_committer_dt(root, env, sha)
  if staging_dt is None:
    return f"（无法读取 staging 锚点 {short_sha(sha) or sha[:EMAIL_SHA_LEN]} 的提交时间。）"

  try:
    body = run(["git", "log", "-1", "--format=%B", sha], str(root), env=env)
  except Exception:
    return "（无法读取 staging 包装提交说明。）"

  master_raw = parse_master_commit_from_message(body)
  if not master_raw:
    return "（staging 包装提交无 master commit 指针，无法列出已同步的 master 说明。）"

  try:
    run(["git", "fetch", "upstream", "master", "--deepen=40"], str(root), env=env)
  except Exception:
    pass

  master_full = canonical_commit_sha(master_raw, root, env)
  if not master_full:
    try:
      run(["git", "fetch", "upstream", "master"], str(root), env=env)
    except Exception:
      pass
    master_full = canonical_commit_sha(master_raw, root, env)
  if not master_full:
    return f"（无法解析 staging 钉住的 master commit {master_raw}。）"

  now = datetime.datetime.now(datetime.timezone.utc)
  age = now - staging_dt
  window = datetime.timedelta(hours=hours)
  shas: list[str] = []
  if age <= window:
    cutoff = staging_dt - window
    try:
      raw = run(
        [
          "git",
          "log",
          "--first-parent",
          f"-n{walk_cap}",
          "--format=%H %cI",
          master_full,
        ],
        str(root),
        env=env,
      )
    except Exception:
      return "（无法列出已同步 master 向下的提交。）"
    for i, line in enumerate(raw.splitlines()):
      parts = line.strip().split(None, 1)
      if len(parts) != 2:
        continue
      h, iso = parts[0].strip().lower(), parts[1].strip()
      dt = _parse_git_iso_dt(iso)
      if dt is None:
        continue
      # 指针自身始终列出；更旧的用 staging 时刻往下的时间窗裁剪（staging 合并晚于 master）
      if i > 0 and dt < cutoff:
        break
      shas.append(h)
  else:
    try:
      raw = run(
        [
          "git",
          "log",
          "--first-parent",
          f"-n{max_old}",
          "--format=%H",
          master_full,
        ],
        str(root),
        env=env,
      )
    except Exception:
      return "（无法列出已同步 master 向下的提交。）"
    shas = [ln.strip().lower() for ln in raw.splitlines() if len(ln.strip()) >= 7]

  if not shas:
    return "（无提交可列出。）"
  entries = [_plain_commit_email_block(root, env, h) for h in shas]
  return _bullet_prefix_entries(entries)


def collect_upstream_commits_for_email(
  root: Path,
  env: dict[str, str],
  branch: str,
  base_sha: str | None,
  head_sha: str,
  *,
  reason_tag: str,
  max_commits: int | None = None,
) -> str:
  """
  所有通知邮件共用：时间看 staging 包装提交，列表从其中 master commit 指针向更旧走。
  branch / base_sha / reason_tag / max_commits 保留兼容调用方。
  """
  _ = (branch, base_sha, reason_tag, max_commits)
  try:
    return _collect_staging_synced_master_commits(root, env, head_sha)
  except Exception:
    return "（未能列出上游提交。）"


def should_update_submodules(recorded_upstream_sha: str | None, upstream_sha: str, root: Path, env: dict[str, str]) -> bool:
  """
  根据 upstream 两个 commit 之间的 diff 判断是否需要更新子模块。
  - 若 `.gitmodules` 有变化：需要更新（新增/删除/URL 变更等）
  - 若有 mode 160000 的条目变化：需要更新（子模块指针变更）
  - 若拿不到 recorded_upstream_sha：保守起见更新一次
  """
  if not recorded_upstream_sha:
    return True

  try:
    names = run(["git", "diff", "--name-only", f"{recorded_upstream_sha}..{upstream_sha}", "--", ".gitmodules"], str(root), env=env)
    if names.strip():
      return True
  except Exception:
    return True

  try:
    raw = run(["git", "diff", "--raw", f"{recorded_upstream_sha}..{upstream_sha}"], str(root), env=env)
    # 子模块在 raw diff 中表现为 mode 160000 的 gitlink
    return (" 160000 " in raw)
  except Exception:
    return True


@dataclass
class PatchResult:
  name: str
  changed_files: list[str] = field(default_factory=list)
  changed: bool = False


def _track_change(res: PatchResult, path: Path, changed: bool) -> None:
  if changed:
    res.changed = True
    res.changed_files.append(str(path))


def _installer_cn_route_block(gitee: MainRepoSource, codeup: MainRepoSource) -> str:
  return f'''
#include <unistd.h>

// cn_main_repo_route_installer — author: /data/ssh/id_ed25519_codeup → Codeup, else Gitee public
static const char *CN_DATA_CODEUP_KEY = "/data/ssh/id_ed25519_codeup";
static const char *CN_GITEE_HTTPS = "{gitee.https_url}";
static const char *CN_GITEE_SSH = "{gitee.ssh_url}";
static const char *CN_CODEUP_SSH = "{codeup.ssh_url}";

static bool cn_is_author_device() {{
  return access(CN_DATA_CODEUP_KEY, F_OK) == 0;
}}

static std::string cn_main_repo_fetch_url() {{
  // Codeup 仓库需要认证；作者设备必须用 /data 私钥对应的 SSH URL。
  return cn_is_author_device() ? CN_CODEUP_SSH : CN_GITEE_HTTPS;
}}

static std::string cn_main_repo_ssh_url() {{
  return cn_is_author_device() ? CN_CODEUP_SSH : CN_GITEE_SSH;
}}

static void cn_prepare_installer_git_env() {{
  if (!cn_is_author_device()) {{
    unsetenv("GIT_SSH_COMMAND");
    return;
  }}
  setenv("GIT_SSH_COMMAND",
         "ssh -i /data/ssh/id_ed25519_codeup -o IdentitiesOnly=yes "
         "-o StrictHostKeyChecking=accept-new -o BatchMode=yes",
         1);
}}

'''


_RAYLIB_INCLUDE_ANCHORS = (
  '#include "third_party/raylib/include/raylib.h"\n',
  '#include "raylib.h"\n',
)


def _installer_cn_route_inject_anchor(s: str) -> str | None:
  for anchor in _RAYLIB_INCLUDE_ANCHORS:
    if anchor in s:
      return anchor
  for hw_anchor in (
    '#include "system/hardware/hw.h"\n',
    '#include "common/hardware/hw.h"\n',
  ):
    if hw_anchor in s:
      return hw_anchor
  return None


def _inject_installer_cn_route(s: str, gitee: MainRepoSource, codeup: MainRepoSource) -> str:
  """刷机安装器：有私钥走 Codeup，无钥走 Gitee 公开。"""
  s2 = apply_text_replacement_rows(
    s,
    [
      (
        _with_git_url_variants("https://github.com/commaai/openpilot.git"),
        gitee.https_url,
      ),
    ],
    path=Path("installer.cc"),
    require_all=False,
  )
  if _CN_INSTALLER_ROUTE_SENTINEL not in s2:
    anchor = _installer_cn_route_inject_anchor(s2)
    if not anchor:
      raise RuntimeError("installer.cc: 未找到 raylib/hw.h include 锚点，无法注入 cn_main_repo_route_installer")
    s2 = s2.replace(anchor, anchor + _installer_cn_route_block(gitee, codeup), 1)

  # 兼容 get_str("url" "?padding") 与 get_str("url?padding") 两种上游写法。
  git_url_line = re.compile(
    r"const\s+std::string\s+GIT_URL\s*=\s*get_str\([^;]*\);\s*\n",
    re.MULTILINE,
  )
  if git_url_line.search(s2):
    s2 = git_url_line.sub("", s2, count=1)

  if "#define GIT_SSH_URL" in s2:
    s2 = re.sub(r'#define GIT_SSH_URL[^\n]+\n', "", s2, count=1)

  # 迁移已生成的旧版动态路由。
  s2 = s2.replace("cn_main_repo_https_url()", "cn_main_repo_fetch_url()")
  s2 = re.sub(r'^static const char \*CN_CODEUP_HTTPS = "[^"]+";\n', "", s2, count=1, flags=re.MULTILINE)
  s2 = s2.replace(
    "static std::string cn_main_repo_fetch_url() {\n"
    "  return cn_is_author_device() ? CN_CODEUP_HTTPS : CN_GITEE_HTTPS;\n"
    "}",
    "static std::string cn_main_repo_fetch_url() {\n"
    "  // Codeup 仓库需要认证；作者设备必须用 /data 私钥对应的 SSH URL。\n"
    "  return cn_is_author_device() ? CN_CODEUP_SSH : CN_GITEE_HTTPS;\n"
    "}",
  )

  if "int freshClone() {\n  cn_prepare_installer_git_env();" not in s2:
    s2 = s2.replace(
      "int freshClone() {\n",
      "int freshClone() {\n  cn_prepare_installer_git_env();\n",
      1,
    )
  if "std::string git_url = cn_main_repo_fetch_url()" not in s2:
    s2 = s2.replace(
      '  std::string cmd = util::string_format("git clone --progress %s -b %s --depth=1 --recurse-submodules %s 2>&1",\n'
      "                                        GIT_URL.c_str(), migrated_branch.c_str(), TMP_INSTALL_PATH);",
      '  std::string git_url = cn_main_repo_fetch_url();\n'
      '  std::string cmd = util::string_format("git clone --progress %s -b %s --depth=1 --recurse-submodules %s 2>&1",\n'
      "                                        git_url.c_str(), migrated_branch.c_str(), TMP_INSTALL_PATH);",
      1,
    )
  if "int cachedFetch(const std::string &cache) {\n  cn_prepare_installer_git_env();" not in s2:
    s2 = s2.replace(
      "int cachedFetch(const std::string &cache) {\n",
      "int cachedFetch(const std::string &cache) {\n  cn_prepare_installer_git_env();\n",
      1,
    )
  s2 = s2.replace(
    'run(util::string_format("cd %s && git remote set-url origin %s", TMP_INSTALL_PATH, GIT_URL.c_str()).c_str());',
    'run(util::string_format("cd %s && git remote set-url origin %s", TMP_INSTALL_PATH, cn_main_repo_fetch_url().c_str()).c_str());',
    1,
  )
  old_push = (
    '  run(("cd " + INSTALL_PATH + " && "\n'
    '      "git remote set-url origin --push " GIT_SSH_URL " && "\n'
    '      "git config --replace-all remote.origin.fetch \\"+refs/heads/*:refs/remotes/origin/*\\"").c_str());'
  )
  new_push = (
    '  run(("cd " + INSTALL_PATH + " && "\n'
    '      "git remote set-url origin --push " + cn_main_repo_ssh_url() + " && "\n'
    '      "git config --replace-all remote.origin.fetch \\"+refs/heads/*:refs/remotes/origin/*\\"").c_str());'
  )
  if old_push in s2:
    s2 = s2.replace(old_push, new_push, 1)
  elif "GIT_SSH_URL" in s2:
    s2 = re.sub(
      r'"git remote set-url origin --push " GIT_SSH_URL " && "',
      '"git remote set-url origin --push " + cn_main_repo_ssh_url() + " && "',
      s2,
      count=1,
    )
  return s2


def patch_main_repo_cn_routing(root: Path) -> PatchResult:
  """主仓 cn_main_repo_route.py 全量写入 + installer 运行时选 URL。"""
  res = PatchResult("main_repo_cn_routing")
  route_py = cn_path(root, "system/cn_main_repo_route.py")
  body = _render_cn_main_repo_route_py(main_repo_source_gitee(), main_repo_source_codeup())
  _track_change(res, route_py, write_if_changed(route_py, body))
  gitee = main_repo_source_gitee()
  codeup = main_repo_source_codeup()
  installer = cn_path(root, "selfdrive/ui/installer/installer.cc")
  s = installer.read_text(encoding="utf-8")
  s2 = _inject_installer_cn_route(s, gitee, codeup)
  _track_change(res, installer, write_if_changed(installer, s2))
  return res


def patch_version_py(root: Path) -> PatchResult:
  res = PatchResult("system_version_py")
  gitee = main_repo_source_gitee()
  codeup = main_repo_source_codeup()
  version_py = cn_path(root, "system/version.py")
  s = version_py.read_text(encoding="utf-8")
  s2 = s
  for key in (gitee.version_remote_key, codeup.version_remote_key):
    if key not in s2:
      try:
        s2 = ensure_gitee_in_sunnypilot_remote_tuple_flex(s2, key)
      except RuntimeError:
        s2 = ensure_line_in_tuple_block(s2, key)
  _track_change(res, version_py, write_if_changed(version_py, s2))
  return res


def patch_updated_insteadof(root: Path) -> PatchResult:
  res = PatchResult("updated_insteadof")
  updated_py = cn_path(root, "system/updated/updated.py")
  s = updated_py.read_text(encoding="utf-8")
  s2 = inject_updated_insteadof_block_flex(s)
  s2 = inject_updated_cn_route_hook(s2)
  s2 = inject_updated_check_for_update_order(s2)
  s2 = inject_agnos_manifest_compat(s2)
  _track_change(res, updated_py, write_if_changed(updated_py, s2))
  return res


_CN_AGNOS_MANIFEST_SENTINEL = "cn_agnos_manifest_compat"
# 旧版车端 updated 在 OVERLAY_MERGED 上硬编码该相对路径；新布局文件在 openpilot/ 下。
_CN_AGNOS_OTA_SHIM_REL = "system/hardware/tici/agnos.json"


def _is_agnos_json_payload(data: bytes) -> bool:
  s = data.lstrip()
  return s.startswith(b"{") or s.startswith(b"[")


def _read_agnos_json_payload(path: Path) -> bytes | None:
  """读取 agnos.json 实体；兼容 git 把 symlink 存成相对路径文本的情况。"""
  if not path.is_file():
    return None
  data = path.read_bytes()
  if _is_agnos_json_payload(data):
    return data
  text = data.decode("utf-8", errors="replace").strip()
  # symlink 文本：例如 ../../../common/hardware/tici/agnos.json
  if len(data) < 512 and "/" in text and "\n" not in text and not _is_agnos_json_payload(data):
    target = (path.parent / text).resolve()
    if target.is_file():
      return _read_agnos_json_payload(target)
  return None


def _resolve_agnos_json_bytes(root: Path) -> bytes:
  candidates = [
    root / "openpilot/common/hardware/tici/agnos.json",
    root / "openpilot/system/hardware/tici/agnos.json",
    root / "common/hardware/tici/agnos.json",
    root / "system/hardware/tici/agnos.json",
  ]
  for p in candidates:
    payload = _read_agnos_json_payload(p)
    if payload is not None:
      return payload
  raise RuntimeError("找不到可用的 agnos.json（openpilot/common 或 system/hardware）")


def inject_agnos_manifest_compat(s: str) -> str:
  """handle_agnos_update：按候选路径解析 manifest，避免布局迁移后 OTA 半失败。"""
  if _CN_AGNOS_MANIFEST_SENTINEL in s:
    return s
  pat = re.compile(
    r'^([ \t]+)manifest_path = os\.path\.join\(OVERLAY_MERGED, "[^"]*agnos\.json"\)\s*$',
    re.M,
  )
  m = pat.search(s)
  if not m:
    raise RuntimeError("updated.py: 未找到 manifest_path agnos.json 赋值，无法注入兼容逻辑")
  ind = m.group(1)
  block = (
    f"{ind}# {_CN_AGNOS_MANIFEST_SENTINEL}: old OTA clients + openpilot/ layout\n"
    f"{ind}_agnos_rels = (\n"
    f'{ind}  "openpilot/system/hardware/tici/agnos.json",\n'
    f'{ind}  "openpilot/common/hardware/tici/agnos.json",\n'
    f'{ind}  "system/hardware/tici/agnos.json",\n'
    f"{ind})\n"
    f"{ind}manifest_path = next(\n"
    f"{ind}  (os.path.join(OVERLAY_MERGED, rel) for rel in _agnos_rels\n"
    f"{ind}   if os.path.isfile(os.path.join(OVERLAY_MERGED, rel))),\n"
    f"{ind}  os.path.join(OVERLAY_MERGED, _agnos_rels[0]),\n"
    f"{ind})\n"
  )
  return s[: m.start()] + block + s[m.end() :]


def patch_agnos_ota_compat(root: Path) -> PatchResult:
  """
  让仍在跑旧 updated.py 的设备也能完成「拉新树 → 刷 AGNOS」。

  旧客户端硬编码 OVERLAY_MERGED/system/hardware/tici/agnos.json；
  上游迁到 openpilot/ 后该路径缺失，git fetch 成功但 AGNOS 阶段 FileNotFoundError。
  在发布树根保留一份实体 JSON shim（车友 Gitee / 作者 Codeup 同样受益）。
  """
  res = PatchResult("agnos_ota_compat")
  updated_py = cn_path(root, "system/updated/updated.py")
  if updated_py.is_file():
    s2 = inject_agnos_manifest_compat(updated_py.read_text(encoding="utf-8"))
    _track_change(res, updated_py, write_if_changed(updated_py, s2))

  payload = _resolve_agnos_json_bytes(root)
  shim = root / _CN_AGNOS_OTA_SHIM_REL
  # 必须写在仓库根 system/...，不能走 cn_path（那会进 openpilot/）
  old = shim.read_bytes() if shim.is_file() else None
  if old != payload:
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_bytes(payload)
    _track_change(res, shim, True)
  return res


def inject_mici_home_repo_suffix_flex(s: str, path: Path) -> str:
  """在 mici home.py 的 _get_version_text return 行注入 main_repo_ui_suffix（多锚点 + regex）。"""
  if "main_repo_ui_suffix" in s:
    return s
  import_line = "    from openpilot.system.cn_main_repo_route import main_repo_ui_suffix\n"

  def _inject_for_slice(n: int) -> str:
    return (
      import_line
      + f"    return version, branch, commit[:{n}] + main_repo_ui_suffix(), date_str"
    )

  anchors = [
    "    return version, branch, commit[:7], date_str",
    "    return version, branch, commit[:8], date_str",
  ]
  for old in anchors:
    if old in s:
      m = re.search(r"commit\[:(\d+)\]", old)
      n = int(m.group(1)) if m else 7
      return s.replace(old, _inject_for_slice(n), 1)
  m = re.search(
    r"\n([ \t]+return version, branch, commit\[:(\d+)\], date_str)",
    s,
  )
  if m:
    old = m.group(0).lstrip("\n")
    n = int(m.group(2))
    return s.replace(old, _inject_for_slice(n), 1)
  raise RuntimeError(f"{path}: 未找到 _get_version_text return，无法注入 {_CN_MICI_HOME_SUFFIX_SENTINEL}")


def patch_mici_home_repo_suffix(root: Path) -> PatchResult:
  res = PatchResult("mici_home_repo_suffix")
  home_py = cn_path(root, "selfdrive/ui/mici/layouts/home.py")
  if not home_py.exists():
    return res
  s = home_py.read_text(encoding="utf-8")
  s2 = inject_mici_home_repo_suffix_flex(s, home_py)
  _track_change(res, home_py, write_if_changed(home_py, s2))
  return res


def patch_tici_setup(root: Path) -> PatchResult:
  res = PatchResult("tici_setup")
  tici_setup = cn_path(root, "system/ui/tici_setup.py")
  if not tici_setup.exists():
    return res

  s = tici_setup.read_text(encoding="utf-8")
  orig = s
  gitee_install = f'OPENPILOT_URL = "{gitee_sp_cn_installer_openpilot_url()}"\n'
  s = replace_first_alias(
    s,
    [
      'OPENPILOT_URL = "https://openpilot.comma.ai"\n',
      "OPENPILOT_URL = 'https://openpilot.comma.ai'\n",
    ],
    gitee_install,
  )
  if "CONNECTIVITY_CHECK_URLS" not in s:
    s = s.replace(
      gitee_install,
      gitee_install
      + '# 国内环境可能无法访问 openpilot.comma.ai，导致安装流程卡在 “Waiting for internet”。\n'
      '# 这里使用多个候选 URL：任意一个可访问即认为“已联网”。\n'
      'CONNECTIVITY_CHECK_URLS = [\n'
      '  # 恢复/刚刷机阶段系统时间可能不准，HTTPS 证书校验会失败，导致误判“无网络”\n'
      '  # 优先使用 HTTP 探测（不依赖系统时间），再回退到 HTTPS。\n'
      '  "http://www.baidu.com/",\n'
      '  "https://www.baidu.com/",\n'
      '  OPENPILOT_URL,\n'
      ']\n'
    )
    s = s.replace(
      "          urllib.request.urlopen(OPENPILOT_URL, timeout=2.0)\n",
      "          ok = False\n"
      "          for url in CONNECTIVITY_CHECK_URLS:\n"
      "            try:\n"
      "              # Hard fallback: raw TCP connect (ignores TLS/time, HTTP quirks)\n"
      "              # Also avoid DNS dependency in recovery mode by probing well-known CN DNS IPs.\n"
      "              if url.startswith(\"http://www.baidu.com\"):\n"
      "                import socket\n"
      "                for host, port in ((\"223.5.5.5\", 53), (\"114.114.114.114\", 53), (\"www.baidu.com\", 80)):\n"
      "                  try:\n"
      "                    socket.create_connection((host, port), timeout=2.0).close()\n"
      "                    ok = True\n"
      "                    break\n"
      "                  except Exception:\n"
      "                    continue\n"
      "                if ok:\n"
      "                  break\n"
      "\n"
      "              # Some sites don't reliably support HEAD; fall back to GET.\n"
      "              try:\n"
      "                req = urllib.request.Request(url, method=\"HEAD\")\n"
      "                urllib.request.urlopen(req, timeout=2.0)\n"
      "              except Exception:\n"
      "                req = urllib.request.Request(url, method=\"GET\")\n"
      "                urllib.request.urlopen(req, timeout=2.0)\n"
      "              ok = True\n"
      "              break\n"
      "            except Exception:\n"
      "              continue\n"
      "          if not ok:\n"
      "            raise RuntimeError(\"no connectivity\")\n"
    )
  else:
    s = s.replace('"https://gitee.com/",\n  "https://www.baidu.com/",\n', '"http://www.baidu.com/",\n  "https://www.baidu.com/",\n')

  if "continue_enabled = self.network_connected.is_set() or self.wifi_connected.is_set()" not in s:
    s = s.replace("    continue_enabled = self.network_connected.is_set()\n",
                  "    # 只要 Wi-Fi 已连接就允许继续，避免因探测失败导致卡死（国内网络/DNS/证书等问题）\n"
                  "    continue_enabled = self.network_connected.is_set() or self.wifi_connected.is_set()\n")

  if "def wlan0_has_ipv4()" not in s:
    if "import subprocess" not in s:
      s = s.replace("import urllib.error\n", "import urllib.error\nimport subprocess\n")
    s = s.replace(
      "  def check_network_connectivity(self):\n",
      "  def check_network_connectivity(self):\n"
      "    def wlan0_has_ipv4() -> bool:\n"
      "      try:\n"
      "        out = subprocess.check_output([\"ip\", \"-4\", \"addr\", \"show\", \"dev\", \"wlan0\"], text=True, stderr=subprocess.DEVNULL)\n"
      "        return \"inet \" in out\n"
      "      except Exception:\n"
      "        return False\n\n"
    )
  # 旧补丁把 Wi-Fi 状态更新放在联网探测成功之后，首次探测失败时无法放行。
  stale_wifi_block = (
    "          # Wi-Fi connect detection in recovery should not depend on network_type reporting.\n"
    "          # If wlan0 has an IPv4 address, treat it as connected.\n"
    "          if wlan0_has_ipv4():\n"
    "            self.wifi_connected.set()\n"
    "          else:\n"
    "            self.wifi_connected.clear()\n"
  )
  s = s.replace(stale_wifi_block, "")
  if "cn_wifi_ipv4_bypass" not in s:
    loop_anchor = (
      "    while not self.stop_network_check_thread.is_set():\n"
      "      if self.state == SetupState.NETWORK_SETUP:\n"
    )
    loop_repl = (
      "    while not self.stop_network_check_thread.is_set():\n"
      "      if self.state == SetupState.NETWORK_SETUP:\n"
      "        # cn_wifi_ipv4_bypass: Wi-Fi 状态独立于外网探测，且每轮清理陈旧状态。\n"
      "        if wlan0_has_ipv4():\n"
      "          self.wifi_connected.set()\n"
      "        else:\n"
      "          self.wifi_connected.clear()\n"
    )
    if loop_anchor not in s:
      raise RuntimeError(f"{tici_setup}: 未找到网络检测循环，无法注入 Wi-Fi 独立放行")
    s = s.replace(loop_anchor, loop_repl, 1)
  if s != orig:
    _track_change(res, tici_setup, write_if_changed(tici_setup, s))
  return res


def patch_mici_setup(root: Path) -> PatchResult:
  res = PatchResult("mici_setup")
  mici_setup = cn_path(root, "system/ui/mici_setup.py")
  if not mici_setup.exists():
    return res
  s = mici_setup.read_text(encoding="utf-8")
  orig = s
  gitee_install = f'OPENPILOT_URL = "{gitee_sp_cn_installer_openpilot_url()}"\n'
  s = replace_first_alias(
    s,
    [
      'OPENPILOT_URL = "https://openpilot.comma.ai"\n',
      "OPENPILOT_URL = 'https://openpilot.comma.ai'\n",
    ],
    gitee_install,
  )
  if "CONNECTIVITY_CHECK_URLS" not in s:
    s = s.replace(
      gitee_install,
      gitee_install
      + '# 国内环境可能无法访问 openpilot.comma.ai，导致 setup 卡在 “waiting for internet...”。\n'
      '# 这里使用多个候选 URL：任意一个可访问即认为“已联网”。\n'
      'CONNECTIVITY_CHECK_URLS = [\n'
      '  # 恢复/刚刷机阶段系统时间可能不准，HTTPS 证书校验会失败，导致误判“无网络”\n'
      '  # 优先使用 HTTP 探测（不依赖系统时间），再回退到 HTTPS。\n'
      '  "http://www.baidu.com/",\n'
      '  "https://www.baidu.com/",\n'
      '  OPENPILOT_URL,\n'
      ']\n'
    )
    s = s.replace(
      "          request = urllib.request.Request(OPENPILOT_URL, method=\"HEAD\")\n"
      "          urllib.request.urlopen(request, timeout=2.0)\n",
      "          ok = False\n"
      "          last_err: Exception | None = None\n"
      "          for url in CONNECTIVITY_CHECK_URLS:\n"
      "            try:\n"
      "              # Hard fallback: raw TCP connect (ignores TLS/time, HTTP quirks)\n"
      "              # Also avoid DNS dependency in recovery mode by probing well-known CN DNS IPs.\n"
      "              if url.startswith(\"http://www.baidu.com\"):\n"
      "                import socket\n"
      "                for host, port in ((\"223.5.5.5\", 53), (\"114.114.114.114\", 53), (\"www.baidu.com\", 80)):\n"
      "                  try:\n"
      "                    socket.create_connection((host, port), timeout=2.0).close()\n"
      "                    ok = True\n"
      "                    break\n"
      "                  except Exception:\n"
      "                    continue\n"
      "                if ok:\n"
      "                  break\n"
      "\n"
      "              # Some sites don't reliably support HEAD; fall back to GET.\n"
      "              try:\n"
      "                request = urllib.request.Request(url, method=\"HEAD\")\n"
      "                urllib.request.urlopen(request, timeout=2.0)\n"
      "              except Exception:\n"
      "                request = urllib.request.Request(url, method=\"GET\")\n"
      "                urllib.request.urlopen(request, timeout=2.0)\n"
      "              ok = True\n"
      "              break\n"
      "            except urllib.error.URLError as e:\n"
      "              last_err = e\n"
      "            except Exception as e:\n"
      "              last_err = e\n"
      "          if not ok:\n"
      "            if isinstance(last_err, urllib.error.URLError):\n"
      "              raise last_err\n"
      "            raise RuntimeError(\"no connectivity\")\n"
    )
  else:
    s = s.replace('"https://gitee.com/",\n  "https://www.baidu.com/",\n', '"http://www.baidu.com/",\n  "https://www.baidu.com/",\n')

  if "cn_wifi_connected_bypass" not in s:
    old_has_internet = (
      "    has_internet = (self._network_monitor.network_connected.is_set() and\n"
      "                    not network_changing and\n"
      "                    not self._network_monitor.recheck_event.is_set())\n"
    )
    new_has_internet = (
      "    # cn_wifi_connected_bypass: Wi-Fi 已连接即可继续，下载阶段再报告真实网络错误。\n"
      "    wifi_connected = self._wifi_manager.wifi_state.status == ConnectStatus.CONNECTED\n"
      "    has_internet = (wifi_connected or self._network_monitor.network_connected.is_set()) and \\\n"
      "                   (not network_changing) and \\\n"
      "                   (not self._network_monitor.recheck_event.is_set())\n"
    )
    if old_has_internet not in s:
      raise RuntimeError(f"{mici_setup}: 未找到 _has_internet 原始逻辑，无法注入 Wi-Fi 放行")
    s = s.replace(old_has_internet, new_has_internet, 1)

  if s != orig:
    _track_change(res, mici_setup, write_if_changed(mici_setup, s))
  return res


def patch_setup_sh(root: Path) -> PatchResult:
  res = PatchResult("tools_setup_sh")
  device = main_repo_device_source()
  repo_base = device.https_url.removesuffix(".git")
  contrib = f"{repo_base}/blob/staging/docs/CONTRIBUTING.md"
  setup_sh = root / "tools/setup.sh"
  s = setup_sh.read_text(encoding="utf-8")
  s2 = apply_text_replacement_rows(
    s,
    [
      (
        _with_git_url_variants("https://github.com/commaai/openpilot.git"),
        device.https_url,
      ),
      (
        [
          "https://github.com/commaai/openpilot/blob/master/docs/CONTRIBUTING.md",
          "https://github.com/commaai/openpilot/blob/staging/docs/CONTRIBUTING.md",
        ],
        contrib,
      ),
    ],
    path=setup_sh,
    require_all=True,
  )
  # 此前补丁写入 blob/master；远端已无 master 时需迁移为 staging
  for old_base in (main_repo_source_gitee().https_url.removesuffix(".git"),
                   main_repo_source_codeup().https_url.removesuffix(".git")):
    s2 = s2.replace(
      f"{old_base}/blob/master/docs/CONTRIBUTING.md",
      contrib,
    )
  interactive_url = f"{repo_base}/raw/staging/tools/setup.sh"
  s2 = s2.replace(
    "bash <(curl -fsSL openpilot.comma.ai)",
    f"bash <(curl -fsSL {interactive_url})",
  )
  _track_change(res, setup_sh, write_if_changed(setup_sh, s2))
  return res


def patch_msgq_setup(root: Path) -> PatchResult:
  res = PatchResult("msgq_setup")
  msgq_setup = root / "msgq_repo/setup.sh"
  if msgq_setup.exists():
    s = msgq_setup.read_text(encoding="utf-8")
    catch2_old = _with_git_url_variants("https://github.com/catchorg/Catch2.git")
    catch2_new = gitee_https_repo("Catch2.git")
    if any(o in s for o in catch2_old) or catch2_new in s:
      s2 = apply_text_replacement_rows(
        s,
        [(catch2_old, catch2_new)],
        path=msgq_setup,
        require_all=True,
      )
      _track_change(res, msgq_setup, write_if_changed(msgq_setup, s2))

  # 新版 msgq 由 pyproject/uv 管理 Catch2，实际依赖已迁到 commaai/dependencies。
  msgq_pyproject = root / "msgq_repo/pyproject.toml"
  if msgq_pyproject.exists():
    s = msgq_pyproject.read_text(encoding="utf-8")
    old_urls = [
      "git+https://github.com/commaai/dependencies.git",
      "git+https://github.com/commaai/dependencies",
      "git+http://github.com/commaai/dependencies.git",
    ]
    if any(url in s for url in old_urls):
      s2 = apply_text_replacement_rows(
        s,
        [(old_urls, f"git+{gitee_https_repo('dependencies.git')}")],
        path=msgq_pyproject,
        require_all=True,
      )
      _track_change(res, msgq_pyproject, write_if_changed(msgq_pyproject, s2))
  return res


def patch_opendbc_pyproject(root: Path) -> PatchResult:
  res = PatchResult("opendbc_pyproject")
  opendbc_pyproj = root / "opendbc_repo/pyproject.toml"
  if opendbc_pyproj.exists():
    s = opendbc_pyproj.read_text(encoding="utf-8")
    s2 = apply_text_replacement_rows(
      s,
      [
        (
          [
            "git+https://github.com/commaai/dependencies.git",
            "git+https://github.com/commaai/dependencies",
            "git+http://github.com/commaai/dependencies.git",
          ],
          f"git+{gitee_https_repo('dependencies.git')}",
        ),
      ],
      path=opendbc_pyproj,
      require_all=True,
    )
    _track_change(res, opendbc_pyproj, write_if_changed(opendbc_pyproj, s2))
  return res


def _rewrite_models_url_attr(s: str, attr: str, *, required: bool, path: Path) -> str:
  m = re.search(rf'{attr}\s*=\s*"([^"]+)"', s)
  if not m:
    if required:
      raise RuntimeError(f"{path}: 找不到 {attr} 赋值")
    return s
  json_name = _extract_model_json_basename(m.group(1))
  new_url = _resolve_gitee_models_json_url(json_name)
  current = _fix_models_json_url_typos(m.group(1).strip())
  if current == new_url:
    return s
  s2, n = re.subn(rf'({attr}\s*=\s*")[^"]+(")', rf'\1{new_url}\2', s, count=1)
  if n != 1:
    raise RuntimeError(f"{path}: {attr} 替换未生效（请检查 upstream 是否改了字段写法）")
  return s2


def patch_models_fetcher(root: Path) -> PatchResult:
  res = PatchResult("models_fetcher")
  fetcher = cn_path(root, "sunnypilot/models/fetcher.py")
  if not fetcher.is_file():
    return res

  s = fetcher.read_text(encoding="utf-8")
  s = inject_models_fetcher_stale_cache_guard(s)
  s = inject_sunnylink_activejson_cors_guard(s)
  s = inject_models_hf_mirror(s)
  s = _rewrite_models_url_attr(s, "MODEL_URL", required=True, path=fetcher)
  s = _rewrite_models_url_attr(s, "MODEL_URL_USBGPU", required=False, path=fetcher)
  s = _rewrite_models_url_attr(s, "MODEL_URL_CHESTNUT", required=False, path=fetcher)
  _track_change(res, fetcher, write_if_changed(fetcher, s))
  return res


_CN_MODELS_CACHE_GUARD = "cn_models_cache_selector_guard"
_CN_SUNNYLINK_ACTIVEJSON = "cn_sunnylink_activejson"
_CN_HF_MIRROR = "cn_hf_mirror"
_HF_MIRROR_HOST = "https://hf-mirror.com"
_HF_UPSTREAM_HOST = "https://huggingface.co"


_SUNNYLINK_ACTIVEJSON_GITHUB_PREFIX = (
  "https://raw.githubusercontent.com/sunnypilot/sunnypilot-models/gh-pages/docs/"
)


def _sunnylink_activejson_put_block() -> str:
  return (
    f"      # {_CN_SUNNYLINK_ACTIVEJSON}: sunnylink.ai fetches this URL in the browser; "
    "Gitee raw has no CORS. Device still uses MODEL_URL (Gitee).\n"
    "      # Empty ActiveJson falls back to ModelsCache (~100KB) which sunnylink often drops.\n"
    '      self.params.put(\n'
    '        "ModelManager_ActiveJson",\n'
    f'        "{_SUNNYLINK_ACTIVEJSON_GITHUB_PREFIX}"\n'
    '        + self.model_url.rsplit("/", 1)[-1],\n'
    "        block=True,\n"
    "      )"
  )


def _sunnylink_activejson_dict_put_block(entries: list[tuple[str, str]]) -> str:
  """ActiveJson map in ModelFetcher.__init__ (keys vary: usbgpu / chestnut)."""
  lines = [
    f"    # {_CN_SUNNYLINK_ACTIVEJSON}: sunnylink.ai fetches these URLs in the browser; "
    "Gitee raw has no CORS.",
    "    # Device still downloads manifests via MODEL_URL* (rewritten to Gitee).",
    f'    _cn_active_json_gh = "{_SUNNYLINK_ACTIVEJSON_GITHUB_PREFIX}"',
    '    self.params.put("ModelManager_ActiveJson", {',
  ]
  for key, attr in entries:
    lines.append(f'      "{key}": _cn_active_json_gh + self.{attr}.rsplit("/", 1)[-1],')
  lines.append("    }, block=True)")
  return "\n".join(lines) + "\n"


_ACTIVEJSON_DICT_PUT_RE = re.compile(
  r'(?m)^    self\.params\.put\("ModelManager_ActiveJson", \{\n'
  r'(?:      "[^"]+": self\.[A-Z0-9_]+,\n)+'
  r'    \}, block=True\)\n'
)


def inject_sunnylink_activejson_cors_guard(s: str) -> str:
  """
  sunnylink.ai/dashboard/models 用浏览器 fetch ModelManager_ActiveJson 里的 URL。
  设备拉清单走 Gitee；Gitee raw 无 CORS。留空则前端回退 ModelsCache，但 ~100KB JSON
  经常传不出 sunnylink，网页仍然空白。改为把官方 GitHub raw（有 CORS *）写给网页。
  """
  if _CN_SUNNYLINK_ACTIVEJSON in s and _SUNNYLINK_ACTIVEJSON_GITHUB_PREFIX in s:
    return s

  m_dict = _ACTIVEJSON_DICT_PUT_RE.search(s)
  if m_dict:
    entries = re.findall(r'"([^"]+)": self\.([A-Z0-9_]+)', m_dict.group(0))
    if not entries:
      raise RuntimeError("fetcher.py: ActiveJson dict 无法解析源键，无法注入 sunnylink CORS 规避")
    return s[: m_dict.start()] + _sunnylink_activejson_dict_put_block(entries) + s[m_dict.end() :]

  new = _sunnylink_activejson_put_block()
  empty = (
    f"      # {_CN_SUNNYLINK_ACTIVEJSON}: do not publish Gitee URL to sunnylink.ai "
    "(no CORS; frontend will not fall back to ModelsCache)\n"
    '      self.params.put("ModelManager_ActiveJson", "", block=True)'
  )
  orig = '      self.params.put("ModelManager_ActiveJson", self.model_url, block=True)'
  if empty in s:
    return s.replace(empty, new, 1)
  if orig in s:
    return s.replace(orig, new, 1)
  raise RuntimeError("fetcher.py: 未找到 ModelManager_ActiveJson 写入点，无法注入 sunnylink CORS 规避")


def _hf_mirror_parse_download_uri_block() -> str:
  return (
    "  @staticmethod\n"
    "  def _parse_download_uri(download_uri_data) -> custom.ModelManagerSP.DownloadUri:\n"
    "    download_uri = custom.ModelManagerSP.DownloadUri()\n"
    f"    # {_CN_HF_MIRROR}: JSON keeps HF URLs; rewrite host so device downloads via CN mirror\n"
    '    url = download_uri_data.get("url")\n'
    f'    if isinstance(url, str) and url.startswith("{_HF_UPSTREAM_HOST}"):\n'
    f'      url = "{_HF_MIRROR_HOST}" + url[len("{_HF_UPSTREAM_HOST}"):]\n'
    "    download_uri.uri = url\n"
    '    download_uri.sha256 = download_uri_data.get("sha256")\n'
    "    return download_uri\n"
  )


def inject_models_hf_mirror(s: str) -> str:
  """
  模型清单里的权重仍是 huggingface.co；国内设备直连失败。
  解析 download_uri 时改写为 hf-mirror.com（反向代理，不改 JSON / sha256）。
  """
  if _CN_HF_MIRROR in s and _HF_MIRROR_HOST in s:
    return s
  new = _hf_mirror_parse_download_uri_block()
  orig = (
    "  @staticmethod\n"
    "  def _parse_download_uri(download_uri_data) -> custom.ModelManagerSP.DownloadUri:\n"
    "    download_uri = custom.ModelManagerSP.DownloadUri()\n"
    '    download_uri.uri = download_uri_data.get("url")\n'
    '    download_uri.sha256 = download_uri_data.get("sha256")\n'
    "    return download_uri\n"
  )
  if orig not in s:
    raise RuntimeError(
      "fetcher.py: 未找到 _parse_download_uri 原文，无法注入 hf-mirror 改写"
    )
  return s.replace(orig, new, 1)


def inject_models_fetcher_stale_cache_guard(s: str) -> str:
  """
  OTA 后 Params 里可能仍留着旧 driving_models_*.json 缓存（minimum_selector_version 与
  helpers.REQUIRED_JSON_VERSION 不一致），parse 后 bundles 为空 → UI 只剩 CD210 默认项。

  新上游（get_bundles_for_source）已在「parsed 为空则 refetch」；仅打标记注释。
  旧上游仍注入 _parse_cached_bundles 包装。
  """
  if _CN_MODELS_CACHE_GUARD in s:
    return s

  # New ModelFetcher (qcom/usbgpu sources): upstream already refetches empty parses.
  if "def get_bundles_for_source(self, source: str)" in s:
    needle = (
      '          cloudlog.warning(f"Cached models for {source} have no valid bundles; refetching")\n'
    )
    if needle not in s:
      raise RuntimeError(
        "fetcher.py: 新版 get_bundles_for_source 未找到 empty-bundles refetch，"
        "无法标记 cn_models_cache_selector_guard"
      )
    mark = (
      f"          # {_CN_MODELS_CACHE_GUARD}: upstream refetches when parse yields no bundles\n"
      + needle
    )
    return s.replace(needle, mark, 1)

  m = re.search(
    r"(?m)^  def get_available_bundles\(self(?P<sig>, chestnut_present: bool = False)?\)"
    r" -> list\[custom\.ModelManagerSP\.ModelBundle\]:\n"
    r'    """Gets the list of available models, with smart cache handling"""\n'
    r"(?:    self\._update_model_source\((?P<upd>chestnut_present)?\)\n)?"
    r"    cached_data, is_expired = self\.model_cache\.get\(\)\n",
    s,
  )
  if not m:
    raise RuntimeError("fetcher.py: 未找到 get_available_bundles，无法注入模型缓存兼容逻辑")
  # Preserve upstream signature / source-update call (chestnut_present landed on staging).
  sig = m.group("sig") or ""
  if m.group("upd") is not None:
    prelude = "    self._update_model_source(chestnut_present)\n"
  elif "self._update_model_source()" in m.group(0):
    prelude = "    self._update_model_source()\n"
  else:
    prelude = ""
  block = (
    "  def _parse_cached_bundles(self, cached_data: dict) -> list[custom.ModelManagerSP.ModelBundle] | None:\n"
    f"    # {_CN_MODELS_CACHE_GUARD}: drop stale JSON cache after OTA / selector version bump\n"
    "    if not cached_data:\n"
    "      return None\n"
    "    bundles = self.model_parser.parse_models(cached_data)\n"
    '    if cached_data.get("bundles") and not bundles:\n'
    '      cloudlog.warning("Model cache incompatible with selector version; refetching")\n'
    "      return None\n"
    "    return bundles\n\n"
    f"  def get_available_bundles(self{sig}) -> list[custom.ModelManagerSP.ModelBundle]:\n"
    '    """Gets the list of available models, with smart cache handling"""\n'
    f"{prelude}"
    "    cached_data, is_expired = self.model_cache.get()\n"
  )
  s = s[: m.start()] + block + s[m.end() :]
  old_hit = (
    "    if cached_data and not is_expired:\n"
    "      cloudlog.debug(\"Using valid cached models data\")\n"
    "      return self.model_parser.parse_models(cached_data)\n"
  )
  new_hit = (
    "    if cached_data and not is_expired:\n"
    "      cached_bundles = self._parse_cached_bundles(cached_data)\n"
    "      if cached_bundles is not None:\n"
    '        cloudlog.debug("Using valid cached models data")\n'
    "        return cached_bundles\n"
    "      is_expired = True\n"
  )
  if old_hit not in s:
    raise RuntimeError("fetcher.py: 未找到有效缓存返回路径，无法注入模型缓存兼容逻辑")
  s = s.replace(old_hit, new_hit, 1)
  old_fb = (
    '    cloudlog.warning("Failed to fetch fresh data. Using expired cache as fallback")\n'
    "    return self.model_parser.parse_models(cached_data)\n"
  )
  new_fb = (
    '    cloudlog.warning("Failed to fetch fresh data. Using expired cache as fallback")\n'
    "    fallback = self._parse_cached_bundles(cached_data)\n"
    "    return fallback if fallback is not None else []\n"
  )
  if old_fb not in s:
    raise RuntimeError("fetcher.py: 未找到过期缓存回退路径，无法注入模型缓存兼容逻辑")
  return s.replace(old_fb, new_fb, 1)


def _tinygrad_fetch_remotes() -> list[str]:
  """tinygrad 对齐用的远端。Gitee 走 SSH，避免 Windows Git Credential Manager 弹 https://gitee.com 登录框。"""
  return [
    gitee_git_ssh_repo("tinygrad"),
    TINYGRAD_UPSTREAM_URL,
  ]


def _git_env_no_credential_prompt() -> dict[str, str]:
  env = os.environ.copy()
  env["GIT_TERMINAL_PROMPT"] = "0"
  env["GCM_INTERACTIVE"] = "never"
  return env


def _git_cmd_no_credential_prompt(cmd: list[str]) -> list[str]:
  if not cmd or cmd[0] != "git":
    return cmd
  return ["git", "-c", "credential.helper=", "-c", "credential.interactive=never", *cmd[1:]]


def _tinygrad_git(cmd: list[str], cwd: str | Path) -> str:
  return run(
    _git_cmd_no_credential_prompt(cmd),
    str(cwd),
    env=_git_env_no_credential_prompt(),
  )


def _rmtree_force(path: Path) -> None:
  """删除目录。Windows 上 git 工作区常带只读位，且杀毒/索引会短时占用，默认 rmtree 会 WinError 5。"""
  def _clear_readonly(p: str) -> None:
    try:
      os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
      pass

  def _onerror(func, p, _exc) -> None:
    _clear_readonly(p)
    try:
      func(p)
    except FileNotFoundError:
      pass

  last: BaseException | None = None
  for i in range(8):
    if not path.exists() and not path.is_symlink():
      return
    try:
      shutil.rmtree(path, onerror=_onerror)
      if not path.exists() and not path.is_symlink():
        return
    except OSError as e:
      last = e
    time.sleep(0.2 * (i + 1))
  if path.exists() or path.is_symlink():
    raise PermissionError(f"无法删除 {path}: {last}") from last


def _replace_dir(src: Path, dest: Path) -> None:
  """用 src 目录替换 dest。Windows 上 os.rename 不能覆盖已有目录，rmtree 后路径常处于 DELETE_PENDING。"""
  backup = dest.with_name(f"{dest.name}.__sp_sync_old__")
  _rmtree_force(backup)
  last: BaseException | None = None
  for i in range(8):
    try:
      if dest.exists() or dest.is_symlink():
        try:
          dest.rename(backup)
        except OSError:
          _rmtree_force(dest)
      src.rename(dest)
      _rmtree_force(backup)
      return
    except OSError as e:
      last = e
      time.sleep(0.25 * (i + 1))
      _rmtree_force(backup)
  try:
    _rmtree_force(dest)
    shutil.copytree(src, dest, symlinks=True)
    _rmtree_force(src)
    return
  except OSError as e:
    last = e
  raise PermissionError(f"无法将 {src} 替换到 {dest}: {last}") from last


def _materialize_tinygrad_commit(commit: str, dest: Path) -> None:
  """将 tinygrad 某 commit 导出为 vendored 目录（不含 .git）。"""
  raw = commit
  commit = _git_sha40(commit)
  if not commit:
    raise RuntimeError(f"invalid tinygrad commit: {raw!r}")

  staging = dest.with_name(f"{dest.name}.__sp_sync_staging__")
  if staging.exists():
    _rmtree_force(staging)

  with tempfile.TemporaryDirectory() as td:
    src = Path(td) / "repo"
    src.mkdir()
    run(["git", "init"], str(src))
    fetched = False
    last_err: str | None = None
    for url in _tinygrad_fetch_remotes():
      try:
        _tinygrad_git(["git", "remote", "add", "origin", url], src)
        _tinygrad_git(["git", "fetch", "--depth", "1", "origin", commit], src)
        fetched = True
        break
      except RuntimeError as e:
        last_err = str(e)
        try:
          _tinygrad_git(["git", "remote", "remove", "origin"], src)
        except RuntimeError:
          pass
    if not fetched:
      hint = f": {last_err}" if last_err else ""
      raise RuntimeError(f"无法 fetch tinygrad {commit[:12]}（已尝试 Gitee/GitHub）{hint}")

    run(["git", "checkout", "--force", "FETCH_HEAD"], str(src))
    staging.mkdir(parents=True)
    for child in src.iterdir():
      if child.name == ".git":
        continue
      dst = staging / child.name
      if child.is_dir():
        shutil.copytree(child, dst, symlinks=True)
      else:
        shutil.copy2(child, dst)
    (staging / ".sp_tinygrad_vendored_ref").write_text(commit + "\n", encoding="utf-8")

  marker = _read_vendored_tinygrad_marker(staging)
  if marker != commit:
    _rmtree_force(staging)
    raise RuntimeError(f"{dest}: tinygrad 导出后标记文件校验失败")

  try:
    _replace_dir(staging, dest)
  except Exception:
    _rmtree_force(staging)
    raise


def _read_vendored_tinygrad_marker(tg: Path) -> str | None:
  marker = tg / ".sp_tinygrad_vendored_ref"
  if not marker.is_file():
    return None
  return _git_sha40(marker.read_text(encoding="utf-8"))


def _vendored_tinygrad_already_aligned(tg: Path, *, target_commit: str, target_tree: str) -> bool:
  """HEAD tree 或工作区 marker 已表明 tinygrad 与目标 commit 一致（幂等跳过）。"""
  root = tg.parent
  head_ent = _git_ls_tree_entry(root, "HEAD", "tinygrad_repo")
  if head_ent and head_ent[0] == "tree" and head_ent[1] == target_tree:
    return True
  marker = _read_vendored_tinygrad_marker(tg)
  if marker != target_commit:
    return False
  marker_tree = _tinygrad_commit_root_tree(marker)
  return bool(marker_tree and marker_tree == target_tree)


def patch_tinygrad_vendored_align(root: Path) -> PatchResult:
  """
  upstream staging 将 tinygrad_repo vendored 为 tree，可能与 models JSON tinygrad_ref 不一致。
  国内镜像在此对齐 vendored 内容，避免 OTA 上 ModelManager 下载模型 unpickle 失败（nan m/s）。
  """
  res = PatchResult("tinygrad_vendored_align")
  if (os.environ.get("SYNC_PATCH_TINYGRAD_VENDORED") or "1").strip().lower() in ("0", "false", "no", "off"):
    return res

  tg = root / "tinygrad_repo"
  if not tg.is_dir():
    return res
  if (tg / ".git").exists():
    return res

  head_ent = _git_ls_tree_entry(root, "HEAD", "tinygrad_repo")
  if not head_ent or head_ent[0] != "tree":
    return res

  target_commit, ref_source, _ = _resolve_tinygrad_models_ref(root, offline_ok=True)
  if not target_commit:
    raise RuntimeError(
      "patch_tinygrad_vendored_align: 无法确定 tinygrad 对齐目标"
      "（models JSON 与 TINYGRAD_MODELS_REF 均不可用）"
    )

  target_tree = _tinygrad_commit_root_tree(target_commit)
  if not target_tree:
    raise RuntimeError(
      f"patch_tinygrad_vendored_align: 无法解析 {ref_source} commit={target_commit[:12]} 的根 tree"
    )
  if _vendored_tinygrad_already_aligned(tg, target_commit=target_commit, target_tree=target_tree):
    return res

  log(
    "tinygrad",
    f"vendored tree {head_ent[1][:7]} → {target_commit[:7]} "
    f"({ref_source}，根 tree {target_tree[:7]})",
  )
  _materialize_tinygrad_commit(target_commit, tg)
  if not _vendored_tinygrad_already_aligned(tg, target_commit=target_commit, target_tree=target_tree):
    raise RuntimeError(
      f"{tg}: tinygrad 对齐后校验失败（marker/tree 与 {target_commit[:12]} 不一致）"
    )
  _track_change(res, tg, True)
  return res


def patch_osm(root: Path) -> PatchResult:
  res = PatchResult("osm_layout")
  osm_py = cn_path(root, "selfdrive/ui/sunnypilot/layouts/settings/osm.py")
  s = osm_py.read_text(encoding="utf-8")
  s2 = apply_text_replacement_rows(
    s,
    [
      (
        [
          "https://raw.githubusercontent.com/pfeiferj/openpilot-mapd/main/",
          "https://raw.githubusercontent.com/pfeiferj/openpilot-mapd/main",
        ],
        gitee_raw_repo("openpilot-mapd", "main"),
      ),
    ],
    path=osm_py,
    require_all=True,
  )
  _track_change(res, osm_py, write_if_changed(osm_py, s2))
  return res


def patch_mapd_installer(root: Path) -> PatchResult:
  res = PatchResult("mapd_installer")
  mapd_installer = cn_path(root, "sunnypilot/mapd/mapd_installer.py")
  s = mapd_installer.read_text(encoding="utf-8")
  s2 = s
  if 'os.getenv("MAPD_TAG"' not in s and "os.getenv('MAPD_TAG'" not in s:
    s2 = re.sub(
      r"^VERSION\s*=\s*(?:\"[^\"]+\"|'[^']+')",
      'VERSION = os.getenv("MAPD_TAG", "v1.12.0")',
      s2,
      count=1,
      flags=re.MULTILINE,
    )
    if s2 == s:
      raise RuntimeError(f"{mapd_installer}: 未找到 VERSION = \"…\" 赋值行，上游 mapd_installer 可能已改版")
  if f"gitee.com/{GITEE_OWNER}/openpilot-mapd" not in s2:
    s2 = apply_text_replacement_rows(
      s2,
      [
        (
          [
            "https://github.com/pfeiferj/openpilot-mapd/releases/download/",
            "http://github.com/pfeiferj/openpilot-mapd/releases/download/",
          ],
          f"{gitee_https_repo('openpilot-mapd')}/releases/download/",
        ),
      ],
      path=mapd_installer,
      require_all=True,
    )
  _track_change(res, mapd_installer, write_if_changed(mapd_installer, s2))
  return res


_CN_SOFTWARE_UPDATE_CONFIRM = "cn_confirm_software_update"


def inject_tici_software_update_confirm(s: str) -> str:
  """tici/tizi Software 菜单：有更新时先英文确认，Yes 才下载/安装。"""
  if _CN_SOFTWARE_UPDATE_CONFIRM in s:
    return s
  old_dl = (
    "  def _on_download_update(self):\n"
    "    # Check if we should start checking or start downloading\n"
    "    self._download_btn.action_item.set_enabled(False)\n"
    "    if self._download_btn.action_item.text == tr(\"CHECK\"):\n"
    "      # Start checking for updates\n"
    "      self._waiting_for_updater = True\n"
    "      self._waiting_start_ts = time.monotonic()\n"
    "      subprocess.run(\"pkill -SIGUSR1 -f openpilot.system.updated.updated\", shell=True)\n"
    "    else:\n"
    "      # Start downloading\n"
    "      self._waiting_for_updater = True\n"
    "      self._waiting_start_ts = time.monotonic()\n"
    "      subprocess.run(\"pkill -SIGHUP -f openpilot.system.updated.updated\", shell=True)\n"
  )
  new_dl = (
    "  def _on_download_update(self):\n"
    f"    # {_CN_SOFTWARE_UPDATE_CONFIRM}\n"
    "    if self._download_btn.action_item.text == tr(\"CHECK\"):\n"
    "      self._cn_start_software_update(check_only=True)\n"
    "      return\n"
    "    def handle_confirm(result: DialogResult):\n"
    "      if result == DialogResult.CONFIRM:\n"
    "        self._cn_start_software_update(check_only=False)\n"
    "      else:\n"
    "        self._download_btn.action_item.set_enabled(True)\n"
    "    gui_app.push_widget(ConfirmDialog(\"Confirm update?\", \"Yes\", callback=handle_confirm))\n"
    "\n"
    "  def _cn_start_software_update(self, check_only: bool):\n"
    "    self._download_btn.action_item.set_enabled(False)\n"
    "    self._waiting_for_updater = True\n"
    "    self._waiting_start_ts = time.monotonic()\n"
    "    sig = \"-SIGUSR1\" if check_only else \"-SIGHUP\"\n"
    "    subprocess.run(f\"pkill {sig} -f openpilot.system.updated.updated\", shell=True)\n"
  )
  if old_dl not in s:
    raise RuntimeError("layouts/settings/software.py: 未找到 _on_download_update，无法注入更新确认")
  s = s.replace(old_dl, new_dl, 1)
  old_inst = (
    "  def _on_install_update(self):\n"
    "    # Trigger reboot to install update\n"
    "    self._install_btn.action_item.set_enabled(False)\n"
    "    ui_state.params.put_bool(\"DoReboot\", True, block=True)\n"
  )
  new_inst = (
    "  def _on_install_update(self):\n"
    f"    # {_CN_SOFTWARE_UPDATE_CONFIRM}\n"
    "    def handle_confirm(result: DialogResult):\n"
    "      if result == DialogResult.CONFIRM:\n"
    "        self._install_btn.action_item.set_enabled(False)\n"
    "        ui_state.params.put_bool(\"DoReboot\", True, block=True)\n"
    "    gui_app.push_widget(ConfirmDialog(\"Confirm update?\", \"Yes\", callback=handle_confirm))\n"
  )
  if old_inst not in s:
    raise RuntimeError("layouts/settings/software.py: 未找到 _on_install_update，无法注入更新确认")
  return s.replace(old_inst, new_inst, 1)


def inject_mici_software_update_confirm(s: str) -> str:
  """C4/mici Software 菜单：有更新时滑动确认（小屏无法用 tici 的 Yes/Cancel 弹窗）。"""
  if _CN_SOFTWARE_UPDATE_CONFIRM in s:
    return s
  old_imp = "from openpilot.selfdrive.ui.mici.widgets.dialog import BigDialog\n"
  new_imp = "from openpilot.selfdrive.ui.mici.widgets.dialog import BigDialog, BigConfirmationDialog\n"
  if old_imp not in s:
    raise RuntimeError("mici/layouts/settings/software.py: 未找到 BigDialog import")
  s = s.replace(old_imp, new_imp, 1)
  old_check_signal = (
    "    if not system_time_valid():\n"
    "      dlg = BigDialog(\"\", tr(\"Please connect to Wi-Fi to update.\"))\n"
    "      gui_app.push_widget(dlg)\n"
    "      return\n"
    "\n"
    "    self._signal_updater(self.DOWNLOAD_UPDATE if self.get_value() == \"download update\" else self.CHECK_FOR_UPDATE)\n"
  )
  new_check_signal = (
    "    if not system_time_valid():\n"
    "      dlg = BigDialog(\"\", tr(\"Please connect to Wi-Fi to update.\"))\n"
    "      gui_app.push_widget(dlg)\n"
    "      return\n"
    "\n"
    f"    # {_CN_SOFTWARE_UPDATE_CONFIRM}\n"
    "    if self.get_value() == \"download update\":\n"
    "      gui_app.push_widget(BigConfirmationDialog(\n"
    "        \"slide to\\nconfirm update\",\n"
    "        self._txt_update_icon,\n"
    "        lambda: self._signal_updater(self.DOWNLOAD_UPDATE),\n"
    "      ))\n"
    "      return\n"
    "    self._signal_updater(self.CHECK_FOR_UPDATE)\n"
  )
  old_check = (
    "    self.set_enabled(False)\n"
    "    self._state = UpdaterState.WAITING_FOR_UPDATER\n"
    "    self.set_icon(self._txt_update_icon)\n"
    "\n"
    "    def run():\n"
    "      if self.get_value() == \"download update\":\n"
    "        subprocess.run(\"pkill -SIGHUP -f openpilot.system.updated.updated\", shell=True)\n"
    "      else:\n"
    "        subprocess.run(\"pkill -SIGUSR1 -f openpilot.system.updated.updated\", shell=True)\n"
    "\n"
    "    threading.Thread(target=run, daemon=True).start()\n"
  )
  new_check = (
    f"    # {_CN_SOFTWARE_UPDATE_CONFIRM}\n"
    "    if self.get_value() == \"download update\":\n"
    "      gui_app.push_widget(BigConfirmationDialog(\n"
    "        \"slide to\\nconfirm update\",\n"
    "        self._txt_update_icon,\n"
    "        lambda: self._cn_start_updater(download=True),\n"
    "      ))\n"
    "      return\n"
    "    self._cn_start_updater(download=False)\n"
    "\n"
    "  def _cn_start_updater(self, download: bool):\n"
    "    self.set_enabled(False)\n"
    "    self._state = UpdaterState.WAITING_FOR_UPDATER\n"
    "    self.set_icon(self._txt_update_icon)\n"
    "\n"
    "    def run():\n"
    "      if download:\n"
    "        subprocess.run(\"pkill -SIGHUP -f openpilot.system.updated.updated\", shell=True)\n"
    "      else:\n"
    "        subprocess.run(\"pkill -SIGUSR1 -f openpilot.system.updated.updated\", shell=True)\n"
    "\n"
    "    threading.Thread(target=run, daemon=True).start()\n"
  )
  if old_check_signal in s:
    s = s.replace(old_check_signal, new_check_signal, 1)
  elif old_check in s:
    s = s.replace(old_check, new_check, 1)
  else:
    raise RuntimeError("mici/layouts/settings/software.py: 未找到 CheckUpdateButton 下载分支，无法注入更新确认")
  old_inst = (
    "  def _handle_mouse_release(self, mouse_pos: MousePos):\n"
    "    super()._handle_mouse_release(mouse_pos)\n"
    "\n"
    "    self.set_enabled(False)\n"
    "\n"
    "    def run():\n"
    "      ui_state.params.put_bool(\"DoReboot\", True, block=True)\n"
    "\n"
    "    threading.Thread(target=run, daemon=True).start()\n"
  )
  new_inst = (
    "  def _handle_mouse_release(self, mouse_pos: MousePos):\n"
    "    super()._handle_mouse_release(mouse_pos)\n"
    f"    # {_CN_SOFTWARE_UPDATE_CONFIRM}\n"
    "    icon = gui_app.texture(\"icons_mici/settings/device/reboot.png\", 64, 70)\n"
    "\n"
    "    def do_reboot():\n"
    "      self.set_enabled(False)\n"
    "      def run():\n"
    "        ui_state.params.put_bool(\"DoReboot\", True, block=True)\n"
    "      threading.Thread(target=run, daemon=True).start()\n"
    "\n"
    "    gui_app.push_widget(BigConfirmationDialog(\"slide to\\nconfirm update\", icon, do_reboot))\n"
  )
  if old_inst not in s:
    raise RuntimeError("mici/layouts/settings/software.py: 未找到 InstallUpdateButton，无法注入更新确认")
  return s.replace(old_inst, new_inst, 1)


def patch_software_update_confirm(root: Path) -> PatchResult:
  res = PatchResult("software_update_confirm")
  tici = cn_path(root, "selfdrive/ui/layouts/settings/software.py")
  if tici.is_file():
    s2 = inject_tici_software_update_confirm(tici.read_text(encoding="utf-8"))
    _track_change(res, tici, write_if_changed(tici, s2))
  mici = cn_path(root, "selfdrive/ui/mici/layouts/settings/software.py")
  if mici.is_file():
    s2 = inject_mici_software_update_confirm(mici.read_text(encoding="utf-8"))
    _track_change(res, mici, write_if_changed(mici, s2))
  if not tici.is_file() and not mici.is_file():
    raise RuntimeError("未找到 Software 菜单 software.py（tici/mici），无法注入更新确认")
  return res


_DM_SENTINEL_AWARENESS = "cn_dm_relaxed: avoid awarenessStatus<0 (forceDecel)"
_DM_SENTINEL_TERMINAL = "cn_dm_relaxed: no terminal strike lockout"
_DM_SENTINEL_RED_EXIT = "cn_dm_relaxed: auto exit red after 6s attention"
_DM_SENTINEL_NO_FORCE_DECEL = "cn_dm_relaxed: DM alertLevel.three does not set forceDecel"
_DM_SENTINEL_ESCALATION_TIMER = "cn_dm_relaxed: preserve no-response escalation timer"

_RE_CONTROLS_FORCE_DECEL_WITH_DM = re.compile(
  r"cs\.forceDecel\s*=\s*bool\s*\(\s*"
  r"\(\s*self\.sm\['driverMonitoringState'\]\.alertLevel\s*==\s*"
  r"log\.DriverMonitoringState\.AlertLevel\.three\s*\)\s*or\s*\n"
  r"\s*\(\s*self\.sm\['selfdriveState'\]\.state\s*==\s*State\.softDisabling\s*\)\s*\)",
  re.MULTILINE,
)

# 上游改用 noResponseForceDecel 驱动纵向收油（仍属 DM 红屏升级路径）。
_RE_CONTROLS_FORCE_DECEL_NO_RESPONSE = re.compile(
  r"cs\.forceDecel\s*=\s*bool\s*\(\s*"
  r"self\.sm\['driverMonitoringState'\]\.noResponseForceDecel\s*or\s*\n?"
  r"\s*\(?\s*self\.sm\['selfdriveState'\]\.state\s*==\s*State\.softDisabling\s*\)?\s*\)",
  re.MULTILINE,
)

_RE_CONTROLS_FORCE_DECEL_AWARENESS = re.compile(
  r"cs\.forceDecel\s*=\s*bool\s*\(\s*"
  r"(?:\(\s*)?self\.sm\['driverMonitoringState'\]\.awarenessStatus\s*<\s*0\.?\s*\)?\s*or\s*\n?"
  r"\s*\(?\s*self\.sm\['selfdriveState'\]\.state\s*==\s*State\.softDisabling\s*\)?\s*\)",
  re.MULTILINE,
)

# 宽松匹配「递减下限 -0.1」：不绑定整行/赋值左侧写法，便于上游换行、注释微调。
_RE_DM_AWARENESS_FLOOR_LOOSE = re.compile(
  r"max\s*\(\s*self\.awareness\s*-\s*self\.step_change\s*,\s*-0\.1\s*\)",
  re.MULTILINE,
)

# 策略 1：与历史上游结构一致（整段替换 if 分支体）。
_RE_DM_TERMINAL_STRIKES_STRICT = re.compile(
  r"^([ \t]+)if\s+self\.awareness\s*<=\s*(?:0\.|0)\s*:\s*\n"
  r"[ \t]*#\s*terminal red alert[^\n]*\n"
  r"[ \t]*alert\s*=\s*EventName\.driverDistracted3\s+if\s+self\.active_monitoring_mode\s+else\s+EventName\.driverUnresponsive3\s*\n"
  r"[ \t]*self\.terminal_time\s*\+=\s*1\s*\n"
  r"[ \t]*if\s+awareness_prev\s*>\s*(?:0\.|0)\s*:\s*\n"
  r"[ \t]*self\.terminal_alert_cnt\s*\+=\s*1",
  re.MULTILINE,
)

# 策略 2：只搜「红线 alert 行 + 累计 terminal」片段，吞掉可选的内层 if（sed 式剥层）。
_RE_DM_TERMINAL_STRIP = re.compile(
  r"(^[ \t]*alert\s*=\s*EventName\.driverDistracted3\b[^\n]*\n)"
  r"(^[ \t]*self\.terminal_time\s*\+=\s*1\s*\n)"
  r"(?:^[ \t]*if\s+awareness_prev\b[^\n]*:\s*\n"
  r"^[ \t]*self\.terminal_alert_cnt\s*\+=\s*1\s*\n)?",
  re.MULTILINE,
)

# 策略 3：upstream staging 起 DM 迁至 policy.py（alertLevel 分级，无 EventName alert 变量）。
_RE_DM_TERMINAL_POLICY_STRICT = re.compile(
  r"^([ \t]+)if\s+self\.awareness\s*<=\s*(?:0\.|0)\s*:\s*\n"
  r"[ \t]*#\s*terminal alert[^\n]*\n"
  r"[ \t]*self\.alert_level\s*=\s*AlertLevel\.three\s*\n"
  r"[ \t]*self\.terminal_time\s*\+=\s*1\s*\n"
  r"[ \t]*if\s+awareness_prev\s*>\s*(?:0\.|0)\s*:\s*\n"
  r"[ \t]*self\.terminal_alert_cnt\s*\+=\s*1",
  re.MULTILINE,
)

# 策略 4：policy v2（alert_3_cnt / no_response_cnt / lockout，无 terminal_time）。
_RE_DM_TERMINAL_POLICY_V2 = re.compile(
  r"^([ \t]+)if\s+self\.awareness\s*<=\s*(?:0\.|0)\s*:\s*\n"
  r"[ \t]*#\s*terminal alert[^\n]*\n"
  r"[ \t]*self\.alert_level\s*=\s*AlertLevel\.three\s*\n"
  r"[ \t]*if\s+awareness_prev\s*>\s*(?:0\.|0)\s*:\s*\n"
  r"[ \t]*self\.alert_3_cnt\s*\+=\s*1\s*\n"
  r"[ \t]*self\.cnt_since_alert_3\s*=\s*0\s*\n"
  r"[ \t]*else:\s*\n"
  r"[ \t]*self\.cnt_since_alert_3\s*\+=\s*1\s*\n"
  r"[ \t]*if\s+self\.cnt_since_alert_3\s*==\s*self\.no_response_timeout\s*:\s*\n"
  r"[ \t]*self\.no_response_cnt\s*\+=\s*1",
  re.MULTILINE,
)


def _dm_monitoring_path(root: Path) -> Path | None:
  for base in ("openpilot/selfdrive/monitoring", "selfdrive/monitoring"):
    policy = root / base / "policy.py"
    if policy.exists():
      return policy
    helpers = root / base / "helpers.py"
    if helpers.exists():
      return helpers
  return None


def _dm_is_policy_arch(s: str) -> bool:
  return "def _update_events" in s and "AlertLevel.three" in s and "if alert is not None" not in s


def _patch_dm_awareness_flexible(s: str, path_for_err: Path) -> str:
  """将 max(..., -0.1) 改为 0. 并带 sentinel；匹配处数必须为 1。"""
  if _DM_SENTINEL_AWARENESS in s:
    return s
  matches = list(_RE_DM_AWARENESS_FLOOR_LOOSE.finditer(s))
  if len(matches) == 0:
    raise RuntimeError(
      f"{path_for_err}: 未找到 max(self.awareness - self.step_change, -0.1)；"
      "上游可能已改名 step_change 或改写递减逻辑，请人工对照 selfdrive/monitoring/policy.py 或 helpers.py。"
    )
  if len(matches) > 1:
    raise RuntimeError(
      f"{path_for_err}: awareness 下限 -0.1 出现 {len(matches)} 处，无法自动判定，请人工打补丁。"
    )
  repl = f"max(self.awareness - self.step_change, 0.)  # {_DM_SENTINEL_AWARENESS}"
  return _RE_DM_AWARENESS_FLOOR_LOOSE.sub(repl, s, count=1)


def _patch_dm_terminal_flexible(s: str, path_for_err: Path) -> str:
  """去掉红线阶段对 terminal_time / terminal_alert_cnt 的累计；保留告警赋值。多策略依次尝试。"""
  if _DM_SENTINEL_TERMINAL in s:
    return s

  def _strict(m: re.Match) -> str:
    ind_if = m.group(1)
    ind_body = ind_if + "  "
    return (
      f"{ind_if}if self.awareness <= 0.:\n"
      f"{ind_body}# terminal red alert: disengagement required ({_DM_SENTINEL_TERMINAL})\n"
      f"{ind_body}alert = EventName.driverDistracted3 if self.active_monitoring_mode else EventName.driverUnresponsive3"
    )

  strict_hits = list(_RE_DM_TERMINAL_STRIKES_STRICT.finditer(s))
  if len(strict_hits) == 1:
    return _RE_DM_TERMINAL_STRIKES_STRICT.sub(_strict, s, count=1)
  if len(strict_hits) > 1:
    raise RuntimeError(
      f"{path_for_err}: 严格模式匹配到多处 terminal 累计块（{len(strict_hits)}），请人工处理。"
    )

  strip_hits = list(_RE_DM_TERMINAL_STRIP.finditer(s))
  if len(strip_hits) == 1:
    m = strip_hits[0]
    alert_line = m.group(1)
    ind_m = re.match(r"^([ \t]*)", alert_line)
    ind = ind_m.group(1) if ind_m else ""
    return (
      s[: m.start()]
      + alert_line
      + f"{ind}# {_DM_SENTINEL_TERMINAL}\n"
      + s[m.end() :]
    )
  if len(strip_hits) > 1:
    raise RuntimeError(
      f"{path_for_err}: 宽松模式匹配到多处「driverDistracted3 + terminal_time」片段（{len(strip_hits)}），请人工处理。"
    )

  def _policy_strict(m: re.Match) -> str:
    ind_if = m.group(1)
    ind_body = ind_if + "  "
    return (
      f"{ind_if}if self.awareness <= 0.:\n"
      f"{ind_body}# terminal alert: disengagement required ({_DM_SENTINEL_TERMINAL})\n"
      f"{ind_body}self.alert_level = AlertLevel.three"
    )

  policy_hits = list(_RE_DM_TERMINAL_POLICY_STRICT.finditer(s))
  if len(policy_hits) == 1:
    return _RE_DM_TERMINAL_POLICY_STRICT.sub(_policy_strict, s, count=1)
  if len(policy_hits) > 1:
    raise RuntimeError(
      f"{path_for_err}: policy 严格模式匹配到多处 terminal 累计块（{len(policy_hits)}），请人工处理。"
    )

  def _policy_v2(m: re.Match) -> str:
    ind_if = m.group(1)
    ind_body = ind_if + "  "
    return (
      f"{ind_if}if self.awareness <= 0.:\n"
      f"{ind_body}# terminal alert: disengagement required ({_DM_SENTINEL_TERMINAL})\n"
      f"{ind_body}self.alert_level = AlertLevel.three\n"
      f"{ind_body}if awareness_prev > 0.:\n"
      f"{ind_body}  self.cnt_since_alert_3 = 0\n"
      f"{ind_body}else:\n"
      f"{ind_body}  self.cnt_since_alert_3 += 1  # {_DM_SENTINEL_ESCALATION_TIMER}"
    )

  policy_v2_hits = list(_RE_DM_TERMINAL_POLICY_V2.finditer(s))
  if len(policy_v2_hits) == 1:
    return _RE_DM_TERMINAL_POLICY_V2.sub(_policy_v2, s, count=1)
  if len(policy_v2_hits) > 1:
    raise RuntimeError(
      f"{path_for_err}: policy v2 模式匹配到多处 alert_3/no_response 累计块（{len(policy_v2_hits)}），请人工处理。"
    )

  raise RuntimeError(
    f"{path_for_err}: 无法匹配 terminal 累计块（helpers 严格/宽松与 policy 严格/v2 均失败）。"
    "上游若重构 DM 分支，请对照 terminal_time / alert_3_cnt / alertLevel.three 附近代码手工合并补丁。"
  )


def _ensure_dm_policy_escalation_timer(s: str, path_for_err: Path) -> str:
  """新版 policy 保留 noResponseForceDecel 的计时，仅移除 strike/no_response 锁定累计。"""
  if not _dm_is_policy_arch(s) or "noResponseForceDecel" not in s:
    return s
  if _DM_SENTINEL_ESCALATION_TIMER in s:
    return s
  anchor = re.search(
    rf"(?m)^(?P<ind>[ \t]+)# terminal alert: disengagement required "
    rf"\({_DM_SENTINEL_TERMINAL}\)\s*\n"
    rf"(?P=ind)self\.alert_level = AlertLevel\.three\s*$",
    s,
  )
  if not anchor:
    raise RuntimeError(f"{path_for_err}: 未找到已削弱的 policy 红屏块，无法恢复原厂 escalation 计时")
  ind = anchor.group("ind")
  timer = (
    f"\n{ind}if awareness_prev > 0.:\n"
    f"{ind}  self.cnt_since_alert_3 = 0\n"
    f"{ind}else:\n"
    f"{ind}  self.cnt_since_alert_3 += 1  # {_DM_SENTINEL_ESCALATION_TIMER}"
  )
  return s[:anchor.end()] + timer + s[anchor.end():]


_RE_DM_RED_EXIT_HEADER = re.compile(
  r"(?m)^(?P<ind>[ \t]*)# cn_dm_relaxed: auto exit red after 6s attention\s*$",
)


def _dm_red_exit_is_misplaced_helpers(s: str) -> bool:
  """
  旧版 helpers 补丁锚在 driverDistracted1 行之后，会落入 elif threshold_pre 分支（更深缩进）；
  红线（awareness<=0）时该分支不执行，6 秒自动恢复形同虚设。
  正确位置：与 `if alert is not None` 同级，在整段 if/elif 分级之后。
  """
  m_sent = _RE_DM_RED_EXIT_HEADER.search(s)
  m_if_alert = re.search(r"(?m)^(?P<ind>[ \t]+)if\s+alert\s+is\s+not\s+None\s*:\s*$", s)
  if not m_sent:
    return False
  if not m_if_alert:
    return True
  return len(m_sent.group("ind")) != len(m_if_alert.group("ind"))


def _dm_red_exit_is_misplaced_policy(s: str) -> bool:
  """policy.py：red_exit 须与外层 awareness 分支同级，并位于下一方法之前。"""
  m_sent = _RE_DM_RED_EXIT_HEADER.search(s)
  m_outer = re.search(
    r"(?m)^(?P<ind>[ \t]+)if self\.awareness <= 0\.:\s*\n"
    r"(?P=ind)  # terminal alert",
    s,
  )
  if not m_sent:
    return False
  if not m_outer:
    return True
  m_next_def = re.search(r"(?m)^  def\s+\w+\(", s[m_outer.end():])
  next_def_pos = m_outer.end() + m_next_def.start() if m_next_def else len(s)
  no_face_recovery = "or (not self.face_detected and self.driver_interacting)" in s[m_sent.start():next_def_pos]
  return (
    m_sent.start() <= m_outer.end()
    or m_sent.start() >= next_def_pos
    or m_sent.group("ind") != m_outer.group("ind")
    or not no_face_recovery
  )


def _dm_red_exit_is_misplaced(s: str) -> bool:
  if _dm_is_policy_arch(s):
    return _dm_red_exit_is_misplaced_policy(s)
  return _dm_red_exit_is_misplaced_helpers(s)


_RE_DM_RED_EXIT_BLOCK = re.compile(
  r"\n(?P<ind>[ \t]+)# cn_dm_relaxed: auto exit red after 6s attention\n"
  r"(?P=ind)# allow recovery from red without disengage\.\n"
  r"(?P=ind)# If driver is clearly attentive again for <=6s, exit red state automatically\.\n"
  r"(?P=ind)if self\.awareness <= 0\.:\n"
  r"(?P=ind)  attentive = \(self\.driver_distraction_filter\.x < 0\.37 and self\.face_detected and self\.pose\.low_std\)\n"
  r"(?P=ind)  if attentive:\n"
  r"(?P=ind)    self\.red_recover_cnt \+= 1\n"
  r"(?P=ind)    if self\.red_recover_cnt \* self\.settings\._DT_DMON >= 6\.0:\n"
  r"(?P=ind)      # Move just above prompt threshold so banner clears immediately\.\n"
  r"(?P=ind)      self\.awareness = min\(1\.0, self\.threshold_prompt \+ 1e-3\)\n"
  r"(?P=ind)      self\.red_recover_cnt = 0\n"
  r"(?P=ind)      alert = None\n"
  r"(?P=ind)  else:\n"
  r"(?P=ind)    self\.red_recover_cnt = 0\n"
  r"(?P=ind)else:\n"
  r"(?P=ind)  self\.red_recover_cnt = 0\n",
  re.MULTILINE,
)


_RE_DM_RED_EXIT_BLOCK_POLICY = re.compile(
  r"\n(?P<ind>[ \t]+)# cn_dm_relaxed: auto exit red after 6s attention\n"
  r"(?P=ind)# allow recovery from red without disengage\.\n"
  r"(?P=ind)# If driver is clearly attentive again for <=6s, exit red state automatically\.\n"
  r"(?P=ind)if self\.awareness <= 0\.:\n"
  r"(?P=ind)  attentive = \(self\.driver_distraction_filter\.x < 0\.37 and self\.face_detected and self\.pose\.low_std\)\n"
  r"(?P=ind)  if attentive:\n"
  r"(?P=ind)    self\.red_recover_cnt \+= 1\n"
  r"(?P=ind)    if self\.red_recover_cnt \* DT_DMON >= 6\.0:\n"
  r"(?P=ind)      # Move just above orange threshold so banner clears immediately\.\n"
  r"(?P=ind)      self\.awareness = min\(1\.0, self\.threshold_alert_2 \+ 1e-3\)\n"
  r"(?P=ind)      self\.alert_level = AlertLevel\.two\n"
  r"(?P=ind)      self\.red_recover_cnt = 0\n"
  r"(?P=ind)  else:\n"
  r"(?P=ind)    self\.red_recover_cnt = 0\n"
  r"(?P=ind)else:\n"
  r"(?P=ind)  self\.red_recover_cnt = 0\n",
  re.MULTILINE,
)

_RE_DM_RED_EXIT_BLOCK_GENERIC = re.compile(
  r"\n(?P<ind>[ \t]+)# cn_dm_relaxed: auto exit red after 6s attention\n"
  r".*?"
  r"(?P=ind)else:\n"
  r"(?P=ind)  self\.red_recover_cnt = 0\n",
  re.MULTILINE | re.DOTALL,
)


def _dm_red_exit_inject_block_policy(ind: str) -> str:
  return (
    f"\n"
    f"{ind}# {_DM_SENTINEL_RED_EXIT}\n"
    f"{ind}# allow recovery from red without disengage.\n"
    f"{ind}# Face attention or continuous no-face driver interaction for 6s exits red.\n"
    f"{ind}if self.awareness <= 0.:\n"
    f"{ind}  attentive = ((self.driver_distraction_filter.x < 0.37 and self.face_detected and self.pose.low_std)\n"
    f"{ind}               or (not self.face_detected and self.driver_interacting))\n"
    f"{ind}  if attentive:\n"
    f"{ind}    self.red_recover_cnt += 1\n"
    f"{ind}    if self.red_recover_cnt * DT_DMON >= 6.0:\n"
    f"{ind}      # Move just above orange threshold so banner clears immediately.\n"
    f"{ind}      self.awareness = min(1.0, self.threshold_alert_2 + 1e-3)\n"
    f"{ind}      self.alert_level = AlertLevel.two\n"
    f"{ind}      self.red_recover_cnt = 0\n"
    f"{ind}  else:\n"
    f"{ind}    self.red_recover_cnt = 0\n"
    f"{ind}else:\n"
    f"{ind}  self.red_recover_cnt = 0\n"
  )


def _dm_red_exit_inject_block_helpers(ind: str) -> str:
  return (
    f"\n"
    f"{ind}# {_DM_SENTINEL_RED_EXIT}\n"
    f"{ind}# allow recovery from red without disengage.\n"
    f"{ind}# If driver is clearly attentive again for <=6s, exit red state automatically.\n"
    f"{ind}if self.awareness <= 0.:\n"
    f"{ind}  attentive = (self.driver_distraction_filter.x < 0.37 and self.face_detected and self.pose.low_std)\n"
    f"{ind}  if attentive:\n"
    f"{ind}    self.red_recover_cnt += 1\n"
    f"{ind}    if self.red_recover_cnt * self.settings._DT_DMON >= 6.0:\n"
    f"{ind}      # Move just above prompt threshold so banner clears immediately.\n"
    f"{ind}      self.awareness = min(1.0, self.threshold_prompt + 1e-3)\n"
    f"{ind}      self.red_recover_cnt = 0\n"
    f"{ind}      alert = None\n"
    f"{ind}  else:\n"
    f"{ind}    self.red_recover_cnt = 0\n"
    f"{ind}else:\n"
    f"{ind}  self.red_recover_cnt = 0\n"
  )


def _strip_dm_red_exit_block(s: str) -> str:
  s2 = _RE_DM_RED_EXIT_BLOCK.sub("", s, count=1)
  if s2 != s:
    return s2
  s2 = _RE_DM_RED_EXIT_BLOCK_POLICY.sub("", s, count=1)
  if s2 != s:
    return s2
  return _RE_DM_RED_EXIT_BLOCK_GENERIC.sub("", s, count=1)


def _ensure_dm_red_recover_counter(s: str, path_for_err: Path) -> str:
  if "self.red_recover_cnt" in s:
    return s
  m_init = re.search(r"(?m)^(?P<ind>[ \t]+)self\.terminal_time\s*=\s*0\s*$", s)
  if not m_init:
    m_init = re.search(r"(?m)^(?P<ind>[ \t]+)self\.alert_3_cnt\s*=\s*0\s*$", s)
  if not m_init:
    m_init = re.search(r"(?m)^(?P<ind>[ \t]+)self\.step_change\s*=\s*0\.\s*$", s)
  if not m_init:
    raise RuntimeError(
      f"{path_for_err}: 未找到 terminal_time/alert_3_cnt/step_change 初始化行，无法注入 red_exit 计数器"
    )
  ind = m_init.group("ind")
  insert_line = f"{ind}self.red_recover_cnt = 0  # {_DM_SENTINEL_RED_EXIT}\n"
  return s[: m_init.end()] + "\n" + insert_line + s[m_init.end() + 1 :]


def _patch_dm_red_exit_helpers(s: str, path_for_err: Path) -> str:
  m_events_add = re.search(r"(?m)^(?P<ind>[ \t]+)if\s+alert\s+is\s+not\s+None\s*:\s*$", s)
  if not m_events_add:
    raise RuntimeError(
      f"{path_for_err}: 未找到 `if alert is not None:`，无法注入 red_exit 逻辑（上游可能重构）"
    )
  ind = m_events_add.group("ind")
  inject = _dm_red_exit_inject_block_helpers(ind)
  return s[: m_events_add.start()] + inject + s[m_events_add.start() :]


def _patch_dm_red_exit_policy(s: str, path_for_err: Path) -> str:
  m_outer = re.search(
    r"(?m)^(?P<ind>[ \t]+)if self\.awareness <= 0\.:\s*\n"
    r"(?P=ind)  # terminal alert",
    s,
  )
  if not m_outer:
    raise RuntimeError(
      f"{path_for_err}: 未找到 awareness 红屏外层分支，无法注入 red_exit 逻辑（policy.py 可能重构）"
    )
  m_next_def = re.search(r"(?m)^  def\s+\w+\(", s[m_outer.end():])
  if not m_next_def:
    raise RuntimeError(f"{path_for_err}: 未找到 awareness 分支后的下一方法，无法安全注入 red_exit")
  insert_at = m_outer.end() + m_next_def.start()
  ind = m_outer.group("ind")
  inject = _dm_red_exit_inject_block_policy(ind)
  return s[:insert_at].rstrip() + "\n" + inject + "\n" + s[insert_at:].lstrip("\n")


def _patch_dm_red_exit_flexible(s: str, path_for_err: Path) -> str:
  """
  红色告警（awareness<=0）默认会“粘住”，需要 disengage 才能恢复。
  国内化补丁：检测到人脸专注，或无人脸时持续驾驶交互满 6 秒，自动退出红色告警。
  幂等：sentinel 已在正确位置则跳过；旧版误嵌 elif 分支则先剥离再重插。
  """
  if _DM_SENTINEL_RED_EXIT in s and not _dm_red_exit_is_misplaced(s):
    return s

  if _RE_DM_RED_EXIT_HEADER.search(s):
    s = _strip_dm_red_exit_block(s)
    if _RE_DM_RED_EXIT_HEADER.search(s):
      raise RuntimeError(
        f"{path_for_err}: red_exit 补丁块无法剥离（上游可能改写注释/缩进），请人工合并。"
      )

  s = _ensure_dm_red_recover_counter(s, path_for_err)
  if _dm_is_policy_arch(s):
    return _patch_dm_red_exit_policy(s, path_for_err)
  return _patch_dm_red_exit_helpers(s, path_for_err)


def _patch_controlsd_no_dm_force_decel(root: Path, res: PatchResult) -> None:
  """
  staging 上游用 DM 红屏/无响应置 forceDecel → 纵向收油。
  国内化：仅保留 softDisabling 路径，DM 红屏仍告警但不 forceDecel。
  """
  path = cn_path(root, "selfdrive/controls/controlsd.py")
  if not path.is_file():
    return
  s = path.read_text(encoding="utf-8")
  if "cs.forceDecel" not in s:
    raise RuntimeError(f"{path}: 未找到 cs.forceDecel 赋值，上游可能已重构")

  soft_only = (
    f"cs.forceDecel = bool(self.sm['selfdriveState'].state == State.softDisabling)"
    f"  # {_DM_SENTINEL_NO_FORCE_DECEL}"
  )
  dm_escalation_expr: str | None = None
  matcher_exprs = (
    (
      _RE_CONTROLS_FORCE_DECEL_NO_RESPONSE,
      "self.sm['driverMonitoringState'].noResponseForceDecel",
    ),
    (
      _RE_CONTROLS_FORCE_DECEL_WITH_DM,
      "(self.sm['driverMonitoringState'].alertLevel == log.DriverMonitoringState.AlertLevel.three)",
    ),
    (
      _RE_CONTROLS_FORCE_DECEL_AWARENESS,
      "(self.sm['driverMonitoringState'].awarenessStatus < 0.)",
    ),
  )
  for rx, escalation_expr in matcher_exprs:
    if rx.search(s):
      s2, n = rx.subn(soft_only, s, count=1)
      if n != 1:
        raise RuntimeError(f"{path}: forceDecel 替换未生效")
      s = s2
      dm_escalation_expr = escalation_expr
      break
  else:
    if _DM_SENTINEL_NO_FORCE_DECEL not in s and re.search(
    r"cs\.forceDecel\s*=\s*bool\s*\(\s*self\.sm\['selfdriveState'\]\.state\s*==\s*State\.softDisabling\s*\)",
    s,
    ):
      s = re.sub(
        r"(cs\.forceDecel\s*=\s*bool\s*\(\s*self\.sm\['selfdriveState'\]\.state\s*==\s*State\.softDisabling\s*\)\s*)",
        rf"\1  # {_DM_SENTINEL_NO_FORCE_DECEL}",
        s,
        count=1,
      )
    elif _DM_SENTINEL_NO_FORCE_DECEL not in s:
      raise RuntimeError(
        f"{path}: 无法匹配 DM → forceDecel 赋值块"
        "（alertLevel.three / noResponseForceDecel / awarenessStatus）；请人工对照 controlsd.publish"
      )

  # forceDecel 仅控制纵向减速；保留原厂 driverMonitoringEscalation 行为。
  if "cn_dm_relaxed: preserve stock DM escalation" not in s:
    if dm_escalation_expr is None:
      policy = _dm_monitoring_path(root)
      policy_text = policy.read_text(encoding="utf-8", errors="replace") if policy else ""
      if "noResponseForceDecel" in policy_text:
        dm_escalation_expr = "self.sm['driverMonitoringState'].noResponseForceDecel"
      elif "AlertLevel.three" in policy_text:
        dm_escalation_expr = "(self.sm['driverMonitoringState'].alertLevel == log.DriverMonitoringState.AlertLevel.three)"
    if dm_escalation_expr and "CC.driverMonitoringEscalation = cs.forceDecel" in s:
      s = s.replace(
        "CC.driverMonitoringEscalation = cs.forceDecel",
        f"CC.driverMonitoringEscalation = bool({dm_escalation_expr} or cs.forceDecel)"
        "  # cn_dm_relaxed: preserve stock DM escalation",
        1,
      )

  _track_change(res, path, write_if_changed(path, s))


def patch_dm_relaxed_terminal(root: Path) -> PatchResult:
  """
  国内化 DM：保留分级告警/鸣音，但削弱「第三次终端锁死」与红屏纵向 forceDecel。
  - awareness 递减下限由 -0.1 改为 0
  - 红线阶段不再累计 terminal_time / terminal_alert_cnt，避免 DriverTooDistracted / tooDistracted 路径
  - 红屏专注恢复约 6 秒自动退出（red_recover_cnt）
  - controlsd：红屏 alertLevel.three 不再置 forceDecel（仅 softDisabling 仍 forceDecel）
  目标：policy.py（staging）或 helpers.py（旧架构）+ selfdrive/controls/controlsd.py
  幂等：各 sentinel 已存在则跳过对应子补丁。
  """
  res = PatchResult("dm_relaxed_terminal")
  dm_path = _dm_monitoring_path(root)
  if dm_path is not None:
    s = dm_path.read_text(encoding="utf-8")
    monitoring_done = (
      _DM_SENTINEL_AWARENESS in s
      and _DM_SENTINEL_TERMINAL in s
      and _RE_DM_RED_EXIT_HEADER.search(s)
      and not _dm_red_exit_is_misplaced(s)
      and (not _dm_is_policy_arch(s) or _DM_SENTINEL_ESCALATION_TIMER in s)
    )
    if not monitoring_done:
      s = _patch_dm_awareness_flexible(s, dm_path)
      s = _patch_dm_terminal_flexible(s, dm_path)
      s = _ensure_dm_policy_escalation_timer(s, dm_path)
      s = _patch_dm_red_exit_flexible(s, dm_path)
      changed = write_if_changed(dm_path, s)
      _track_change(res, dm_path, changed)

  _patch_controlsd_no_dm_force_decel(root, res)
  return res


def patch_dm_relaxed_tests(root: Path) -> PatchResult:
  """同步调整上游 DM 测试，并覆盖有人脸/无人脸两种红屏自动恢复。"""
  res = PatchResult("dm_relaxed_tests")
  path = cn_path(root, "selfdrive/monitoring/test_monitoring.py")
  if not path.is_file():
    return res
  s = path.read_text(encoding="utf-8")
  s2 = s.replace(
    "  # engaged, distracted past red and beyond the no-response window -> unavailability response + lockout\n"
    "  def test_distracted_lockout(self):\n",
    "  # engaged and distracted past red: alert remains, but no persistent lockout is accumulated\n"
    "  def test_distracted_no_lockout(self):\n",
  )
  s2 = s2.replace(
    "  # no face -> wheeltouch red, sustained past the no-response timeout -> unavailability response + lockout\n"
    "  def test_invisible_lockout(self):\n",
    "  # no face -> wheeltouch red: alert remains, but no persistent lockout is accumulated\n"
    "  def test_invisible_no_lockout(self):\n",
  )
  s2 = s2.replace(
    "    assert d_status.lockout_active\n"
    "    assert d_status.lockout_time_elapsed > 0\n"
    "    assert d_status.lockout_count >= 1\n",
    "    # cn_dm_relaxed: terminal strikes do not create a persistent lockout\n"
    "    assert not d_status.lockout_active\n"
    "    assert d_status.alert_3_cnt == 0\n"
    "    assert d_status.no_response_cnt == 0\n",
    1,
  )
  s2 = s2.replace(
    "    assert d_status.lockout_active\n"
    "    assert d_status.lockout_count >= 1\n",
    "    # cn_dm_relaxed: no-face red alert does not create a persistent lockout\n"
    "    assert not d_status.lockout_active\n"
    "    assert d_status.alert_3_cnt == 0\n"
    "    assert d_status.no_response_cnt == 0\n",
    1,
  )
  if "test_cn_red_recovery_with_face" not in s2:
    anchor = "  # engaged, down to orange, driver pays attention, back to normal; then down to orange, driver touches wheel\n"
    tests = (
      "  # cn_dm_relaxed: red exits after 6s of clear face attention\n"
      "  def test_cn_red_recovery_with_face(self):\n"
      "    red = [msg_DISTRACTED] * int(DISTRACTED_SECONDS_TO_RED / DT_DMON)\n"
      "    recovery = [msg_ATTENTIVE] * int(7 / DT_DMON)\n"
      "    alert_lvls, d_status = self._run_seq(red + recovery, always_false[:len(red + recovery)],\n"
      "                                          always_true[:len(red + recovery)], always_false[:len(red + recovery)])\n"
      "    assert log.DriverMonitoringState.AlertLevel.three in alert_lvls\n"
      "    assert d_status.awareness > 0.\n"
      "    assert d_status.alert_level != log.DriverMonitoringState.AlertLevel.three\n"
      "\n"
      "  # cn_dm_relaxed: no-face wheeltouch red exits after 6s of continuous driver interaction\n"
      "  def test_cn_red_recovery_without_face(self):\n"
      "    red = [msg_NO_FACE_DETECTED] * int(INVISIBLE_SECONDS_TO_RED / DT_DMON)\n"
      "    recovery = [msg_NO_FACE_DETECTED] * int(7 / DT_DMON)\n"
      "    interactions = [False] * len(red) + [True] * len(recovery)\n"
      "    msgs = red + recovery\n"
      "    alert_lvls, d_status = self._run_seq(msgs, interactions, always_true[:len(msgs)], always_false[:len(msgs)])\n"
      "    assert log.DriverMonitoringState.AlertLevel.three in alert_lvls\n"
      "    assert d_status.awareness > 0.\n"
      "    assert d_status.alert_level != log.DriverMonitoringState.AlertLevel.three\n"
      "\n"
    )
    if anchor not in s2:
      raise RuntimeError(f"{path}: 未找到 test_normal_driver 锚点，无法注入 DM 恢复测试")
    s2 = s2.replace(anchor, tests + anchor, 1)
  _track_change(res, path, write_if_changed(path, s2))
  return res


def patch_gitmodules(root: Path) -> PatchResult:
  res = PatchResult("gitmodules_urls")
  gitmodules = root / ".gitmodules"
  if not gitmodules.exists():
    return res
  gm = gitmodules.read_text(encoding="utf-8")
  teleop_old = list(dict.fromkeys(
    _with_git_url_variants("https://github.com/commaai/teleoprtc.git")
    + ["https://github.com/commaai/teleoprtc"],
  ))
  gm2 = apply_text_replacement_rows(
    gm,
    [
      (_with_git_url_variants("https://github.com/commaai/msgq.git"), gitee_git_ssh_repo("msgq")),
      (_with_git_url_variants("https://github.com/sunnypilot/opendbc.git"), gitee_git_ssh_repo("opendbc")),
      (_with_git_url_variants("https://github.com/commaai/rednose.git"), gitee_git_ssh_repo("rednose")),
      (teleop_old, gitee_git_ssh_repo("teleoprtc")),
      (_with_git_url_variants("https://github.com/sunnypilot/tinygrad.git"), gitee_git_ssh_repo("tinygrad")),
      (_with_git_url_variants("https://github.com/sunnyhaibin/panda.git"), gitee_git_ssh_repo("panda")),
      (_with_git_url_variants("https://github.com/sunnypilot/neural-network-data.git"), gitee_git_ssh_repo("neural_network_data")),
    ],
    path=gitmodules,
    require_all=False,
  )
  _track_change(res, gitmodules, write_if_changed(gitmodules, gm2))
  return res


_CN_DM_WARP_COMPILE_MISSING = "cn_dm_warp_compile_missing"

_CN_ENSURE_DM_WARP_FN = '''
def _cn_ensure_dm_warp(cam_w: int, cam_h: int):
  # {sentinel}: git OTA has no SCons dm_warp pkl; compile once on device
  warp_path = MODELS_DIR / f'dm_warp_{{cam_w}}x{{cam_h}}_tinygrad.pkl'
  if warp_path.is_file() and warp_path.stat().st_size > 0:
    with open(warp_path, "rb") as f:
      return pickle.load(f)
  cloudlog.warning(f"compiling missing DM warp {{warp_path.name}} (first onroad after git OTA)")
  from openpilot.common.transformations.model import DM_INPUT_SIZE
  from openpilot.selfdrive.modeld.compile_dm_warp import compile_dm_warp
  from openpilot.selfdrive.modeld.compile_modeld import NV12Frame
  nv12 = NV12Frame(cam_w, cam_h, *get_nv12_info(cam_w, cam_h))
  tmp_path = warp_path.with_name(warp_path.name + ".tmp")
  try:
    compile_dm_warp(nv12, DM_INPUT_SIZE[0], DM_INPUT_SIZE[1], str(tmp_path))
    os.replace(tmp_path, warp_path)
  finally:
    try:
      os.unlink(tmp_path)
    except FileNotFoundError:
      pass
  with open(warp_path, "rb") as f:
    return pickle.load(f)

'''


def patch_dm_warp_compile_missing(root: Path) -> PatchResult:
  """
  上游 dmonitoringmodeld 改为加载 SCons/CI 编好的 dm_warp_WxH_tinygrad.pkl。
  官方刷机有该文件；Gitee git OTA 没有。缺失时在设备上调用 compile_dm_warp 编一次。
  旧树尚未读该 pkl 时跳过。幂等：sentinel 已在则跳过。
  """
  res = PatchResult("dm_warp_compile_missing")
  path = cn_path(root, "selfdrive/modeld/dmonitoringmodeld.py")
  if not path.is_file():
    return res
  s = path.read_text(encoding="utf-8")
  if _CN_DM_WARP_COMPILE_MISSING in s:
    return res
  if "dm_warp_" not in s:
    return res
  compile_py = cn_path(root, "selfdrive/modeld/compile_dm_warp.py")
  if not compile_py.is_file():
    raise RuntimeError(
      f"{path}: 已引用 dm_warp_*.pkl 但缺少 {compile_py}，无法注入 {_CN_DM_WARP_COMPILE_MISSING}"
    )
  m = re.search(
    r"METADATA_PATH = MODELS_DIR / ['\"]dmonitoring_model_metadata\.pkl['\"]\n",
    s,
  )
  if not m:
    raise RuntimeError(f"{path}: 未找到 METADATA_PATH，无法注入 {_CN_DM_WARP_COMPILE_MISSING}")
  helper = _CN_ENSURE_DM_WARP_FN.format(sentinel=_CN_DM_WARP_COMPILE_MISSING)
  s = s[: m.end()] + helper + s[m.end() :]
  s2, n = re.subn(
    r"(?P<ind>[ \t]*)with open\(MODELS_DIR / f['\"]dm_warp_\{(?P<w>[^}]+)\}x\{(?P<h>[^}]+)\}_tinygrad\.pkl['\"], ['\"]rb['\"]\) as f:\n"
    r"(?P=ind)  self\.image_warp = pickle\.load\(f\)",
    r"\g<ind>self.image_warp = _cn_ensure_dm_warp(\g<w>, \g<h>)",
    s,
    count=1,
  )
  if n == 0:
    s2, n = re.subn(
      r"(?P<ind>[ \t]*)warp_path = MODELS_DIR / f['\"]dm_warp_\{(?P<w>[^}]+)\}x\{(?P<h>[^}]+)\}_tinygrad\.pkl['\"]\n"
      r"(?P=ind)with open\(warp_path, ['\"]rb['\"]\) as f:\n"
      r"(?P=ind)  self\.image_warp = pickle\.load\(f\)",
      r"\g<ind>self.image_warp = _cn_ensure_dm_warp(\g<w>, \g<h>)",
      s,
      count=1,
    )
  if n == 0:
    raise RuntimeError(
      f"{path}: 未找到 dm_warp pickle.load，无法注入 {_CN_DM_WARP_COMPILE_MISSING}"
    )
  _track_change(res, path, write_if_changed(path, s2))
  return res


_CN_MANAGER_RESTART_DEAD = "cn_manager_restart_dead"


def patch_manager_restart_dead(root: Path) -> PatchResult:
  """
  manager 发现应运行进程已退出时自动 stop + start。
  上游 start() 在 proc 对象仍在时直接 return，micd/soundd 等 get_stream 失败后
  会一直 Process Not Running，直到 offroad。幂等：sentinel 已在则跳过。
  """
  res = PatchResult("manager_restart_dead")
  path = cn_path(root, "system/manager/process.py")
  if not path.is_file():
    raise RuntimeError(f"{path}: 缺少 system/manager/process.py，无法注入进程死后重启")
  s = path.read_text(encoding="utf-8")
  if _CN_MANAGER_RESTART_DEAD in s:
    return res
  old = (
    "  for p in running:\n"
    "    p.start()\n"
    "\n"
    "  return running\n"
  )
  new = (
    "  for p in running:\n"
    f"    # {_CN_MANAGER_RESTART_DEAD}: crashed procs stay in proc= until offroad; restart them\n"
    "    if p.proc is not None and not p.proc.is_alive():\n"
    "      cloudlog.error(f\"restarting dead process {p.name} (exit {p.proc.exitcode})\")\n"
    "      p.stop()\n"
    "    p.start()\n"
    "\n"
    "  return running\n"
  )
  if old not in s:
    raise RuntimeError(
      f"{path}: 未找到 ensure_running 的 start 循环，无法注入 {_CN_MANAGER_RESTART_DEAD}"
    )
  _track_change(res, path, write_if_changed(path, s.replace(old, new, 1)))
  return res


def patch_all(root: Path) -> list[PatchResult]:
  patches = [
    patch_main_repo_cn_routing,
    patch_version_py,
    patch_updated_insteadof,
    patch_agnos_ota_compat,
    patch_mici_home_repo_suffix,
    patch_tici_setup,
    patch_mici_setup,
    patch_setup_sh,
    patch_msgq_setup,
    patch_opendbc_pyproject,
    patch_models_fetcher,
    patch_tinygrad_vendored_align,
    patch_osm,
    patch_mapd_installer,
    patch_software_update_confirm,
    patch_dm_relaxed_terminal,
    patch_dm_relaxed_tests,
    patch_dm_warp_compile_missing,
    patch_manager_restart_dead,
    patch_gitmodules,
  ]
  results: list[PatchResult] = []
  for fn in patches:
    r = fn(root)
    results.append(r)
  return results


def _verify_python_syntax(root: Path, rel_paths: list[str]) -> list[str]:
  out: list[str] = []
  for rel in rel_paths:
    p = root / rel
    if not p.exists():
      continue
    src = p.read_text(encoding="utf-8", errors="replace")
    try:
      ast.parse(src, filename=str(p))
    except SyntaxError as e:
      out.append(f"{rel}: ast.parse 失败: {e}")
  return out


def _verify_no_debug_markers(root: Path, rel_paths: list[str]) -> list[str]:
  needles = ("breakpoint()", "pdb.set_trace()", "# SYNC_DEBUG")
  out: list[str] = []
  for rel in rel_paths:
    p = root / rel
    if not p.exists():
      continue
    tx = p.read_text(encoding="utf-8", errors="replace")
    for n in needles:
      if n in tx:
        out.append(f"{rel}: 含调试标记 {n!r}，禁止推送半成品")
  return out


def _skip_tinygrad_models_verify() -> bool:
  return (os.environ.get("VERIFY_TINYGRAD_MODELS") or "").strip().lower() in ("0", "false", "no", "skip")


def _parse_models_json_url(root: Path) -> str | None:
  fetcher = cn_path(root, "sunnypilot/models/fetcher.py")
  if not fetcher.is_file():
    return None
  m = re.search(r'MODEL_URL\s*=\s*"([^"]+)"', fetcher.read_text(encoding="utf-8"))
  if not m:
    return None
  return _normalize_models_json_url(m.group(1).strip())


def _normalize_models_json_url(url: str) -> str:
  return _fix_models_json_url_typos(url.strip())


def _models_json_url_candidates(primary: str | None) -> list[str]:
  out: list[str] = []

  def add(u: str | None) -> None:
    if not u:
      return
    u = _normalize_models_json_url(u.strip())
    if u not in out:
      out.append(u)

  add(primary)
  primary_name = _extract_model_json_basename(primary) if primary else ""
  for name in _models_json_version_fallbacks(primary_name)[1:]:
    add(_expected_models_json_url(name))
  add(f"{gitee_models_raw_gh_pages()}docs/driving_models_v19.json")
  add(f"{gitee_models_raw_gh_pages()}docs/driving_models_v17.json")
  allow_gh = (os.environ.get("VERIFY_TINYGRAD_ALLOW_GITHUB_MODELS") or "").strip().lower()
  if allow_gh in ("1", "true", "yes", "on"):
    add(
      "https://raw.githubusercontent.com/sunnypilot/sunnypilot-models/"
      "refs/heads/gh-pages/docs/driving_models_v17.json"
    )
  return out


def _fetch_models_tinygrad_ref(url: str, *, timeout_s: int = 25) -> str | None:
  try:
    req = urllib.request.Request(url, headers={"User-Agent": "spsync-verify-tinygrad/1"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
      payload = json.loads(resp.read().decode("utf-8"))
    ref = payload.get("tinygrad_ref")
    if not ref:
      return None
    ref_s = str(ref).strip().lower()
    return _git_sha40(ref_s)
  except urllib.error.HTTPError as e:
    if e.code != 404:
      log("verify-tinygrad", f"拉取 models JSON 失败 ({url}): {e}")
    return None
  except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
    log("verify-tinygrad", f"拉取 models JSON 失败 ({url}): {e}")
    return None


def _fetch_models_tinygrad_ref_candidates(urls: list[str], *, timeout_s: int = 25) -> tuple[str | None, str | None]:
  for url in urls:
    ref = _fetch_models_tinygrad_ref(url, timeout_s=timeout_s)
    if ref:
      return ref, url
  return None, urls[0] if urls else None


def _require_tinygrad_pin_match() -> bool:
  """为 true 时 models JSON tinygrad_ref 须与 TINYGRAD_MODELS_REF 一致（默认仅 warn）。"""
  return (os.environ.get("SYNC_TINYGRAD_REQUIRE_PIN_MATCH") or "").strip().lower() in ("1", "true", "yes", "on")


def _resolve_tinygrad_models_ref(
  root: Path,
  *,
  offline_ok: bool = True,
) -> tuple[str | None, str, str | None]:
  """
  确定 tinygrad 对齐/校验用的 commit。
  优先 models JSON tinygrad_ref；失败且 offline_ok 时回退 TINYGRAD_MODELS_REF。
  返回 (commit, source_label, models_json_url_used)。
  """
  pin = TINYGRAD_MODELS_REF.lower()

  if (os.environ.get("SYNC_TINYGRAD_ALIGN_PIN_ONLY") or "").strip().lower() in ("1", "true", "yes", "on"):
    return pin, "TINYGRAD_MODELS_REF (SYNC_TINYGRAD_ALIGN_PIN_ONLY)", None

  if (os.environ.get("VERIFY_TINYGRAD_SKIP_JSON_FETCH") or "").strip().lower() in ("1", "true", "yes", "on"):
    return pin, "TINYGRAD_MODELS_REF (VERIFY_TINYGRAD_SKIP_JSON_FETCH)", None

  models_url = _parse_models_json_url(root)
  candidates = _models_json_url_candidates(models_url)
  ref, url_used = _fetch_models_tinygrad_ref_candidates(candidates)
  if ref:
    if ref != pin:
      log(
        "tinygrad",
        f"对齐/校验目标自 models JSON: {ref[:7]}（脚本 pin {pin[:7]}，建议同步更新 TINYGRAD_MODELS_REF）",
      )
    return ref, "models JSON tinygrad_ref", url_used

  if offline_ok:
    log(
      "warn",
      f"无法拉取 models JSON tinygrad_ref，回退 TINYGRAD_MODELS_REF={pin[:7]}",
    )
    return pin, "TINYGRAD_MODELS_REF (JSON 不可用回退)", models_url

  return None, "unavailable", models_url


def _git_ls_tree_entry(root: Path, treeish: str, subpath: str) -> tuple[str, str] | None:
  """返回 ls-tree 条目 (mode, sha)，mode 如 commit / tree / blob。"""
  try:
    line = run(["git", "ls-tree", treeish, subpath], str(root)).strip()
  except RuntimeError:
    return None
  if not line:
    return None
  parts = line.split()
  if len(parts) < 3:
    return None
  sha = _git_sha40(parts[2])
  if not sha:
    return None
  return parts[1], sha


def _git_ls_tree_submodule_sha(root: Path, treeish: str, subpath: str) -> str | None:
  ent = _git_ls_tree_entry(root, treeish, subpath)
  if ent and ent[0] == "commit":
    return ent[1]
  return None


_tinygrad_commit_root_tree_cache: dict[str, str | None] = {}


def _tinygrad_commit_root_tree(commit: str) -> str | None:
  """tinygrad commit 的根 tree SHA（用于与 superproject 内 vendored tinygrad_repo tree 比对）。"""
  commit = commit.strip().lower()
  if commit in _tinygrad_commit_root_tree_cache:
    return _tinygrad_commit_root_tree_cache[commit]
  if not re.fullmatch(r"[0-9a-f]{40}", commit):
    _tinygrad_commit_root_tree_cache[commit] = None
    return None

  tree: str | None = None
  with tempfile.TemporaryDirectory() as td:
    run(["git", "init"], td)
    for url in _tinygrad_fetch_remotes():
      try:
        _tinygrad_git(["git", "remote", "remove", "origin"], td)
      except RuntimeError:
        pass
      try:
        _tinygrad_git(["git", "remote", "add", "origin", url], td)
        _tinygrad_git(["git", "fetch", "--depth", "1", "origin", commit], td)
        got = _tinygrad_git(["git", "rev-parse", "FETCH_HEAD^{tree}"], td).strip().lower()
        if re.fullmatch(r"[0-9a-f]{40}", got):
          tree = got
          break
      except RuntimeError:
        continue

  _tinygrad_commit_root_tree_cache[commit] = tree
  return tree


def _match_vendored_tinygrad_tree(
  vendored_tree: str,
  *,
  models_ref: str | None = None,
) -> tuple[str | None, str]:
  """将 superproject 内 vendored tree SHA 映射到 tinygrad commit（比对 commit 根 tree）。"""
  candidates: list[tuple[str, str]] = []
  if models_ref and re.fullmatch(r"[0-9a-f]{40}", models_ref):
    candidates.append((models_ref.lower(), "models JSON tinygrad_ref"))
  pin = TINYGRAD_MODELS_REF.lower()
  if pin not in {c[0] for c in candidates}:
    candidates.append((pin, "TINYGRAD_MODELS_REF"))

  for commit, label in candidates:
    root_tree = _tinygrad_commit_root_tree(commit)
    if root_tree and root_tree == vendored_tree:
      return commit, f"vendored tree 匹配 {label} ({commit[:12]})"

  ref_note = ""
  if models_ref and re.fullmatch(r"[0-9a-f]{40}", models_ref):
    models_tree = _tinygrad_commit_root_tree(models_ref.lower())
    if models_tree:
      ref_note = f"models tinygrad_ref 根 tree={models_tree[:12]}… ≠ vendored {vendored_tree[:12]}…。"

  return None, (
    f"tinygrad_repo 为 vendored tree ({vendored_tree[:12]}…)，与 models JSON tinygrad_ref 不一致。"
    f"{ref_note}"
    "upstream staging 亦可能为 vendored tree（非 submodule gitlink）。"
    "推送后 ModelManager 下载模型可能 unpickle 失败（Speed Error: nan m/s）；"
    f"需 OTA 将 tinygrad 对齐到 {TINYGRAD_MODELS_REF[:7]}，或用当前 tinygrad 重编 GitLab 模型并更新 JSON。"
  )


def resolve_workdir_tinygrad_commit(root: Path, *, models_ref: str | None = None) -> tuple[str | None, str]:
  """解析 workdir 内 tinygrad_repo 当前 commit（checkout / gitlink / vendored tree 映射）。"""
  root = root.resolve()
  tg = root / "tinygrad_repo"
  if not tg.is_dir():
    return None, "tinygrad_repo 目录不存在"

  if (tg / ".git").exists():
    try:
      sha = run(["git", "-C", str(tg), "rev-parse", "HEAD"], str(root)).strip().lower()
      if re.fullmatch(r"[0-9a-f]{40}", sha):
        return sha, "tinygrad_repo checkout HEAD"
    except RuntimeError:
      pass

  sha = _git_ls_tree_submodule_sha(root, "HEAD", "tinygrad_repo")
  if sha:
    return sha, "git ls-tree HEAD tinygrad_repo (submodule gitlink)"

  try:
    st = run(["git", "submodule", "status", "tinygrad_repo"], str(root)).strip()
    m = re.search(r"([0-9a-f]{40})", st)
    if m:
      return m.group(1).lower(), "git submodule status tinygrad_repo"
  except RuntimeError:
    pass

  head_ent = _git_ls_tree_entry(root, "HEAD", "tinygrad_repo")
  if head_ent and head_ent[0] == "tree":
    matched, detail = _match_vendored_tinygrad_tree(head_ent[1], models_ref=models_ref)
    if matched:
      return matched, detail
    marker = _read_vendored_tinygrad_marker(tg)
    if marker:
      pin = TINYGRAD_MODELS_REF.lower()
      want = {pin}
      if models_ref and _git_sha40(models_ref):
        want.add(models_ref.lower())
      if marker in want:
        marker_tree = _tinygrad_commit_root_tree(marker)
        if marker_tree and marker_tree == head_ent[1]:
          return marker, f"vendored tree 匹配 .sp_tinygrad_vendored_ref ({marker[:12]})"
        if marker_tree:
          return marker, (
            f"vendored 已对齐 {marker[:12]}（.sp_tinygrad_vendored_ref，工作区待 commit；"
            f"HEAD tree 仍为 {head_ent[1][:12]}…）"
          )
    return None, detail

  # 旧式 submodule 指针（upstream 仍可能为 gitlink）
  for treeish, label in (("upstream/staging", "upstream/staging"), ("origin/staging", "origin/staging")):
    sha = _git_ls_tree_submodule_sha(root, treeish, "tinygrad_repo")
    if sha:
      return sha, f"git ls-tree {label} tinygrad_repo（HEAD 非 gitlink 时用 upstream 指针）"

  return None, "无法解析 tinygrad_repo commit（请先 pull / git submodule update --init tinygrad_repo）"


def collect_tinygrad_models_verify_errors(root: Path) -> list[str]:
  """
  校验 staging workdir 的 tinygrad_repo 与 models JSON tinygrad_ref 一致。
  不一致时 OTA 上 ModelManager 下载模型 JIT unpickle 失败 → speed error nan m/s。
  """
  if _skip_tinygrad_models_verify():
    return []

  errors: list[str] = []

  models_url = _parse_models_json_url(root)
  if not models_url:
    errors.append("sunnypilot/models/fetcher.py: 无法解析 MODEL_URL")

  models_ref, ref_source, models_url_used = _resolve_tinygrad_models_ref(root, offline_ok=True)
  if not models_ref:
    tried = "\n      ".join(_models_json_url_candidates(models_url))
    errors.append(
      "无法确定 tinygrad_ref（models JSON 与 TINYGRAD_MODELS_REF 回退均失败），已尝试：\n      "
      f"{tried}\n      若 MODEL_URL 含 raw/master/refs/heads/gh-pages，请先菜单 1 pull 修复 fetcher 补丁"
    )

  workdir_sha, workdir_src = resolve_workdir_tinygrad_commit(root, models_ref=models_ref)

  if models_ref and models_ref != TINYGRAD_MODELS_REF.lower():
    pin_msg = (
      f"models JSON tinygrad_ref ({models_ref[:12]}…) 与 sync 脚本 TINYGRAD_MODELS_REF "
      f"({TINYGRAD_MODELS_REF[:12]}…) 不一致；请同步更新 ensure_tinygrad 镜像 pin 与 JSON"
    )
    if _require_tinygrad_pin_match():
      errors.append(pin_msg)
    else:
      log("warn", pin_msg + "（对齐跟 JSON，默认非阻塞；设 SYNC_TINYGRAD_REQUIRE_PIN_MATCH=1 可强制失败）")

  if not workdir_sha:
    errors.append(f"workdir tinygrad: {workdir_src}")
  elif models_ref and workdir_sha != models_ref:
    errors.append(
      "tinygrad_repo 与 models JSON tinygrad_ref 不一致："
      f"workdir={workdir_sha} ({workdir_src}) ≠ models tinygrad_ref={models_ref}。"
      "推送此 staging 后，ModelManager 下载模型会在设备上 unpickle 失败（Speed Error: nan m/s）；"
      "Default CD210 仍可用（固件自带 pkl 与 OTA tinygrad 同链编译）。"
      f"修复：将 tinygrad_repo 对齐到 {models_ref[:7]}，或用当前 tinygrad 重编 GitLab recompiled 模型并更新 JSON tinygrad_ref。"
    )

  if models_url_used and models_url_used != models_url:
    log("verify-tinygrad", f"models JSON 自 {models_url_used}")

  if not errors and workdir_sha and models_ref:
    log(
      "verify-tinygrad",
      f"OK tinygrad_repo={workdir_sha[:7]} target={models_ref[:7]} ({ref_source}) pin={TINYGRAD_MODELS_REF[:7]}",
    )
  return errors


def verify_tinygrad_models_alignment(root: Path) -> None:
  errors = collect_tinygrad_models_verify_errors(root)
  if errors:
    raise RuntimeError("verify_tinygrad_models 失败（不会提交/推送）：\n  - " + "\n  - ".join(errors))


def verify_patches(root: Path) -> None:
  """
  补丁后的门禁校验：失败则阻止提交/推送，避免半成品国内化进入 Gitee。
  与 patch_* 中的 replace_or_fail 互补（后者对缺字符串硬失败；此处捕获条件补丁未生效的情况）。
  """
  errors: list[str] = []

  def rpath(rel: str) -> str:
    return cn_rel(root, rel)

  def rt(rel: str) -> str:
    p = root / rpath(rel)
    if not p.exists():
      return ""
    return p.read_text(encoding="utf-8", errors="replace")

  gitee = main_repo_source_gitee()
  codeup = main_repo_source_codeup()

  route_rel = rpath("system/cn_main_repo_route.py")
  route_tx = rt("system/cn_main_repo_route.py")
  if _CN_MAIN_REPO_ROUTE_SENTINEL not in route_tx:
    errors.append(f"{route_rel}: 缺少路由模块或 sentinel")
  else:
    for fn in ("ensure_codeup_ssh_key", "codeup_ssh_identity_file", "prepare_main_repo_git", "main_repo_ui_suffix"):
      if f"def {fn}" not in route_tx:
        errors.append(f"{route_rel}: 缺少 {fn}")
    if codeup.ssh_url not in route_tx or gitee.https_url not in route_tx:
      errors.append(f"{route_rel}: 缺少 Gitee/Codeup 双地址常量")
    if "/root/.ssh" in route_tx or "ROOT_CODEUP_KEY" in route_tx:
      errors.append(f"{route_rel}: 仍引用 /root/.ssh（C4 只读会导致 OTA Permission denied）")

  inst_rel = rpath("selfdrive/ui/installer/installer.cc")
  inst = rt("selfdrive/ui/installer/installer.cc")
  if not inst.strip():
    errors.append(f"{inst_rel}: 文件缺失或为空")
  elif _CN_INSTALLER_ROUTE_SENTINEL not in inst:
    errors.append("installer.cc: 缺少 cn_main_repo_route_installer 运行时路由")
  elif gitee.https_url not in inst or codeup.ssh_url not in inst:
    errors.append("installer.cc: 缺少 Gitee/Codeup 双地址常量")
  elif "std::string git_url = cn_main_repo_fetch_url()" not in inst:
    errors.append("installer.cc: freshClone 未使用 cn_main_repo_fetch_url 运行时选 URL")
  elif "return cn_is_author_device() ? CN_CODEUP_SSH : CN_GITEE_HTTPS;" not in inst:
    errors.append("installer.cc: 作者设备 fetch 未使用 Codeup SSH（HTTPS 私仓会 401）")
  elif "GIT_URL.c_str()" in inst or re.search(r"\bconst\s+std::string\s+GIT_URL\b", inst):
    errors.append("installer.cc: 仍残留编译期 GIT_URL，未完全切到运行时路由")

  ver_rel = rpath("system/version.py")
  ver = rt("system/version.py")
  if gitee.version_remote_key not in ver:
    errors.append(f"{ver_rel}: sunnypilot_remote 元组中缺少 {gitee.label} URL")
  if codeup.version_remote_key not in ver:
    errors.append(f"{ver_rel}: sunnypilot_remote 元组中缺少 {codeup.label} URL")

  upd_rel = rpath("system/updated/updated.py")
  upd = rt("system/updated/updated.py")
  if "ensure_url_insteadof" not in upd:
    errors.append(f"{upd_rel}: 缺少 ensure_url_insteadof 注入")
  if "prepare_main_repo_git(cwd)" not in upd:
    errors.append(f"{upd_rel}: 缺少 prepare_main_repo_git 注入")
  if "inject_cn_check_for_update_order" not in upd:
    errors.append(f"{upd_rel}: check_for_update 未在 ls-remote 前调用 setup_git_options")
  if _CN_AGNOS_MANIFEST_SENTINEL not in upd:
    errors.append(f"{upd_rel}: 缺少 {_CN_AGNOS_MANIFEST_SENTINEL}（AGNOS manifest 多路径，旧客户端 OTA 会死锁）")
  owner_needle = f"gitee.com/{GITEE_OWNER}"
  if "ensure_url_insteadof" in upd and owner_needle not in upd:
    errors.append(f"{upd_rel}: ensure_url_insteadof 未指向 Gitee 组织 {GITEE_OWNER!r}")

  # 旧 updated 硬编码 system/hardware/tici/agnos.json；新布局必须在发布树根保留 shim
  if (root / "openpilot/common/hardware/tici/agnos.json").is_file() or (
    root / "openpilot/system/hardware/tici/agnos.json"
  ).is_file():
    shim = root / _CN_AGNOS_OTA_SHIM_REL
    shim_payload = _read_agnos_json_payload(shim) if shim.is_file() else None
    if shim_payload is None or not _is_agnos_json_payload(shim_payload):
      errors.append(
        f"{_CN_AGNOS_OTA_SHIM_REL}: 缺少实体 JSON shim（旧车端 OTA 刷 AGNOS 会 FileNotFoundError）"
      )

  if gitee.version_remote_key not in rt("tools/setup.sh"):
    errors.append(f"tools/setup.sh: 缺少主仓 URL（{gitee.label} 公开默认）")
  if "openpilot.comma.ai" in rt("tools/setup.sh"):
    errors.append("tools/setup.sh: 交互安装提示仍指向 openpilot.comma.ai")

  home_rel = rpath("selfdrive/ui/mici/layouts/home.py")
  home_py = root / home_rel
  if home_py.exists() and "main_repo_ui_suffix" not in rt("selfdrive/ui/mici/layouts/home.py"):
    errors.append(f"{home_rel}: 缺少 main_repo_ui_suffix（主页 commit 后缀）")

  mirror_needles = gitee_mirror_needles()
  catch2_needle = next(n for n in mirror_needles if "Catch2" in n)
  msgq_setup_path = root / "msgq_repo/setup.sh"
  if msgq_setup_path.exists():
    msgq_setup_text = rt("msgq_repo/setup.sh")
    catch2_old = _with_git_url_variants("https://github.com/catchorg/Catch2.git")
    if any(o in msgq_setup_text for o in catch2_old) and catch2_needle not in msgq_setup_text:
      errors.append(f"msgq_repo/setup.sh: 缺少 Gitee Catch2 URL（期望含 {catch2_needle!r}）")
  msgq_pyproject = root / "msgq_repo/pyproject.toml"
  if msgq_pyproject.exists():
    msgq_pyproject_text = rt("msgq_repo/pyproject.toml")
    dep_needle = f"gitee.com/{GITEE_OWNER}/dependencies"
    if "github.com/commaai/dependencies" in msgq_pyproject_text:
      errors.append("msgq_repo/pyproject.toml: Catch2 依赖仍指向 GitHub")
    if "catch2 @" in msgq_pyproject_text and dep_needle not in msgq_pyproject_text:
      errors.append(f"msgq_repo/pyproject.toml: Catch2 缺少 Gitee dependencies URL（期望含 {dep_needle!r}）")

  if (root / "opendbc_repo/pyproject.toml").exists():
    dep_needle = f"gitee.com/{GITEE_OWNER}/dependencies"
    if dep_needle not in rt("opendbc_repo/pyproject.toml"):
      errors.append(f"opendbc_repo/pyproject.toml: 缺少 Gitee dependencies URL（期望含 {dep_needle!r}）")

  fetcher_rel = rpath("sunnypilot/models/fetcher.py")
  models_needle = f"gitee.com/{GITEE_OWNER}/sunnypilot-models"
  if models_needle not in rt("sunnypilot/models/fetcher.py"):
    errors.append(f"{fetcher_rel}: 缺少 Gitee models raw URL（期望含 {models_needle!r}）")
  fetcher_url = _parse_models_json_url(root)
  if fetcher_url:
    fetcher_url = _fix_models_json_url_typos(fetcher_url)
  if fetcher_url and "raw/master/refs/heads/gh-pages" in fetcher_url:
    errors.append(
      f"{fetcher_rel}: MODEL_URL 仍为错误路径 raw/master/refs/heads/gh-pages（Gitee 404）；"
      "请重新 pull 以应用 models_fetcher 补丁"
    )
  gh_pages_prefix = gitee_models_raw_gh_pages()
  if fetcher_url and models_needle in fetcher_url and gh_pages_prefix not in fetcher_url:
    errors.append(
      f"{fetcher_rel}: MODEL_URL 应使用 gh-pages raw 前缀（期望含 {gh_pages_prefix!r}）"
    )
  if _CN_MODELS_CACHE_GUARD not in rt("sunnypilot/models/fetcher.py"):
    errors.append(
      f"{fetcher_rel}: 缺少 {_CN_MODELS_CACHE_GUARD}（OTA 后旧模型缓存会导致列表只剩 CD210）"
    )
  fetcher_tx = rt("sunnypilot/models/fetcher.py")
  if _CN_SUNNYLINK_ACTIVEJSON not in fetcher_tx:
    errors.append(
      f"{fetcher_rel}: 缺少 {_CN_SUNNYLINK_ACTIVEJSON}（Gitee ActiveJson 会导致 sunnylink.ai 模型列表空白）"
    )
  if 'put("ModelManager_ActiveJson", self.model_url' in fetcher_tx:
    errors.append(
      f"{fetcher_rel}: 仍把 MODEL_URL 写入 ModelManager_ActiveJson（sunnylink.ai 浏览器无法 CORS 访问 Gitee raw）"
    )
  if re.search(
    r'"(?:qcom|usbgpu|chestnut)": self\.MODEL_URL(?:_USBGPU|_CHESTNUT)?,',
    fetcher_tx,
  ):
    errors.append(
      f"{fetcher_rel}: ActiveJson dict 仍直接写入 Gitee/本地 MODEL_URL（应改写为 GitHub raw 供 sunnylink）"
    )
  if 'put("ModelManager_ActiveJson", ""' in fetcher_tx:
    errors.append(
      f"{fetcher_rel}: ActiveJson 不能留空（ModelsCache ~100KB 常传不出 sunnylink，网页仍空白）"
    )
  if _CN_SUNNYLINK_ACTIVEJSON in fetcher_tx and _SUNNYLINK_ACTIVEJSON_GITHUB_PREFIX not in fetcher_tx:
    errors.append(
      f"{fetcher_rel}: ActiveJson 应写入 GitHub raw（CORS *），供 sunnylink.ai 浏览器拉取"
    )
  for attr in ("MODEL_URL", "MODEL_URL_USBGPU", "MODEL_URL_CHESTNUT"):
    m_url = re.search(rf'{attr}\s*=\s*"([^"]+)"', fetcher_tx)
    if m_url and "raw.githubusercontent.com/sunnypilot/sunnypilot-models" in m_url.group(1):
      errors.append(f"{fetcher_rel}: {attr} 仍指向 GitHub raw，未改 Gitee")
  if _CN_HF_MIRROR not in fetcher_tx or _HF_MIRROR_HOST not in fetcher_tx:
    errors.append(
      f"{fetcher_rel}: 缺少 {_CN_HF_MIRROR}（权重下载应改写 huggingface.co → hf-mirror.com）"
    )
  if (
    'download_uri.uri = download_uri_data.get("url")' in fetcher_tx
    and _CN_HF_MIRROR not in fetcher_tx
  ):
    errors.append(
      f"{fetcher_rel}: _parse_download_uri 仍直写 HF URL，未注入镜像改写"
    )

  osm_rel = rpath("selfdrive/ui/sunnypilot/layouts/settings/osm.py")
  mapd_rel = rpath("sunnypilot/mapd/mapd_installer.py")
  mapd_needle = f"gitee.com/{GITEE_OWNER}/openpilot-mapd"
  if mapd_needle not in rt("selfdrive/ui/sunnypilot/layouts/settings/osm.py"):
    errors.append(f"{osm_rel}: 缺少 Gitee mapd raw URL（期望含 {mapd_needle!r}）")

  if mapd_needle not in rt("sunnypilot/mapd/mapd_installer.py"):
    errors.append(f"{mapd_rel}: 缺少 Gitee mapd release URL（期望含 {mapd_needle!r}）")
  if 'os.getenv("MAPD_TAG"' not in rt("sunnypilot/mapd/mapd_installer.py"):
    errors.append(f"{mapd_rel}: VERSION 未支持 MAPD_TAG 环境变量")

  sw_tici_rel = rpath("selfdrive/ui/layouts/settings/software.py")
  sw_mici_rel = rpath("selfdrive/ui/mici/layouts/settings/software.py")
  sw_tici = rt("selfdrive/ui/layouts/settings/software.py")
  sw_mici = rt("selfdrive/ui/mici/layouts/settings/software.py")
  if sw_tici and _CN_SOFTWARE_UPDATE_CONFIRM not in sw_tici:
    errors.append(f"{sw_tici_rel}: 缺少 {_CN_SOFTWARE_UPDATE_CONFIRM}（有更新时需英文确认）")
  if sw_mici and _CN_SOFTWARE_UPDATE_CONFIRM not in sw_mici:
    errors.append(f"{sw_mici_rel}: 缺少 {_CN_SOFTWARE_UPDATE_CONFIRM}（有更新时需确认）")

  if (root / ".gitmodules").exists():
    gm = rt(".gitmodules")
    stale = [
      ("commaai/msgq", "https://github.com/commaai/msgq.git"),
      ("sunnypilot/opendbc", "https://github.com/sunnypilot/opendbc.git"),
      ("commaai/rednose", "https://github.com/commaai/rednose.git"),
    ]
    for label, needle in stale:
      if needle in gm:
        errors.append(f".gitmodules: 仍包含上游 GitHub URL（{label}），国内化未写完")

  for rel, label in (("system/ui/tici_setup.py", "tici_setup"), ("system/ui/mici_setup.py", "mici_setup")):
    resolved = rpath(rel)
    if (root / resolved).exists():
      tx = rt(rel)
      sp_install = f"gitee.com/{GITEE_OWNER}/sp-cn_install"
      if "OPENPILOT_URL" in tx and sp_install not in tx:
        errors.append(f"{label}: 仍存在 OPENPILOT_URL 但未指向 Gitee sp-cn_install（期望含 {sp_install!r}）")
      marker = "cn_wifi_ipv4_bypass" if label == "tici_setup" else "cn_wifi_connected_bypass"
      if marker not in tx:
        errors.append(f"{label}: 缺少 Wi-Fi 独立放行逻辑 {marker}")
      if "CONNECTIVITY_CHECK_URLS" not in tx:
        errors.append(f"{label}: 缺少国内网络多候选探测")

  dm_path = _dm_monitoring_path(root)
  if dm_path is None:
    errors.append("selfdrive/monitoring: 缺少 policy.py 与 helpers.py，无法校验 DM 补丁")
  else:
    rel = dm_path.relative_to(root).as_posix()
    hm = rt(rel) if (root / rel).exists() else dm_path.read_text(encoding="utf-8", errors="replace")
    if _DM_SENTINEL_AWARENESS not in hm:
      errors.append(f"{rel}: 缺少 DM 补丁 sentinel（awareness 下限）")
    if _DM_SENTINEL_TERMINAL not in hm:
      errors.append(f"{rel}: 缺少 DM 补丁 sentinel（terminal 累计）")
    if not _RE_DM_RED_EXIT_HEADER.search(hm):
      errors.append(f"{rel}: 缺少 DM 补丁 sentinel（red 自动退出）")
    elif "self.red_recover_cnt" not in hm:
      errors.append(f"{rel}: 缺少 red_recover_cnt 计数器（red 自动退出）")
    elif _dm_red_exit_is_misplaced(hm):
      errors.append(
        f"{rel}: red_exit 补丁位置/无人脸恢复不正确（须与 awareness 外层分支同级）"
      )
    if _dm_is_policy_arch(hm) and "or (not self.face_detected and self.driver_interacting)" not in hm:
      errors.append(f"{rel}: red_exit 缺少无人脸驾驶交互恢复")
    if (
      _dm_is_policy_arch(hm)
      and "noResponseForceDecel" in hm
      and _DM_SENTINEL_ESCALATION_TIMER not in hm
    ):
      errors.append(f"{rel}: 缺少原厂 no-response escalation 计时保留逻辑")
    if re.search(r"max\s*\(\s*self\.awareness\s*-\s*self\.step_change\s*,\s*-0\.1\s*\)", hm):
      errors.append(f"{rel}: 仍存在原版 -0.1 awareness 下限，DM 补丁未生效")
    if not re.search(
      r"max\s*\(\s*self\.awareness\s*-\s*self\.step_change\s*,\s*0\.?\s*\)",
      hm,
    ):
      errors.append(f"{rel}: 缺少 0. awareness 下限，DM 补丁未生效")
    if _dm_is_policy_arch(hm):
      if re.search(
        r"if self\.awareness <= 0\.:[^\n]*\n(?:.*\n)*?\s*self\.alert_level = AlertLevel\.three[^\n]*\n\s*self\.terminal_time\s*\+=\s*1",
        hm,
        re.MULTILINE,
      ):
        errors.append(f"{rel}: 红线分支仍在累计 terminal_time，DM 补丁未生效")
      if re.search(
        r"if self\.awareness <= 0\.:[^\n]*\n(?:.*\n)*?\s*self\.alert_level = AlertLevel\.three[^\n]*\n(?:.*\n)*?self\.terminal_alert_cnt\s*\+=\s*1",
        hm,
        re.MULTILINE,
      ):
        errors.append(f"{rel}: 红线分支仍在累计 terminal_alert_cnt，DM 补丁未生效")
      if re.search(
        r"if self\.awareness <= 0\.:[^\n]*\n(?:.*\n)*?\s*self\.alert_level = AlertLevel\.three[^\n]*\n(?:.*\n)*?self\.alert_3_cnt\s*\+=\s*1",
        hm,
        re.MULTILINE,
      ):
        errors.append(f"{rel}: 红线分支仍在累计 alert_3_cnt，DM 补丁未生效")
      if re.search(
        r"if self\.awareness <= 0\.:[^\n]*\n(?:.*\n)*?\s*self\.alert_level = AlertLevel\.three[^\n]*\n(?:.*\n)*?self\.no_response_cnt\s*\+=\s*1",
        hm,
        re.MULTILINE,
      ):
        errors.append(f"{rel}: 红线分支仍在累计 no_response_cnt，DM 补丁未生效")
    else:
      if re.search(
        r"driverDistracted3[^\n]*\n\s*self\.terminal_time\s*\+=\s*1",
        hm,
        re.MULTILINE,
      ):
        errors.append(f"{rel}: 红线分支仍在累计 terminal_time，DM 补丁未生效")
      if re.search(
        r"driverDistracted3[^\n]*\n\s*self\.terminal_alert_cnt\s*\+=\s*1",
        hm,
        re.MULTILINE,
      ):
        errors.append(f"{rel}: 红线分支仍在累计 terminal_alert_cnt，DM 补丁未生效")

  controls_rel = rpath("selfdrive/controls/controlsd.py")
  if (root / controls_rel).is_file():
    cs_tx = (root / controls_rel).read_text(encoding="utf-8", errors="replace")
    if _DM_SENTINEL_NO_FORCE_DECEL not in cs_tx:
      errors.append(f"{controls_rel}: 缺少 DM forceDecel 补丁 sentinel")
    if (
      _RE_CONTROLS_FORCE_DECEL_WITH_DM.search(cs_tx)
      or _RE_CONTROLS_FORCE_DECEL_NO_RESPONSE.search(cs_tx)
      or _RE_CONTROLS_FORCE_DECEL_AWARENESS.search(cs_tx)
    ):
      errors.append(
        f"{controls_rel}: 仍为 DM → forceDecel（红屏会收纵向，国内化未生效）"
      )
    if (
      "noResponseForceDecel" in cs_tx
      and "cs.forceDecel" in cs_tx
      and _DM_SENTINEL_NO_FORCE_DECEL not in cs_tx
    ):
      errors.append(
        f"{controls_rel}: forceDecel 仍关联 noResponseForceDecel（需仅 softDisabling 置 forceDecel）"
      )
    if re.search(
      r"driverMonitoringState[^\n]*alertLevel[^\n]*three",
      cs_tx,
    ) and "cs.forceDecel" in cs_tx and _DM_SENTINEL_NO_FORCE_DECEL not in cs_tx:
      errors.append(
        f"{controls_rel}: forceDecel 仍可能关联 DM 红屏（需仅 softDisabling 置 forceDecel）"
      )
    if "CC.driverMonitoringEscalation = cs.forceDecel" in cs_tx:
      errors.append(f"{controls_rel}: 原厂 driverMonitoringEscalation 被误绑定到已削弱的 forceDecel")

  dm_tests_rel = rpath("selfdrive/monitoring/test_monitoring.py")
  if (root / dm_tests_rel).is_file():
    dm_tests = (root / dm_tests_rel).read_text(encoding="utf-8", errors="replace")
    for test_name in ("test_cn_red_recovery_with_face", "test_cn_red_recovery_without_face"):
      if test_name not in dm_tests:
        errors.append(f"{dm_tests_rel}: 缺少 {test_name} 语义测试")

  mgr_rel = rpath("system/manager/process.py")
  mgr_tx = rt("system/manager/process.py")
  if not mgr_tx.strip():
    errors.append(f"{mgr_rel}: 文件缺失或为空")
  elif _CN_MANAGER_RESTART_DEAD not in mgr_tx:
    errors.append(f"{mgr_rel}: 缺少 {_CN_MANAGER_RESTART_DEAD}（进程死后不会自动重启）")
  else:
    if "restarting dead process" not in mgr_tx:
      errors.append(f"{mgr_rel}: 缺少 dead process 重启日志")
    if "not p.proc.is_alive()" not in mgr_tx:
      errors.append(f"{mgr_rel}: ensure_running 未检查 proc.is_alive()")
    # start 循环须先清掉已死进程再 start，避免 PythonProcess.start 直接 return
    if not re.search(
      r"for p in running:\s*"
      r".*not p\.proc\.is_alive\(\).*"
      r"p\.stop\(\)\s*"
      r"p\.start\(\)",
      mgr_tx,
      re.MULTILINE | re.DOTALL,
    ):
      errors.append(f"{mgr_rel}: ensure_running 未在 start 前 stop 已死进程")

  dm_warp_rel = rpath("selfdrive/modeld/dmonitoringmodeld.py")
  dm_warp_tx = rt("selfdrive/modeld/dmonitoringmodeld.py")
  if "dm_warp_" in dm_warp_tx:
    if _CN_DM_WARP_COMPILE_MISSING not in dm_warp_tx:
      errors.append(
        f"{dm_warp_rel}: 缺少 {_CN_DM_WARP_COMPILE_MISSING}（git OTA 无 dm_warp pkl 会 commIssue）"
      )
    else:
      if "def _cn_ensure_dm_warp(" not in dm_warp_tx:
        errors.append(f"{dm_warp_rel}: 缺少 _cn_ensure_dm_warp")
      if "compile_dm_warp(" not in dm_warp_tx:
        errors.append(f"{dm_warp_rel}: _cn_ensure_dm_warp 未调用 compile_dm_warp")
      if "warp_path.is_file()" not in dm_warp_tx:
        errors.append(f"{dm_warp_rel}: 未在缺失时才编译 dm_warp pkl")
      if re.search(
        r"with open\(MODELS_DIR / f['\"]dm_warp_",
        dm_warp_tx,
      ):
        errors.append(f"{dm_warp_rel}: 仍直接 open dm_warp pkl，未走缺失编译")
      compile_rel = rpath("selfdrive/modeld/compile_dm_warp.py")
      if not (root / compile_rel).is_file():
        errors.append(f"{compile_rel}: 缺少 compile_dm_warp.py")

  # 语法兜底（不验证逻辑正确性）
  py_verify = [
    rpath("system/cn_main_repo_route.py"),
    rpath("system/version.py"),
    rpath("system/updated/updated.py"),
    rpath("system/ui/tici_setup.py"),
    rpath("system/ui/mici_setup.py"),
    rpath("selfdrive/ui/mici/layouts/home.py"),
    rpath("sunnypilot/models/fetcher.py"),
    rpath("selfdrive/ui/sunnypilot/layouts/settings/osm.py"),
    rpath("sunnypilot/mapd/mapd_installer.py"),
    rpath("selfdrive/ui/layouts/settings/software.py"),
    rpath("selfdrive/ui/mici/layouts/settings/software.py"),
    controls_rel,
    dm_tests_rel,
    mgr_rel,
    dm_warp_rel,
  ]
  dm_verify = _dm_monitoring_path(root)
  if dm_verify is not None:
    py_verify.append(str(dm_verify.relative_to(root)).replace("\\", "/"))
  errors.extend(_verify_python_syntax(root, py_verify))
  errors.extend(_verify_no_debug_markers(root, py_verify))
  for rel in py_verify:
    p = root / rel
    if p.exists():
      try:
        py_compile.compile(str(p), doraise=True)
      except py_compile.PyCompileError as e:
        errors.append(f"{rel}: py_compile 失败: {e}")

  for sub in ("openpilot/system", "system"):
    d = root / sub
    if d.is_dir():
      if not compileall.compile_dir(str(d), quiet=1):
        errors.append(f"{sub}/: compileall 失败")

  errors.extend(collect_tinygrad_models_verify_errors(root))

  if errors:
    raise RuntimeError("verify_patches 失败（不会提交/推送）：\n  - " + "\n  - ".join(errors))


def ci_sync_state_path() -> Path:
  return REPO_ROOT / ".ci-cache" / "sync_ci_state.json"


def save_ci_sync_state(state: dict[str, object]) -> None:
  p = ci_sync_state_path()
  p.parent.mkdir(parents=True, exist_ok=True)
  p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_ci_sync_state() -> dict[str, object]:
  p = ci_sync_state_path()
  if not p.exists():
    return {}
  try:
    return json.loads(p.read_text(encoding="utf-8"))
  except json.JSONDecodeError:
    return {}


def write_ci_github_output(state: dict[str, object]) -> None:
  """供 GitHub Actions 读取 steps.sync.outputs.*（分端推送结果 + 邮件）。"""
  if os.environ.get("GITHUB_ACTIONS") != "true":
    return
  path = os.environ.get("GITHUB_OUTPUT")
  if not path:
    return
  codeup_out = (os.environ.get("CI_STEP_OUTCOME_CODEUP") or "").strip() or None
  gitee_out = (os.environ.get("CI_STEP_OUTCOME_GITEE") or "").strip() or None
  plan = build_ci_notify(state, codeup_step_outcome=codeup_out, gitee_step_outcome=gitee_out)
  attempted = bool(state.get("attempted"))
  pushed_gitee = bool(state.get("pushed_gitee"))
  pushed_codeup = bool(state.get("pushed_codeup"))
  pushed = bool(state.get("pushed")) or pushed_gitee or pushed_codeup
  branches = state.get("branches") or []
  br_line = ",".join(str(b) for b in branches) if branches else ""
  variant = _sync_reason_variant(state)

  delim_body = "SYNC_NOTIFY_BODY_EOF"
  with open(path, "a", encoding="utf-8") as f:
    f.write(f"attempted_sync={'true' if attempted else 'false'}\n")
    f.write(f"pushed={'true' if pushed else 'false'}\n")
    f.write(f"pushed_gitee={'true' if pushed_gitee else 'false'}\n")
    f.write(f"pushed_codeup={'true' if pushed_codeup else 'false'}\n")
    f.write(f"sync_branches={br_line}\n")
    f.write(f"notify_variant={variant}\n")
    f.write(f"notify_mail_kind={plan.mail_kind}\n")
    f.write(f"notify_send={'true' if plan.notify_send else 'false'}\n")
    f.write(f"notify_subject={plan.subject}\n")
    f.write(f"notify_body<<{delim_body}\n")
    f.write(plan.body)
    if plan.body and not plan.body.endswith("\n"):
      f.write("\n")
    f.write(f"{delim_body}\n")
    if plan.mail_kind == "full_ok":
      f.write(f"notify_success_body<<{delim_body}\n")
      f.write(plan.body)
      if plan.body and not plan.body.endswith("\n"):
        f.write("\n")
      f.write(f"{delim_body}\n")


def _gitee_git_ssh_env(port: int = 22) -> dict[str, str]:
  """Gitee SSH：短超时 + 非交互，避免 CI 上 port 22 挂死数分钟。"""
  env = os.environ.copy()
  env["GIT_SSH_COMMAND"] = (
    f"ssh -p {port} -o ConnectTimeout=25 -o ConnectionAttempts=1 "
    "-o ServerAliveInterval=10 -o ServerAliveCountMax=2 -o BatchMode=yes"
  )
  return env


def ensure_tinygrad_submodule_commit_reachable(root: Path | None = None) -> None:
  """
  确保 Gitee tinygrad 镜像含对齐目标 commit（优先 models JSON tinygrad_ref）。
  子模块 update 阶段仍走 GitHub upstream；此处仅为 Gitee 克隆者兜底。
  """
  sha = TINYGRAD_MODELS_REF
  if root is not None:
    resolved, source, _ = _resolve_tinygrad_models_ref(root, offline_ok=True)
    if resolved:
      sha = resolved
      log("tinygrad", f"ensure mirror: {source} → {sha[:7]}")
  url = gitee_git_ssh_repo("tinygrad")

  def _gitee_has_commit(port: int) -> bool:
    with tempfile.TemporaryDirectory() as td:
      try:
        run(["git", "init"], td)
        run(["git", "remote", "add", "origin", url], td)
        run(
          ["git", "fetch", "--depth=1", "origin", sha],
          td,
          env=_gitee_git_ssh_env(port),
        )
        return True
      except Exception:
        return False

  for port in (22, 443):
    if _gitee_has_commit(port):
      log("tinygrad", f"Gitee mirror already has {sha[:7]} (ssh port {port})")
      return

  branch = f"submodule-pin-{sha[:12]}"
  with tempfile.TemporaryDirectory() as td:
    run(["git", "init"], td)
    run(["git", "remote", "add", "upstream", "https://github.com/sunnypilot/tinygrad.git"], td)
    run(["git", "fetch", "--depth=1", "upstream", sha], td)
    run(["git", "update-ref", f"refs/heads/{branch}", sha], td)
    run(["git", "remote", "add", "origin", url], td)
    last_err: Exception | None = None
    for port in (22, 443):
      try:
        run(
          ["git", "push", "origin", f"+refs/heads/{branch}:refs/heads/{branch}"],
          td,
          env=_gitee_git_ssh_env(port),
        )
        log("tinygrad", f"mirrored {sha[:7]} to Gitee (ssh port {port})")
        return
      except Exception as e:
        last_err = e
        log("warn", f"tinygrad mirror push via port {port} failed: {e}")

  soft_ok = os.environ.get("GITHUB_ACTIONS") == "true" and (
    os.environ.get("SYNC_TINYGRAD_MIRROR_REQUIRED", "").strip().lower() not in ("1", "true", "yes", "on")
  )
  if soft_ok:
    log(
      "warn",
      "skip tinygrad Gitee mirror (SSH unreachable); CI 子模块 update 仍用 GitHub upstream，"
      "同步可继续。若 Gitee 克隆缺 commit 可稍后重试或设 SYNC_TINYGRAD_MIRROR_REQUIRED=1 强制失败。",
    )
    return
  raise RuntimeError(f"tinygrad mirror push failed: {last_err}")


def current_arch(root: Path) -> str:
  # follow repo's logic: larch64 iff aarch64 + /TICI file exists
  machine = run(["uname", "-m"]).strip()
  if machine == "aarch64" and Path("/TICI").is_file():
    return "larch64"
  return machine


def build_installers_if_possible(root: Path) -> None:
  arch = current_arch(root)
  if arch != "larch64":
    raise RuntimeError(f"installer 仅在 larch64（TICI 设备）构建。当前 arch={arch}。请在设备上运行同一脚本加 --build-installer。")
  env = os.environ.copy()
  # 设备端一般不需要跳过字体；若要跳过可自行设置
  env.setdefault("SKIP_FONT_BUILD", "")
  # 需要 extras=true（默认 minimal 会关 extras）
  run(["scons", "-j", str(os.cpu_count() or 4)], cwd=str(root), env=env)
  # installer 产物在 selfdrive/ui/installer/installers/


def resolve_mapd_tag(tag: str) -> str:
  if tag and tag != "latest":
    return tag
  # latest: query GitHub API unauthenticated
  data = http_json(f"https://api.github.com/repos/{MAPD_UPSTREAM}/releases/latest", headers={"User-Agent": "sync-to-gitee"})
  return data["tag_name"]


def gitee_api_url(path: str, token: str) -> str:
  sep = "&" if "?" in path else "?"
  return f"https://gitee.com/api/v5{path}{sep}access_token={token}"


def _gitee_http_error_body(e: urllib.error.HTTPError) -> str:
  try:
    return (e.read() or b"").decode("utf-8", errors="replace").strip()
  except Exception:
    return ""


def gitee_branch_exists(token: str, owner: str, repo: str, branch: str) -> bool:
  if not branch:
    return False
  url = gitee_api_url(f"/repos/{owner}/{repo}/branches/{quote(branch, safe='')}", token)
  try:
    data = http_json(url, headers={"User-Agent": "sync-to-gitee"})
    return isinstance(data, dict) and bool(data.get("name") or data.get("commit"))
  except urllib.error.HTTPError as e:
    if e.code == 404:
      return False
    raise


def gitee_release_target_commitish(token: str, owner: str, repo: str) -> str:
  """Gitee 创建 Release 必须指向已存在的分支；mapd 镜像没有 master，默认分支还可能是 docs-update。"""
  info = http_json(gitee_api_url(f"/repos/{owner}/{repo}", token), headers={"User-Agent": "sync-to-gitee"})
  default_branch = str((info or {}).get("default_branch") or "").strip()
  for cand in ("main", "master", default_branch):
    if gitee_branch_exists(token, owner, repo, cand):
      return cand
  raise RuntimeError(f"Gitee {owner}/{repo} 无可用分支作为 release target_commitish（default={default_branch!r}）")


def gitee_get_release_by_tag(token: str, tag: str) -> dict | None:
  url = gitee_api_url(f"/repos/{GITEE_OWNER}/{MAPD_REPO}/releases/tags/{quote(tag, safe='')}", token)
  try:
    rel = http_json(url, headers={"User-Agent": "sync-to-gitee"})
    if not isinstance(rel, dict) or "id" not in rel:
      return None
    return rel
  except urllib.error.HTTPError as e:
    if e.code in (400, 404):
      return None
    raise


def gitee_create_release(token: str, tag: str) -> dict:
  target = gitee_release_target_commitish(token, GITEE_OWNER, MAPD_REPO)
  print(f"[mapd] create Gitee release tag={tag} target_commitish={target}")
  url = gitee_api_url(f"/repos/{GITEE_OWNER}/{MAPD_REPO}/releases", token)
  payload = json.dumps({
    "tag_name": tag,
    "name": tag,
    "body": f"Auto-synced mapd binary for {tag}",
    "prerelease": False,
    "target_commitish": target,
  }).encode("utf-8")
  req = urllib.request.Request(
    url,
    data=payload,
    method="POST",
    headers={"Content-Type": "application/json", "User-Agent": "sync-to-gitee"},
  )
  try:
    with urllib.request.urlopen(req, timeout=30) as resp:
      rel = json.loads(resp.read().decode("utf-8"))
  except urllib.error.HTTPError as e:
    detail = _gitee_http_error_body(e)
    raise RuntimeError(
      f"Gitee 创建 release 失败 HTTP {e.code} tag={tag} target_commitish={target}: {detail or e.reason}"
    ) from e
  if not isinstance(rel, dict) or "id" not in rel:
    raise RuntimeError(f"Gitee release 创建返回异常（tag={tag}）：{rel!r}")
  return rel


def gitee_upload_release_asset(token: str, release_id: int, asset_name: str, file_path: Path) -> None:
  # Gitee v5: POST /repos/{owner}/{repo}/releases/{release_id}/attach_files
  # Note: query param `access_token` is appended by gitee_api_url
  url = gitee_api_url(f"/repos/{GITEE_OWNER}/{MAPD_REPO}/releases/{release_id}/attach_files", token)
  file_bytes = file_path.read_bytes()
  # field name is `file`, optional name can be inferred from filename
  http_multipart_post(url, fields={}, file_field="file", filename=asset_name, file_bytes=file_bytes)


def gitee_list_release_attach_files(token: str, release_id: int) -> list[dict]:
  url = gitee_api_url(f"/repos/{GITEE_OWNER}/{MAPD_REPO}/releases/{release_id}/attach_files", token)
  return http_json(url, headers={"User-Agent": "sync-to-gitee"})


def gitee_delete_release_attach_file(token: str, release_id: int, attach_file_id: int) -> None:
  url = gitee_api_url(f"/repos/{GITEE_OWNER}/{MAPD_REPO}/releases/{release_id}/attach_files/{attach_file_id}", token)
  req = urllib.request.Request(url, method="DELETE", headers={"User-Agent": "sync-to-gitee"})
  with urllib.request.urlopen(req, timeout=30) as _:
    return


def publish_installer_release(token: str, installer_repo_url: str, installer_latest: Path) -> str:
  """
  在 Gitee 上为 installer 仓库创建 Release 并上传附件。
  tag 使用本地时间 YYYYMMDDHHMM（例如 202604291938）。
  若同名附件已存在则替换（不会越积越多）。comma 设备只识别固定文件名 installer_openpilot。
  返回 tag。
  """
  m = re.fullmatch(r"https?://gitee\.com/([^/]+)/([^/]+?)(?:\.git)?/?", installer_repo_url.strip())
  if not m:
    raise RuntimeError(f"仅支持 https gitee 仓库地址发布 release：{installer_repo_url}")
  owner, repo = m.group(1), m.group(2)

  if not installer_latest.exists():
    raise RuntimeError("installer 文件不存在，无法发布 release")

  tag = datetime.datetime.now().strftime("%Y%m%d%H%M")
  body = "Auto-published installer binaries."

  # get or create release
  url_get = gitee_api_url(f"/repos/{owner}/{repo}/releases/tags/{tag}", token)
  try:
    rel = http_json(url_get, headers={"User-Agent": "sp-installer"})
    # Gitee 有时在 tag 不存在时返回 JSON null（None），这里视为不存在并走创建逻辑
    if rel is None:
      raise urllib.error.HTTPError(url_get, 404, "not found", hdrs=None, fp=None)
    if not isinstance(rel, dict) or "id" not in rel:
      raise RuntimeError(f"Gitee release 查询返回异常（tag={tag}）：{rel!r}")
  except urllib.error.HTTPError as e:
    if e.code != 404:
      raise
    url_create = gitee_api_url(f"/repos/{owner}/{repo}/releases", token)
    payload = json.dumps({
      "tag_name": tag,
      "name": tag,
      "body": body,
      "prerelease": False,
      "target_commitish": "master",
    }).encode("utf-8")
    req = urllib.request.Request(url_create, data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
      raw = resp.read().decode("utf-8")
      if not raw.strip():
        raise RuntimeError("Gitee release 创建返回空响应（可能是 token 权限不足或接口异常）")
      rel = json.loads(raw)
      if not isinstance(rel, dict) or "id" not in rel:
        raise RuntimeError(f"Gitee release 创建返回异常（tag={tag}）：{rel!r}")

  rid = int(rel["id"])

  # list existing assets
  url_list = gitee_api_url(f"/repos/{owner}/{repo}/releases/{rid}/attach_files", token)
  existing = http_json(url_list, headers={"User-Agent": "sp-installer"})
  if existing is None:
    existing = []
  if not isinstance(existing, list):
    raise RuntimeError(f"Gitee attach_files 返回异常：{existing!r}")

  def delete_by_name(name: str) -> None:
    olds = [a for a in existing if a.get("name") == name]
    for a in olds:
      aid = int(a["id"])
      url_del = gitee_api_url(f"/repos/{owner}/{repo}/releases/{rid}/attach_files/{aid}", token)
      req_del = urllib.request.Request(url_del, method="DELETE", headers={"User-Agent": "sp-installer"})
      with urllib.request.urlopen(req_del, timeout=30) as _:
        pass

  def upload(name: str, path: Path) -> None:
    url_up = gitee_api_url(f"/repos/{owner}/{repo}/releases/{rid}/attach_files", token)
    http_multipart_post(url_up, fields={}, file_field="file", filename=name, file_bytes=path.read_bytes())

  delete_by_name("installer_openpilot")
  upload("installer_openpilot", installer_latest)

  return tag


def sync_mapd_release(token: str, tag_env: str) -> None:
  tag = resolve_mapd_tag(tag_env)
  # download from GitHub release
  tmp_dir = Path(tempfile.mkdtemp(prefix="mapd_sync_"))
  try:
    bin_path = tmp_dir / "mapd"
    gh_url = f"https://github.com/{MAPD_UPSTREAM}/releases/download/{tag}/mapd"
    print(f"[mapd] download {gh_url}")
    http_download(gh_url, bin_path)

    rel = gitee_get_release_by_tag(token, tag)
    if rel is None:
      rel = gitee_create_release(token, tag)
    rid = int(rel["id"])

    # idempotent: if same-sized mapd already exists, skip.
    existing = gitee_list_release_attach_files(token, rid)
    same = [a for a in existing if a.get("name") == "mapd" and int(a.get("size") or -1) == bin_path.stat().st_size]
    if same:
      # keep exactly one and delete duplicates (idempotent cleanup)
      keep = int(same[0]["id"])
      dups = [int(a["id"]) for a in same[1:]]
      for aid in dups:
        print(f"[mapd] delete duplicate attach_file id={aid} (same size={bin_path.stat().st_size})")
        gitee_delete_release_attach_file(token, rid, aid)
      print(f"[mapd] already present (kept_id={keep} deleted_dups={len(dups)} size={bin_path.stat().st_size}), skip upload")
      return

    # if there are old mapd assets (wrong size/duplicates), delete them first
    olds = [a for a in existing if a.get("name") == "mapd"]
    for a in olds:
      aid = int(a["id"])
      print(f"[mapd] delete old attach_file id={aid} size={a.get('size')}")
      gitee_delete_release_attach_file(token, rid, aid)

    print(f"[mapd] upload to gitee release_id={rid} name=mapd size={bin_path.stat().st_size}")
    try:
      gitee_upload_release_asset(token, rid, "mapd", bin_path)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
      # Client can time out while Gitee still accepts the body; re-check attachments before failing.
      err_l = str(e).lower()
      if not (isinstance(e, TimeoutError) or "timed out" in err_l or "timeout" in err_l):
        raise
      expect_sz = bin_path.stat().st_size
      existing2 = gitee_list_release_attach_files(token, rid)
      same2 = [a for a in existing2 if a.get("name") == "mapd" and int(a.get("size") or -1) == expect_sz]
      if same2:
        print(
          f"[mapd] upload client error ({type(e).__name__}) but mapd already on Gitee "
          f"(attach_id={same2[0].get('id')} size={expect_sz}), treating as success"
        )
        return
      raise
  finally:
    try:
      for p in tmp_dir.glob("*"):
        p.unlink(missing_ok=True)
      tmp_dir.rmdir()
    except Exception:
      pass


def main() -> None:
  ap = argparse.ArgumentParser()
  ap.add_argument("--workdir", default=WORKDIR_DEFAULT, help="sunnypilot 仓库根目录")
  ap.add_argument("--upstream", default=UPSTREAM_DEFAULT)
  ap.add_argument("--origin", default=REPO_DEFAULT)
  ap.add_argument(
      "--force-staging",
      action="store_true",
      default=False,
      help="已废弃：此前将本地 master 强推到远端 staging。当前仅同步 staging，此选项无操作。",
  )
  ap.add_argument("--action",
                  choices=[
                    "menu", "pull", "push", "push-gitee", "push-codeup", "emit-outputs",
                    "print-ci-push-targets", "verify-tinygrad-models", "all", "archive-tag",
                  ],
                  default="menu",
                  help=(
                    "执行模式：menu=交互菜单；pull=拉取+补丁；push=推全部启用源；"
                    "push-gitee/push-codeup=仅推一端（CI 分步）；emit-outputs=写 GITHUB_OUTPUT；"
                    "print-ci-push-targets=输出 workflow 条件变量；"
                    "archive-tag=推送成功后打留档 tag（默认 Beta-短SHA-北京时间到秒，可选 stable）；"
                    "verify-tinygrad-models=仅校验 tinygrad_repo 与 models JSON ref；all=pull+push"
                  ))
  ap.add_argument("--build-installer", action="store_true", default=False, help="在 larch64 设备上构建 installer（需要 extras=on）")
  ap.add_argument("--sync-mapd-release", action="store_true", default=False, help="同步 mapd 二进制到 Gitee Release（需要 GITEE_TOKEN）")
  ap.add_argument("--comma-host", default=COMMA_HOST_DEFAULT, help="comma 设备 IP/域名（用于远程编译 installer）")
  ap.add_argument("--comma-user", default=COMMA_USER_DEFAULT, help="comma 设备 SSH 用户名")
  ap.add_argument("--comma-key", default=COMMA_SSH_KEY_DEFAULT, help="comma 设备 SSH 私钥路径")
  ap.add_argument("--installer-outdir", default="/home/perfume/sp", help="将编译好的 installer 拷贝到本机的目录")
  ap.add_argument("--installer-repo", default=INSTALLER_REPO_DEFAULT, help="用于发布 installer 的仓库（默认 sp-cn_install）")
  ap.add_argument("--installer-repo-branch", default="master", help="发布 installer 的分支")
  ap.add_argument("--publish-installer-release", action="store_true", default=True, help="发布 installer 后自动创建 Gitee Release（tag=YYYYMMDDHHMM）")
  ap.add_argument(
      "--archive-tag-kind",
      default=None,
      metavar="KIND",
      help="留档 tag：Beta（默认，Beta-SHA-北京时间到秒）或 stable。也可用 SYNC_ARCHIVE_TAG_KIND。",
  )
  args = ap.parse_args()
  if getattr(args, "archive_tag_kind", None):
    os.environ["SYNC_ARCHIVE_TAG_KIND"] = str(args.archive_tag_kind).strip()

  if args.action == "print-ci-push-targets":
    write_push_targets_github_output()
    return

  root = Path(args.workdir).resolve()
  if not (root / ".git").exists():
    raise SystemExit(f"未找到 git 仓库: {root}")

  env, shim_dir = prepare_git_env(root)
  if os.environ.get("GITHUB_ACTIONS") == "true":
    # CI 只需源码打补丁，不 smudge LFS；显著缩短 clone/checkout。
    env.setdefault("GIT_LFS_SKIP_SMUDGE", "1")
    env.setdefault(
      "GIT_SSH_COMMAND",
      "ssh -o ConnectTimeout=25 -o ConnectionAttempts=1 -o BatchMode=yes",
    )
  ci_sync_state: dict[str, object] | None = None
  try:
    ci_sync_state: dict[str, object] = {
      "attempted": False,
      "pushed": False,
      "pushed_gitee": False,
      "pushed_codeup": False,
      "to_push": False,
      "branches": [],
      "sync_reason_tags": [],
      "archive_tag_kind": archive_tag_kind_from_env(),
    }
    # load optional .env next to this repo (never committed)
    sp_dotenv = load_dotenv(REPO_ROOT / ".env")
    for k, v in sp_dotenv.items():
      env.setdefault(k, v)
    ci_sync_state["archive_tag_kind"] = archive_tag_kind_from_env()
    ensure_sp_cn_token(env, required=args.build_installer)
    _push_sources = enabled_main_repo_push_sources()
    _device_src = main_repo_device_source()
    log(
      "config",
      f"主仓设备拉取={_device_src.label}({_device_src.id})；"
      f"推送={', '.join(s.label for s in _push_sources) or '（无，请检查 enabled_main_repo_push_sources）'}",
    )

    if (
      os.environ.get("SP_SYNC_SOURCE", "").strip().lower() == "local"
      and sys.stdout.isatty()
      and args.action in ("menu", "pull", "push", "all")
    ):
      print(
        "【sp-sync 本地】补丁与分支策略仍来自本脚本（与 CI 一致）。\n"
        "  · 上游 fetch 默认只更新分支 "
        + ", ".join(SYNC_BRANCHES)
        + "（不扫全仓库 tag，传输更少）；需要与 CI 完全一致时请设 SYNC_FULL_UPSTREAM_FETCH=1。\n"
        "  · 长耗时 git 直连本终端；fetch 默认带 --progress（可用 SYNC_LOCAL_GIT_PROGRESS=0 关闭）。\n",
        flush=True,
      )

    # remotes (idempotent)
    remotes = run(["git", "remote"], str(root), env=env).splitlines()
    if "upstream" not in remotes:
      run(["git", "remote", "add", "upstream", args.upstream], str(root), env=env)
    run(["git", "remote", "set-url", "upstream", args.upstream], str(root), env=env)
    gitee_src = main_repo_source_gitee()
    origin_url = (args.origin or gitee_src.ssh_url).strip()
    if "origin" not in remotes:
      run(["git", "remote", "add", "origin", origin_url], str(root), env=env)
    run(["git", "remote", "set-url", "origin", origin_url], str(root), env=env)
    codeup_src = main_repo_source_codeup()
    codeup_url = (env.get("ALIYUN_REPO_SSH") or codeup_src.ssh_url).strip()
    need_codeup_remote = any(s.id == "codeup" for s in _push_sources) or _device_src.id == "codeup"
    if need_codeup_remote:
      if "aliyun" not in remotes:
        run(["git", "remote", "add", "aliyun", codeup_url], str(root), env=env)
      run(["git", "remote", "set-url", "aliyun", codeup_url], str(root), env=env)

    fu_cmd, fu_log = upstream_fetch_argv()
    log("git", fu_log)
    run(fu_cmd, str(root), env=env)
    sm_mode = (env.get("SKIP_SUBMODULES", "auto").strip().lower() or "auto")
    force_sync = (env.get("FORCE_SYNC", "").strip().lower() in ("1", "true", "yes", "y", "on"))
    # legacy compatibility: "1/true" means always skip; "0/false" means always update
    if sm_mode in ("1", "true", "yes", "y", "on"):
      sm_mode = "skip"
    elif sm_mode in ("0", "false", "no", "n", "off"):
      sm_mode = "update"
    elif sm_mode not in ("auto", "skip", "update"):
      sm_mode = "auto"

    def hard_clean_worktree() -> None:
      """
      确保后续 checkout 不会被未跟踪文件阻塞。
      典型场景：子模块目录里残留了一堆文件，但 superproject 视角是 untracked，
      git 在切换分支时会提示 “未跟踪文件将会因为检出操作而被覆盖”。
      """
      # 先卸载子模块（避免 submodule 自身状态干扰），再清理工作区
      run(["git", "submodule", "deinit", "-f", "--all"], str(root), env=env)
      run(["git", "reset", "--hard"], str(root), env=env)
      # -d: 目录, -f: 强制, -x: 连同忽略文件一起清
      run(["git", "clean", "-fdx"], str(root), env=env)

    def sync_branch_local(branch: str) -> bool:
      # 若上游分支 HEAD 未变化，直接跳过（避免每小时重复跑一遍补丁/推送）
      upstream_sha = run(["git", "rev-parse", f"upstream/{branch}"], str(root), env=env).strip().lower()

      recorded_sha, recorded_canon, recorded_source, tip_head_sha = resolve_recorded_upstream_sha(
        root, env, branch
      )

      if (not force_sync) and (recorded_canon is not None) and (recorded_canon == upstream_sha):
        print(f"[skip] {branch}: upstream sha unchanged ({upstream_sha[:7]})")
        return False
      if force_sync and recorded_canon is not None and recorded_canon == upstream_sha:
        print(f"[force] {branch}: upstream sha unchanged but FORCE_SYNC=1, will re-sync")

      assert ci_sync_state is not None
      ci_sync_state["attempted"] = True
      if force_sync:
        ci_sync_state["force_sync"] = True
      ci_sync_state.setdefault("branches", []).append(branch)
      # 邮件区分「上游有新提交」vs「仅手动 Force」：Force 且无记录 / SHA 未变 → force_same
      if force_sync and (recorded_canon is None or recorded_canon == upstream_sha):
        ci_sync_state.setdefault("sync_reason_tags", []).append("force_same")
      else:
        ci_sync_state.setdefault("sync_reason_tags", []).append("upstream_delta")
        if force_sync:
          ci_sync_state.setdefault("sync_reason_tags", []).append("force_same")

      if recorded_canon is not None:
        rec_note = short_sha(recorded_canon) or recorded_canon[:EMAIL_SHA_LEN]
      elif recorded_sha:
        rec_note = short_sha(recorded_sha) or recorded_sha[:EMAIL_SHA_LEN]
      elif tip_head_sha:
        # 提交正文无 upstream-* 行时，用镜像 tip 短哈希（与旧版成功邮件一致）
        rec_note = short_sha(tip_head_sha) or tip_head_sha[:EMAIL_SHA_LEN]
      else:
        rec_note = "—"
      up_note = short_sha(upstream_sha) or upstream_sha[:EMAIL_SHA_LEN]
      ci_sync_state.setdefault("notify_branch_notes", []).append(
        f"- {branch}: 上次记录 upstream-{branch}={rec_note}（{recorded_source}）"
        f" → 当前 upstream/{branch}={up_note}"
      )

      reason_tag = (
        "force_same"
        if force_sync and (recorded_canon is None or recorded_canon == upstream_sha)
        else "upstream_delta"
      )
      try:
        commit_block = collect_upstream_commits_for_email(
          root,
          env,
          branch,
          recorded_canon,
          upstream_sha,
          reason_tag=reason_tag,
        )
        ci_sync_state.setdefault("upstream_commit_blocks", []).append(f"【{branch}】\n{commit_block}")
      except Exception as e:
        log(branch, f"upstream commit list for mail skipped: {e!r}")

      # 切换分支前先强制清理一次，避免被子模块残留文件阻塞
      hard_clean_worktree()
      # sync to upstream/<branch> baseline
      run(["git", "checkout", "-B", branch, f"upstream/{branch}"], str(root), env=env)

      # submodules（先按 upstream 的 .gitmodules 更新到正确 commit）
      # 说明：Gitee 镜像偶尔会缺少某些 submodule commit；若先改写为镜像 URL 再 update，
      # 会触发 "not our ref"。因此先用 upstream URL 完成 submodule update，再进行国内化改写。
      do_update_submodules = False
      if sm_mode == "skip":
        do_update_submodules = False
      elif sm_mode == "update":
        do_update_submodules = True
      else:
        rec_for_diff = recorded_canon or recorded_sha
        do_update_submodules = should_update_submodules(rec_for_diff, upstream_sha, root, env)

      if do_update_submodules:
        run(["git", "submodule", "sync", "--recursive"], str(root), env=env)
        ensure_tinygrad_submodule_commit_reachable(root)
        run(["git", "submodule", "update", "--init", "--recursive"], str(root), env=env)
      else:
        print(f"[skip] {branch}: SKIP_SUBMODULES=auto (no submodule pointer changes)")

      # apply patches (idempotent)
      log(branch, "apply patches")
      results = patch_all(root)
      changed = [r for r in results if r.changed]
      if changed:
        log(branch, "patch summary: " + ", ".join(f"{r.name}({len(r.changed_files)})" for r in changed))
      else:
        log(branch, "patch summary: no file changes")

      # patches 可能会改写 .gitmodules；同步一次 URL（不再更新 commit）
      if do_update_submodules:
        run(["git", "submodule", "sync", "--recursive"], str(root), env=env)

      log(branch, "verify patches (gate before commit/push)")
      verify_patches(root)

      # optional: installer build (device side)
      if args.build_installer:
        build_installers_if_possible(root)

      # commit if there are changes (per-branch)
      status = run(["git", "status", "--porcelain"], str(root), env=env)
      if status.strip():
        run(["git", "add", "-A"], str(root), env=env)
        upstream_short = upstream_sha[:7]
        msg = (
          f"based-on: sunnypilot/{branch}@{upstream_short}\n\n"
          "cn: redirect GitHub URLs to Gitee mirrors\n"
          f"upstream-{branch}: {upstream_sha}\n"
          "Made-with: tools\n"
        )
        run(["git",
             "-c", "user.name=sunnypilot-cn-bot",
             "-c", "user.email=sunnypilot-cn-bot@local",
             "commit", "-m", msg], str(root), env=env)
      else:
        # 若没有任何补丁改动但 upstream 已变化，也提交一个空提交记录 upstream SHA，便于后续跳过。
        upstream_short = upstream_sha[:7]
        msg = (
          f"based-on: sunnypilot/{branch}@{upstream_short}\n\n"
          "cn: sync upstream (no patch changes)\n"
          f"upstream-{branch}: {upstream_sha}\n"
          "Made-with: tools\n"
        )
        run(["git",
             "-c", "user.name=sunnypilot-cn-bot",
             "-c", "user.email=sunnypilot-cn-bot@local",
             "commit", "--allow-empty", "-m", msg], str(root), env=env)
      return True

    def push_branch(
      branch: str,
      *,
      targets: set[str] | None = None,
      squash_first: bool = False,
      sync_state: dict[str, object] | None = None,
    ) -> bool:
      """推送单个分支；targets 为 {"gitee","codeup"} 子集。返回是否至少一端 push 成功。"""
      log(branch, "verify patches (gate before push)")
      verify_patches(root)
      codeup_ssh_url = (env.get("ALIYUN_REPO_SSH") or main_repo_source_codeup().ssh_url).strip()
      want = targets or {s.id for s in enabled_main_repo_push_sources()}
      push_sources = [s for s in enabled_main_repo_push_sources() if s.id in want]
      if not push_sources:
        log("push", f"{branch}: 无匹配的推送目标（targets={want}）")
        return False
      if squash_first and _env_truthy("SYNC_GITEE_SINGLE_COMMIT"):
        squash_branch_single_commit(root, branch, env)
      did_push = False
      gitee_to = push_target_timeout_s("gitee")

      def _push_gitee() -> None:
        origin_env = dict(env)
        origin_env["GIT_SSH_COMMAND"] = _gitee_git_ssh_env()["GIT_SSH_COMMAND"]
        origin_env["GIT_LFS_SKIP_PUSH"] = "1"
        run(
          ["git", "push", "-f", "-u", "origin", branch],
          str(root),
          env=origin_env,
          timeout_s=gitee_to,
        )
        log("push", f"{branch}: {main_repo_source_gitee().label} pushed (origin)")

      def _push_codeup_ssh() -> None:
        run(
          ["git", "push", "-f", "-u", "aliyun", branch],
          str(root),
          env=aliyun_git_push_env(env),
        )
        log("push", f"{branch}: {main_repo_source_codeup().label} pushed via SSH (aliyun)")

      def _push_codeup_https() -> None:
        token = sp_cn_token_from_env(env)
        if not token:
          raise RuntimeError(f"缺少 {SP_CN_TOKEN_ENV}/SP_CN_TOKEN，无法 HTTPS 推送到 Codeup")
        push_url = codeup_https_url_with_token(token, env)
        run(["git", "remote", "set-url", "aliyun", push_url], str(root), env=env)
        try:
          run(
            ["git", "push", "-f", "-u", "aliyun", branch],
            str(root),
            env=aliyun_git_https_push_env(env),
          )
        finally:
          run(["git", "remote", "set-url", "aliyun", codeup_ssh_url], str(root), env=env)
        log("push", f"{branch}: {main_repo_source_codeup().label} pushed via HTTPS (aliyun)")

      def _push_codeup() -> None:
        if not aliyun_push_available(env):
          log("push", f"{branch}: {main_repo_source_codeup().label} push skipped（无 SSH 密钥且无 HTTPS 令牌）")
          if sync_state is not None:
            record_push_branch(sync_state, "codeup", branch, "skip", "无 SSH 密钥且无 HTTPS 令牌")
          return
        if aliyun_push_via_https(env):
          try:
            _push_codeup_https()
          except RuntimeError as e:
            if _is_codeup_https_auth_error(e) and aliyun_ssh_key_path().exists():
              log("warn", "Codeup HTTPS 认证失败，回退 SSH push")
              _push_codeup_ssh()
            else:
              raise
        else:
          _push_codeup_ssh()

      for src in push_sources:
        tid = src.id
        tries = 2 if tid == "gitee" else 4
        try:
          if tid == "gitee":
            retry(f"git push origin {branch}", _push_gitee, tries=tries, base_sleep_s=2.0)
          elif tid == "codeup":
            retry(f"git push aliyun {branch}", _push_codeup, tries=tries, base_sleep_s=2.0)
          if sync_state is not None:
            pr = ensure_push_results(sync_state)
            br = (pr.get(tid) or {}).get("branches", {}) if isinstance(pr.get(tid), dict) else {}
            if not (isinstance(br, dict) and branch in br and br[branch].get("status") == "skip"):
              record_push_branch(sync_state, tid, branch, "ok")
          did_push = True
        except Exception as e:
          st_label, detail = _exception_push_status(e)
          if sync_state is not None:
            record_push_branch(sync_state, tid, branch, st_label, detail)
          log("push", f"{branch}: {src.label} {st_label}: {e}")
      if sync_state is not None:
        for src in push_sources:
          finalize_push_target(sync_state, src.id)
      return did_push

    def pull_all() -> list[str]:
      to_push: list[str] = []
      for b in SYNC_BRANCHES:
        if sync_branch_local(b):
          to_push.append(b)
      return to_push

    def push_all(
      branches: list[str] | None = None,
      *,
      targets: set[str] | None = None,
      squash_first: bool = False,
      sync_state: dict[str, object] | None = None,
    ) -> bool:
      branches = branches or list(SYNC_BRANCHES)
      any_pushed = False
      for b in branches:
        try:
          run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{b}"], str(root), env=env)
        except Exception:
          print(f"[skip] push {b}: local branch missing")
          if sync_state is not None and targets:
            for tid in targets:
              record_push_branch(sync_state, tid, b, "skip", "本地分支不存在")
              finalize_push_target(sync_state, tid)
          continue
        if push_branch(
          b,
          targets=targets,
          squash_first=squash_first and not any_pushed,
          sync_state=sync_state,
        ):
          any_pushed = True
      return any_pushed

    def _ci_state_for_push() -> dict[str, object]:
      st = load_ci_sync_state()
      if st:
        ensure_push_results(st)
        return st
      assert ci_sync_state is not None
      ensure_push_results(ci_sync_state)
      return ci_sync_state

    def _target_push_failed(st: dict[str, object], target_id: str) -> bool:
      pr = st.get("push_results") or {}
      entry = pr.get(target_id) if isinstance(pr, dict) else None
      if not isinstance(entry, dict) or not entry.get("enabled"):
        return False
      return entry.get("overall") in ("fail", "timeout", "partial")

    def _branches_to_push_from_state() -> list[str]:
      st = load_ci_sync_state()
      if not st.get("to_push"):
        return []
      br = st.get("branches") or []
      return [str(b) for b in br] if br else list(SYNC_BRANCHES)

    def maybe_sync_mapd_release() -> None:
      if not args.sync_mapd_release:
        return
      token = env.get("GITEE_TOKEN", "").strip().strip('"')
      if not token:
        raise RuntimeError("缺少环境变量 GITEE_TOKEN（只从环境变量读取，不写入仓库）")
      sync_mapd_release(token, env.get("MAPD_TAG", "latest"))

    def do_all() -> None:
      to_push = pull_all()
      _finalize_pull_state(to_push)
      env.setdefault("GIT_LFS_SKIP_PUSH", "1")
      if not to_push:
        print("[skip] no branches changed; nothing to push")
      else:
        push_all(to_push, squash_first=True, sync_state=ci_sync_state)
        assert ci_sync_state is not None
        update_pushed_flags_from_push_results(ci_sync_state)
        maybe_archive_cn_tag_after_ci_push(root, env, ci_sync_state)
      if args.force_staging:
        log("warn", "--force-staging 已废弃：当前仅同步 staging，不再执行 master→staging 强推。")
      maybe_sync_mapd_release()

    def _finalize_pull_state(to_push: list[str]) -> None:
      assert ci_sync_state is not None
      ci_sync_state["to_push"] = bool(to_push)
      if to_push:
        ci_sync_state["branches"] = list(to_push)
      ensure_push_results(ci_sync_state)
      save_ci_sync_state(ci_sync_state)

    def interactive_menu() -> None:
      if not sys.stdin.isatty():
        # 非交互环境（比如 CI）：回退到 all，保证不挂起。。
        do_all()
        return

      while True:
        print("\n=== sync_to_gitee 菜单（CI 路径请用 --action all；mapd/installer 请用 sync_to_gitee_local.py）===")
        print("1) 拉取 upstream + 仅对 staging 应用补丁 + 更新子模块（不推送）")
        print("2) 推送本地 staging 到 Gitee + 云效 Codeup（强推）")
        print("3) 一键执行（1 + 2）")
        print("0) 退出\n")
        choice = input("请选择操作 [0-3]: ").strip()
        try:
          if choice == "1":
            pull_all()
            print("[ok] 已完成拉取+补丁+子模块更新。")
          elif choice == "2":
            env.setdefault("GIT_LFS_SKIP_PUSH", "1")
            push_all()
            if args.force_staging:
              log("warn", "--force-staging 已废弃：当前仅同步 staging，不再执行 master→staging 强推。")
            print("[ok] 已完成推送。")
          elif choice == "3":
            do_all()
            print("[ok] 已完成一键执行。")
          elif choice == "0":
            return
          else:
            print("无效选择，请输入 0-3。")
        except Exception as e:
          print(f"[error] {e}")
          print(traceback.format_exc())

    if args.action == "menu":
      interactive_menu()
    elif args.action == "pull":
      to_push = pull_all()
      _finalize_pull_state(to_push)
      maybe_sync_mapd_release()
    elif args.action == "push":
      env.setdefault("GIT_LFS_SKIP_PUSH", "1")
      st = _ci_state_for_push()
      branches = _branches_to_push_from_state() or list(SYNC_BRANCHES)
      push_all(branches, squash_first=True, sync_state=st)
      update_pushed_flags_from_push_results(st)
      save_ci_sync_state(st)
      maybe_archive_cn_tag_after_ci_push(root, env, st)
      if args.force_staging:
        log("warn", "--force-staging 已废弃：当前仅同步 staging，不再执行 master→staging 强推。")
      maybe_sync_mapd_release()
    elif args.action == "push-gitee":
      env.setdefault("GIT_LFS_SKIP_PUSH", "1")
      st = _ci_state_for_push()
      if "gitee" not in enabled_push_target_ids():
        for b in (st.get("branches") or list(SYNC_BRANCHES)):
          record_push_branch(st, "gitee", str(b), "disabled")
        finalize_push_target(st, "gitee")
        update_pushed_flags_from_push_results(st)
        save_ci_sync_state(st)
        print("[skip] push-gitee: enabled_main_repo_push_sources 未启用 Gitee")
      else:
        branches = _branches_to_push_from_state()
        if not branches:
          print("[skip] push-gitee: pull 阶段未产生待推送分支（upstream 未变？）")
        else:
          try:
            push_all(branches, targets={"gitee"}, squash_first=False, sync_state=st)
          finally:
            update_pushed_flags_from_push_results(st)
            save_ci_sync_state(st)
          if _target_push_failed(st, "gitee"):
            raise SystemExit(1)
    elif args.action == "push-codeup":
      st = _ci_state_for_push()
      if "codeup" not in enabled_push_target_ids():
        for b in (st.get("branches") or list(SYNC_BRANCHES)):
          record_push_branch(st, "codeup", str(b), "disabled")
        finalize_push_target(st, "codeup")
        update_pushed_flags_from_push_results(st)
        save_ci_sync_state(st)
        print("[skip] push-codeup: enabled_main_repo_push_sources 未启用 Codeup")
      else:
        branches = _branches_to_push_from_state()
        if not branches:
          print("[skip] push-codeup: pull 阶段未产生待推送分支（upstream 未变？）")
        else:
          try:
            push_all(branches, targets={"codeup"}, squash_first=True, sync_state=st)
          finally:
            update_pushed_flags_from_push_results(st)
            save_ci_sync_state(st)
          if _target_push_failed(st, "codeup"):
            raise SystemExit(1)
    elif args.action == "emit-outputs":
      write_ci_github_output(load_ci_sync_state())
    elif args.action == "archive-tag":
      if _env_truthy("SYNC_SKIP_ARCHIVE_TAG"):
        print("[skip] archive-tag: SYNC_SKIP_ARCHIVE_TAG=1")
      else:
        st = load_ci_sync_state()
        update_pushed_flags_from_push_results(st)
        if not st.get("to_push"):
          print("[skip] archive-tag: pull 阶段未产生待推送分支")
        elif not st.get("pushed"):
          print("[skip] archive-tag: 本轮无成功推送")
        else:
          want = _archive_tag_push_targets_from_state(st)
          create_and_push_cn_archive_tag(
            root, env, targets=want, tag_kind=archive_tag_kind_from_env(),
          )
    elif args.action == "verify-tinygrad-models":
      verify_tinygrad_models_alignment(root)
      print("[ok] tinygrad_repo 与 models JSON tinygrad_ref 一致")
    elif args.action == "all":
      do_all()
    else:
      raise RuntimeError(f"未知 action: {args.action}")

  finally:
    if shim_dir is not None:
      try:
        for p in shim_dir.glob("*"):
          p.unlink(missing_ok=True)
        shim_dir.rmdir()
      except Exception:
        pass
    try:
      if ci_sync_state is not None and args.action == "all":
        save_ci_sync_state(ci_sync_state)
        write_ci_github_output(ci_sync_state)
    except Exception:
      pass


if __name__ == "__main__":
  main()

