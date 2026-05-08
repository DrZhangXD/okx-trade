# okx-trade

Async OKX API client (REST + WebSocket) for crypto quant trading.

第一阶段只提供底层 SDK，覆盖 OKX Swap 永续合约 + Spot 现货，全异步（asyncio）。

## Quick Start

```bash
# 安装（开发模式）
pip install -e ".[dev]"

# 配置凭证
cp .env.example .env
# 编辑 .env 填入 OKX API key / secret / passphrase

# 跑单测（无网络）
pytest tests/unit -v

# 跑集成测试（需要 demo 凭证 + 国内代理）
pytest tests/integration -v -m integration
```

## 用法示例

### REST

```python
import asyncio
from okx_trade import OKXRestClient, OKXSettings

async def main():
    async with OKXRestClient(OKXSettings()) as client:
        ticker = await client.market.get_ticker("BTC-USDT-SWAP")
        print(ticker.last)

asyncio.run(main())
```

### WS 订阅（高层 async iterator）

```python
import asyncio
from okx_trade import OKXSettings, OKXWSClient

async def main():
    async with OKXWSClient(OKXSettings()) as ws:
        async with ws.subscribe("books5", instId="BTC-USDT-SWAP") as stream:
            async for event in stream:
                print(event["data"][0])
                break  # 退出 with 自动 unsubscribe

asyncio.run(main())
```

### WS 下单

```python
from okx_trade import OKXSettings, OKXWSClient
from okx_trade.models.trade import OrderRequest
from okx_trade.enums import OrdType, PosSide, Side, TdMode

async def main():
    async with OKXWSClient(OKXSettings()) as ws:
        req = OrderRequest(
            instId="BTC-USDT-SWAP", tdMode=TdMode.CROSS, side=Side.BUY,
            ordType=OrdType.LIMIT, sz="1", px="20000", posSide=PosSide.LONG,
        )
        result = await ws.trade.place_order(req)
        print(result.ord_id)
```

更多见 [`examples/`](examples/)。

## 项目结构

```
src/okx_trade/
├── auth.py          # HMAC-SHA256 签名（纯函数）
├── config.py        # OKXSettings（pydantic-settings）
├── exceptions.py    # OKX 异常体系
├── enums.py         # InstType / TdMode / Side / OrdType / BarSize ...
├── models/          # pydantic v2 数据模型
├── rest/            # REST 客户端
└── ws/              # WebSocket 客户端（public/private/business 三连接）
```

## 设计原则

- 全异步（httpx + websockets）
- 价格/数量用 `Decimal`，杜绝浮点精度误差
- 不依赖 pandas / numpy（保持精简，DataFrame 化交给上层策略层）
- 国内代理通过 `.env` 配置，httpx 与 websockets 共用

## 状态

✅ **第一阶段完成**：REST + WS（public/private/business）客户端封装，含订阅/下单/重连/限频/重试。

- 131 个 unit test 全绿，覆盖签名、限频、订阅幂等、重连、WS 下单 等
- 集成测试 `tests/integration/` 默认 skip，配凭证后手动跑

后续阶段（不在本项目内）：策略层、风控、回测引擎、执行引擎。
