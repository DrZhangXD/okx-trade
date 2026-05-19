# Factor Research Lab + FactorPortfolioStrategy 设计

**日期**：2026-05-19
**作者**：Claude + zhangxudong（brainstorm 决策见末尾"决策记录"）
**状态**：设计阶段，待 user 评审 → 转 writing-plans

---

## 1. 背景与动机

仓库已有 9 个策略 + `ml_fusion` 雏形 + `_features.py` 10 个特征 + `backtest/walk_forward.py`。
缺的是**系统化评估任意新因子是否值得上线的研究流程**——目前加因子等同于直接动 `_features.py`
+ 等 ml_fusion 模型 retrain，没有 IC / IR / decay / 换手 / capacity 这些客观指标，无法
回答"这个因子有没有 alpha？衰减多快？容量多大？"。

本项目交付一个**因子研究实验室**（offline pipeline，CLI 驱动），把任意一个用户写的因子
函数自动 grade，并通过一个**通用的** `FactorPortfolioStrategy` 把通过 grade 的因子合成
为实盘信号。研究侧出口 = 策略侧入口（同一份 `factor_zoo.yaml`），实现"研究→灰度→实盘"
零摩擦闭环。

## 2. 目标与非目标

### Goals

1. 任意新因子可在 ≤ 5 分钟内得到 IC/IR/decay/换手报告（CLI 一行命令）。
2. 因子定义是**纯函数 + 元数据注册**，零侵入现有策略代码。
3. 通过 grade 的因子追加到 yaml → `FactorPortfolioStrategy` 下一次 rebalance 直接读到，
   无需改 Python。
4. 灰度上线：每个因子有显式权重，可从 0.1 起步 paper 跑，糟糕的删一行配置即下线。
5. 一份因子定义同时支撑：(a) 离线研究、(b) 实盘策略、(c) 后续 `ml_fusion` 重用。
6. v1 ship 15 个因子横跨 5 大类（动量、funding/OI、basis、波动率、流向）。

### Non-goals

- **不做高频微观结构因子**（books5 / trades tick 历史落盘是 phase 2 范围）。
- **不做链上/宏观因子**（需要 OKX 之外的数据源；phase 3）。
- **不做暴力搜索/genetic programming**（数据短易过拟合；先有评估流程再谈搜索）。
- **不在 v1 让 `ml_fusion` 自动消费新因子**（v1 先让 `FactorPortfolioStrategy` 跑通；
  ml_fusion 接入是 phase 2，且必须用同一份因子注册表）。
- **不做 Jupyter notebook 工作流**——仓库整体偏 CLI/script（`scripts/` 目录），保持一致。

## 3. 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│  CLI Layer                                                       │
│   python -m okx_trade.research.factor eval --factor <id>         │
│   python -m okx_trade.research.factor list                       │
│   python -m okx_trade.research.factor approve --factor <id> ...  │
│   python -m okx_trade.research.factor backtest-portfolio         │
├─────────────────────────────────────────────────────────────────┤
│  Research Pipeline (offline, 纯 numpy + sqlite)                  │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │
│   │ Registry │→ │ Compute  │→ │  Grade   │→ │ Approve(yaml)│   │
│   │ (decor.) │  │ (panel)  │  │ (IC/IR…) │  │              │   │
│   └──────────┘  └──────────┘  └──────────┘  └──────────────┘   │
├─────────────────────────────────────────────────────────────────┤
│  Data Layer (research/data.py)                                   │
│   FactorPanel:  ts × inst × {close, vol24h, funding, oi, basis}  │
│   缓存：var/factor_research/panel/{date_range}.parquet           │
├─────────────────────────────────────────────────────────────────┤
│  Strategy Layer                                                  │
│   FactorPortfolioStrategy (新)：读 factor_zoo.yaml,              │
│     compute_all → weighted z-score → top-K long / bot-K short    │
├─────────────────────────────────────────────────────────────────┤
│  Existing infra（不改）                                          │
│   rest.market / rest.public  + backtest.data_loader              │
│   risk / pnl / portfolio / monitor / NT adapter                  │
└─────────────────────────────────────────────────────────────────┘
```

研究侧和策略侧通过两份制品对接：
1. `src/okx_trade/research/factors/*.py`：因子定义（带 `@register_factor` 装饰器）。
2. `configs/factor_portfolio.yaml`：approved 因子 + 权重，运行时被两侧共同读取。

## 4. 模块布局

```
src/okx_trade/
├── research/                          ← 新模块（全部纯 Python，不依赖 NT）
│   ├── __init__.py
│   ├── registry.py                    ← @register_factor 装饰器 + 全局 _REGISTRY
│   ├── panel.py                       ← FactorPanel dataclass + 构造器
│   ├── data.py                        ← 批量拉历史 + parquet 缓存
│   ├── compute.py                     ← 跑某因子在某 panel 上的值矩阵
│   ├── grade.py                       ← IC / IR / decay / turnover / capacity
│   ├── store.py                       ← sqlite 操作（factor 元数据 + grade 记录）
│   ├── report.py                      ← markdown 报告生成
│   ├── walk_forward_grade.py          ← 因子在滚动窗口上的 OOS IC（见 §11）
│   ├── cli.py                         ← `python -m okx_trade.research.factor`
│   └── factors/                       ← 因子定义（每文件一类）
│       ├── __init__.py                ← 触发所有 factor 注册
│       ├── momentum.py                ← 4 个：1d/3d/7d/risk_adj
│       ├── funding_oi.py              ← 4 个：funding_z, oi_chg, oi_to_vol, funding_oi_cross
│       ├── basis.py                   ← 2 个：perp_spot_basis, basis_z
│       ├── volatility.py              ← 3 个：rv_pct, rv_skew, vol_of_vol
│       └── flow.py                    ← 2 个：spread_avg, taker_buy_ratio
├── strategies/
│   └── factor_portfolio.py            ← 新：FactorPortfolioStrategy（消费 yaml）
configs/
└── factor_portfolio.yaml              ← approved 因子 + 权重
scripts/
└── factor_research_smoke.sh           ← 一键回归：list → eval 3 个 → backtest
docs/superpowers/specs/
└── 2026-05-19-factor-research-lab-design.md  （本文件）
var/factor_research/
├── factor_zoo.db                      ← sqlite：factors / grade_runs / ic_history
├── panel/                             ← parquet 缓存
└── reports/                           ← markdown 报告
```

**为什么 `research/` 是独立顶层模块、不放进 `strategies/`**：研究模块零 NT 依赖、纯函数 +
numpy，能在 CI 里跑而不用装 `nautilus_trader`；策略侧 `factor_portfolio.py` 也只 import
`research.registry` + `research.factors`，不反向依赖 NT。

## 5. 核心抽象

### 5.1 `FactorPanel`

```python
@dataclass(frozen=True, slots=True)
class FactorPanel:
    """某段时间内一组 instrument 的对齐数据快照。

    所有数组 shape = (T, N)，T 是时间步数，N 是 instrument 数。
    时间步对齐到 bar 收盘（UTC）。inst_ids 顺序固定，对应数组的列。
    """
    inst_ids: tuple[str, ...]            # 长度 N
    timestamps_ms: tuple[int, ...]       # 长度 T，bar close ms
    close: np.ndarray                    # (T, N) float64
    volume_usdt: np.ndarray              # (T, N) float64 — 24h 滚动成交额
    funding_rate: np.ndarray | None      # (T, N) float64 — 当前 8h funding；非 SWAP 为 NaN
    open_interest: np.ndarray | None     # (T, N) float64 — 名义 USDT OI
    basis_apr: np.ndarray | None         # (T, N) float64 — 永续 vs 现货年化 basis
    # 缺失数据用 np.nan；因子函数自己决定怎么处理
```

### 5.2 因子注册

```python
@register_factor(
    id="momentum_7d",
    category="momentum",
    description="7-day price momentum, (close_t / close_{t-7d}) - 1",
    direction="long_high",          # high score 倾向多；alt: "long_low"
    required_data=("close",),       # panel 必须有的字段
    min_history_bars=7 * 24,        # 1H bar 计 168 根
    rebalance_minutes=240,          # 因子刷新周期（建议值，不强制）
)
def momentum_7d(panel: FactorPanel) -> np.ndarray:
    """返回 shape (T, N) 的因子值；不够 history 的位置返回 np.nan。"""
    closes = panel.close
    out = np.full_like(closes, np.nan)
    lb = 7 * 24
    out[lb:] = closes[lb:] / closes[:-lb] - 1.0
    return out
```

合同：因子函数**纯函数**（无 IO、无副作用、不依赖全局）、输入 `FactorPanel`、输出
`(T, N)` `np.ndarray`，缺失位置 `np.nan`。`direction` 字段告诉评估器 "long_high" =
高分做多 / "long_low" = 低分做多（如 funding_z，funding 越低越值得多）。

### 5.3 评估指标

```python
@dataclass(frozen=True, slots=True)
class FactorGrade:
    factor_id: str
    panel_start_ms: int
    panel_end_ms: int
    horizon_bars: int                # 评估的 forward return 期数

    # 横截面 IC（spearman）
    ic_mean: float                   # rank IC 时间平均
    ic_std: float
    ir: float                        # ic_mean / ic_std
    ic_t_stat: float                 # ic_mean * sqrt(n_periods) / ic_std
    ic_positive_rate: float          # IC > 0 的时段占比

    # 衰减
    ic_decay: list[float]            # 顺序对应 horizon=[1, 2, 4, 8, 16, 32] bars

    # 换手 / 容量
    turnover_avg: float              # top-K vs bot-K 集合每期变化率
    autocorr_1: float                # 因子值 1 期自相关，越高换手越低
    long_short_spread: float         # top-K 平均 fwd_ret − bot-K 平均 fwd_ret（不扣费）

    # 净 PnL（扣 round-trip 5bps × 2）
    net_ls_spread_after_fees: float

    # 元
    n_periods: int
    n_instruments: int
```

**通过门槛**（推荐默认，可在 CLI 覆盖）：
- `ic_t_stat ≥ 2.0`
- `ir ≥ 0.3`
- `ic_positive_rate ≥ 0.55`
- `net_ls_spread_after_fees > 0`
- `autocorr_1 ≥ 0.3`（防止纯噪声因子，换手爆掉）

## 6. v1 因子动物园（15 个）

| ID | category | direction | 一句话 |
|---|---|---|---|
| `momentum_1d` | momentum | long_high | 24h 收益率 |
| `momentum_3d` | momentum | long_high | 72h 收益率 |
| `momentum_7d` | momentum | long_high | 168h 收益率 |
| `momentum_risk_adj_7d` | momentum | long_high | momentum_7d / rv_30d |
| `funding_current` | funding_oi | long_low | 当前 8h funding rate（越负越值得多） |
| `funding_z_30d` | funding_oi | long_low | funding 30 天滚动 z-score |
| `oi_change_1d` | funding_oi | long_high | OI 24h 变化率 |
| `oi_to_volume_ratio` | funding_oi | long_high | OI / 24h volume，高 = 持仓粘性强 |
| `basis_apr` | basis | long_low | 永续 vs 现货年化 basis（高 = 期货溢价，做空 perp 套利） |
| `basis_z_30d` | basis | long_low | basis_apr 30 天 z-score |
| `rv_pct_365d` | volatility | long_low | RV 在 365 天的分位数（低波动倾向跑赢） |
| `rv_skew_up_down` | volatility | long_high | (up_day_rv − down_day_rv) / total_rv |
| `vol_of_vol_30d` | volatility | long_low | RV 的 RV，30 天 |
| `spread_avg_1d` | flow | long_low | 平均 bid-ask spread（流动性代理） |
| `taker_buy_ratio_1d` | flow | long_high | 主动买 / 总成交（aggressor flow） |

每个因子有独立单测：(a) 纯函数 happy path 用合成 panel；(b) 缺失 history 返回全 nan；
(c) 缺失 required_data 时 `compute_factor` raise 友好错误。

## 7. 研究 pipeline

### 7.1 用户工作流

```bash
# 1) 看现有因子列表 + grade 状态
python -m okx_trade.research.factor list
# 输出：id, category, last_graded_at, ic_mean, ir, approved?

# 2) 第一次拉数据（30 个币 × 180 天 × 1H bar，~5 分钟）
python -m okx_trade.research.factor fetch \
    --start 2025-11-01 --end 2026-05-15 --universe top30

# 3) 写一个新因子，跑 grade
python -m okx_trade.research.factor eval \
    --factor my_new_factor \
    --horizon 1d --top-k 5
# → 输出到 var/factor_research/reports/my_new_factor_2026-05-19.md
# → 同时写一行到 var/factor_research/factor_zoo.db.grade_runs

# 4) approve（写 yaml + 标 sqlite approved=1）
python -m okx_trade.research.factor approve \
    --factor my_new_factor --weight 0.15 --direction long_high

# 5) 跑 portfolio backtest 看合成效果
python -m okx_trade.research.factor backtest-portfolio \
    --start 2026-02-01 --end 2026-05-15
```

### 7.2 grade 算法（horizon=1d 示例）

输入：`FactorPanel`，因子函数，horizon=24 bars（1H 频率下 = 1 天）。

```
for t in range(min_history, T - horizon):
    factor_vec = factor_func(panel)[t]              # shape (N,)
    fwd_ret    = panel.close[t+horizon] / panel.close[t] - 1.0  # (N,)
    # 横截面 spearman rank IC
    ic_t = spearman(factor_vec, fwd_ret)             # 标量
    ic_series.append(ic_t)

ic_mean       = mean(ic_series)
ic_std        = std(ic_series)
ir            = ic_mean / ic_std
ic_t_stat     = ic_mean * sqrt(len(ic_series)) / ic_std
ic_positive_rate = mean([1 if ic > 0 else 0 for ic in ic_series])
```

衰减：horizon ∈ {1, 2, 4, 8, 16, 32} bars 都跑一遍 IC mean。

换手：每期取 top-K 集合 `S_t`，turnover = `|S_t \ S_{t-1}| / K`。

PnL：每期算 `mean(fwd_ret[top_K]) - mean(fwd_ret[bot_K])`，扣 0.10% × 2 × turnover。

### 7.3 报告示例

`var/factor_research/reports/momentum_7d_2026-05-19.md`：

```markdown
# Factor Grade: momentum_7d
- Period: 2025-11-01 → 2026-05-15 (180d, 4320 bars × 30 inst)
- Horizon: 1d (24 bars)

## IC
| metric | value |
|---|---|
| ic_mean | 0.045 |
| ic_std | 0.082 |
| ir | 0.549 |
| t_stat | 4.31 |
| positive_rate | 0.62 |

## Decay (IC by horizon)
... 表 ...

## Long-Short Spread (top-5 vs bot-5)
- gross daily: +0.18%
- after fees:  +0.11%
- annualized:  ~40%
- turnover_avg: 0.22

## Verdict: PASS ✅ (满足 ic_t_stat≥2 / ir≥0.3 / pos_rate≥0.55 / net>0 / autocorr≥0.3)
```

## 8. 交易侧：`FactorPortfolioStrategy`

### 8.1 yaml 契约

`configs/factor_portfolio.yaml`：

```yaml
bar: "1H"
rebalance_hours: 4
top_k_long: 5
top_k_short: 5
risk_pct_per_leg: 0.002
universe:
  size: 30
  settle_ccy: USDT
  sort_by: vol24h

factors:
  - id: momentum_7d
    weight: 0.3
  - id: funding_z_30d
    weight: 0.25
  - id: oi_change_1d
    weight: 0.15
  - id: basis_z_30d
    weight: 0.15
  - id: rv_pct_365d
    weight: 0.15
```

权重和不必为 1（合成前内部做截面 z-score normalization 再加权）。`direction` 不在
yaml 里写——直接读因子注册表，避免双源。

### 8.2 运行时

```
on_bar(1H):
    closes[inst_id].append(...)
每 rebalance_hours:
    panel = build_live_panel(closes_buffer, funding_buffer, oi_buffer, basis_buffer)
    factor_vals = {id: compute_factor(id, panel)[-1] for id in cfg.factors}
    # 应用 direction：long_low 翻号
    for id in factor_vals:
        if registry[id].direction == "long_low":
            factor_vals[id] *= -1
    # 截面 z-score → 加权合成
    z = {id: cross_section_z(vals) for id, vals in factor_vals.items()}
    score = sum(weight * z[id] for id, weight in cfg.factors)
    # top-K / bot-K
    sorted_insts = sorted(score.items(), key=lambda x: x[1])
    longs  = [i for i, _ in sorted_insts[-top_k_long:]]
    shorts = [i for i, _ in sorted_insts[:top_k_short]]
    rebalance_to(longs, shorts)
```

复用 `xs_momentum` 的 vol-managed sizing + `apply_risk_manager` + `record_strategy_trade`。
即：每个 inst 仓位按 `vol_target_position_size` 缩放到目标年化波动，过 `KellyCheck /
DrawdownTracker / VolTargetCheck / CorrelationCheck` 风控链，PnL 喂 `PnLTracker`。

### 8.3 失效保护

- 启动时 `factor_zoo.yaml` 任一因子 id 找不到注册表 → fail-fast（不 idle）；
- 任一因子在 live panel 上返回全 nan → log WARN + 把该因子权重临时按 0 处理，
  其他因子继续工作；
- `regime_state` 在 `mean_reverting` 时，整策略 cooldown——regime 来源沿用
  `risk.regime_detector`（与 `xs_momentum` / `ml_fusion` 共享同一信号源，避免新增
  独立 regime 检测），防止震荡市强行 momentum-style 暴击；
- 单 inst 最大权重 ≤ 25%（写在 `factor_portfolio.yaml`，过软上限触发 WARN）。

## 9. 存储层

| 存储 | 用途 | 后端 |
|---|---|---|
| `var/factor_research/factor_zoo.db` | factor 元数据（id/category/description/direction），grade 历史（每次 eval 一行），approved 状态 | sqlite（已用于 pnl）|
| `var/factor_research/panel/` | 历史 panel 缓存（按 start_end 命名 parquet） | parquet（沿用 NT catalog 同款）|
| `var/factor_research/reports/` | markdown 报告 | 纯文件 |
| `configs/factor_portfolio.yaml` | live 因子 + 权重（被策略读取）| yaml |

sqlite schema：

```sql
CREATE TABLE factors (
  id TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  direction TEXT NOT NULL,            -- 'long_high' | 'long_low'
  description TEXT,
  approved INTEGER NOT NULL DEFAULT 0,
  approved_weight REAL,
  approved_at_ms INTEGER
);

CREATE TABLE grade_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  factor_id TEXT NOT NULL REFERENCES factors(id),
  panel_start_ms INTEGER NOT NULL,
  panel_end_ms INTEGER NOT NULL,
  horizon_bars INTEGER NOT NULL,
  ic_mean REAL, ic_std REAL, ir REAL, ic_t_stat REAL, ic_positive_rate REAL,
  turnover_avg REAL, autocorr_1 REAL,
  long_short_spread REAL, net_ls_spread_after_fees REAL,
  n_periods INTEGER, n_instruments INTEGER,
  verdict TEXT NOT NULL,              -- 'pass' | 'fail'
  graded_at_ms INTEGER NOT NULL
);

CREATE INDEX idx_grade_runs_factor ON grade_runs(factor_id, graded_at_ms DESC);
```

## 10. 数据加载

`research/data.py`：

```python
async def fetch_panel(
    rest_client: OKXRestClient,
    inst_ids: list[str],
    start_ms: int,
    end_ms: int,
    *,
    bar: BarSize = BarSize.H1,
    include: tuple[str, ...] = ("close", "volume_usdt", "funding_rate",
                                 "open_interest", "basis_apr"),
) -> FactorPanel:
    """并发拉每个 inst 的 candles + funding history + OI history（如果 swap）。
    OKX 限频：candles 端点 20 req/2s，自动 batch。复用 backtest.data_loader
    的 download_historical_bars。新增的 OI/basis fetch 走 rest.market + rest.public。
    """
```

缓存策略：`fetch_panel` 第一次跑后落 `var/factor_research/panel/{start}_{end}_{inst_hash}.parquet`，
再次调用同参数直接 mmap 读 parquet，零网络。

OKX REST 已覆盖：candles、history-candles（`market.get_candles_extended`）、
funding-rate、funding-rate-history（`public.get_funding_rate_history_extended`）。

**本 spec 唯一对 SDK 的扩展**（其它模块零侵入）：在 `rest/public.py` 新增两个端点
（OI 属于 public 类，与 instruments / funding 同源）：
- `get_open_interest(inst_id) → OpenInterest`（即时 OI，对应 OKX `/api/v5/public/open-interest`）
- `get_open_interest_history(inst_id, period, start, end) → list[OpenInterestPoint]`
  （对应 `/api/v5/rubik/stat/contracts/open-interest-volume` 或 `/api/v5/public/open-interest`
  的 history 形态，分页串行）

模型加在 `models/market.py`（`OpenInterest` / `OpenInterestPoint` dataclass）。
Basis 由 perp candle close − spot candle close 计算，不需要新端点。

## 11. 回测集成

复用 `backtest/walk_forward.py` 的 `walk_forward_splits`，新增 `research/walk_forward_grade.py`：
对一个因子做 6m train / 1m test 滚动 IC，输出衰减曲线，回答"因子是否在不同 regime 都稳"。

`FactorPortfolioStrategy` 直接挂到 `scripts/backtest.py`（已有 yaml 驱动的策略加载机制），
通过 `--strategy factor_portfolio --config configs/factor_portfolio.yaml` 跑端到端回测，
出 equity / Sharpe / max DD / 与现有策略的相关性。

## 12. CLI 详细

```
python -m okx_trade.research.factor <subcommand>

subcommands:
  list                          # 列所有注册的因子 + 最新 grade
  fetch  --start --end --universe [top30|<list>]
  eval   --factor <id> --horizon [1d|4h|...] [--top-k 5] [--no-cache]
  grade-all                     # 跑所有注册因子（CI 友好）
  approve --factor <id> --weight <f> [--force]
  reject  --factor <id>
  backtest-portfolio --start --end [--yaml configs/factor_portfolio.yaml]
  report  --factor <id>         # 重出 markdown 报告
```

`scripts/factor_research_smoke.sh`：list → fetch（小窗口）→ eval 3 个内置因子 → backtest，
作为 CI smoke + 新机器首次跑的回归脚本。

## 13. 测试策略

| 层 | 测什么 | 怎么测 |
|---|---|---|
| `research/registry.py` | 注册去重、查询、required_data 验证 | 单测 |
| `research/panel.py` | shape 对齐、缺数据 nan、column 顺序稳定 | 单测 + 合成数据 |
| `research/compute.py` | 已知因子返回值精确匹配 | 黄金值 fixture |
| `research/grade.py` | IC 已知答案（合成 panel：完美预测因子 IC=1.0；纯噪声 IC≈0）| 合成数据 |
| `research/store.py` | sqlite schema 创建 + grade 写入 + approve 状态机 | 内存 sqlite |
| `research/data.py` | mock rest_client + 验证缓存命中 | 单测 mock |
| 每个 factor 函数 | happy path + 缺 history + 缺 required_data raise | 单测 |
| `factor_portfolio.py` | 纯函数：z-score → 合成 → top-K 选股 | NT 无关单测 |
| `factor_portfolio` NT 类 | rebalance 触发频率、风控接入 | NT BacktestEngine 集成测试 |
| smoke | `scripts/factor_research_smoke.sh` 走完不报错 | shell |

目标：单测数 ≥ 80（现有 449，加完 ≈ 530）。grade pipeline 在合成 panel 上要能达到
**确定性**结果（设 random_seed），避免 flaky。

## 14. 分期 & 范围控制

| Phase | 范围 | 验收 |
|---|---|---|
| **P1（本 spec）** | research/ 模块全套 + 15 因子 + FactorPortfolioStrategy + CLI + 文档 | smoke 通过 + paper 跑 24h 无异常 |
| P2（后续 spec） | ml_fusion 改用 research.registry；高频 tick 落盘 + 微观结构因子 | — |
| P3（后续 spec） | 外部数据（链上、宏观）；遗传/LLM 因子生成 | — |

P2/P3 **不在本 spec 范围**，避免一次性吞太多。

## 15. 风险与对策

1. **回测过拟合**：15 因子用同段历史 grade，必然过拟合。对策：(a) grade 必须留 **out-of-sample
   30 天** 不可见；(b) 默认 walk-forward 6m/1m 滚动，看 OOS IC 是否塌；(c) 上线后每周
   重 grade，OOS IC 跌破 60% 阈值的因子触发 WARN。
2. **因子相关性高**：动量 1d/3d/7d 之间高度共线，加权合成等价于"动量重权重"。对策：
   `grade-all` 输出 15×15 因子相关矩阵，user 在分配 weight 时人为去重；v1 不做自动
   PCA 降维。
3. **OKX 数据缺失**：早期上线币种没有 funding/OI 历史。`fetch_panel` 用 nan 填充；因子
   函数自然返回 nan；评估器跳过 nan 期；避免静默用 0 假装有数据。
4. **CLI/yaml 串号**：approve 写 yaml 失败但 sqlite 已标 approved。对策：**先写 yaml，
   写成功再更新 sqlite**；任一步失败 rollback sqlite。
5. **策略与现有 xs_momentum 同向**：`FactorPortfolioStrategy` 的 momentum 因子和
   `xs_momentum` 强相关。对策：上线时让 `CorrelationCheck` 接入（已有），corr > 0.7 时
   砍 factor_portfolio 仓位；启用前先 paper 7 天观察相关性。
6. **每 4h rebalance 的换手**：top-K=5 全换 = 20 笔 swap 单 + 5bps × 2 × 5 = 50bps 费用。
   对策：grade 已扣费、未通过门槛的因子无法 approve；策略级再加 `min_holding_hours=4`
   强制对冲过快换手。

## 16. 决策记录

来自 brainstorm 对话（2026-05-19）：

| Q | 选项 | 决策 | 理由 |
|---|---|---|---|
| Q1 形态 | A 研究实验室 / B 扩 ml_fusion / C 暴力搜 | **A** | 缺评估流程不缺想法；B 是 A 的下游应用；C 数据短易过拟合 |
| Q2 universe+horizon | A 中频横截面 / B 微观 / C 低频宏观 / D 全要 | **A** | 现有策略全在此尺度；OKX 数据 sufficient；Sharpe 1.5-2.5 区间合理 |
| Q3 交易出口 | A 喂 ml_fusion / B 一因子一策略 / C 通用 FactorPortfolio / D AC 都做 | **C** | 灰度可控、yaml 即上线、xs_momentum 模式可抄、不与 ml_fusion 互斥 |

未询问、由 designer 拍板的：
- 工作流：CLI（与 `scripts/` 风格一致，不引 Jupyter）
- 存储：sqlite（沿用 pnl.sqlite 经验）+ parquet（沿用 NT catalog）+ yaml（沿用 strategy 配置）
- 因子签名：纯函数 + `@register_factor` 装饰器
- v1 因子规模：15 个 / 5 类（够覆盖、不至于稀释 IC）
- 通过门槛：t_stat≥2、IR≥0.3、pos_rate≥0.55、net>0、autocorr≥0.3（可 CLI 覆盖）
