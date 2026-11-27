# btc5m 上游项目复核与五个问题

日期：2026-11-24
范围：GLFTmm、asgrid-mm、polymarket-mm 对 btc5m 的可复用性。
证据：本地代码路径加行号，或 `.research/` 中的实测记录。推断会标 `INFERENCE`。

## 结论

btc5m 不需要把 GLFT 或 A-S 的定价公式搬过来。两个仓库值得拿的是执行纪律、风控闸门形状和记账，
不是数学模型。OKX 前 20 档可以作为特征，但当前 1 小时录制只有 12 个窗口，**不足以回答预测力问题**。
在线 ML 现在没有证据支持；先用确定性闸门和离线逻辑回归。

## 1. 映射：拿什么，扔什么

### GLFTmm

`StationaryGLFTSolver` 在 `02_rwa_tokenized_equity_mm/core/hyperliquid_sndk_live.py:91-124`。
它把半价差写成 `max(min_spread/2, fee_maker*2)`，库存项按 `inventory * gamma * sigma^2 * price` 缩放。
`.research/glft-dive.md` 的实测结论是：在真实波动率区间内，半价差被常数地板钉死，库存项量级约 `1e-9`，
所以它在永续上也没有真正按库存报价。

btc5m 的 payoff 是 0/1，tick 是 1c。0.50 附近 1c 等于 200 bps 的总价差，任何以 bps 为单位的
“手续费地板”都没有意义。把 GLFT 解搬过来只会得到一个与 1c tick 不匹配的报价。

| 组件 | 动作 | 原因 | 证据 |
|---|---|---|---|
| `StationaryGLFTSolver` 定价 | drop | 常数地板，库存项无效；二元 payoff 不适用 | `hyperliquid_sndk_live.py:91-124`，`.research/glft-dive.md` |
| `VPINCalculator` | reuse shape | 分桶毒性指标可以离线算，不是定价核心 | `01_crypto_altcoin_perp_mm/models/vpin_gate.py:1-80` |
| `DefensiveCooldownManager` | reuse | 单边成交后冻结同向开仓，直接适用于单腿风险 | 同文件 `:80-110` |
| 毒性预测调用 | rewrite | 模型路径 `/opt/...`，特征与 btc5m 不同 | `hyperliquid_sndk_live.py:302-320, 376-380` |
| post-only + 队列保持 | reuse | 执行纪律，不依赖市场类型 | `:399` 附近的改单逻辑，`.research/glft-dive.md` |

### asgrid-mm

生产路径是 `live_cluster/run_live.py`。它导入 `NautilusAvellanedaV2MMStrategy`
（`run_live.py:31`），不是 `src/as_ml_engine.py`。`as_ml_engine.py` 里的 “ML” 是手写线性和，
`run_live.py` 也没有加载 LightGBM。`cron_accountbot3_ml_guard.py:111-120` 只检查 checkpoint 文件是否存在，
不是在线推理。

`as_nautilus_strategy.py:48-150` 的核心是：`ofi_norm = clip(rolling_ofi/5, -3, 3)`，
`mu_pred = 0.35*ofi_norm - 0.20*ma20_dev`，半价差下限 `min_spread_bps = 9.0`。
在 btc5m 上 9 bps 相当于 0.0009，而最小报价步长是 0.01，所以这个下限永远不会绑定；
真正绑定的是 1c tick。它的库存偏置和 OFI 漂移仍可借鉴，但不能直接当二元期权定价器。

| 组件 | 动作 | 原因 | 证据 |
|---|---|---|---|
| `NautilusAvellanedaV2MMStrategy.compute_quotes` | rewrite | bps 价差下限与 1c tick 不匹配；连续价格假设不成立 | `as_nautilus_strategy.py:48-150` |
| OFI + microprice 特征 | reuse | 特征本身可迁移 | `okx_ws_manager.py:114-162` |
| 队列保持与改单容忍 | reuse | 直接降低撤单频率 | `run_live.py:239-260` |
| 单边冷却 | reuse | 与 GLFTmm 的 cooldown 同形 | `run_live.py:200-210` |
| 回测引擎 | drop | 无排队、无资金约束，且手续费重复扣 | `.research/asgrid-dive.md` |
| `as_ml_engine.py` | drop | 生产路径不导入；线性启发式不是 ML | `run_live.py:31`，`.research/asgrid-dive.md` |

### polymarket-mm

| 组件 | 动作 | 原因 |
|---|---|---|
| `data/clob.py` | reuse | 只读 HTTP、超时、重试、错误类型都完整 |
| `execution/clob.py` | reuse | post-only、心跳、签名、token bucket 已经过实盘验证 |
| `execution/positions.py` | reuse | 实盘库存必须从 venue 读，不能从 journal 推 |
| `execution/merge.py` | reuse | 配对后 merge 回 pUSD |
| `risk/gates.py` | reuse shape | 库存箱、冷却、到期、硬停的闸门协议可直接套 |
| `pricing/quotes.py` | rewrite | 奖励带语义与 btc5m 无关；btc5m 当前无奖励 |
| `core/selection.py` | drop | 按奖励池选市，btc5m 没有奖励池 |

结论：新代码只保留 polymarket-mm 的读取、执行、持仓、merge 和闸门形状。定价与选市重写。

## 2. OKX websocket：现有实现有什么，缺什么

`asgrid-mm/live_cluster/okx_ws_manager.py`：

- 只订阅 `books5` 和 `trades`，标的默认 `BTC-USDT-SWAP`（`:86-104`）。
- 只保留最优档：`b1/v_b1/a1/v_a1`（`:114-124`）。
- OFI 用 Cont-Kukanov-Stoikov 的 top1 公式（`:126-150`）。
- `_handle_trades` 把逐笔成交流量追加到 **同一个** `recent_ofi_window`（`:164-171`），
  `get_rolling_ofi` 再把两者相加（`:259-263`）。这是两个不同量纲：book OFI 是挂单变化，
  trade flow 是成交方向。混在一起后，`ofi_norm` 无法解释。`INFERENCE`：这是实盘信号噪音的一个来源。
- 使用私有 WS 和 OKX 密钥，btc5m 录制只需要公共行情。

btc5m 录制器已经改成：

- OKX `books` 频道，`BTC-USDT` 和 `BTC-USDT-SWAP` 各一份，保留完整本地簿，输出前 20 档。
- `prevSeqId == -1` 视为快照，之后按价格增删改；`size == 0` 删除。
- 单独的 `trade` 事件，`side` 是 taker 方向；book OFI 和 trade flow 分开计算。
- Polymarket 用 `wss://ws-subscriptions-clob.polymarket.com/ws/market`，按 `book` 快照加
  `price_change` 增量维护，前 50 档、每 token 最多 100ms 一次快照。

还需要补的：OKX 序列号缺口检测。当前实现只应用增量，不检测 `seqId` 跳号。跳号时应丢弃本地簿并等下一次快照。
这是 `btc5m/recorder.py` 的已知缺口，不影响 1 小时样本，但不能带进实盘。

## 3. OKX 前 20 档能否预测 5 分钟涨跌

先给结论：**1 小时数据回答不了。** 1 小时只有 12 个 5 分钟窗口。要在 50/50 基准上稳定看出
5 个百分点的准确率提升，至少需要数百个窗口；要估计 AUC 的置信区间，样本量还要更大。
`INFERENCE`：按二项分布，n=100 时 5pp 提升的标准误约 5pp，n=400 时约 2.5pp。

可执行的检验设计：

- 标签：窗口结算 `Up=1`，`Down=0`。
- 特征时点：窗口开始后 30s、60s、120s、180s、240s，以及结束前 30s。每个时点只用
  `ts_ns <= cutoff` 的数据。
- 特征：
  - OKX spot/swap：mid、spread、前 20 档深度、深度失衡、microprice 偏离、1s/5s/30s OFI、
    30s 成交流失衡、60s 已实现波动、相对窗口开始的收益。
  - Polymarket：mid、spread、前 20 档深度、深度失衡、30s 成交流失衡。
  - 交叉：`poly_mid - p_okx`，其中 `p_okx` 用 OKX 收益、60s 波动和剩余时间算正态概率。
- 防泄漏：resolution 字段、cutoff 之后的 book、cutoff 之后的 trade 都不能进特征。
  Polymarket 的 250ms taker delay 意味着 cutoff 附近的书可能还没反映最新成交；用 OKX 时间戳做对齐。
- 模型：多数类基线；单变量 AUC；3 个固定特征的逻辑回归；留一窗口外样本。
- 判定：n < 300 时一律 `inconclusive`；n ≥ 300 且 OOS 准确率比多数类高 ≥ 5pp、
  AUC ≥ 0.6、Brier 不劣于基线，才进入“候选特征”。否则不参与报价。
- 决策门槛：即使统计显著，也要把特征接到模拟器，比较 `net_bps` 是否提升；统计显著不等于交易有利。

当前项目已经写了 `btc5m/predictive.py`，实现上述特征、留一逻辑回归和 AUC。1 小时录制结束后会输出
`predictive.json`，但按样本量规则应显示 `inconclusive`。这是正确结果，不是失败。

## 4. Polymarket 和 OKX 盘口怎么组合

### 有信息的部分

- OKX 相对窗口开始的收益：直接对应二元期权的标的变量。
- OKX 前 20 档深度失衡和 microprice：短周期方向压力。
- OKX 与 Polymarket 的背离：`poly_mid - p_okx`。这是最像“错价”的量。
- Polymarket 自身深度失衡：能反映谁在提供/吃掉流动性，但受 1c tick 和 250ms delay 限制。

### 冗余或危险的部分

- OKX spot 和 swap 的 mid/收益高度相关。第一版只留一个，另一个只留 basis 或深度失衡。
- Polymarket 的 mid 在临近结算时会迅速接近 0/1；在 `end - 5s` 用 mid 预测结果等于泄漏。
  必须按 cutoff 截断，并在报告里写清楚。
- Polymarket 的 `last_trade_price` 可能是延迟的；不要当实时公允价。
- resolution outcome 永远不能进特征。
- 用未来的 book 快照（ts > cutoff）是隐蔽泄漏；`predictive.py` 的 `at()` 只取 `<= cutoff`。

### 第一版特征集

只放 3 个，控制小样本过拟合：

1. `okx_ret_since_start`
2. `poly_mid`
3. `divergence = poly_mid - p_okx`

把 `okx_depth_imbalance`、`okx_ofi_5s`、`okx_flow_30s` 作为单变量 AUC 报告，不进入模型。
等窗口数到 300 以上再加。

## 5. 是否需要动态训练或在线 ML

分三层看：

| 层 | 是否需要 | 建议 | 失败模式 |
|---|---|---|---|
| 离线研究 | 需要 | 逻辑回归/GBM 都可以，留一或 walk-forward，目标是判断特征有没有信息 | 小样本过拟合；把 1 小时当结论 |
| 在线毒性闸门 | 暂不需要 | 先用确定性阈值：OKX 1s 跳变 + 断流 + 撤单延迟；记录特征供以后训练 | 模型推断增加 1-5ms 延迟；分布漂移后静默失效 |
| 在线报价偏斜 | 暂不需要 | 用库存箱、配对价差、时间止损；不要让 ML 改报价 | 模型错误直接变成单边库存 |

理由：

- btc5m 的报价粒度是 1c，模型输出的微小概率修正会被 tick 舍入吃掉。
- 250ms taker delay 下，撤单竞速比预测方向更值钱；先把执行做对。
- `asgrid` 的 LightGBM 训练产物样本外为负（`.research/asgrid-dive.md`），不能作为“ML 有效”的证据。
- 当前 1 小时样本只有 12 个窗口，任何在线训练都会过拟合。

建议：P1/P3 阶段只记录特征，不训练。等累计 4 周、至少 2,000 个窗口，再跑一次离线逻辑回归；
只有 OOS 准确率提升 ≥ 5pp 且模拟 net_bps 提升，才考虑把毒性闸门换成模型。报价偏斜继续用规则。

## 决策表

| 组件 | 动作 | 原因 | 证据 |
|---|---|---|---|
| GLFT 求解器 | drop | 常数地板、库存项无效、二元 payoff 不匹配 | `hyperliquid_sndk_live.py:91-124` |
| A-S 报价公式 | drop | bps 下限与 1c tick 不匹配 | `as_nautilus_strategy.py:48-150` |
| VPIN/冷却 | reuse shape | 单边风险控制 | `vpin_gate.py:1-110` |
| OKX top1 OFI | rewrite | 需要 top20、spot+swap、book/trade 分离 | `okx_ws_manager.py:86-171` |
| 队列保持/改单 | reuse | 降撤单频率 | `run_live.py:239-260` |
| post-only/心跳/签名 | reuse | 已实盘验证 | `polymarket-mm/src/pmm/execution/clob.py` |
| 持仓读取/merge | reuse | 实盘库存必须从 venue 读 | `execution/positions.py`, `execution/merge.py` |
| 奖励带定价 | drop | btc5m 当前无奖励 | `.research/poly-platform.md` §8.1 |
| 在线 ML | defer | 样本不足，延迟和漂移成本高 | 本文 §5 |
