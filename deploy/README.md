# okx-trade VPS 部署（M5 paper trading）

让 4 策略在云上 24/7 跑，本地关机不影响。最小成本 ~$5/月（Hetzner CX11 / DigitalOcean basic）。

## 选机

| 候选 | 价格 | 备注 |
|---|---|---|
| Hetzner CX22 (Falkenstein DE) | €4.5/月 | 推荐，欧洲机房 OKX 直连快 |
| DigitalOcean basic 2GB | $14/月 | NYC/SFO/SGP 机房 |
| Vultr Cloud Compute 1GB | $6/月 | 亚太节点离 OKX HK 近 |

**机器要求**：≥1 vCPU，≥1 GB RAM（NT + 4 strategies ~500MB），≥10 GB SSD，Ubuntu 22.04+ 或 Debian 12。

**网络**：海外机房直连 OKX，国内 IP 会被限速；机房延迟 < 100ms 即可（不打高频）。

## 三步部署

### 1. SSH 登入新 VPS

```bash
ssh root@<vps_ip>
```

### 2. 跑 bootstrap

如果代码已 push 到 git remote：

```bash
export REPO_URL=https://github.com/<you>/okx-trade.git
curl -fsSL "${REPO_URL%.git}/raw/main/deploy/bootstrap.sh" | sudo bash
```

如果代码暂时只在本地，先 `scp` 上去：

```bash
# 本地：
scp -r /Users/zhangxudong/okx-trade root@<vps_ip>:/home/okxtrade/okx-trade
ssh root@<vps_ip> 'chown -R okxtrade:okxtrade /home/okxtrade/okx-trade'

# VPS：
sudo bash /home/okxtrade/okx-trade/deploy/bootstrap.sh
```

### 3. 填凭证 + 启动

```bash
sudo -u okxtrade nano /home/okxtrade/okx-trade/.env   # OKX_API_KEY / SECRET / PASSPHRASE / IS_DEMO=true

# 配置 dry-run（验证）
sudo -u okxtrade /home/okxtrade/okx-trade/.venv/bin/python \
    /home/okxtrade/okx-trade/scripts/live.py --check

# 真启动 + healthcheck timer
sudo systemctl enable --now okx-trade
sudo systemctl enable --now okx-trade-healthcheck.timer

# 实时日志
journalctl -u okx-trade -f
```

## 运维

```bash
# 看实时日志
journalctl -u okx-trade -f

# 看 healthcheck 历史
journalctl -u okx-trade-healthcheck -n 50

# 看 alert 文件
sudo tail -f /home/okxtrade/okx-trade/var/alerts.jsonl

# 看 PnL DB
sudo sqlite3 /home/okxtrade/okx-trade/var/pnl.sqlite \
  "SELECT strategy_id, COUNT(*), SUM(pnl_usdt) FROM trades GROUP BY strategy_id;"

# 看每日报表
ls /home/okxtrade/okx-trade/var/daily_reports/

# 重启
sudo systemctl restart okx-trade

# 停
sudo systemctl stop okx-trade
sudo systemctl stop okx-trade-healthcheck.timer

# 查最近一次 healthcheck 结果
systemctl status okx-trade-healthcheck.service
```

## 升级（git pull）

```bash
sudo -u okxtrade -i
cd okx-trade
git pull
.venv/bin/pip install -e ".[strategy]"
exit
sudo systemctl restart okx-trade
```

## healthcheck 行为

`scripts/healthcheck.py` 每 5 分钟由 timer 触发，检查 4 件事：

1. `scripts/live.py` 进程在跑（`pgrep -f`）→ exit 1
2. 进程 RSS < 2GB（防内存泄漏）→ exit 4
3. `var/pnl.sqlite` 在最近 30 分钟内被写过（说明策略仍在喂 equity）→ exit 2
4. `var/alerts.jsonl` 没有最近 10 分钟内的 CRITICAL alert → exit 3

systemd `Restart=on-failure` 在 okx-trade 进程崩了时会自动重启；healthcheck 失败本身**不会**重启 main service（避免 healthcheck bug 把 main 拖崩），只在 systemd journal 留诊断。

需要更激进可改：在 `okx-trade-healthcheck.service` 加 `OnFailure=okx-trade-restart.service`，再写一个 `okx-trade-restart.service` 跑 `systemctl restart okx-trade`。

## 安全

- `okx-trade.service` 已开启 `NoNewPrivileges`、`ProtectSystem=strict`、`ProtectHome=read-only`、`ReadWritePaths=var/`
- `.env` 模式 600，仅 `okxtrade` 用户可读
- 防火墙：`ufw allow 22/tcp; ufw enable`（不需要开任何对外端口）

## 紧急停手

`paper_trading: true` 模式下不会动真钱，但若你怀疑代码有 bug：

```bash
sudo systemctl stop okx-trade
# 在 OKX demo 账户里手动撤所有未成交单（NT 没实现 cancel-all 的话）
```

实盘切换前**必须**先：
1. paper trading 至少 7-14 天
2. 看 `var/daily_reports/*.json` 验证 PnL / 胜率符合预期
3. 看 `var/alerts.jsonl` 是否有 CRITICAL（drawdown 触发）
4. 把 `account.paper_trading: false` + `OKX_IS_DEMO=false` + 资金减半上线
