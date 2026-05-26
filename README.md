# sunnypilot_cn

本仓库为 **GitHub Actions 同步工具**，配合工作流将上游 [sunnypilot](https://github.com/sunnypilot/sunnypilot) 打补丁后推送到国内镜像。

| 路径 | 说明 |
|------|------|
| [`tools/sync_to_gitee.py`](tools/sync_to_gitee.py) | 同步与补丁脚本 |
| [`.github/workflows/`](.github/workflows/) | 定时 / 手动触发 |

使用前请在仓库 **Settings → Secrets and variables → Actions** 中配置所需凭据；勿将密钥写入代码或提交到仓库。

详细说明与本地开发入口见本机工作区文档（不随本仓公开）。
