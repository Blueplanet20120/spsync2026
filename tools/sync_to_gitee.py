#!/usr/bin/env python3
"""
工作区级（/home/perfume/sp）同步脚本：

- 拉取上游 upstream/{master,staging}
- 应用国内化补丁（幂等）
- 更新子模块（包含子依赖）
- 可选：在 larch64 上构建 installer
- 可选：同步 mapd 二进制到 Gitee Release（读取 GITEE_TOKEN）
- 提交并推送到你的 Gitee：master + staging（可选保持旧逻辑：staging=master）

用法示例：
  python3 /home/perfume/sp/tools/sync_to_gitee.py              # 交互菜单（默认）
  python3 /home/perfume/sp/tools/sync_to_gitee.py --action all # 一键拉取+打补丁+推送
  python3 /home/perfume/sp/tools/sync_to_gitee.py --build-installer
  GITEE_TOKEN=xxx python3 /home/perfume/sp/tools/sync_to_gitee.py --sync-mapd-release
"""

import argparse
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import datetime
import time
import urllib.error
import urllib.request
import traceback
from pathlib import Path
from dataclasses import dataclass, field


REPO_ROOT = Path(__file__).resolve().parents[1]


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
REPO_DEFAULT = "git@gitee.com:xc2026/sunnypilot_cn.git"
UPSTREAM_DEFAULT = "https://github.com/sunnypilot/sunnypilot.git"

COMMA_HOST_DEFAULT = "10.90.1.231"
COMMA_USER_DEFAULT = "comma"
COMMA_SSH_KEY_DEFAULT = str(Path("/home/perfume/sp/sp-cn"))
INSTALLER_REPO_DEFAULT = "https://gitee.com/xc2026/sp-cn_install.git"

GITEE_OWNER = "xc2026"
MAPD_REPO = "openpilot-mapd"
MAPD_UPSTREAM = "pfeiferj/openpilot-mapd"


def run(cmd: list[str], cwd: str | None = None, env: dict[str, str] | None = None, *, stream: bool | None = None) -> str:
  """
  默认行为：
  - 交互终端（TTY）下：实时输出（避免 git fetch 等长任务“看起来没反应”）
  - CI/非交互：保持 capture（便于错误时把完整输出带回日志）
  """
  if stream is None:
    stream = sys.stdout.isatty()

  if not stream:
    p = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
      raise RuntimeError(f"命令失败: {' '.join(cmd)}\n{p.stdout}")
    return p.stdout

  # stream mode
  p2 = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
  assert p2.stdout is not None
  out_lines: list[str] = []
  for line in p2.stdout:
    sys.stdout.write(line)
    sys.stdout.flush()
    out_lines.append(line)
  rc = p2.wait()
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
  repo_url_https = "https://gitee.com/xc2026/sunnypilot_cn.git"
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


def http_multipart_post(url: str, fields: dict[str, str], file_field: str, filename: str, file_bytes: bytes) -> dict:
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

  req = urllib.request.Request(url, data=body, method="POST")
  req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
  req.add_header("Content-Length", str(len(body)))
  def _do() -> dict:
    with urllib.request.urlopen(req, timeout=120) as resp:
      data = resp.read().decode("utf-8")
      return json.loads(data) if data else {}
  return retry(f"http_multipart_post {url}", _do, tries=3, base_sleep_s=1.0)


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
    path.write_text(s, encoding="utf-8")


def write_if_changed(path: Path, new_text: str) -> bool:
  old = path.read_text(encoding="utf-8") if path.exists() else ""
  if new_text != old:
    path.write_text(new_text, encoding="utf-8")
    return True
  return False


def replace_or_fail_changed(path: Path, replacements: list[tuple[str, str]]) -> bool:
  s = path.read_text(encoding="utf-8")
  orig = s
  for a, b in replacements:
    if a not in s and b not in s:
      raise RuntimeError(f"{path} 未找到预期内容: {a}")
    s = s.replace(a, b)
  return write_if_changed(path, s) if s != orig else False


def parse_recorded_upstream_sha(commit_body: str, branch: str) -> str | None:
  """
  约定：在提交信息中记录本次同步对应的上游分支 HEAD，例如：
    upstream-staging: <sha>
  """
  m = re.search(rf"(?m)^upstream-{re.escape(branch)}:\s*([0-9a-f]{{7,40}})\s*$", commit_body or "")
  return m.group(1) if m else None


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


def patch_installer_urls(root: Path) -> PatchResult:
  res = PatchResult("installer_urls")
  installer = root / "selfdrive/ui/installer/installer.cc"
  changed = replace_or_fail_changed(installer, [
    ("https://github.com/commaai/openpilot.git", "https://gitee.com/xc2026/sunnypilot_cn.git"),
    ('#define GIT_SSH_URL "git@github.com:commaai/openpilot.git"', '#define GIT_SSH_URL "git@gitee.com:xc2026/sunnypilot_cn.git"'),
  ])
  _track_change(res, installer, changed)
  return res


def patch_version_py(root: Path) -> PatchResult:
  res = PatchResult("system_version_py")
  version_py = root / "system/version.py"
  s = version_py.read_text(encoding="utf-8")
  s2 = ensure_line_in_tuple_block(s, "gitee.com/xc2026/sunnypilot_cn")
  _track_change(res, version_py, write_if_changed(version_py, s2))
  return res


def patch_updated_insteadof(root: Path) -> PatchResult:
  res = PatchResult("updated_insteadof")
  updated_py = root / "system/updated/updated.py"
  s = updated_py.read_text(encoding="utf-8")
  if "ensure_url_insteadof(" not in s:
    insert_after = "for option, value in git_cfg:\n    run([\"git\", \"config\", option, value], cwd)\n"
    if insert_after not in s:
      raise RuntimeError("updated.py 结构变化，无法自动注入 insteadof 规则")
    inject = insert_after + """

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
  ensure_url_insteadof(\"url.https://gitee.com/xc2026/.insteadof\", \"https://github.com/\")
  ensure_url_insteadof(\"url.https://gitee.com/xc2026/.insteadof\", \"https://raw.githubusercontent.com/\")
  ensure_url_insteadof(\"url.git@gitee.com:xc2026/.insteadof\", \"git@github.com:\")
  ensure_url_insteadof(\"url.git@gitee.com:xc2026/.insteadof\", \"ssh://git@github.com/\")
"""
    s2 = s.replace(insert_after, inject)
    _track_change(res, updated_py, write_if_changed(updated_py, s2))
  return res


def patch_tici_setup(root: Path) -> PatchResult:
  res = PatchResult("tici_setup")
  tici_setup = root / "system/ui/tici_setup.py"
  if not tici_setup.exists():
    return res

  s = tici_setup.read_text(encoding="utf-8")
  orig = s
  if 'OPENPILOT_URL = "https://openpilot.comma.ai"' in s:
    s = s.replace('OPENPILOT_URL = "https://openpilot.comma.ai"\n',
                  'OPENPILOT_URL = "https://gitee.com/xc2026/sp-cn_install/raw/master/installer_openpilot"\n')
  if "CONNECTIVITY_CHECK_URLS" not in s:
    s = s.replace(
      'OPENPILOT_URL = "https://gitee.com/xc2026/sp-cn_install/raw/master/installer_openpilot"\n',
      'OPENPILOT_URL = "https://gitee.com/xc2026/sp-cn_install/raw/master/installer_openpilot"\n'
      '# 国内环境可能无法访问 openpilot.comma.ai，导致安装流程卡在 “Waiting for internet”。\n'
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
    s = s.replace(
      "          if HARDWARE.get_network_type() == NetworkType.wifi:\n"
      "            self.wifi_connected.set()\n"
      "          else:\n"
      "            self.wifi_connected.clear()\n",
      "          # Wi-Fi connect detection in recovery should not depend on network_type reporting.\n"
      "          # If wlan0 has an IPv4 address, treat it as connected.\n"
      "          if wlan0_has_ipv4():\n"
      "            self.wifi_connected.set()\n"
      "          else:\n"
      "            self.wifi_connected.clear()\n"
    )
  if s != orig:
    _track_change(res, tici_setup, write_if_changed(tici_setup, s))
  return res


def patch_mici_setup(root: Path) -> PatchResult:
  res = PatchResult("mici_setup")
  mici_setup = root / "system/ui/mici_setup.py"
  if not mici_setup.exists():
    return res
  s = mici_setup.read_text(encoding="utf-8")
  orig = s
  if 'OPENPILOT_URL = "https://openpilot.comma.ai"' in s:
    s = s.replace('OPENPILOT_URL = "https://openpilot.comma.ai"\n',
                  'OPENPILOT_URL = "https://gitee.com/xc2026/sp-cn_install/raw/master/installer_openpilot"\n')
  if "CONNECTIVITY_CHECK_URLS" not in s:
    s = s.replace(
      'OPENPILOT_URL = "https://gitee.com/xc2026/sp-cn_install/raw/master/installer_openpilot"\n',
      'OPENPILOT_URL = "https://gitee.com/xc2026/sp-cn_install/raw/master/installer_openpilot"\n'
      '# 国内环境可能无法访问 openpilot.comma.ai，导致 setup 卡在 “waiting for internet...”。\n'
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

  if "wifi_connected = self._wifi_manager.wifi_state.status == ConnectStatus.CONNECTED" not in s:
    s = s.replace(
      "    has_internet = (self._network_monitor.network_connected.is_set() and\n"
      "                    not network_changing and\n"
      "                    not self._network_monitor.recheck_event.is_set())\n",
      "    # 恢复/安装流程中，某些网络环境下“联网探测”可能失败（DNS/证书/出口限制），导致卡死。\n"
      "    # 只要 Wi-Fi 已连接，就允许继续；后续实际更新/下载阶段再做真正的网络错误提示。\n"
      "    wifi_connected = self._wifi_manager.wifi_state.status == ConnectStatus.CONNECTED\n"
      "    has_internet = (wifi_connected or self._network_monitor.network_connected.is_set()) and \\\n"
      "                   (not network_changing) and \\\n"
      "                   (not self._network_monitor.recheck_event.is_set())\n"
    )

  if s != orig:
    _track_change(res, mici_setup, write_if_changed(mici_setup, s))
  return res


def patch_setup_sh(root: Path) -> PatchResult:
  res = PatchResult("tools_setup_sh")
  setup_sh = root / "tools/setup.sh"
  changed = replace_or_fail_changed(setup_sh, [
    ("https://github.com/commaai/openpilot.git", "https://gitee.com/xc2026/sunnypilot_cn.git"),
    ("https://github.com/commaai/openpilot/blob/master/docs/CONTRIBUTING.md",
     "https://gitee.com/xc2026/sunnypilot_cn/blob/master/docs/CONTRIBUTING.md"),
  ])
  _track_change(res, setup_sh, changed)
  return res


def patch_msgq_setup(root: Path) -> PatchResult:
  res = PatchResult("msgq_setup")
  msgq_setup = root / "msgq_repo/setup.sh"
  if msgq_setup.exists():
    _track_change(res, msgq_setup, replace_or_fail_changed(msgq_setup, [
      ("https://github.com/catchorg/Catch2.git", "https://gitee.com/xc2026/Catch2.git"),
    ]))
  return res


def patch_opendbc_pyproject(root: Path) -> PatchResult:
  res = PatchResult("opendbc_pyproject")
  opendbc_pyproj = root / "opendbc_repo/pyproject.toml"
  if opendbc_pyproj.exists():
    s = opendbc_pyproj.read_text(encoding="utf-8")
    s2 = s.replace("git+https://github.com/commaai/dependencies.git", "git+https://gitee.com/xc2026/dependencies.git")
    _track_change(res, opendbc_pyproj, write_if_changed(opendbc_pyproj, s2))
  return res


def patch_models_fetcher(root: Path) -> PatchResult:
  res = PatchResult("models_fetcher")
  fetcher = root / "sunnypilot/models/fetcher.py"
  _track_change(res, fetcher, replace_or_fail_changed(fetcher, [
    ("https://raw.githubusercontent.com/sunnypilot/sunnypilot-models/",
     "https://gitee.com/xc2026/sunnypilot-models/raw/"),
  ]))
  return res


def patch_osm(root: Path) -> PatchResult:
  res = PatchResult("osm_layout")
  osm_py = root / "selfdrive/ui/sunnypilot/layouts/settings/osm.py"
  _track_change(res, osm_py, replace_or_fail_changed(osm_py, [
    ("https://raw.githubusercontent.com/pfeiferj/openpilot-mapd/main/",
     "https://gitee.com/xc2026/openpilot-mapd/raw/main/"),
  ]))
  return res


def patch_mapd_installer(root: Path) -> PatchResult:
  res = PatchResult("mapd_installer")
  mapd_installer = root / "sunnypilot/mapd/mapd_installer.py"
  s = mapd_installer.read_text(encoding="utf-8")
  if "gitee.com/xc2026/openpilot-mapd" not in s:
    s2 = s.replace('VERSION = "v1.12.0"', 'VERSION = os.getenv("MAPD_TAG", "v1.12.0")')
    s2 = s2.replace("https://github.com/pfeiferj/openpilot-mapd/releases/download/",
                    "https://gitee.com/xc2026/openpilot-mapd/releases/download/")
    _track_change(res, mapd_installer, write_if_changed(mapd_installer, s2))
  return res


def patch_dm_relaxed_terminal(root: Path) -> PatchResult:
  """
  国内化 DM：保留分级告警/鸣音，但削弱「第三次终端锁死」与 awareness<0 触发的纵向 forceDecel。
  - awareness 递减下限由 -0.1 改为 0，避免 driverMonitoringState.awarenessStatus<0
  - 红线阶段不再累计 terminal_time / terminal_alert_cnt，避免 DriverTooDistracted / tooDistracted 路径
  幂等：已含 sentinel 时 replace_or_fail_changed 不报错。
  """
  res = PatchResult("dm_relaxed_terminal")
  helpers = root / "selfdrive/monitoring/helpers.py"
  if not helpers.exists():
    return res
  changed = replace_or_fail_changed(helpers, [
    (
      "        self.awareness = max(self.awareness - self.step_change, -0.1)",
      "        self.awareness = max(self.awareness - self.step_change, 0.)  # cn_dm_relaxed: avoid awarenessStatus<0 (forceDecel)",
    ),
    (
      "    if self.awareness <= 0.:\n"
      "      # terminal red alert: disengagement required\n"
      "      alert = EventName.driverDistracted3 if self.active_monitoring_mode else EventName.driverUnresponsive3\n"
      "      self.terminal_time += 1\n"
      "      if awareness_prev > 0.:\n"
      "        self.terminal_alert_cnt += 1",
      "    if self.awareness <= 0.:\n"
      "      # terminal red alert: disengagement required (cn_dm_relaxed: no terminal strike lockout)\n"
      "      alert = EventName.driverDistracted3 if self.active_monitoring_mode else EventName.driverUnresponsive3",
    ),
  ])
  _track_change(res, helpers, changed)
  return res


def patch_gitmodules(root: Path) -> PatchResult:
  res = PatchResult("gitmodules_urls")
  gitmodules = root / ".gitmodules"
  if not gitmodules.exists():
    return res
  gm = gitmodules.read_text(encoding="utf-8")
  gm2 = gm
  gm2 = gm2.replace("https://github.com/commaai/msgq.git", "git@gitee.com:xc2026/msgq.git")
  gm2 = gm2.replace("https://github.com/sunnypilot/opendbc.git", "git@gitee.com:xc2026/opendbc.git")
  gm2 = gm2.replace("https://github.com/commaai/rednose.git", "git@gitee.com:xc2026/rednose.git")
  gm2 = gm2.replace("https://github.com/commaai/teleoprtc", "git@gitee.com:xc2026/teleoprtc.git")
  gm2 = gm2.replace("https://github.com/sunnypilot/tinygrad.git", "git@gitee.com:xc2026/tinygrad.git")
  gm2 = gm2.replace("https://github.com/sunnyhaibin/panda.git", "git@gitee.com:xc2026/panda.git")
  gm2 = gm2.replace("https://github.com/sunnypilot/neural-network-data.git", "git@gitee.com:xc2026/neural_network_data.git")
  _track_change(res, gitmodules, write_if_changed(gitmodules, gm2))
  return res


def patch_all(root: Path) -> list[PatchResult]:
  patches = [
    patch_installer_urls,
    patch_version_py,
    patch_updated_insteadof,
    patch_tici_setup,
    patch_mici_setup,
    patch_setup_sh,
    patch_msgq_setup,
    patch_opendbc_pyproject,
    patch_models_fetcher,
    patch_osm,
    patch_mapd_installer,
    patch_dm_relaxed_terminal,
    patch_gitmodules,
  ]
  results: list[PatchResult] = []
  for fn in patches:
    r = fn(root)
    results.append(r)
  return results


def verify_patches(root: Path) -> None:
  """
  补丁后的门禁校验：失败则阻止提交/推送，避免半成品国内化进入 Gitee。
  与 patch_* 中的 replace_or_fail 互补（后者对缺字符串硬失败；此处捕获条件补丁未生效的情况）。
  """
  errors: list[str] = []

  def rt(rel: str) -> str:
    p = root / rel
    if not p.exists():
      return ""
    return p.read_text(encoding="utf-8", errors="replace")

  inst = rt("selfdrive/ui/installer/installer.cc")
  if not inst.strip():
    errors.append("selfdrive/ui/installer/installer.cc: 文件缺失或为空")
  elif "gitee.com/xc2026/sunnypilot_cn" not in inst and "git@gitee.com:xc2026/sunnypilot_cn" not in inst:
    errors.append("installer.cc: 未包含 Gitee sunnypilot_cn 安装源")

  if "gitee.com/xc2026/sunnypilot_cn" not in rt("system/version.py"):
    errors.append("system/version.py: sunnypilot_remote 元组中缺少 Gitee URL")

  if "ensure_url_insteadof" not in rt("system/updated/updated.py"):
    errors.append("system/updated/updated.py: 缺少 ensure_url_insteadof 注入")

  if "gitee.com/xc2026/sunnypilot_cn" not in rt("tools/setup.sh"):
    errors.append("tools/setup.sh: 缺少 Gitee sunnypilot_cn URL")

  if (root / "msgq_repo/setup.sh").exists() and "gitee.com/xc2026/Catch2" not in rt("msgq_repo/setup.sh"):
    errors.append("msgq_repo/setup.sh: 缺少 Gitee Catch2 URL")

  if (root / "opendbc_repo/pyproject.toml").exists():
    if "gitee.com/xc2026/dependencies" not in rt("opendbc_repo/pyproject.toml"):
      errors.append("opendbc_repo/pyproject.toml: 缺少 Gitee dependencies URL")

  if "gitee.com/xc2026/sunnypilot-models" not in rt("sunnypilot/models/fetcher.py"):
    errors.append("sunnypilot/models/fetcher.py: 缺少 Gitee models raw URL")

  if "gitee.com/xc2026/openpilot-mapd" not in rt("selfdrive/ui/sunnypilot/layouts/settings/osm.py"):
    errors.append("selfdrive/ui/sunnypilot/layouts/settings/osm.py: 缺少 Gitee mapd raw URL")

  if "gitee.com/xc2026/openpilot-mapd" not in rt("sunnypilot/mapd/mapd_installer.py"):
    errors.append("sunnypilot/mapd/mapd_installer.py: 缺少 Gitee mapd release URL")

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
    if (root / rel).exists():
      tx = rt(rel)
      if "OPENPILOT_URL" in tx and "gitee.com/xc2026/sp-cn_install" not in tx:
        errors.append(f"{label}: 仍存在 OPENPILOT_URL 但未指向 Gitee sp-cn_install")

  helpers_dm = root / "selfdrive/monitoring/helpers.py"
  if helpers_dm.exists():
    hm = rt("selfdrive/monitoring/helpers.py")
    if "cn_dm_relaxed: avoid awarenessStatus<0 (forceDecel)" not in hm:
      errors.append("selfdrive/monitoring/helpers.py: 缺少 DM 补丁 sentinel（awareness 下限）")
    if "cn_dm_relaxed: no terminal strike lockout" not in hm:
      errors.append("selfdrive/monitoring/helpers.py: 缺少 DM 补丁 sentinel（terminal 累计）")
    if "max(self.awareness - self.step_change, -0.1)" in hm:
      errors.append("selfdrive/monitoring/helpers.py: 仍存在原版 -0.1 awareness 下限，DM 补丁未生效")
    if (
      "alert = EventName.driverDistracted3 if self.active_monitoring_mode else EventName.driverUnresponsive3\n"
      "      self.terminal_time += 1\n"
    ) in hm:
      errors.append("selfdrive/monitoring/helpers.py: 红线分支仍在累计 terminal_time，DM 补丁未生效")

  # 语法兜底（不验证逻辑正确性）
  py_verify = [
    "system/version.py",
    "system/updated/updated.py",
    "system/ui/tici_setup.py",
    "system/ui/mici_setup.py",
    "sunnypilot/models/fetcher.py",
    "selfdrive/ui/sunnypilot/layouts/settings/osm.py",
    "sunnypilot/mapd/mapd_installer.py",
    "selfdrive/monitoring/helpers.py",
  ]
  for rel in py_verify:
    p = root / rel
    if p.exists():
      try:
        py_compile.compile(str(p), doraise=True)
      except py_compile.PyCompileError as e:
        errors.append(f"{rel}: py_compile 失败: {e}")

  if errors:
    raise RuntimeError("verify_patches 失败（不会提交/推送）：\n  - " + "\n  - ".join(errors))


def write_ci_github_output(state: dict[str, object]) -> None:
  """供 GitHub Actions 读取 steps.sync.outputs.*（双场景邮件条件）。"""
  if os.environ.get("GITHUB_ACTIONS") != "true":
    return
  path = os.environ.get("GITHUB_OUTPUT")
  if not path:
    return
  attempted = bool(state.get("attempted"))
  pushed = bool(state.get("pushed"))
  branches = state.get("branches") or []
  br_line = ",".join(str(b) for b in branches) if branches else ""
  reason_tags: list[str] = list(state.get("sync_reason_tags") or [])

  # 成功邮件正文：区分「上游相对 Gitee 记录有新版本」与「仅 Force 强制重同步」
  variant = "upstream_only"
  if reason_tags:
    uniq = set(reason_tags)
    if uniq == {"force_same"}:
      variant = "force_only"
    elif uniq == {"upstream_delta"}:
      variant = "upstream_only"
    else:
      variant = "mixed"

  line_ok = "sunnypilot_cn → Gitee 同步成功（本轮已执行补丁校验并完成推送）。"
  if variant == "force_only":
    line_note = "说明：本次为手动 Force 强制重同步（sunnypilot 上游相对上次写入 Gitee 的记录无新版本）。"
  elif variant == "upstream_only":
    line_note = "说明：本次因 sunnypilot 上游相对上次 Gitee 提交有新版本而同步推送。"
  else:
    line_note = "说明：本次 master/staging 中兼有「上游有新版本」与「强制重同步」分支，详见 Actions 日志。"
  success_body = f"{line_ok}\n\n{line_note}"

  delim = "SYNC_OK_BODY_EOF"
  with open(path, "a", encoding="utf-8") as f:
    f.write(f"attempted_sync={'true' if attempted else 'false'}\n")
    f.write(f"pushed={'true' if pushed else 'false'}\n")
    f.write(f"sync_branches={br_line}\n")
    f.write(f"notify_variant={variant}\n")
    f.write(f"notify_success_body<<{delim}\n")
    f.write(success_body)
    if not success_body.endswith("\n"):
      f.write("\n")
    f.write(f"{delim}\n")


def ensure_tinygrad_submodule_commit_reachable() -> None:
  sha = "3501a714785ff370cffb966a45d5f9cdf6c9ea7a"
  url = "git@gitee.com:xc2026/tinygrad.git"
  with tempfile.TemporaryDirectory() as td:
    run(["git", "init"], td)
    run(["git", "remote", "add", "origin", url], td)
    try:
      run(["git", "fetch", "--depth=1", "origin", sha], td)
      return
    except Exception:
      pass

  branch = "submodule-pin-3501a714"
  with tempfile.TemporaryDirectory() as td:
    run(["git", "init"], td)
    run(["git", "remote", "add", "upstream", "https://github.com/sunnypilot/tinygrad.git"], td)
    run(["git", "fetch", "--depth=1", "upstream", sha], td)
    run(["git", "update-ref", f"refs/heads/{branch}", sha], td)
    run(["git", "remote", "add", "origin", url], td)
    run(["git", "push", "origin", f"+refs/heads/{branch}:refs/heads/{branch}"], td)


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


def gitee_get_release_by_tag(token: str, tag: str) -> dict | None:
  url = gitee_api_url(f"/repos/{GITEE_OWNER}/{MAPD_REPO}/releases/tags/{tag}", token)
  try:
    return http_json(url)
  except urllib.error.HTTPError as e:
    if e.code == 404:
      return None
    raise


def gitee_create_release(token: str, tag: str) -> dict:
  url = gitee_api_url(f"/repos/{GITEE_OWNER}/{MAPD_REPO}/releases", token)
  payload = json.dumps({
    "tag_name": tag,
    "name": tag,
    "body": f"Auto-synced mapd binary for {tag}",
    "prerelease": False,
    "target_commitish": "master",
  }).encode("utf-8")
  req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
  with urllib.request.urlopen(req, timeout=30) as resp:
    return json.loads(resp.read().decode("utf-8"))


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
    gitee_upload_release_asset(token, rid, "mapd", bin_path)
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
  ap.add_argument("--force-staging", action="store_true", default=False, help="(deprecated) 保持兼容：将 master 强推到 staging（不推荐）")
  ap.add_argument("--action",
                  choices=["menu", "pull", "push", "all"],
                  default="menu",
                  help="执行模式：menu=交互菜单（默认）；pull=仅拉取+打补丁；push=仅推送；all=pull+push")
  ap.add_argument("--build-installer", action="store_true", default=False, help="在 larch64 设备上构建 installer（需要 extras=on）")
  ap.add_argument("--sync-mapd-release", action="store_true", default=False, help="同步 mapd 二进制到 Gitee Release（需要 GITEE_TOKEN）")
  ap.add_argument("--comma-host", default=COMMA_HOST_DEFAULT, help="comma 设备 IP/域名（用于远程编译 installer）")
  ap.add_argument("--comma-user", default=COMMA_USER_DEFAULT, help="comma 设备 SSH 用户名")
  ap.add_argument("--comma-key", default=COMMA_SSH_KEY_DEFAULT, help="comma 设备 SSH 私钥路径")
  ap.add_argument("--installer-outdir", default="/home/perfume/sp", help="将编译好的 installer 拷贝到本机的目录")
  ap.add_argument("--installer-repo", default=INSTALLER_REPO_DEFAULT, help="用于发布 installer 的仓库（默认 sp-cn_install）")
  ap.add_argument("--installer-repo-branch", default="master", help="发布 installer 的分支")
  ap.add_argument("--publish-installer-release", action="store_true", default=True, help="发布 installer 后自动创建 Gitee Release（tag=YYYYMMDDHHMM）")
  args = ap.parse_args()

  root = Path(args.workdir).resolve()
  if not (root / ".git").exists():
    raise SystemExit(f"未找到 git 仓库: {root}")

  env, shim_dir = prepare_git_env(root)
  ci_sync_state: dict[str, object] | None = None
  try:
    ci_sync_state = {"attempted": False, "pushed": False, "branches": [], "sync_reason_tags": []}
    # load optional .env next to this repo (never committed)
    sp_dotenv = load_dotenv(REPO_ROOT / ".env")
    for k, v in sp_dotenv.items():
      env.setdefault(k, v)

    # remotes (idempotent)
    remotes = run(["git", "remote"], str(root), env=env).splitlines()
    if "upstream" not in remotes:
      run(["git", "remote", "add", "upstream", args.upstream], str(root), env=env)
    run(["git", "remote", "set-url", "upstream", args.upstream], str(root), env=env)
    if "origin" not in remotes:
      run(["git", "remote", "add", "origin", args.origin], str(root), env=env)
    run(["git", "remote", "set-url", "origin", args.origin], str(root), env=env)

    log("git", "fetch upstream --prune --tags")
    run(["git", "fetch", "upstream", "--prune", "--tags"], str(root), env=env)
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
      upstream_sha = run(["git", "rev-parse", f"upstream/{branch}"], str(root), env=env).strip()

      recorded_sha: str | None = None
      try:
        # 只取远端最新一条提交即可（不依赖本地历史）
        run(["git", "fetch", "--depth=1", "origin", branch], str(root), env=env)
        body = run(["git", "log", "-1", "--format=%B", "FETCH_HEAD"], str(root), env=env)
        recorded_sha = parse_recorded_upstream_sha(body, branch)
      except Exception:
        recorded_sha = None

      if (not force_sync) and (recorded_sha == upstream_sha):
        print(f"[skip] {branch}: upstream sha unchanged ({upstream_sha})")
        return False
      if force_sync and recorded_sha == upstream_sha:
        print(f"[force] {branch}: upstream sha unchanged but FORCE_SYNC=1, will re-sync")

      assert ci_sync_state is not None
      ci_sync_state["attempted"] = True
      ci_sync_state.setdefault("branches", []).append(branch)
      # 与 Gitee 上次提交里记录的 upstream-{branch} SHA 对比，用于邮件区分「有新版本」vs「仅 Force」
      if recorded_sha is not None and recorded_sha == upstream_sha:
        ci_sync_state.setdefault("sync_reason_tags", []).append("force_same")
      else:
        ci_sync_state.setdefault("sync_reason_tags", []).append("upstream_delta")

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
        do_update_submodules = should_update_submodules(recorded_sha, upstream_sha, root, env)

      if do_update_submodules:
        run(["git", "submodule", "sync", "--recursive"], str(root), env=env)
        ensure_tinygrad_submodule_commit_reachable()
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

    def push_branch(branch: str) -> None:
      run(["git", "push", "-f", "-u", "origin", branch], str(root), env=env)

    def pull_all() -> list[str]:
      to_push: list[str] = []
      for b in ("master", "staging"):
        if sync_branch_local(b):
          to_push.append(b)
      return to_push

    def push_all(branches: list[str] | None = None) -> None:
      branches = branches or ["master", "staging"]
      for b in branches:
        # 只 push 本地存在的分支，避免 src refspec 不存在
        try:
          run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{b}"], str(root), env=env)
        except Exception:
          print(f"[skip] push {b}: local branch missing")
          continue
        push_branch(b)

    def maybe_sync_mapd_release() -> None:
      if not args.sync_mapd_release:
        return
      token = env.get("GITEE_TOKEN", "").strip().strip('"')
      if not token:
        raise RuntimeError("缺少环境变量 GITEE_TOKEN（只从环境变量读取，不写入仓库）")
      sync_mapd_release(token, env.get("MAPD_TAG", "latest"))

    def do_all() -> None:
      to_push = pull_all()
      # 注意：Gitee LFS 可能导致 SSH 连接不稳定；默认跳过 LFS 上传（可自行取消环境变量）
      env.setdefault("GIT_LFS_SKIP_PUSH", "1")
      if not to_push:
        print("[skip] no branches changed; nothing to push")
      else:
        push_all(to_push)
        assert ci_sync_state is not None
        ci_sync_state["pushed"] = True
      if args.force_staging:
        run(["git", "push", "-f", "origin", "master:staging"], str(root), env=env)
      maybe_sync_mapd_release()

    def interactive_menu() -> None:
      if not sys.stdin.isatty():
        # 非交互环境（比如 CI）：回退到 all，保证不挂起
        do_all()
        return

      while True:
        print("\n=== sync_to_gitee 菜单 ===")
        print("1) 拉取 upstream(master+staging) + 应用补丁 + 更新子模块（不推送）")
        print("2) 推送本地 master+staging 到 Gitee（强推）")
        print("3) 一键执行（1 + 2）")
        print("4) 仅同步 mapd release（需要 GITEE_TOKEN）")
        print("5) 远程连接 comma：编译 staging installer → 拷贝到本机 → 同步到 sp-cn_install（文件名=installer_openpilot）")
        print("0) 退出\n")
        choice = input("请选择操作 [0-5]: ").strip()
        try:
          if choice == "1":
            pull_all()
            print("[ok] 已完成拉取+补丁+子模块更新。")
          elif choice == "2":
            env.setdefault("GIT_LFS_SKIP_PUSH", "1")
            push_all()
            if args.force_staging:
              run(["git", "push", "-f", "origin", "master:staging"], str(root), env=env)
            print("[ok] 已完成推送。")
          elif choice == "3":
            do_all()
            print("[ok] 已完成一键执行。")
          elif choice == "4":
            maybe_sync_mapd_release()
            print("[ok] 已完成 mapd release 同步。")
          elif choice == "5":
            out_dir = Path(args.installer_outdir).expanduser().resolve()
            host = (args.comma_host or "").strip()
            while True:
              try:
                print(f"[step] 连接 comma：{args.comma_user}@{host}")
                out = build_comma_installer_staging(host, args.comma_user, args.comma_key, out_dir)
                break
              except Exception as e:
                print(f"[error] 连接/编译失败：{e}")
                new_host = input(f"请输入新的 comma IP/域名（当前 {host}，留空退出）: ").strip()
                if not new_host:
                  raise
                host = new_host

            # publish to repo
            print(f"[step] 同步二进制到仓库：{args.installer_repo} ({args.installer_repo_branch})")
            sync_installer_to_repo(out, args.installer_repo, branch=args.installer_repo_branch)
            if args.publish_installer_release:
              token = env.get("GITEE_TOKEN", "").strip().strip('"')
              if not token:
                print("[warn] 未设置 GITEE_TOKEN，跳过发布 Release。")
              else:
                print("[step] 发布 Gitee Release（tag=YYYYMMDDHHMM）并上传 installer_openpilot")
                tag = publish_installer_release(token, args.installer_repo, out)
                print(f"[ok] 已发布 Release tag={tag}")
            print(f"[ok] 已生成 installer 并同步到仓库：{out}")
          elif choice == "0":
            return
          else:
            print("无效选择，请输入 0-5。")
        except Exception as e:
          print(f"[error] {e}")
          print(traceback.format_exc())

    if args.action == "menu":
      interactive_menu()
    elif args.action == "pull":
      pull_all()
      maybe_sync_mapd_release()
    elif args.action == "push":
      env.setdefault("GIT_LFS_SKIP_PUSH", "1")
      push_all()
      if args.force_staging:
        run(["git", "push", "-f", "origin", "master:staging"], str(root), env=env)
      maybe_sync_mapd_release()
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
        write_ci_github_output(ci_sync_state)
    except Exception:
      pass


if __name__ == "__main__":
  main()

