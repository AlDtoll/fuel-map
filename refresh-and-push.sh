#!/bin/bash
# Cron: обновить данные с gdebenz и запушить, если изменились (держит карту свежей).
cd /home/claudeuser/fuel-map || exit 1
python3 refresh.py >> /home/claudeuser/fuel-map/refresh.log 2>&1
if ! git diff --quiet -- stations.json; then
    git add stations.json
    git -c user.email="daniltclaude@gmail.com" -c user.name="AlDtoll" commit -q -m "data: refresh Krasnodar stations $(date '+%F %H:%M')"
    git push -q origin main 2>>/home/claudeuser/fuel-map/refresh.log && echo "[$(date '+%F %T')] pushed" >> /home/claudeuser/fuel-map/refresh.log
fi
