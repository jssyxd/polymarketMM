# Lane A 24 小时纸面与当前账单

日期：2026-11-27
范围：VPS `vps-evoxt` 的 `pmm-paper24.service`，2026-11-25 05:24:56Z 至 2026-11-26 05:24:56Z。
数据：`/var/lib/pmm/paper24/journal/`，本地 btc5m 录制，VPS 当前账户。

## 结论

纸面服务完整跑了 24 小时，没有真实下单，也没有报错。但 **0 次成交**，而 0 成交不是策略结论，是纸面成交模型的问题。

`Runner._record_paper_fills` 用 `best_ask <= 报价` 判断买单成交。真实 CLOB 里，买单要等 taker `SELL` 打到买价；best ask 下穿买价意味着盘口已经交叉，正常撮合不会让它发生。这个条件几乎永远不成立，所以纸面 fill 记不出来。文档路径：`src/pmm/engine/runner.py:559-586`。

因此这 24 小时的账单只能证明“服务稳定、没有下单、没有报错”，不能证明“策略不成交”或“策略盈亏为 0”。

## 纸面运行明细

| 项目 | 值 |
|---|---:|
| 服务 | `pmm-paper24.service` |
| 启动 | 2026-11-25 05:24:56Z |
| 结束 | 2026-11-26 05:24:56Z |
| 结束原因 | `RuntimeMaxSec=1d`，systemd 发送 TERM，状态 `timeout`，不是崩溃 |
| CPU | 1h 40m 30s |
| cycles | 2,520 |
| 日记 order_intent | 997 |
| 日记 cancel | 995 |
| fills | **0** |
| errors | 0 |
| order_ack | 0 |
| 参数 | half_spread 3c，inventory_box 40，max_leg_risk 12，max_market_usd 25，min_hours 24，mid [0.30, 0.70] |
| 资本 / 尺寸 / 间隔 | 25 pUSD / 20 股 / 20 秒 |

按日拆分：

| 文件 | cycles | order_intent | cancel |
|---|---:|---:|---:|
| `2026-11-25.jsonl` | 1,941 | 995 | 993 |
| `2026-11-26.jsonl` | 579 | 2 | 2 |

cycle 的 `intents` 字段合计 5,040，实际 order_intent 997。差额来自同一价格、同一腿的挂单已经存在，`_place` 不会重复落 intent。不是丢单。

## 报价分布

997 条 order_intent 覆盖 7 个市场：

| 市场前缀 | intents |
|---|---:|
| `0x56ec5ceb` | 366 |
| `0x1764e812` | 258 |
| `0x64264cc8` | 127 |
| `0xe8ccfb67` | 107 |
| `0xe0e57301` | 96 |
| `0x3b09ed9e` | 36 |
| `0x3dada008` | 7 |

- 腿部：Up 495，Down 502。
- 尺寸：全部 20 股。
- 价格范围：0.33 到 0.60。
- 每轮承诺：20/25 pUSD，平均 20，最高 20。

拒绝原因：

| 次数 | 原因 |
|---:|---|
| 5,125 | 需要 20 pUSD，只剩 5 pUSD 预算 |
| 2,520 | mid 0.275 在 `[0.30, 0.70]` 之外 |
| 18 | 单腿风险 12.2–13.2 pUSD，超过 12 |
| 6 | toxicity_fair_mid_gap 0.04–0.065，单腿被门控 |

## 当前 Lane A 账户账单

| 项目 | 值 |
|---|---:|
| 可用抵押品 | 1.167471 pUSD |
| 持仓数 | 17 |
| 持仓标记值 | 16.9466 pUSD |
| 权益 | 18.1141 pUSD |

17 个持仓：

| 市场前缀 | Outcome | 数量 | 当前值 | Redeemable |
|---|---|---:|---:|---|
| `0x64963f64` | Yes | 27.3276 | 11.4776 | false |
| `0x6700893b` | No | 60.0 | 2.58 | false |
| `0xe32519c9` | No | 40.0 | 1.48 | false |
| `0x2c9e5950` | Yes | 40.0 | 0.80 | false |
| `0x62223703` | No | 20.0 | 0.44 | false |
| `0x945175af` | Yes | 0.52 | 0.169 | false |
| `0xc834b283` | Yes | 60.0 | 0.0 | true |
| `0xc050b071` | No | 63.5514 | 0.0 | true |
| `0x149b4c1b` | No | 40.0 | 0.0 | true |
| `0x94fb5b47` | No | 20.0 | 0.0 | true |
| `0x63ec121c` | Yes | 20.0 | 0.0 | true |
| `0x18e8595e` | No | 60.1022 | 0.0 | true |
| `0x236661d2` | No | 61.8 | 0.0 | true |
| `0x535f6852` | No | 62.3789 | 0.0 | true |
| `0x724106a0` | Yes | 20.0 | 0.0 | true |
| `0x87aa76a0` | Yes | 20.0 | 0.0 | true |
| `0x4b637f9d` | Yes | 100.0 | 0.0 | true |

11 个 redeemable 仓位当前值都是 0。redeem 只能清理组合，不能回款。剩余 6 个非 redeemable 仓位合计约 16.95 pUSD，但其中 0x64963f64 一个就占 11.48。

可用现金 1.17 pUSD，连 20 股最小配对（约 20 pUSD）都不够。当前状态不能恢复实盘。

## 本地 btc5m 录制

本地没有正在运行的录制器。最后一轮：

| 项目 | 值 |
|---|---:|
| 路径 | `data/btc5m/20261124T0816Z` |
| 大小 | 113.8 MB |
| 窗口 | 12 |
| 结算 | 8 |
| 可报价 | 8 |
| queue 配对 | 0 |
| queue PnL | -15.60 |
| touch 配对 | 1 |
| touch PnL | -45.20 |
| gap 成交 | 3，1 胜 2 负，-15.60 |
| predictive | 全部 inconclusive，n < 30 |

本地还有两个更早的短录制：`20261124T0805Z` 12.9 MB，`20261124T0815Z` 4.6 MB。都没有在跑。

## 已完成

- Lane B 工具链：recorder、simulator、gap_sim、cancel_test、predictive、backfill、report、analyze.sh；31 测试通过。
- Lane B 证据：46 分钟、12 窗口；纯 maker 裁决不成立；只剩 Lane T。
- Lane A 守卫：255 测试通过；horizon、mid band、单腿风险、单市场敞口、库存箱、告警、merge/harvest/redeem。
- Lane A 运维：VPS 重装后指纹和 key 更新；实盘服务保持 disabled/inactive；24 小时纸面完成。
- 跨 lane 复盘：`docs/lane-ab-review.md`。

## 接下来研究

1. **先修纸面成交模型**。用 taker 成交表或 Polymarket 用户/市场成交流判断“SELL 打到买价”，再叠加队列位置。没有这一步，继续跑纸面只会得到 0 成交。
2. **在活跃市场重跑纸面**。当前 7 个市场只有 2 个进入报价，且 mid 经常在 0.275 被拒。需要按 24h 成交量、成交笔数、spread 筛选，不要只看奖励池。
3. **纸面账单要包含配对率、单腿率、每笔净捕获、最长未配对时间**。现在的 journal 只有 intent/cancel/cycle，没有 fill，无法算这些。
4. **恢复实盘的前置条件**：硬熔断、单腿 60 秒限时补腿/平仓、现金至少 25 pUSD、纸面配对率 > 70%、单腿占比 < 50%。
5. **ML 继续推迟**。0 个纸面 fill 意味着没有标签；先有 500 笔有标签成交和 quote-time 盘口特征，再谈 spread 模型。
6. **Lane B 不再调 maker**。只有拿到 Chainlink key，才开 Lane T 的 T−30s 方向实验。

## 风险

- 纸面 0 成交是模型缺陷，不是策略证据。不要用这 24 小时得出“策略安全”或“策略不赚钱”的结论。
- 当前现金 1.17 pUSD，不能恢复实盘。先处理仓位和充值，再谈重新报价。
- 11 个 redeemable 仓位值为 0，redeem 只清理视图，不产生现金。
