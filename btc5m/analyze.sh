#!/usr/bin/env bash
set -euo pipefail
run_dir="${1:?usage: btc5m/analyze.sh <run_dir>}"
py="${PYTHON:-/home/da/桌面/polymarketMM/polymarket-mm/.venv/bin/python}"
export PYTHONPATH="${PYTHONPATH:-.}"
"$py" -m btc5m.backfill --events "$run_dir/events.jsonl" --out "$run_dir/events.resolved.jsonl"
"$py" -m btc5m.simulator --events "$run_dir/events.resolved.jsonl" --out "$run_dir/sim"
"$py" -m btc5m.predictive --events "$run_dir/events.resolved.jsonl" --out "$run_dir/predictive"
"$py" -m btc5m.gap_sim --events "$run_dir/events.resolved.jsonl" --out "$run_dir/gap" --hold 60
"$py" -m btc5m.cancel_test --replay "$run_dir/events.resolved.jsonl" --out "$run_dir/cancel"
"$py" -m btc5m.report --run-dir "$run_dir" --out "docs/btc5m-capture-$(basename "$run_dir").md"
