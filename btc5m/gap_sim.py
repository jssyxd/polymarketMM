"""Test the last maker path: quote into a transient empty price level.

The queue model shows a new order at a populated level rarely fills. This
simulator looks for a price within 1-5 cents of mid where no bid exists at or
above it, places a 20-share bid there, and waits for a taker SELL. It is still
optimistic: it assumes no better bid arrives while the order rests.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .simulator import book_at, floor_tick, load_recording, mid_of, visible_ahead


@dataclass
class GapResult:
    slug: str
    side: str
    offset: float
    bid: float
    placed_ts: float
    filled_ts: float | None
    outcome: str | None
    pnl: float
    reason: str


def simulate_window(win, books, trades, resolution, size: float, max_offset: int, hold: float) -> list[GapResult]:
    if resolution is None or resolution.outcome not in ("Up", "Down"):
        return []
    up_books = [b for b in books if b.token_id == win.up_token]
    down_books = [b for b in books if b.token_id == win.down_token]
    rows: list[GapResult] = []
    start = int(win.start_ts * 1e9) + 10 * 10**9
    end = int(win.end_ts * 1e9) - 45 * 10**9
    for side, token, token_books in (("up", win.up_token, up_books), ("down", win.down_token, down_books)):
        placed = False
        ts = start
        while ts < end and not placed:
            book = book_at(token_books, ts)
            if book is not None:
                mid = mid_of(book)
                if 0.15 <= mid <= 0.85:
                    for cents in range(1, max_offset + 1):
                        bid = floor_tick(mid - cents / 100.0, win.tick_size)
                        if bid < win.tick_size:
                            continue
                        if visible_ahead(book, bid) != 0:
                            continue
                        deadline = min(ts + int(hold * 1e9), end)
                        fill = None
                        for trade in trades:
                            if trade.ts_ns < ts or trade.ts_ns > deadline:
                                continue
                            if trade.token_id != token or trade.side.upper() != "SELL":
                                continue
                            if trade.price <= bid + 1e-9:
                                fill = trade
                                break
                        if fill is not None:
                            won = (side == "up" and resolution.outcome == "Up") or (side == "down" and resolution.outcome == "Down")
                            pnl = size * ((1.0 if won else 0.0) - bid)
                            rows.append(GapResult(win.slug, side, cents / 100.0, bid, ts / 1e9, fill.ts_ns / 1e9, resolution.outcome, pnl, f"single_{side}_{'win' if won else 'loss'}"))
                        else:
                            rows.append(GapResult(win.slug, side, cents / 100.0, bid, ts / 1e9, None, resolution.outcome, 0.0, "no_fill"))
                        placed = True
                        break
            ts += 1 * 10**9
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate first-in-queue gap quotes")
    parser.add_argument("--events", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--size", type=float, default=20.0)
    parser.add_argument("--max-offset", type=int, default=5)
    parser.add_argument("--hold", type=float, default=10.0)
    args = parser.parse_args()
    windows, books, trades, resolutions, bad = load_recording(Path(args.events))
    rows: list[GapResult] = []
    for slug, win in sorted(windows.items()):
        rows.extend(simulate_window(win, books.get(slug, []), trades.get(slug, []), resolutions.get(slug), args.size, args.max_offset, args.hold))
    filled = [r for r in rows if r.filled_ts is not None]
    wins = [r for r in filled if r.pnl > 0]
    pnl = sum(r.pnl for r in filled)
    notional = sum(args.size * r.bid for r in filled)
    out = {
        "windows": len(windows),
        "quotes": len(rows),
        "fills": len(filled),
        "wins": len(wins),
        "win_rate": len(wins) / len(filled) if filled else None,
        "pnl": pnl,
        "notional": notional,
        "net_bps": pnl / notional * 10_000 if notional else 0.0,
        "rows": [r.__dict__ for r in rows],
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "gap.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    with (out_dir / "gap.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(GapResult.__annotations__))
        writer.writeheader()
        writer.writerows(r.__dict__ for r in rows)
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
