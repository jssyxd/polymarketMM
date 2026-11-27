# btc5m recorder, simulator, and cancel harness

Three read-mostly tools for the btc5m branch.

- `recorder.py` records Polymarket BTC 5m books and trades plus OKX top-20 books and trades.
- `simulator.py` replays a recording and reports paired-fill PnL, single-leg loss, and rebates.
- `cancel_test.py` measures cancel latency and natural cancel-vs-fill races. Dry-run is the default.
- `predictive.py` tests whether OKX and Polymarket book features predict the 5m outcome.

Run from this directory with the project venv:

```bash
PYTHONPATH=. /home/da/桌面/polymarketMM/polymarket-mm/.venv/bin/python -m btc5m.recorder --duration 3600
PYTHONPATH=. /home/da/桌面/polymarketMM/polymarket-mm/.venv/bin/python -m btc5m.simulator --events data/btc5m/<run>/events.jsonl --out /tmp/btc5m-sim
PYTHONPATH=. /home/da/桌面/polymarketMM/polymarket-mm/.venv/bin/python -m btc5m.cancel_test --dry-run
PYTHONPATH=. /home/da/桌面/polymarketMM/polymarket-mm/.venv/bin/python -m btc5m.predictive --events data/btc5m/<run>/events.jsonl --out /tmp/btc5m-pred
```

The recorder writes one `events.jsonl`, `run.json`, and no secrets. `--live` in the cancel harness
requires `POLY_OWNER_PK`, `POLY_API_KEY`, `POLY_API_SECRET`, and `POLY_API_PASSPHRASE`.
The simulator's `touch` and `queue` rows are an upper and lower bound, not a forecast.
`predictive.py` returns `inconclusive` below 30 resolved windows by design.

Data contract: `docs/btc5m-data-contract.md`.
