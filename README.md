# sunnypilot_cn（GitHub→Gitee 自动同步闭环）

本仓库用于将上游 [`sunnypilot/sunnypilot`](https://github.com/sunnypilot/sunnypilot) 的 `master`/`staging` 分支定时同步到你的 Gitee 仓库，并在同步过程中应用“国内化补丁”（将常见 GitHub/Raw 地址重写为 Gitee 镜像等）。

同步逻辑由：
- 脚本：[`tools/sync_to_gitee.py`](tools/sync_to_gitee.py)
- 定时任务：[`/.github/workflows/sync-to-gitee.yml`](.github/workflows/sync-to-gitee.yml)

## Actions 做了什么
- 每小时（UTC 整点）触发一次（也支持手动触发）
- 拉取上游 `sunnypilot/sunnypilot` 的 `master` + `staging`
- 应用补丁（幂等）
- 强推到你配置的 Gitee 仓库（`master` + `staging`）

说明：脚本会自动兼容两种目录布局：
- 仓库根目录本身就是 git 仓库（推荐）
- 仓库根目录下存在 `sunnypilot/` 子目录且 `sunnypilot/.git` 才是真正仓库（你本地当前形态）

## 一次性配置（必须）

### 1) 在 Gitee 创建目标仓库
示例：`gitee.com/<你的账号>/sunnypilot_cn`

### 2) 生成并添加 Gitee Deploy Key（可写）
在你本地生成一对 SSH key（示例命令，仅供参考）：

```bash
ssh-keygen -t ed25519 -C "github-actions-sync-to-gitee" -f gitee_deploy_key -N ""
```

然后：
- 把 `gitee_deploy_key.pub`（公钥）添加到 Gitee 仓库的 **Deploy Keys**，并勾选 **可写/Write access**
- 私钥 `gitee_deploy_key` 用于 GitHub Secrets（见下一节）

### 3) 在 GitHub 仓库设置 Secrets
进入 GitHub 仓库 `Settings -> Secrets and variables -> Actions` 添加：
- **`GITEE_SSH_PRIVATE_KEY`**：上面生成的 Deploy Key 私钥内容（完整粘贴）
- **`GITEE_REPO_SSH`**：Gitee 仓库 SSH 地址，例如：`git@gitee.com:<你的账号>/sunnypilot_cn.git`
- **（可选）`GITEE_TOKEN`**：仅当你要启用脚本的 `--sync-mapd-release` / 发布 Release 等功能时需要

## 手动触发同步
在 GitHub 仓库 `Actions` 里找到工作流 **sync-to-gitee**，点击 **Run workflow**。

## 常见排错
- **SSH 权限问题（Permission denied）**：检查 Gitee Deploy Key 是否勾选“可写”，以及 `GITEE_REPO_SSH` 是否正确。
- **上游结构变化导致补丁失败**：脚本会硬失败并终止同步，避免推送半成品到 Gitee。需要按失败日志定位 `replace_or_fail` 触发点并更新补丁逻辑。
- **Schedule 没按时触发**：GitHub 的定时任务可能延迟几分钟属正常；同时注意 cron 使用 **UTC**。

