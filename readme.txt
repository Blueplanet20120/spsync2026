#小段思考：
对于应用补丁有没有应用不成功的提示？要有一个检查结果的逻辑，如果失败也不要推送到Gitee，避免送坏包到Gitee，考虑QQ邮件通知，Action如果有通知，其实也可以，我只需要在两个情况通知，一是上游代码有更新并且成功应用补丁推送到Gitee时通知，二是上游代码有更新并应用补丁失败时通知

#sp-sync：uqhqpsmejxunbcef
#sshkey:
ssh -i "/home/perfume/sp/sp-cn" -p 22 -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null comma@10.90.1.231 

#任意窗口执行 sp-sync 即可运行sync_to_gitee.py脚本。
你也可以用非交互模式：

sp-sync --action pull
sp-sync --action push
sp-sync --action all
#短链地址：
https://ai.stagingspcn.top

#平时安装：
ai.stagingspcn.top

#设备：点更新或手动

cd /data/openpilot
git fetch origin staging --prune
git reset --hard origin/staging

#SSH 直接把 /data/openpilot 切到你的 Gitee（最快）
在设备正常启动、能 SSH 时执行（你已验证 10.90.1.231 可连）：


cd /data/openpilot
git remote set-url origin https://gitee.com/xc2026/sunnypilot_cn.git
git fetch origin staging --prune
git reset --hard origin/staging
sudo systemctl restart comma || sudo reboot