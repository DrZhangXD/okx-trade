#!/usr/bin/env bash
# 把 VPS 生成的报告复制到 git-tracked published_reports/ 目录 + commit + push origin main
# 让远程 Claude agent / /schedule 任务能从 github 读到这些 runtime 数据。
#
# 触发方式：VPS cron 在以下时间调用
#   - 每日 00:15 CST（reconcile + truth dashboard 跑完之后 5 分钟）
#   - 5/22 23:35 CST（day_14 cron 跑完之后 5 分钟）
#
# 复制路径：
#   var/observation_reports/*.md → published_reports/observation/
#   var/audits/*.md              → published_reports/truth/
#
# 仓库 private，所以 PnL / 持仓信息不会泄露公网。

set -euo pipefail

REPO_DIR=/home/okxtrade/okx-trade
PUB_DIR=$REPO_DIR/published_reports

cd "$REPO_DIR"

mkdir -p "$PUB_DIR/observation" "$PUB_DIR/truth"

# 复制最新报告（mtime > git 上次 commit 时间的）
cp -p var/observation_reports/*.md "$PUB_DIR/observation/" 2>/dev/null || true
cp -p var/audits/*.md "$PUB_DIR/truth/" 2>/dev/null || true

# 没变化就 noop
if [[ -z "$(git status --porcelain published_reports/)" ]]; then
    echo "$(date -u +%FT%TZ) push_reports: no changes"
    exit 0
fi

git add published_reports/
git -c user.email="okx-vps-bot@noreply.local" -c user.name="okx-vps cron" \
    commit -m "report: $(date -u +%FT%TZ) auto-publish"
git push origin main

echo "$(date -u +%FT%TZ) push_reports: pushed"
