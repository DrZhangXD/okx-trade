#!/usr/bin/env bash
set -uo pipefail

# Generate markdown observation report for okx-trade paper trading.
# Usage: observation_report.sh <label>
#   label: short tag like "day_7" or "day_14" (used in filename)
#
# Writes to /home/okxtrade/okx-trade/var/observation_reports/<label>_<YYYYmmdd>.md
# Designed to be called from root's crontab; writes file owned by okxtrade.

LABEL="${1:-adhoc}"
ROOT=/home/okxtrade/okx-trade
PNL_DB="$ROOT/var/pnl.sqlite"
ALERTS="$ROOT/var/alerts.jsonl"
DAILY_DIR="$ROOT/var/daily_reports"
OUT_DIR="$ROOT/var/observation_reports"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/${LABEL}_$(date +%Y%m%d).md"

run() { eval "$@" 2>&1 || true; }

{
  echo "# okx-trade observation report — $LABEL"
  echo
  echo "_Generated $(date -Iseconds) on $(hostname)_"
  echo

  echo "## 1. 服务存活与稳定性"
  echo '```'
  run "systemctl is-active okx-trade okx-trade-healthcheck.timer"
  echo
  run "systemctl show okx-trade -p ActiveEnterTimestamp -p MainPID -p MemoryCurrent -p NRestarts --no-pager"
  echo
  echo "# 启动次数（自上次重启服务以来）"
  run "journalctl -u okx-trade | grep -cE 'Started okx-trade'"
  echo "# 最近 5 次 Started/Stopped"
  run "journalctl -u okx-trade --no-pager | grep -E 'Started|Stopped|Killed|main process exited' | tail -5"
  echo '```'
  echo

  echo "## 2. OOM / 内存压力"
  echo '```'
  echo "# dmesg / journalctl OOM hit (整机)"
  run "journalctl -k --no-pager | grep -iE 'oom|killed process' | tail -10"
  echo
  echo "# okx-trade 服务自身 memory 相关日志"
  run "journalctl -u okx-trade --no-pager | grep -iE 'memory|oom|killed' | tail -10"
  echo '```'
  echo

  echo "## 3. PnL 汇总（按 strategy）"
  echo '```'
  run "sqlite3 -header -column $PNL_DB \"SELECT strategy_id, COUNT(*) AS trades, ROUND(COALESCE(SUM(pnl_usdt),0),2) AS pnl_usdt, ROUND(COALESCE(MIN(pnl_usdt),0),2) AS worst, ROUND(COALESCE(MAX(pnl_usdt),0),2) AS best FROM trades GROUP BY strategy_id;\""
  echo '```'
  echo

  echo "## 4. Equity 曲线（最近 24h，每策略最多 5 条）"
  echo '```'
  run "sqlite3 -header -column $PNL_DB \"
    WITH ranked AS (
      SELECT strategy_id, datetime(ts_ms/1000,'unixepoch','localtime') AS t, ROUND(equity_usdt,2) AS equity,
             ROW_NUMBER() OVER (PARTITION BY strategy_id ORDER BY ts_ms DESC) AS rn
      FROM equities
      WHERE ts_ms > (strftime('%s','now')-86400)*1000
    )
    SELECT strategy_id, t, equity FROM ranked WHERE rn <= 5 ORDER BY strategy_id, t DESC;\""
  echo '```'
  echo

  echo "## 5. WARN / ERROR top 频次（最近 24h）"
  echo '```'
  run "journalctl -u okx-trade --since '24 hours ago' --no-pager | grep -E '\[(WARN|ERROR)\]' | sed -E 's/.*\[(WARN|ERROR)\][^:]*: //' | awk -F: '{print \$1}' | sort | uniq -c | sort -rn | head -15"
  echo '```'
  echo

  echo "## 6. Daily reports 生成情况"
  echo '```'
  run "ls -la $DAILY_DIR 2>&1 | tail -20"
  echo '```'
  echo

  echo "## 7. 最近告警"
  echo '```'
  if [ -s "$ALERTS" ]; then
    run "tail -30 $ALERTS"
    echo
    echo "# CRITICAL 计数"
    run "grep -c '\"level\":\"CRITICAL\"' $ALERTS"
  else
    echo "(alerts.jsonl 为空或不存在)"
  fi
  echo '```'
  echo

  echo "## 评估清单（对照填）"
  echo
  echo "- [ ] **存活**：14 天内 NRestarts == 0？OOM 没杀过？"
  echo "- [ ] **覆盖**：4 个 strategy 都有 trades > 0？（信号阈值/universe 设置合理）"
  echo "- [ ] **PnL 分布**：最差策略亏损是否在心理可接受范围？"
  echo "- [ ] **错误率**：WARN top1 不再是 instrument missing / 反复抛同一条？"
  echo "- [ ] **报表**：daily_reports 每天一个文件？"
  echo "- [ ] **告警**：alerts.jsonl 没有 CRITICAL？"
  echo
  echo "## Verdict"
  echo
  echo "选一个："
  echo "- (a) **推进 M6**：实盘切换 + Telegram alert"
  echo "- (b) **调参后再观察 7 天**"
  echo "- (c) **发现严重问题，停下修代码**"
} > "$OUT"

chown okxtrade:okxtrade "$OUT" 2>/dev/null || true
echo "$OUT"
