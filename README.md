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

### 4) （可选）邮件通知（QQ 邮箱 SMTP）
工作流在 **两种** 情况下尝试发信（均要求：本轮已判定「上游相对 Gitee 有更新」并进入打补丁流程）：
- **成功**：补丁校验通过且已推送到 Gitee。
- **失败**：补丁校验失败、git 错误或未完成推送等（不会把未通过校验的树推向 Gitee）。

在 Actions Secrets 中增加（不配则通知步骤可能报错，已设置 `continue-on-error`，不影响同步结果）：
- **`SMTP_SERVER`**：QQ 邮箱填 `smtp.qq.com`
- **`SMTP_USER`**：一般为你的 QQ 邮箱完整地址
- **`SMTP_PASS`**：QQ 邮箱 **SMTP 授权码**（不是 QQ 登录密码；在邮箱设置里开启 SMTP 并生成授权码）
- **`MAIL_FROM`**：发件人，例如 `你的名字 <123456@qq.com>`
- **`MAIL_TO`**：收件人邮箱（可与发件人相同）

端口固定为 **465 + SSL**（与工作流一致）。若不用 QQ，可自行改工作流里的 `server_port` / `secure`。

## 手动触发同步
在 GitHub 仓库 `Actions` 里找到工作流 **sync-to-gitee**，点击 **Run workflow**。

## 补丁门禁（防坏包）
[`tools/sync_to_gitee.py`](tools/sync_to_gitee.py) 在每个分支打完补丁后、提交前会执行 **`verify_patches`**：检查关键文件是否已包含约定的 Gitee 镜像字符串、`updated.py` 是否已注入 `insteadof` 辅助函数、`.gitmodules` 是否仍残留未改写的 GitHub URL 等；必要时对关键 `.py` 做 **`py_compile`**。任一检查失败会 **`RuntimeError` 退出**，该分支**不会 commit / push**，避免半成品进入 Gitee。

日志中会列出具体缺失项，形如：`verify_patches 失败（不会提交/推送）： ...`

### 本地专用脚本（不参与云端同步）
[`tools/sync_to_gitee_local.py`](tools/sync_to_gitee_local.py) 仅用于本机交互菜单（如远程编译 installer 等），**GitHub Actions 只调用 `sync_to_gitee.py`**。若不希望把本地脚本提交到 GitHub 工具仓，可将其列入 `.gitignore`（见仓库内 `.gitignore`）。

## 常见排错
- **SSH 权限问题（Permission denied）**：检查 Gitee Deploy Key 是否勾选“可写”，以及 `GITEE_REPO_SSH` 是否正确。
- **上游结构变化导致补丁失败**：`replace_or_fail` 或 **`verify_patches`** 会终止同步；按日志更新补丁或校验规则后再跑。
- **Schedule 没按时触发**：GitHub 的定时任务可能延迟几分钟属正常；同时注意 cron 使用 **UTC**。

