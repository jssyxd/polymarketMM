# btc5m recorder, simulator, and cancel-test contract

Date: 2026-11-24
Scope: BTC 5m Up/Down only. No authenticated order flow in the recorder or simulator.

## Files

`btc5m/recorder.py` writes one append-only `events.jsonl` under
`data/btc5m/<run_id>/`, plus `run.json`. The recorder never places orders.
`btc5m/simulator.py` reads the JSONL and writes `sim.json` and `sim.csv`.
`btc5m/cancel_test.py` measures cancel latency and cancel-vs-fill races. Its
default mode is dry-run. Live mode needs `--live` and the environment variables
from `polymarket-mm`.

## Event envelope

Every line is one JSON object:

```json
{"type": "poly_book", "ts_ns": 1795507500123456789, "data": {}}
```

`ts_ns` is the local wall clock at receipt. The recorder also stores the venue
timestamp inside the payload where the venue provides one.

## Event types

`window`

```json
{"slug": "btc-updown-5m-1795507500", "condition_id": "0x...", "question": "...",
 "start_ts": 1795507500.0, "end_ts": 1795507800.0,
 "up_token": "...", "down_token": "...", "tick_size": 0.01,
 "min_order_size": 5.0, "fee_rate": 0.05, "neg_risk": false}
```

`poly_book`

```json
{"slug": "...", "token_id": "...",
 "bids": [[0.48, 120.0], [0.47, 300.0]],
 "asks": [[0.49, 90.0], [0.50, 200.0]],
 "last_trade_price": 0.48}
```

Levels are `[price, size]`, best first. At most 50 levels per side. Snapshots
are emitted at most every 100 ms per token.

`okx_book`

```json
{"inst_id": "BTC-USDT",
 "bids": [[60414.7, 0.342], [60413.7, 0.008]],
 "asks": [[60415.0, 0.21], [60416.0, 1.4]],
 "seq_id": 84880411098}
```

The recorder subscribes to OKX `books` for `BTC-USDT` and `BTC-USDT-SWAP`,
keeps the full local book, and emits the top 20 levels at most every 100 ms.
The payload is already the top N, best first.

`trade`

```json
{"ts_ns": 1795507500000000000, "received_ts_ns": 1795507500123456789,
 "venue": "polymarket", "symbol": "btc-updown-5m-1795507500",
 "price": 0.48, "size": 120.0, "side": "SELL",
 "trade_id": "0x...|token|1795507500|0.480000|120.000000|SELL",
 "slug": "btc-updown-5m-1795507500", "token_id": "..."}
```

`side` is the taker side as reported by the venue. For Polymarket, `SELL`
means the taker sold the outcome token into a bid. For OKX, `buy` and `sell`
are the taker side.

`resolution`

```json
{"slug": "...", "condition_id": "0x...", "closed": true,
 "up_price": 1.0, "down_price": 0.0, "outcome": "Up"}
```

The recorder writes a resolution after the CLOB market reports `closed: true`,
using the token `winner` flags or the terminal token prices. It falls back to
Gamma only when the CLOB read fails. Gamma's `outcomePrices` on an open market
are live prices, not settlement prices, so the recorder ignores them until close.

## Simulator rules

The simulator is deliberately conservative about what it can claim.

- Quote only inside `mid ∈ [0.15, 0.85]`.
- Place both bids at `p - δ` and `(1 - p) - δ` at the first book at or after
  `start + warmup`, where `p` is the midpoint of the size-cutoff-adjusted book.
- Cancel both at `end - 45s`. A fill after that is ignored.
- A buy fills only when a trade with taker side `SELL` prints at or below the
  bid price. A `BUY` print cannot fill a resting bid.
- Two fill models are reported separately:
  - `touch`: first qualifying print fills the whole quote. Upper bound.
  - `queue`: subtract every visible bid at or above our price at quote time
    from cumulative qualifying volume; fill only after that size is exhausted.
    Lower bound.
- If one leg fills and the other does not, value the filled leg at the
  resolution payout (`1` for the winning outcome, `0` for the losing one).
  A window without a resolution is excluded from the aggregate and counted.
- `net_bps = net_pnl / filled_notional * 10_000`.
- Maker weighted volume per fill is `shares * p * (1 - p)`. Fee-equivalent is
  `shares * fee_rate * p * (1 - p)`. Report both, plus the rebate at tiers
  1, 2, 3, and 4 (3%, 7%, 10%, 15%) as separate columns. Rebate is not added
  to `net_pnl`; it is reported beside it.

## Cancel test rules

- Default `--dry-run` sends no order.
- `--live` requires `POLY_OWNER_PK`, `POLY_API_KEY`, `POLY_API_SECRET`, and
  `POLY_API_PASSPHRASE`; the script refuses if any is missing.
- Use 5-share minimum orders. Place only post-only bids at least 3 cents behind
  the touch unless `--near-touch` is explicit.
- Never place a marketable order from the same account. A forced 250 ms race
  needs a second funded account and is out of scope.
- The harness records `place_ack_ms`, `cancel_ack_ms`, `cancel_request_ms`,
  fill events, and `cancel_win` when the cancel acknowledgement arrives before
  any fill. Natural races are the sample; synthetic self-trades are forbidden.
- The first run measures latency and order lifecycle only. It does not claim a
  win rate without observed races.

## Out of scope for this pass

- Real order placement in the recorder or simulator.
- Chainlink TWAP ingestion. The recorder stores OKX spot and swap books only.
- Any claim that OKX top-20 book imbalance predicts the 5-minute outcome
  before the predictive report is produced from the recorded sample.
