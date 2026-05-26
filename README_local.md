# sunnypilot_cn（GitHub→Gitee 自动同步闭环）

本仓库用于将上游 [`sunnypilot/sunnypilot`](https://github.com/sunnypilot/sunnypilot) 的 **`staging`** 分支定时同步到你的 Gitee 仓库（**不对上游 `master` 打补丁或推送**，避免重复大包推送；设备侧使用 `staging`）。同步过程中会应用“国内化补丁”（将常见 GitHub/Raw 地址重写为 Gitee 镜像等）。。

同步逻辑由：
- 云端 / CI（**本仓库**）：[`tools/sync_to_gitee.py`](tools/sync_to_gitee.py) + [`.github/workflows/sync-to-gitee.yml`](.github/workflows/sync-to-gitee.yml)
- 本机菜单与扩展（mapd、installer、Windows 等）：工作区 **`../tools/sync_to_gitee_local.py`**（与 `sunnypilot_cn_github` 并列，不在本仓 `tools/` 内），入口为上级目录的 `sp_sync.py` / `sp-sync`

**仓库分工**：本 GitHub 仓库存同步工具；**打补丁后的代码**推送到 **Gitee** 供国内更新。Actions **只执行** `sync_to_gitee.py`；本地编排脚本单独放在工作区 `tools/`，避免两份 `sync_to_gitee.py` 不同步。

## Actions 做了什么
- 每小时（UTC 整点）触发一次（也支持手动触发）
- `git fetch` 上游 `sunnypilot/sunnypilot`；仅对 **`upstream/staging`** 打补丁、校验并强推到 Gitee 的 **`staging`**
- 应用补丁（幂等）
- 推送失败时会自动重试（缓解 Gitee/网络偶发断连）

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
工作流在 pull 成功后根据 `notify_send` 发 **推送结果** 邮件（不要求整 job `success()`；Codeup 成功、Gitee 超时也可收到 **`[Partially OK]`**）。另在 pull 失败时发单独失败信。

常见主题：`[OK]` / `[Partially OK]` / `[FAIL]` sp_cn_sync-bot（正文含各端推送结果、分支核对、upstream/Force 说明块）。

说明：**「上游是否有新变化」与「是否发成功信」不是简单等同**。通知用的是 **`attempted_sync`（本轮是否执行了检出+补丁流程）** 与 **`pushed`** 的组合（AND）。手动勾选 **Force re-sync** 时，即使上游 SHA 相对 Gitee **未变**，也会设置 `attempted_sync`，推送成功后同样会发成功通知。

在 Actions Secrets 中增加（不配则通知步骤可能报错，已设置 `continue-on-error`，不影响同步结果）：
- **`SMTP_SERVER`**：QQ 邮箱填 `smtp.qq.com`
- **`SMTP_USER`**：一般为你的 QQ 邮箱完整地址
- **`SMTP_PASS`**：QQ 邮箱 **SMTP 授权码**（不是 QQ 登录密码；在邮箱设置里开启 SMTP 并生成授权码）
- **`MAIL_FROM`**：发件人，例如 `你的名字 <123456@qq.com>`
- **`MAIL_TO`**：收件人邮箱（可与发件人相同）

端口固定为 **465 + SSL**（与工作流一致）。若不用 QQ，可自行改工作流里的 `server_port` / `secure`。

正文由 `emit-outputs` 写入 `GITHUB_OUTPUT`；按 `notify_mail_kind` 与 `notify_variant`（`upstream_only` / `force_only` / `mixed`）生成说明与上游提交摘要。Gitee 推送 step 限时 **10 分钟**（`continue-on-error`），避免长时间卡住不发 Codeup 结果邮件。

与 QQ 邮箱官方「第三方客户端」说明一致（仅需 **发信 SMTP**，无需填 IMAP/POP）：

| 官方要求 | 本项目 Secrets |
|----------|----------------|
| 用户名 / 帐户 | `SMTP_USER` = **完整 QQ 邮箱地址** |
| 密码 | `SMTP_PASS` = **生成的授权码**（不是 QQ 登录密码） |
| 电子邮件地址 | `MAIL_FROM` / `MAIL_TO` 使用完整邮箱；`MAIL_FROM` 可为 `昵称 <邮箱>` |
| 发送邮件服务器 | `SMTP_SERVER` = `smtp.qq.com`，SSL，端口 **465**（工作流已写死；官方亦允许 587） |

## 手动触发同步
在 GitHub 仓库 `Actions` 里找到工作流 **sync-to-gitee**，点击 **Run workflow**。

## 补丁门禁（防坏包）
[`tools/sync_to_gitee.py`](tools/sync_to_gitee.py) 在每个分支打完补丁后、提交前会执行 **`verify_patches`**：检查关键文件是否已包含约定的 Gitee 镜像字符串、`updated.py` 是否已注入 `insteadof` 辅助函数、`.gitmodules` 是否仍残留未改写的 GitHub URL 等；必要时对关键 `.py` 做 **`py_compile`**。任一检查失败会 **`RuntimeError` 退出**，该分支**不会 commit / push**，避免半成品进入 Gitee。

日志中会列出具体缺失项，形如：`verify_patches 失败（不会提交/推送）： ...`

### 驾驶员监控（DM）补丁

由 [`patch_dm_relaxed_terminal`](tools/sync_to_gitee.py) 写入同步树中的 `selfdrive/monitoring/helpers.py`（`verify_patches` 会检查 sentinel）。

**产品决策：红屏只警告、不因 DM 收纵向（不 forceDecel）**

- **目标**：分心到红色全屏时仍提醒驾驶员，但不因 DM 触发强制减速、不锁死下次上车；专注恢复约 **6 秒** 可自动退出红屏。
- **补丁要点**（与官方 sunnypilot 对照）：
  - `awareness` 递减下限 **0**（官方为 **-0.1**）：避免 `awarenessStatus < 0` → `controlsState.forceDecel`。
  - 红线保留 `driverDistracted3` 全屏/鸣音，**去掉** `terminal_time` / `terminal_alert_cnt` 累计（官方可能写入 `DriverTooDistracted`，影响再次 engage）。
  - 红屏下若「脸在 + 姿态稳 + 分心滤波低」持续约 6 秒，自动将 `awareness` 拉回橙区并清 `alert`（官方通常需 disengage 才清红）。
- **纵向控车**：官方在**刚到红**（`awareness = 0`）时纵向仍正常；**持续分心**时 awareness 可到 -0.1，触发 `forceDecel` → 纵向规划 `v_cruise = 0`（强烈收油/趋向停车）。国内化在红屏期间**不因 DM 收油**，相对官方持续分心路径更保留 ACC；相对「应立即接管」官方更严——为团队有意选择，**代码维持现状**。
- `driverDistracted3` 仍为 `ET.PERMANENT`（非 `SOFT_DISABLE` / `IMMEDIATE_DISABLE`）；文案 “DISENGAGE IMMEDIATELY” 为提示，不表示栈自动断纵向。

### 本地专用脚本（不在本仓库内）
本机菜单脚本为工作区 **`tools/sync_to_gitee_local.py`**（与 `sunnypilot_cn_github` 同级），加载本仓的 `tools/sync_to_gitee.py` 打补丁。**GitHub Actions 只调用本仓 `sync_to_gitee.py`**。

## 常见排错
- **SMTP 535 / Login fail（QQ）**：`SMTP_PASS` 必须用**授权码**，且与开启 SMTP 时生成的一致；参见 [QQ 邮箱 SMTP 登录报错说明](https://help.mail.qq.com/detail/108/1023)。短时间多次失败请间隔后再试。
- **SSH 权限问题（Permission denied）**：检查 Gitee Deploy Key 是否勾选“可写”，以及 `GITEE_REPO_SSH` 是否正确。
- **上游结构变化导致补丁失败**：`replace_or_fail` 或 **`verify_patches`** 会终止同步；按日志更新补丁或校验规则后再跑。
- **Schedule 没按时触发**：GitHub 的定时任务可能延迟几分钟属正常；同时注意 cron 使用 **UTC**。

