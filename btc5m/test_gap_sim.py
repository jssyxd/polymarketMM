import json
from pathlib import Path

from btc5m.gap_sim import simulate_window
from btc5m.simulator import load_recording


def _events(tmp_path, trade=True, resolution=True):
    rows = [
        {"type": "window", "ts_ns": 10**9, "data": {"slug": "w", "condition_id": "0x", "question": "", "start_ts": 1.0, "end_ts": 301.0, "up_token": "up", "down_token": "down", "tick_size": 0.01, "min_order_size": 5.0, "fee_rate": 0.05, "neg_risk": False}},
        {"type": "poly_book", "ts_ns": 20 * 10**9, "data": {"slug": "w", "token_id": "up", "bids": [[0.45, 10.0]], "asks": [[0.51, 10.0]], "last_trade_price": None}},
        {"type": "poly_book", "ts_ns": 20 * 10**9, "data": {"slug": "w", "token_id": "down", "bids": [[0.45, 10.0]], "asks": [[0.51, 10.0]], "last_trade_price": None}},
    ]
    if trade:
        rows.append({"type": "trade", "ts_ns": 21 * 10**9, "data": {"ts_ns": 21 * 10**9, "received_ts_ns": 21 * 10**9, "venue": "polymarket", "symbol": "w", "price": 0.47, "size": 20.0, "side": "SELL", "trade_id": "t1", "slug": "w", "token_id": "up"}})
    if resolution:
        rows.append({"type": "resolution", "ts_ns": 302 * 10**9, "data": {"ts_ns": 302 * 10**9, "slug": "w", "condition_id": "0x", "closed": True, "up_price": 1.0, "down_price": 0.0, "outcome": "Up"}})
    path = Path(tmp_path) / "events.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_gap_fill_is_positive_when_first_in_queue(tmp_path):
    path = _events(tmp_path)
    windows, books, trades, resolutions, _ = load_recording(path)
    rows = simulate_window(windows["w"], books["w"], trades["w"], resolutions["w"], 20.0, 5, 10.0)
    filled = [r for r in rows if r.filled_ts is not None]
    assert len(filled) == 1
    assert filled[0].side == "up"
    assert abs(filled[0].pnl - 10.6) < 1e-9
    assert len(rows) == 2


def test_no_trade_means_no_fill(tmp_path):
    path = _events(tmp_path, trade=False)
    windows, books, trades, resolutions, _ = load_recording(path)
    rows = simulate_window(windows["w"], books["w"], trades["w"], resolutions["w"], 20.0, 5, 10.0)
    assert len(rows) == 2
    assert all(r.filled_ts is None for r in rows)


def test_missing_resolution_skips_window(tmp_path):
    path = _events(tmp_path, resolution=False)
    windows, books, trades, resolutions, _ = load_recording(path)
    rows = simulate_window(windows["w"], books["w"], trades["w"], resolutions.get("w"), 20.0, 5, 10.0)
    assert rows == []
