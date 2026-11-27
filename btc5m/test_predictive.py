import json
import math
from pathlib import Path

from btc5m.predictive import WindowData, features, load, loo, run


def test_loo_separates_a_known_signal():
    rows = []
    for i in range(40):
        label = i % 2
        rows.append({
            "okx_ret_since_start": (1 if label else -1) + 0.05 * math.sin(i),
            "poly_mid": 0.5 + 0.1 * (1 if label else -1),
            "divergence": 0.1 * (1 if label else -1),
            "okx_depth_imbalance": 0.0,
            "okx_ofi_5s": 0.0,
            "okx_flow_30s": 0.0,
            "poly_flow_30s": 0.0,
            "swap_depth_imbalance": 0.0,
            "label": float(label),
        })
    result = loo(rows, ["okx_ret_since_start", "poly_mid", "divergence"])
    assert result["n"] == 40
    assert result["accuracy"] > 0.8


def test_missing_resolution_returns_none():
    w = WindowData("w", 1.0, 301.0, "up", "down", outcome=None, poly_books={}, okx_books={}, trades=[])
    assert features(w, 271.0) is None


def test_features_use_only_data_before_cutoff():
    w = WindowData(
        "w", 1.0, 301.0, "up", "down", outcome="Up",
        poly_books={"up": [(6 * 10**9, [(0.48, 10.0)], [(0.52, 10.0)]), (200 * 10**9, [(0.90, 10.0)], [(0.92, 10.0)])]},
        okx_books={"BTC-USDT": [(6 * 10**9, [(100.0, 1.0)], [(101.0, 1.0)])], "BTC-USDT-SWAP": [(6 * 10**9, [(100.0, 1.0)], [(101.0, 1.0)])]},
        trades=[],
    )
    row = features(w, 100.0)
    assert row is not None
    assert abs(row["poly_mid"] - 0.50) < 1e-9


def test_load_reads_window_and_resolution(tmp_path):
    path = Path(tmp_path) / "events.jsonl"
    rows = [
        {"type": "window", "ts_ns": 1, "data": {"slug": "w", "start_ts": 1.0, "end_ts": 301.0, "up_token": "up", "down_token": "down"}},
        {"type": "resolution", "ts_ns": 2, "data": {"slug": "w", "outcome": "Up"}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    windows, bad = load(path)
    assert bad == 0
    assert windows["w"].outcome == "Up"


def test_small_sample_is_inconclusive():
    windows = {
        "w1": WindowData("w1", 1.0, 301.0, "up", "down", outcome="Up", poly_books={}, okx_books={}, trades=[]),
    }
    result = run(windows, [30.0])
    assert result["cutoff_30s"]["verdict"] == "inconclusive"
