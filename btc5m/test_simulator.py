import json
from pathlib import Path

from btc5m.simulator import floor_tick, load_recording, simulate


def event(kind, ts_ns, data):
    return {"type": kind, "ts_ns": ts_ns, "data": data}


def write(tmp_path, rows):
    path = Path(tmp_path) / "events.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def window(slug="w", outcome="Up", start=1_000, end=1_300):
    return event("window", start * 10**9, {
        "slug": slug, "condition_id": "0xabc", "question": "BTC",
        "start_ts": start, "end_ts": end,
        "up_token": "up", "down_token": "down",
        "tick_size": 0.01, "min_order_size": 5.0, "fee_rate": 0.05, "neg_risk": False,
    })


def book(slug, token, ts, bids, asks):
    return event("poly_book", ts, {"slug": slug, "token_id": token, "bids": bids, "asks": asks, "last_trade_price": None})


def trade(slug, token, ts, price, size, side="SELL"):
    return event("trade", ts, {
        "ts_ns": ts, "received_ts_ns": ts, "venue": "polymarket", "symbol": slug,
        "price": price, "size": size, "side": side, "trade_id": f"{slug}-{token}-{ts}",
        "slug": slug, "token_id": token,
    })


def resolution(slug="w", outcome="Up"):
    return event("resolution", 1_400 * 10**9, {
        "ts_ns": 1_400 * 10**9, "slug": slug, "condition_id": "0xabc", "closed": True,
        "up_price": 1.0 if outcome == "Up" else 0.0,
        "down_price": 0.0 if outcome == "Up" else 1.0,
        "outcome": outcome,
    })


def base_rows(with_down_trade=True):
    rows = [
        window(),
        book("w", "up", 1_004 * 10**9, [[0.49, 5.0]], [[0.51, 5.0]]),
        book("w", "down", 1_004 * 10**9, [[0.49, 5.0]], [[0.51, 5.0]]),
    ]
    rows.append(trade("w", "up", 1_010 * 10**9, 0.49, 20.0))
    if with_down_trade:
        rows.append(trade("w", "down", 1_011 * 10**9, 0.49, 20.0))
    rows.append(resolution())
    return rows


def run(tmp_path, rows, delta=0.01, model="touch"):
    path = write(tmp_path, rows)
    windows, books, trades, res, bad = load_recording(path)
    out, agg = simulate(windows, books, trades, res, [delta], 20.0, 5.0, 45.0, 0.15, 0.85)
    return path, out, agg, bad


def test_floor_tick():
    assert floor_tick(0.499, 0.01) == 0.49
    assert floor_tick(0.5, 0.01) == 0.5


def test_paired_fill_is_positive_and_outcome_independent(tmp_path):
    _, out, agg, _ = run(tmp_path, base_rows())
    assert out[0]["paired"] is True
    assert abs(out[0]["pnl"] - 0.4) < 1e-9
    assert agg["delta_0.0100_touch"]["paired"] == 1


def test_single_leg_loss_uses_resolution(tmp_path):
    rows = base_rows(with_down_trade=False)
    rows[-1] = resolution(outcome="Down")
    _, out, _, _ = run(tmp_path, rows)
    assert out[0]["reason"] == "single_up_loss"
    assert abs(out[0]["pnl"] + 9.8) < 1e-9


def test_single_leg_win_uses_resolution(tmp_path):
    rows = base_rows(with_down_trade=False)
    _, out, _, _ = run(tmp_path, rows)
    assert out[0]["reason"] == "single_up_win"
    assert abs(out[0]["pnl"] - 10.2) < 1e-9


def test_queue_model_counts_better_bids_ahead(tmp_path):
    rows = base_rows()
    rows[1]["data"]["bids"] = [[0.50, 100.0], [0.49, 100.0]]
    rows[2]["data"]["bids"] = [[0.50, 100.0], [0.49, 100.0]]
    _, out, _, _ = run(tmp_path, rows, model="queue")
    queue = [row for row in out if row["model"] == "queue"][0]
    assert queue["reason"] == "no_fill"


def test_mid_outside_band_skips_quote(tmp_path):
    rows = base_rows()
    rows[1]["data"]["bids"] = [[0.89, 5.0]]
    rows[1]["data"]["asks"] = [[0.91, 5.0]]
    rows[2]["data"]["bids"] = [[0.09, 5.0]]
    rows[2]["data"]["asks"] = [[0.11, 5.0]]
    _, out, agg, _ = run(tmp_path, rows)
    assert out == []
    assert agg == {}


def test_missing_resolution_is_unresolved(tmp_path):
    rows = base_rows()[:-1]
    _, out, agg, _ = run(tmp_path, rows)
    assert out[0]["reason"] == "unresolved"
    assert agg["delta_0.0100_touch"]["resolved"] == 0


def test_malformed_line_is_counted(tmp_path):
    rows = base_rows()
    path = write(tmp_path, rows)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
    _, _, _, _, bad = load_recording(path)
    assert bad == 1
