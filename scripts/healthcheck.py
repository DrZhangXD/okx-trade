"""Paper trading 健康检查（M5 配套）。

检查 4 件事，任一失败 → exit 非 0 + 打印诊断：
1. ``scripts/live.py`` 进程在跑（pgrep）；
2. ``var/heartbeat.ts`` 在最近 ``--max-stale-mins`` 内被刷过（说明 LiveMonitor 还在跑 poll
   loop，间接证明 NT TradingNode + strategies 没卡死）；
3. ``var/alerts.jsonl`` 没有新出现的 CRITICAL alert（drawdown 触发 → 应人工 review）；
4. NT 进程的 RSS < ``--max-rss-mb``（防内存泄漏）。

历史
----
原本 #2 检查 ``var/pnl.sqlite`` 的 MAX(ts_ms)；但 ``record_strategy_equity_daily``
设计上每个 UTC 日只写一条，正常情况下 30 min 阈值会误报 stale。改成由 LiveMonitor
每 ``poll_interval_s`` 刷一次 heartbeat 文件，作为真正的 wall-clock 心跳。
``pnl_age_minutes`` 仍保留供诊断 CLI 使用。

用法
----
::

    # cron 每 5 分钟跑一次
    */5 * * * * cd /path/to/okx-trade && /usr/bin/python3 scripts/healthcheck.py \\
        --max-stale-mins 30 --max-rss-mb 2000 || systemctl restart okx-trade

退出码
------
- ``0``：全部 OK
- ``1``：进程没在跑
- ``2``：heartbeat 太久没刷（monitor poll loop 卡了；可能是 NT 主线程卡死）
- ``3``：发现新 CRITICAL alert（人工 review 必要）
- ``4``：内存超限
- ``5``：内部检查异常（命令缺失等）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

EXIT_OK = 0
EXIT_PROCESS_DOWN = 1
EXIT_HEARTBEAT_STALE = 2  # 原 EXIT_PNL_STALE，语义已迁移到 heartbeat
EXIT_PNL_STALE = EXIT_HEARTBEAT_STALE  # 向后兼容别名，外部脚本可能引用
EXIT_CRITICAL_ALERT = 3
EXIT_MEM_OVER = 4
EXIT_INTERNAL = 5


def find_live_pid(script_basename: str = "scripts/live.py") -> int | None:
    """``pgrep -f scripts/live.py`` 找运行中的 paper trading 进程。"""
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        return None
    try:
        out = subprocess.check_output(
            [pgrep, "-f", script_basename], text=True, timeout=5,
        )
    except subprocess.CalledProcessError:
        return None
    pids = [int(p) for p in out.split() if p.strip()]
    # 排除自己（healthcheck.py 进程也可能匹配 "scripts/live.py" 子串——这里
    # 只保留 cmdline 真包含 scripts/live.py 的）
    me = os.getpid()
    for pid in pids:
        if pid == me:
            continue
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_text().replace("\x00", " ")
        except FileNotFoundError:
            # macOS 没有 /proc，用 ps 查
            try:
                cmdline = subprocess.check_output(
                    ["ps", "-p", str(pid), "-o", "command="], text=True, timeout=2,
                )
            except Exception:
                cmdline = ""
        if script_basename in cmdline and "healthcheck" not in cmdline:
            return pid
    return None


def get_rss_mb(pid: int) -> float | None:
    """读 ``/proc/<pid>/status`` VmRSS（macOS fallback 到 ps -o rss）。"""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                # "VmRSS:    123456 kB"
                kb = int(line.split()[1])
                return kb / 1024.0
    except FileNotFoundError:
        # macOS
        try:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "rss="], text=True, timeout=2,
            )
            return float(out.strip()) / 1024.0
        except Exception:
            return None
    return None


def heartbeat_age_minutes(path: Path) -> float | None:
    """读 ``var/heartbeat.ts``（LiveMonitor 每 poll 写一次的 wall-clock ms），
    返回距今多少分钟。文件不存在或内容无效 → ``None``（main 走 cold-start WARN）。
    """
    if not path.exists():
        return None
    try:
        content = path.read_text().strip()
        last_ms = int(content)
    except (OSError, ValueError):
        return None
    now_ms = int(time.time() * 1000)
    age = (now_ms - last_ms) / 60_000.0
    # heartbeat 文件里写的是 wall-clock ms；若内容时间在未来（时钟回拨等），
    # 当作 0 分钟前——freshness 检查不应被未来值误判。
    return max(0.0, age)


def pnl_age_minutes(db_path: Path) -> float | None:
    """读 pnl.sqlite，取 trades + equities 的最大 ts_ms，换算成"距今多少分钟"。

    过滤掉未来时间戳：曾经 XSMomentum 用 bar 边界（1D HKT close = UTC 16:00）当 ts_ms，
    导致 MAX 永远是未来值，age 恒负，PNL_STALE 检查永远不触发。SQL 里就用
    ``WHERE ts <= now_ms`` 排除。
    """
    if not db_path.exists():
        return None
    now_ms = int(time.time() * 1000)
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute(
            "SELECT MAX(ts) FROM ("
            "SELECT MAX(closed_ts_ms) AS ts FROM trades  WHERE closed_ts_ms <= ? "
            "UNION ALL "
            "SELECT MAX(ts_ms)        AS ts FROM equities WHERE ts_ms        <= ?"
            ")",
            (now_ms, now_ms),
        )
        row = cur.fetchone()
        conn.close()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    last_ms = int(row[0])
    return (now_ms - last_ms) / 60_000.0


def critical_alerts_since(jsonl_path: Path, since_ts_ms: int) -> list[dict]:
    """扫 alerts.jsonl，挑 ``severity=critical`` 且 ts_ms >= since_ts_ms 的。"""
    if not jsonl_path.exists():
        return []
    out: list[dict] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                a = json.loads(line)
            except json.JSONDecodeError:
                continue
            if a.get("severity") == "critical" and int(a.get("ts_ms", 0)) >= since_ts_ms:
                out.append(a)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="okx-trade paper trading healthcheck")
    p.add_argument("--var-dir", default="var",
                   help="var directory containing pnl.sqlite + alerts.jsonl")
    p.add_argument("--max-stale-mins", type=float, default=30.0,
                   help="heartbeat last-write 超过此分钟数视为 monitor 卡住")
    p.add_argument("--max-rss-mb", type=float, default=2000.0,
                   help="进程 RSS 超过此 MB 视为内存泄漏")
    p.add_argument("--critical-since-mins", type=float, default=10.0,
                   help="检查最近 N 分钟内的 CRITICAL alert（避免重复报警旧的）")
    p.add_argument("--script", default="scripts/live.py",
                   help="paper trading 入口脚本路径（pgrep -f 匹配）")
    p.add_argument("--quiet", action="store_true",
                   help="只 exit code，不打印诊断")
    args = p.parse_args()

    var_dir = Path(args.var_dir)
    pnl_db = var_dir / "pnl.sqlite"  # 仅供诊断；不在主流程使用
    alerts_jsonl = var_dir / "alerts.jsonl"
    heartbeat_file = var_dir / "heartbeat.ts"

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg)

    # 1) 进程
    pid = find_live_pid(args.script)
    if pid is None:
        say(f"FAIL[{EXIT_PROCESS_DOWN}]: no running process matching {args.script!r}")
        return EXIT_PROCESS_DOWN
    say(f"OK: live.py running pid={pid}")

    # 2) RSS
    rss = get_rss_mb(pid)
    if rss is not None:
        if rss > args.max_rss_mb:
            say(f"FAIL[{EXIT_MEM_OVER}]: RSS={rss:.0f}MB > limit={args.max_rss_mb:.0f}MB")
            return EXIT_MEM_OVER
        say(f"OK: RSS={rss:.0f}MB (limit {args.max_rss_mb:.0f}MB)")

    # 3) Heartbeat freshness（LiveMonitor 每 poll_interval_s 刷一次 var/heartbeat.ts）
    age = heartbeat_age_minutes(heartbeat_file)
    if age is None:
        # 启动初期 monitor 还没第一次刷；30s-1min 内常见，仅 WARN 不挂
        say(f"WARN: {heartbeat_file} missing or unreadable (cold start?)")
    elif age > args.max_stale_mins:
        say(
            f"FAIL[{EXIT_HEARTBEAT_STALE}]: heartbeat was {age:.1f} min ago "
            f"(>{args.max_stale_mins:.0f} min) — monitor poll loop stuck?"
        )
        return EXIT_HEARTBEAT_STALE
    else:
        say(f"OK: heartbeat {age:.1f} min ago")

    # 4) Recent CRITICAL alerts
    since_ms = int((time.time() - args.critical_since_mins * 60) * 1000)
    crits = critical_alerts_since(alerts_jsonl, since_ms)
    if crits:
        say(
            f"FAIL[{EXIT_CRITICAL_ALERT}]: {len(crits)} CRITICAL alert(s) in last "
            f"{args.critical_since_mins:.0f} min:"
        )
        for a in crits[:3]:
            say(f"  - [{a.get('source')}] {a.get('message')}")
        return EXIT_CRITICAL_ALERT
    say(
        f"OK: no CRITICAL alerts in last {args.critical_since_mins:.0f} min"
    )

    say("HEALTHY")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"FAIL[{EXIT_INTERNAL}]: internal error: {exc}", file=sys.stderr)
        sys.exit(EXIT_INTERNAL)
