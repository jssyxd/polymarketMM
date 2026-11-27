import json
from pathlib import Path

from btc5m.report import cancel_block, coverage, predictive_table, sim_table


def test_coverage_counts_windows_and_books(tmp_path):
    path = Path(tmp_path) / "events.jsonl"
    rows = [
        {"type": "window", "ts_ns": 1, "data": {"slug": "w", "start_ts": 1.0, "end_ts": 301.0, "up_token": "up", "down_token": "down"}},
        {"type": "poly_book", "ts_ns": 2, "data": {"slug": "w", "token_id": "up", "bids": [[0.5, 1.0]], "asks": [[0.51, 1.0]]}},
        {"type": "resolution", "ts_ns": 3, "data": {"slug": "w", "outcome": "Up"}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    cov = coverage(path)
    assert cov["counts"]["window"] == 1
    assert cov["resolutions"] == 1
    assert cov["windows"][0]["books"] == 1


def test_tables_label_empty_outputs():
    assert sim_table(None) == "No simulator output."
    assert predictive_table(None) == "No predictive output."
    assert cancel_block(None) == "No cancel output."
