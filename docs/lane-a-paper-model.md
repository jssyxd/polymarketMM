# Lane A 纸面成交模型修复与活跃市场重跑

日期：2026-11-27
范围：`polymarket-mm` 的 paper mode，VPS `pmm-paper30.service`。

## 旧模型的问题

旧代码在 `src/pmm/engine/runner.py` 的 `_record_paper_fills` 里用：

```python
ask = _best_ask(book)
if ask <= intent.price:
    fill
```

这要求盘口 best ask 下穿买单。正常 CLOB 里 best ask 永远高于 best bid，只有盘口交叉才会出现这个条件。结果 24 小时纸面 2,520 轮、997 条 order_intent、0 个 fill。

## 新模型

新增：

- `src/pmm/data/public_trades.py`：读取 Data API v2 `/v2/trades?condition=...&limit=500`，得到真实 taker 成交，字段包括 side、price、size、token_id、timestamp、transaction_hash。
- `src/pmm/engine/paper_fills.py`：
  - `queue_ahead(book, price)` 统计价格 `>= price` 的可见买单量。
  - `consume(ahead, trades, price, token_id, placed_ts, seen)` 按时间顺序消费 taker `SELL`；成交价必须 `<= price`，先扣前方队列，超过剩余队列才算 fill。
- `Runner._record_paper_fills` 改为每轮读最近 500 笔公共成交，按 `(condition, leg, price)` 维护 `ahead`、`placed_ts`、`seen`。
- 只在 quote 第一次放置时采样队列；之后不假设新的排队或撤单，保持保守。

## 测试

本地与 VPS 都是 **273 passed**。新增测试覆盖：

- 前方队列求和。
- 卖单小于队列不成交。
- 卖单大于队列成交。
- 卖单正好等于队列只清空队列，不成交。
- side、价格、token、时间过滤。
- 旧的 integration test 改为 mock 公共成交，不再依赖盘口 ask。

## 真实盘口校验

对活跃市场 `0x32018ac6...`（Riemann Hypothesis，0.615 附近，24h 约 6.5 万）直接取真实盘口和最近 500 笔成交：

| 报价 | 前方队列 | 结果 |
|---:|---:|---|
| best bid 0.61 | 3,744.47 | 有一笔 2,025.59 @ 0.57 的大卖单，成交 |
| 0.60 | 6,389.09 | 剩余 637.07，未成交 |
| 0.59 | 6,784.09 | 剩余 4,421.84，未成交 |
| 0.58 | 6,804.09 | 剩余 4,441.84，未成交 |

这说明模型能用真实成交流工作，也说明为什么 3c 报价在活跃市场仍然可能不成交：前方队列有几千股。

## 30 分钟重跑

- 单元：`pmm-paper30.service`
- 市场：`0x32018ac6e226dbcf3154e78f6f834e381077a23bb412dc9d28f480613e34b21e`
- 参数：`--paper --interval 10 --capital 25 --size 20 --min-rate 0 --max-spread 10`
- 报价：Up 0.58，Down 0.35，各 20 股
- 启动：2026-11-27 15:00:57 +08
- 结束：2026-11-27 15:30:57 +08

## 30 分钟结果

| 项目 | 值 |
|---|---:|
| cycles | 158 |
| order_intent | 2 |
| cancel | 0 |
| fills | **0** |
| errors | 0 |
| quoted_max | 1 |
| committed_max | 20 pUSD |

唯一报价是 Up 0.58 和 Down 0.35，各 20 股；30 分钟内没有 taker SELL 打到这两个价格。
对照上面的真实盘口校验，best bid 0.61 时前方队列 3,744 股，只有大卖单能穿透；0.58 的前方队列 6,804 股，30 分钟内没有足够卖量。

所以这次 0 成交不是模型没接上成交流，而是 **3c 半价差 + 深队列** 的结果。模型已在真实成交流上验证能成交，但在 0.58 这个价位没有触发。

## 待办

- 用成交表和队列位置重跑后，如果 3c 报价仍 0 成交，测试 1c 半价差和 best bid 报价，确认是参数问题还是队列问题。
- 若要提高纸面 fill 率，应选择盘口较薄、成交额较高、价格波动更大的市场，而不是只看 volume24hr。
- 恢复实盘前仍需要硬熔断和单腿限时补腿/平仓，纸面 fill 不是实盘成交。

## 1c 半价差 15 分钟重跑

市场选的是最近成交流里的体育盘 `0xbbf57093...`（76ers vs Pacers O/U 231.5，mid 0.465，end 约 16 小时），
因为原来的 Bitcoin 市场 `0x752fa...` 最近两小时没有成交。

| 项目 | 值 |
|---|---:|
| 单元 | `pmm-paper15.service` |
| 报价 | Up 0.45，Down 0.52，各 20 股 |
| cycles | 169 |
| order_intent | 2 |
| fills | **0** |
| cancels | 0 |
| errors | 0 |
| 配对率 | 无法计算，0 fill |
| 单腿率 | 无法计算，0 fill |
| 净捕获 | 0，仅因为没有成交 |

结束时间 2026-11-27 15:57:35 +08，`RuntimeMaxSec=900`，状态 `timeout`，不是崩溃。

## 1c 仍 0 成交的含义

1c 报价已经是 best bid 附近，仍然 0 fill。结合真实盘口校验：

- 在 best bid 0.61，前方队列 3,744 股，只有 2,025 股的大卖单能穿透。
- 在 0.58，前方队列 6,804 股，30 分钟没有足够卖量。
- 15 分钟体育盘 1c 报价 0.45/0.52，也没有遇到 taker `SELL`。

结论按计划第 3 条执行：**当前 btc5m/事件盘的 20 股 maker 主要受队列限制，不是 spread 参数**。
不应该恢复实盘。

## 第 4 项前置条件进度

- 硬熔断：`pmm-alerts` 现在命中 alert 时调用 `disable_quoting()`，把 `quoting_enabled=false` 写入参数文件。已部署，276 测试通过。
- 单腿限时：`repair.DEFAULT_MAX_AGE_SECONDS` 从 900 改为 60，`pmm-repair.timer` 从每 5 分钟改为每 1 分钟。已部署。
  - 仍有限制：repair 只在 `held_cost + ask <= 1` 时补腿；不满足时不会强制亏损平仓。真正的“60 秒强制退出”还没有实现。
- 现金：当前约 1.17 pUSD，低于 25 pUSD 门槛。不能恢复实盘。
- 纸面配对率 >70%、单腿率 <50%：本次 0 fill，无法验收。

## 第 5 项

ML 继续推迟。0 fill 没有标签，先有 500 笔有标签成交和 quote-time 盘口特征，再谈 spread 模型。

## best bid 15 分钟重跑

为了让挂单进入队首，增加了纸面专用 `--paper-best-bid`，把报价从 `mid−1c` 改到该腿的 best bid。
这只用于测试队列，不改变实盘报价。

| 项目 | 值 |
|---|---:|
| 单元 | `pmm-paper15.service` |
| 市场 | `0xbbf57093...`，76ers vs Pacers O/U 231.5 |
| 报价 | Up 0.46，Down 0.53，各 20 股 |
| cycles | 170 |
| order_intent | 2 |
| fills | **0** |
| cancels | 0 |
| errors | 0 |

真实盘口校验：

| 腿 | 报价 | 前方队列 | 最近成交方向 |
|---|---:|---:|---|
| Up | 0.46 | 4,293.37 | BUY |
| Down | 0.53 | 394.55 | BUY |

最近成交只有 BUY，没有 taker SELL 打到买价；即使有，Up 前方 4,293 股也需要大扫单。

## 结论

- 1c 报价 15 分钟 0 fill。
- best bid 报价 15 分钟 0 fill。
- 模型在真实 0.61 盘口有成交校验，说明问题不是成交流没接上。
- 当前 20 股 maker 的主要限制是队列和当前没有打到报价的卖单。按计划第 3 条，**不恢复实盘**。

## 第 4 项代码进度

- 硬熔断：`pmm-alerts` 命中 alert 时调用 `disable_quoting()`，写 `quoting_enabled=false`。已部署。
- 单腿限时：`repair.DEFAULT_MAX_AGE_SECONDS=60`，`pmm-repair.timer` 改为每分钟。已部署。
- 纸面 best bid：`RunnerConfig.paper_best_bid` + `--paper-best-bid`。已部署。
- 测试：本地和 VPS 都是 **277 passed**。
- 仍缺：`repair` 只在 `held_cost + ask <= 1` 时补腿，不满足时没有强制亏损平仓；现金约 1.17 pUSD，低于 25 pUSD 门槛。

## best bid ±1 tick 5 分钟试验

市场：`0x44317caf...`，FAA ground stop 确认市场，mid 约 0.30，spread 约 1.9c，venue min 5，reward min 20。
三个并行的 5 分钟纸面单元，参数相同，只改 best bid 偏移：

| 偏移 | 报价 | cycles | order_intent | fills |
|---|---:|---:|---:|---:|
| -1 tick | Up 0.284 / Down 0.672 | 57 | 2 | 0 |
| 0 tick | Up 0.294 / Down 0.682 | 57 | 2 | 0 |
| +1 tick | 无 | 60 | 0 | 0 |

`+1 tick` 没有 intent，原因是双腿 best bid 各加 1 tick 后，
`up_bid + down_bid > 1 - tick_size`，配对不变量拒绝了报价。要测 +1，需要 spread ≥3c 的市场。

三个单元都是 0 fill。`-1` 和 `0` 有机会挂单，但 5 分钟内没有 taker SELL 打到这两个买价；
`+1` 被配对不变量挡掉。

## 60 秒强制退出代码

新增：

- `src/pmm/execution/exit.py`：`plan_exits()`，单腿超过 `max_age_seconds` 且 `held_cost + ask > 1` 时，计划按 best bid 卖出。
- `tools/exit_stale.py`：dry-run 默认；实盘卖出需要 `POLY_EXIT=1`。
- 位置字段适配实际 Data API：`token_id`、`condition_id`、`current_size`、`avg_price`、`current_price`。
- 入场时间从 journal fills 的 `token_id` / `ts` 读取；实际 API positions 没有 `first_entry_at`。
- 测试：本地和 VPS **284 passed**。

dry-run 当前返回 `[]`。原因不是代码没跑，而是旧仓位没有对应的 journal 入场时间，
或者同一 condition 已经有两个 token，被判定为已配对。新成交会写 journal，之后才会进入 60 秒计时。

## 当前结论

- 1c、best bid、best bid -1 tick 全部 0 fill；best bid +1 tick 被配对不变量拒绝。
- 队列和缺少打到报价的卖单仍是主因。
- 60 秒强制退出代码已实现，但未启用 `POLY_EXIT=1`，也没有实盘卖出。
- 现金约 1.17 pUSD，低于 25 pUSD 门槛，不能恢复实盘。
