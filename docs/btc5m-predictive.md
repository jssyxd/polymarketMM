# OKX 前 20 档能否预测 btc5m 涨跌

日期：2026-11-24
状态：方法与样本量结论；1 小时样本不足，不给方向性结论。

## 问题

在 5 分钟 Up/Down 窗口里，OKX `BTC-USDT` 和 `BTC-USDT-SWAP` 的前 20 档挂单，
加上 Polymarket 自己的盘口，能否在窗口结束前判断最终是 Up 还是 Down？
如果能，信号是否强到值得改变报价或单独下注？

## 数据

`btc5m/recorder.py` 记录：

- Polymarket 两个 outcome token 的前 50 档，WebSocket 快照加增量，REST 兜底。
- OKX spot 和 swap 的 `books` 频道，保留完整本地簿，输出前 20 档。
- 两个场馆的逐笔成交，taker 方向。
- 每个窗口的 CLOB 结算结果。

特征只在 `ts_ns <= cutoff` 上计算。cutoff 取窗口结束前 30、60、120、180、240 秒。
`resolution` 字段永远不进特征。cutoff 之后的 book 和 trade 也不进特征。

## 特征

第一版只放 3 个进模型，控制小样本过拟合：

1. `okx_ret_since_start`：OKX spot 相对窗口开始的收益。
2. `poly_mid`：Polymarket Up token 的中间价。
3. `divergence`：`poly_mid - p_okx`，其中 `p_okx` 用 OKX 收益、60 秒波动和剩余时间算正态概率。

其余特征只做单变量 AUC，不进入模型：

- OKX 前 20 档深度失衡、microprice 偏离、1s/5s OFI、30s 成交流失衡。
- Polymarket 前 20 档深度失衡、30s 成交流失衡。
- swap 深度失衡。

## 定义

- `mid = (best_bid + best_ask) / 2`
- `microprice = (best_bid * ask_size + best_ask * bid_size) / (bid_size + ask_size)`
- 深度失衡 `= (bid_depth_20 - ask_depth_20) / (bid_depth_20 + ask_depth_20)`
- OFI 用 Cont-Kukanov-Stoikov 的最优档增量：买价上移记新增买量，买价下移记减少买量；
  卖价下移记新增卖量，卖价上移记减少卖量。1s/5s/30s 分别求和。
- 成交流失衡 `= (buy_volume - sell_volume) / (buy_volume + sell_volume)`，按 taker 方向。
- `p_okx = Phi(ln(S_t / S_0) / (sigma * sqrt(tau)))`，`sigma` 是 60 秒已实现波动，
  `tau` 是剩余秒数。`p_okx` 是 OKX 代理概率，不是 Chainlink 结算概率。
- `divergence = poly_mid - p_okx`。

## 模型与验证

- 基线：永远预测多数类。
- 单变量：每个特征对标签的 AUC。
- 多变量：固定 3 个特征的逻辑回归，标准化只在训练折上拟合。
- 验证：留一窗口外样本（leave-one-window-out），不是 in-sample。
- 指标：准确率、多数类准确率、Brier、AUC。
- 判定：
  - `n < 30`：`inconclusive`。
  - `n >= 30` 但准确率提升 < 5pp 或 AUC < 0.6：`no_edge`。
  - 准确率提升 >= 5pp 且 AUC >= 0.6：`candidate`，但仍需模拟器验证 `net_bps`。

## 1 小时为什么不够

1 小时只有 12 个 5 分钟窗口。12 个样本的 95% 置信区间宽度约 ±28pp，
连“方向对不对”都说不清。要在 50/50 基线上以 80% 把握检测 5pp 提升，
粗略需要 700–800 个窗口；检测 10pp 提升约需要 200 个窗口。
按每天 288 个窗口算，5pp 目标需要约 3 天不间断样本，实际还要覆盖趋势、震荡、高波动三种 regime。

所以 1 小时录制的正确输出是 `inconclusive`。`predictive.py` 在 `n < 30` 时强制返回这个值。
任何从 12 个窗口里读出的 AUC 都只能当调试信号，不能当交易依据。

## 决策门槛

即使样本够了，统计显著也不等于交易有利。进入报价前还要过两道门：

1. 模拟器：在同一批窗口上，加入特征后的 `net_bps` 比不加特征高，且单边损失没有变差。
2. 执行：特征推断的延迟不能吃掉 250ms 撤单窗口；如果推断超过 20ms，只用于离线选市，不用于在线撤单。

## 结果

完整结果由以下命令生成：

```bash
PYTHONPATH=. python -m btc5m.predictive \
  --events data/btc5m/<run>/events.jsonl --out data/btc5m/<run>/predictive
```

`predictive.json` 包含每个 cutoff 的 n、准确率、多数类基线、Brier、AUC 和 verdict。
1 小时样本的 verdict 应为 `inconclusive`；这不是失败，是样本量规则在起作用。

## 已知缺口

- OKX 序列号缺口没有检测。跳号时本地簿可能短暂错误；1 小时样本影响小，实盘前必须补。
- Polymarket 的 250ms taker delay 让 cutoff 附近的书可能滞后；用 OKX 时间戳对齐，仍有几毫秒不确定性。
- 结算源是 Chainlink 60s TWAP，不是 OKX 现货。`p_okx` 是代理，不是结算概率；Lane T 必须先拿 Chainlink key。
- 1 小时覆盖的 regime 有限；不要用单日样本外推。
