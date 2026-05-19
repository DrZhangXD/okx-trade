#!/usr/bin/env bash
set -uo pipefail

# stat_arb_pairs 24h 观察报告：协整 p-value / spread_z / 信号 / orders / 持仓 / 错误。
# 用法: stat_arb_observe.sh [label]   默认 label=stat_arb_24h
# 写入: var/observation_reports/<label>_<YYYYmmdd_HHMM>.md
#
# 关键日志格式（src/okx_trade/strategies/stat_arb_pairs.py:226）:
#   stat_arb coint: β=X.XXXX p=X.XXXX hl=X.XX n=N tradeable=True/False

LABEL="${1:-stat_arb_24h}"
ROOT=/home/okxtrade/okx-trade
PNL_DB="$ROOT/var/pnl.sqlite"
OUT_DIR="$ROOT/var/observation_reports"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/${LABEL}_$(date +%Y%m%d_%H%M).md"

run() { eval "$@" 2>&1 || true; }

{
  echo "# stat_arb_pairs 观察报告 — $LABEL"
  echo
  echo "_Generated $(date -Iseconds) on $(hostname)_"
  echo

  echo "## 1. 协整检验时间序列（最近 24h）"
  echo
  echo "_p < 0.05 = 可交易；hl = half-life (hours)；n = 用于检验的 bar 数_"
  echo '```'
  run "journalctl -u okx-trade --since '24 hours ago' --no-pager | grep 'stat_arb coint:' | tail -30"
  echo '```'
  echo

  echo "## 2. 当前可交易状态"
  echo '```'
  LAST=$(journalctl -u okx-trade --since '24 hours ago' --no-pager | grep 'stat_arb coint:' | tail -1)
  if [ -n "$LAST" ]; then
    echo "$LAST"
    PVAL=$(echo "$LAST" | grep -oE 'p=[0-9.]+' | head -1 | cut -d= -f2)
    TRADE=$(echo "$LAST" | grep -oE 'tradeable=(True|False)' | cut -d= -f2)
    echo
    echo "→ 最近一次 p-value=$PVAL, tradeable=$TRADE"
  else
    echo "(24h 内无 coint 日志 — 可能 bar 历史还不够 lookback_bars=1440)"
  fi
  echo '```'
  echo

  echo "## 3. 信号 / spread_z 触发"
  echo '```'
  run "journalctl -u okx-trade --since '24 hours ago' --no-pager | grep -iE 'StatArbStrategy.*(z=|entry|exit|stop|enter|signal)' | tail -30"
  echo '```'
  echo

  echo "## 4. StatArb 提交的 orders（PnL DB）"
  echo '```'
  run "sqlite3 -header -column $PNL_DB \"SELECT strategy_id, COUNT(*) AS trades, ROUND(COALESCE(SUM(pnl_usdt),0),2) AS pnl_usdt, ROUND(COALESCE(MIN(pnl_usdt),0),2) AS worst, ROUND(COALESCE(MAX(pnl_usdt),0),2) AS best FROM trades WHERE strategy_id LIKE '%stat_arb%' OR strategy_id LIKE '%StatArb%' GROUP BY strategy_id;\""
  echo '```'
  echo

  echo "## 5. StatArb 错误 / 异常"
  echo '```'
  run "journalctl -u okx-trade --since '24 hours ago' --no-pager | grep -iE 'StatArb.*(error|exception|traceback|WARN|fail)' | head -30"
  echo '```'
  echo

  echo "## 6. 启动校验：策略仍在 RUNNING？"
  echo '```'
  run "systemctl is-active okx-trade"
  run "journalctl -u okx-trade --no-pager | grep 'StatArbStrategy' | tail -5"
  echo '```'
  echo

  echo "## 评估清单"
  echo
  echo "- [ ] **协整稳定**：p < 0.05 占多数？还是反复跨阈值（说明 pair 关系破裂）"
  echo "- [ ] **信号有触发**：spread_z 是否真的进入 ±2.0 区间？"
  echo "- [ ] **没有 traceback**："
  echo "- [ ] **24h 内 NRestarts == 0**："
} > "$OUT"

chown okxtrade:okxtrade "$OUT" 2>/dev/null || true
echo "$OUT"
