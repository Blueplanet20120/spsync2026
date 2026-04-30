python3 /home/perfume/sp/tools/sync_to_gitee.py --help

python3 /home/perfume/sp/tools/sync_to_gitee.py --workdir /home/perfume/sp/sunnypilot --sync-mapd-release

#明天继续时，直接从这两条开始就行：
python3 /home/perfume/sp/tools/sync_to_gitee.py --workdir /home/perfume/sp/sunnypilot
python3 /home/perfume/sp/tools/sync_to_gitee.py --workdir /home/perfume/sp/sunnypilot --sync-mapd-release

sshkey:
ssh -i "/home/perfume/sp/sp-cn" -p 22 -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null comma@10.90.1.231 

#任意窗口执行 sp-sync 即可运行sync_to_gitee.py脚本。
你也可以用非交互模式：

sp-sync --action pull
sp-sync --action push
sp-sync --action all
#短链地址：
https://shortlink.uk/1tXQY

#平时安装：
shortlink.uk/1tXQY

#设备：点更新或手动

cd /data/openpilot
git fetch origin staging --prune
git reset --hard origin/staging