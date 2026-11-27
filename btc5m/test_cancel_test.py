import json
from pathlib import Path

from btc5m.cancel_test import cancel_win, missing_env, percentile, replay


def test_missing_env_reports_all():
    assert missing_env({}) == ["POLY_OWNER_PK", "POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE"]


def test_percentile():
    assert percentile([], 50) is None
    assert percentile([10], 99) == 10
    assert percentile([0, 100], 50) == 50
    assert percentile([0, 100], 99) == 99


def test_cancel_win_rule():
    assert cancel_win(100, None) is None
    assert cancel_win(100, 150) is True
    assert cancel_win(200, 150) is False
    assert cancel_win(None, 150) is False


def _event(kind, ts_ns, data):
    return {"type": kind, "ts_ns": ts_ns, "data": data}


def test_replay_counts_a_win_and_a_loss(tmp_path):
    rows = [
        _event("window", 10**9, {"slug": "w", "condition_id": "0x", "start_ts": 1.0, "end_ts": 301.0, "up_token": "up", "down_token": "down", "tick_size": 0.01}),
        _event("poly_book", 6 * 10**9, {"slug": "w", "token_id": "up", "bids": [[0.49, 10.0]], "asks": [[0.51, 10.0]]}),
        _event("trade", 7 * 10**9, {"venue": "polymarket", "slug": "w", "token_id": "up", "price": 0.49, "size": 5.0, "side": "SELL"}),
    ]
    path = Path(tmp_path) / "events.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    args = type("Args", (), {"warmup": 5.0, "price_offset": 0.0, "hold_ms": 1500.0, "assumed_ack_ms": 120.0, "size": 5.0})()
    out = replay(path, args)
    assert len(out) == 1
    assert out[0].first_fill_ms is not None
    assert out[0].cancel_win is False
