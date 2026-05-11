#!/usr/bin/env python3
"""
Sync dependency repos (upstream) -> Gitee mirrors.

Why a separate script?
- Keep `tools/sync_to_gitee.py` focused on patching/pushing sunnypilot_cn.
- Run less frequently (e.g. every 5 days) via a dedicated GitHub Actions workflow.

Important:
- Do NOT use `git push --mirror` because GitHub repos may contain hidden refs (e.g. refs/pull/*)
  which Gitee rejects. We only sync branches (refs/heads/*) and tags (refs/tags/*).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


def _truthy(s: str | None) -> bool:
  return bool((s or "").strip().lower() in ("1", "true", "yes", "y", "on"))


def _run(cmd: list[str], cwd: Path | None = None) -> str:
  p = subprocess.run(
    cmd,
    cwd=str(cwd) if cwd else None,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
  )
  if p.returncode != 0:
    raise RuntimeError(f"command failed: {' '.join(cmd)}\n{p.stdout}")
  return p.stdout


def _retry(name: str, fn, tries: int = 3, base_sleep_s: float = 2.0) -> None:
  last: Exception | None = None
  for i in range(1, tries + 1):
    try:
      fn()
      return
    except Exception as e:
      last = e
      if i >= tries:
        break
      sleep_s = base_sleep_s * (2 ** (i - 1))
      print(f"[retry] {name} failed (attempt {i}/{tries}): {type(e).__name__}: {e}. sleep {sleep_s:.1f}s")
      time.sleep(sleep_s)
  assert last is not None
  raise last


@dataclass(frozen=True)
class RepoMirror:
  label: str
  upstream: str
  gitee_repo: str  # just "repo" name, owner comes from env/args


DEFAULT_REPOS: list[RepoMirror] = [
  RepoMirror("opendbc", "https://github.com/sunnypilot/opendbc.git", "opendbc"),
  RepoMirror("msgq", "https://github.com/commaai/msgq.git", "msgq"),
  RepoMirror("tinygrad", "https://github.com/sunnypilot/tinygrad.git", "tinygrad"),
  RepoMirror("neural_network_data", "https://github.com/sunnypilot/neural-network-data.git", "neural_network_data"),
  RepoMirror("panda", "https://github.com/sunnyhaibin/panda.git", "panda"),
  RepoMirror("rednose", "https://github.com/commaai/rednose.git", "rednose"),
  RepoMirror("teleoprtc", "https://github.com/commaai/teleoprtc.git", "teleoprtc"),
  RepoMirror("dependencies", "https://github.com/commaai/dependencies.git", "dependencies"),
  RepoMirror("Catch2", "https://github.com/catchorg/Catch2.git", "Catch2"),
  RepoMirror("sunnypilot-models", "https://github.com/sunnypilot/sunnypilot-models.git", "sunnypilot-models"),
]


def _dest_url(owner: str, repo: str, use_https: bool) -> str:
  if use_https:
    return f"https://gitee.com/{owner}/{repo}.git"
  return f"git@gitee.com:{owner}/{repo}.git"


def _safe_name(label: str) -> str:
  return re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_") or "repo"


def _push_would_change(output: str) -> bool:
  """
  `git push --porcelain --dry-run` output is not super stable across git versions,
  but it reliably prints ref update lines when something would change.
  Treat "Everything up-to-date" (or an empty/no-update porcelain) as no-op.
  """
  s = (output or "").strip()
  if not s:
    return False
  if "Everything up-to-date" in s:
    return False
  # Heuristic: porcelain update lines typically begin with "ok " or "ng ".
  for ln in s.splitlines():
    ln = ln.strip()
    if ln.startswith(("ok ", "ng ")):
      return True
  # Fallback: if it contains "->" or "[new tag]" style lines, consider it change.
  if "->" in s or "[new " in s:
    return True
  return False


def sync_one(cache_dir: Path, owner: str, use_https: bool, m: RepoMirror) -> None:
  mirror_dir = cache_dir / f"{_safe_name(m.label)}.git"
  dest = _dest_url(owner, m.gitee_repo, use_https)

  print(f"\n[dep] {m.label}")
  print(f"      upstream: {m.upstream}")
  print(f"      dest:     {dest}")

  if not (mirror_dir / "config").is_file():
    if mirror_dir.exists():
      raise RuntimeError(f"mirror cache directory exists but missing config: {mirror_dir}")
    _run(["git", "clone", "--mirror", m.upstream, str(mirror_dir)])

  _run(["git", "remote", "set-url", "origin", m.upstream], cwd=mirror_dir)
  # add or update gitee remote
  try:
    _run(["git", "remote", "add", "gitee", dest], cwd=mirror_dir)
  except RuntimeError:
    _run(["git", "remote", "set-url", "gitee", dest], cwd=mirror_dir)

  # branches + tags only (avoid refs/pull/* hidden refs)
  _run(["git", "fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"], cwd=mirror_dir)

  push_args = [
    "git",
    "push",
    "--prune",
    "--force",
    "gitee",
    "+refs/heads/*:refs/heads/*",
    "+refs/tags/*:refs/tags/*",
  ]

  # Print "skip" reason when everything is already up to date (like mapd does).
  dry = _run(push_args[:2] + ["--porcelain", "--dry-run"] + push_args[2:], cwd=mirror_dir)
  if not _push_would_change(dry):
    print("[dep] already up-to-date, skip push")
    return

  _run(push_args, cwd=mirror_dir)


def _load_sync_to_gitee_impl() -> object:
  """
  Load `tools/sync_to_gitee.py` as a module (tools/ isn't a package).
  We reuse its `sync_mapd_release` implementation to avoid code duplication.
  """
  repo_root = Path(__file__).resolve().parents[1]
  impl_path = repo_root / "tools" / "sync_to_gitee.py"
  spec = importlib.util.spec_from_file_location("sync_to_gitee_impl_for_deps", impl_path)
  if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load module: {impl_path}")
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)  # type: ignore[attr-defined]
  return mod


def _maybe_sync_mapd_release() -> None:
  """
  Optional: sync `openpilot-mapd` binary to Gitee release.
  Requires `GITEE_TOKEN` (same as sync_to_gitee.py).
  """
  if not _truthy(os.environ.get("SYNC_MAPD_RELEASE")):
    return
  token = (os.environ.get("GITEE_TOKEN") or "").strip().strip('"')
  if not token:
    raise RuntimeError("SYNC_MAPD_RELEASE=1 but missing GITEE_TOKEN")
  tag = os.environ.get("MAPD_TAG", "latest")
  print(f"\n[mapd] sync release (MAPD_TAG={tag})")
  impl = _load_sync_to_gitee_impl()
  fn = getattr(impl, "sync_mapd_release", None)
  if not callable(fn):
    raise RuntimeError("sync_to_gitee.py missing sync_mapd_release()")
  fn(token, tag)


def main() -> None:
  ap = argparse.ArgumentParser(description="Sync dependency repos to Gitee mirrors (branches+tags).")
  ap.add_argument("--owner", default=os.environ.get("DEP_MIRROR_OWNER", "xc2026"), help="Gitee owner/org (default: xc2026)")
  ap.add_argument("--use-https", action="store_true", default=_truthy(os.environ.get("DEP_MIRROR_USE_HTTPS")), help="Use HTTPS remote instead of SSH")
  ap.add_argument("--only", default=os.environ.get("DEP_MIRROR_ONLY", ""), help="Comma-separated labels to sync (optional)")
  ap.add_argument("--skip", default=os.environ.get("DEP_MIRROR_SKIP", ""), help="Comma-separated labels to skip (optional)")
  ap.add_argument("--cache-dir", default=os.environ.get("DEP_MIRROR_CACHE_DIR", ""), help="Cache directory for --mirror clones (optional)")
  ap.add_argument("--sync-mapd-release", action="store_true", default=_truthy(os.environ.get("SYNC_MAPD_RELEASE")), help="Also sync mapd release to Gitee (requires GITEE_TOKEN)")
  ap.add_argument("--mapd-tag", default=os.environ.get("MAPD_TAG", "latest"), help="MAPD_TAG for mapd release sync (default: latest)")
  args = ap.parse_args()

  # Wire args -> env for the reused implementation (sync_to_gitee.py reads env MAPD_TAG).
  if args.sync_mapd_release:
    os.environ["SYNC_MAPD_RELEASE"] = "1"
  os.environ["MAPD_TAG"] = str(args.mapd_tag)

  only = {x.strip() for x in (args.only or "").split(",") if x.strip()}
  skip = {x.strip() for x in (args.skip or "").split(",") if x.strip()}

  selected = []
  for r in DEFAULT_REPOS:
    if only and r.label not in only:
      continue
    if r.label in skip:
      continue
    selected.append(r)

  if not selected:
    print("[warn] no repos selected")
    return

  if args.cache_dir.strip():
    cache_dir = Path(args.cache_dir).expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
  else:
    # allow Actions cache to hook into a stable path if desired
    cache_dir = Path(tempfile.gettempdir()) / "sp_dep_mirrors_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

  print("[step] Sync dependency mirrors (branches + tags)")
  print(f"       owner={args.owner} use_https={args.use_https} cache_dir={cache_dir}")

  for r in selected:
    _retry(f"sync {r.label}", lambda r=r: sync_one(cache_dir, args.owner, args.use_https, r), tries=3, base_sleep_s=3.0)

  _maybe_sync_mapd_release()

  print("\n[ok] dependency mirror sync complete")


if __name__ == "__main__":
  main()

