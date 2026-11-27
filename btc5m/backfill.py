"""Append settlements to a recording that finished before the market closed.

The live recorder polls the CLOB after each window ends. Polymarket can leave a
5m market `closed=false` for a few minutes, so a one-hour capture may finish
before every window settles. This tool reads the recorded windows, asks the
CLOB for the missing ones, and writes a complete event file for the simulator.

    python -m btc5m.backfill --events data/btc5m/<run>/events.jsonl \
      --out /tmp/events.resolved.jsonl
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path
from typing import Any

CLOB = "https://clob.polymarket.com"
UA = "btc5m-backfill/0.1"


def get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def parse_clob(row: dict[str, Any]) -> tuple[float | None, float | None, str | None] | None:
    if not bool(row.get("closed")):
        return None
    up = down = None
    outcome = None
    for token in row.get("tokens") or []:
        try:
            price = float(token.get("price"))
        except (TypeError, ValueError):
            price = None
        name = str(token.get("outcome") or "").lower()
        if name == "up":
            up = price
            if token.get("winner") is True:
                outcome = "Up"
        elif name == "down":
            down = price
            if token.get("winner") is True:
                outcome = "Down"
    if up is None and down is None:
        return None
    if outcome is None and up is not None and down is not None:
        outcome = "Up" if up > down else "Down"
    return up, down, outcome


def backfill(events: Path, out: Path, min_age: float) -> tuple[int, int]:
    rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines() if line.strip()]
    windows = {row["data"]["slug"]: row["data"] for row in rows if row.get("type") == "window"}
    resolved = {row["data"]["slug"] for row in rows if row.get("type") == "resolution"}
    added = 0
    for slug, window in windows.items():
        if slug in resolved or time.time() < float(window["end_ts"]) + min_age:
            continue
        try:
            market = get_json(f"{CLOB}/markets/{window['condition_id']}")
            parsed = parse_clob(market)
        except Exception:
            continue
        if parsed is None:
            continue
        up, down, outcome = parsed
        rows.append({
            "type": "resolution",
            "ts_ns": time.time_ns(),
            "data": {
                "ts_ns": time.time_ns(),
                "slug": slug,
                "condition_id": window["condition_id"],
                "closed": True,
                "up_price": up,
                "down_price": down,
                "outcome": outcome,
            },
        })
        added += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n", encoding="utf-8")
    return added, len(windows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill btc5m resolutions")
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--min-age", type=float, default=30.0)
    args = parser.parse_args()
    added, total = backfill(args.events, args.out, args.min_age)
    print(f"windows={total} added={added} out={args.out}")


if __name__ == "__main__":
    main()
