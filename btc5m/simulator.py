"""Replay a btc5m recording and measure paired-fill economics.

The simulator is intentionally pessimistic. It places a static two-sided
quote at one midpoint, fills a bid only from a taker SELL print at or below
that bid, and values a single filled leg at the resolution payout. Two fill
models bracket the result: ``touch`` is the optimistic upper bound, ``queue``
subtracts the visible size ahead of us and is the lower bound.

Run:

    PYTHONPATH=. python -m btc5m.simulator \
      --events data/btc5m/<run>/events.jsonl --out data/btc5m/<run>/sim
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .model import Level, PolyBook, Resolution, Trade, Window, read_events


@dataclass(frozen=True)
class Quote:
    delta: float
    bid_up: float
    bid_down: float
    mid: float
    visible_up: float
    visible_down: float


@dataclass
class WindowResult:
    slug: str
    delta: float
    model: str
    mid: float
    bid_up: float
    bid_down: float
    filled_up: bool
    filled_down: bool
    paired: bool
    outcome: str | None
    pnl: float
    notional: float
    wv: float
    fee_equiv: float
    reason: str

    @property
    def net_bps(self) -> float:
        return self.pnl / self.notional * 10_000.0 if self.notional > 0 else 0.0


def floor_tick(price: float, tick: float) -> float:
    return round(math.floor(price / tick + 1e-9) * tick, 10)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_recording(path: Path) -> tuple[dict[str, Window], dict[str, list[PolyBook]], dict[str, list[Trade]], dict[str, Resolution], int]:
    windows: dict[str, Window] = {}
    books: dict[str, list[PolyBook]] = defaultdict(list)
    trades: dict[str, list[Trade]] = defaultdict(list)
    resolutions: dict[str, Resolution] = {}
    bad = 0
    with Path(path).open("r", encoding="utf-8") as fh:
        lines = fh.readlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            kind = row["type"]
            data = row["data"]
            ts_ns = int(row["ts_ns"])
            if kind == "window":
                w = Window(**data)
                windows[w.slug] = w
            elif kind == "poly_book":
                books[str(data["slug"])].append(
                    PolyBook(
                        ts_ns=ts_ns,
                        slug=str(data["slug"]),
                        token_id=str(data["token_id"]),
                        bids=tuple(Level(float(p), float(s)) for p, s in data["bids"]),
                        asks=tuple(Level(float(p), float(s)) for p, s in data["asks"]),
                        last_trade_price=data.get("last_trade_price"),
                    )
                )
            elif kind == "trade":
                if data.get("venue") != "polymarket" or not data.get("slug"):
                    continue
                trades[str(data["slug"])].append(
                    Trade(
                        ts_ns=int(data["ts_ns"]),
                        received_ts_ns=int(data.get("received_ts_ns") or ts_ns),
                        venue="polymarket",
                        symbol=str(data.get("symbol") or ""),
                        price=float(data["price"]),
                        size=float(data["size"]),
                        side=str(data.get("side") or ""),
                        trade_id=str(data.get("trade_id") or ""),
                        slug=str(data.get("slug") or ""),
                        token_id=str(data.get("token_id") or ""),
                    )
                )
            elif kind == "resolution":
                data = dict(data)
                data.setdefault("ts_ns", ts_ns)
                r = Resolution(**data)
                resolutions[r.slug] = r
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            bad += 1
    for rows in books.values():
        rows.sort(key=lambda b: b.ts_ns)
    for rows in trades.values():
        rows.sort(key=lambda t: t.ts_ns)
    return windows, books, trades, resolutions, bad


def best(book: PolyBook) -> tuple[float, float]:
    bid = max((x.price for x in book.bids), default=0.0)
    ask = min((x.price for x in book.asks), default=1.0)
    return bid, ask


def mid_of(book: PolyBook) -> float:
    bid, ask = best(book)
    if bid <= 0 or ask >= 1:
        return 0.0
    return (bid + ask) / 2.0


def book_at(rows: list[PolyBook], ts_ns: int) -> PolyBook | None:
    if not rows:
        return None
    stamps = [row.ts_ns for row in rows]
    idx = bisect.bisect_right(stamps, ts_ns) - 1
    return rows[idx] if idx >= 0 else None


def visible_ahead(book: PolyBook | None, price: float) -> float:
    if book is None:
        return 0.0
    return sum(level.size for level in book.bids if level.price >= price - 1e-9)


def build_quote(
    window: Window,
    books: list[PolyBook],
    quote_ts_ns: int,
    delta: float,
    min_mid: float,
    max_mid: float,
) -> Quote | None:
    up_books = [b for b in books if b.token_id == window.up_token]
    down_books = [b for b in books if b.token_id == window.down_token]
    up = book_at(up_books, quote_ts_ns)
    down = book_at(down_books, quote_ts_ns)
    mid = mid_of(up) if up is not None else 0.0
    if mid <= 0 and down is not None:
        down_mid = mid_of(down)
        mid = 1.0 - down_mid if down_mid > 0 else 0.0
    if mid <= 0 or not (min_mid <= mid <= max_mid):
        return None
    bid_up = floor_tick(mid - delta, window.tick_size)
    bid_down = floor_tick((1.0 - mid) - delta, window.tick_size)
    if bid_up + bid_down > 1.0 - window.tick_size + 1e-9:
        bid_down = floor_tick(1.0 - bid_up - window.tick_size, window.tick_size)
    if min(bid_up, bid_down) < window.tick_size or max(bid_up, bid_down) > 1.0 - window.tick_size:
        return None
    return Quote(
        delta=delta,
        bid_up=bid_up,
        bid_down=bid_down,
        mid=mid,
        visible_up=visible_ahead(up, bid_up),
        visible_down=visible_ahead(down, bid_down),
    )


def simulate_window(
    window: Window,
    books: list[PolyBook],
    trades: list[Trade],
    resolution: Resolution | None,
    quote: Quote,
    size: float,
    model: str,
    warmup: float,
    expiry_stop: float,
) -> WindowResult:
    if resolution is None or resolution.outcome not in ("Up", "Down"):
        return WindowResult(window.slug, quote.delta, model, quote.mid, quote.bid_up, quote.bid_down, False, False, False, None, 0.0, 0.0, 0.0, 0.0, "unresolved")
    quote_ts_ns = window.start_ns + int(warmup * 1e9)
    stop_ns = window.end_ns - int(expiry_stop * 1e9)
    filled = {"up": False, "down": False}
    remaining = {"up": quote.visible_up, "down": quote.visible_down}
    fill_prices: dict[str, float] = {}
    for trade in trades:
        if trade.ts_ns < quote_ts_ns or trade.ts_ns > stop_ns:
            continue
        if trade.side.upper() != "SELL":
            continue
        leg = "up" if trade.token_id == window.up_token else "down" if trade.token_id == window.down_token else None
        if leg is None or filled[leg]:
            continue
        bid = quote.bid_up if leg == "up" else quote.bid_down
        if trade.price > bid + 1e-9:
            continue
        if model == "touch":
            filled[leg] = True
            fill_prices[leg] = bid
        else:
            if remaining[leg] <= 0:
                filled[leg] = True
                fill_prices[leg] = bid
            elif trade.size >= remaining[leg] - 1e-9:
                filled[leg] = True
                fill_prices[leg] = bid
            else:
                remaining[leg] -= trade.size
    if not filled["up"] and not filled["down"]:
        return WindowResult(window.slug, quote.delta, model, quote.mid, quote.bid_up, quote.bid_down, False, False, False, resolution.outcome, 0.0, 0.0, 0.0, 0.0, "no_fill")
    notional = 0.0
    wv = 0.0
    fee_equiv = 0.0
    for leg, price in fill_prices.items():
        notional += price * size
        wv += size * price * (1.0 - price)
        fee_equiv += size * window.fee_rate * price * (1.0 - price)
    if filled["up"] and filled["down"]:
        pnl = size * (1.0 - quote.bid_up - quote.bid_down)
        reason = "paired"
    else:
        leg = "up" if filled["up"] else "down"
        fill_price = fill_prices[leg]
        won = (leg == "up" and resolution.outcome == "Up") or (leg == "down" and resolution.outcome == "Down")
        pnl = size * ((1.0 if won else 0.0) - fill_price)
        reason = f"single_{leg}_{'win' if won else 'loss'}"
    return WindowResult(
        window.slug,
        quote.delta,
        model,
        quote.mid,
        quote.bid_up,
        quote.bid_down,
        filled["up"],
        filled["down"],
        filled["up"] and filled["down"],
        resolution.outcome,
        pnl,
        notional,
        wv,
        fee_equiv,
        reason,
    )


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * q
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def aggregate(rows: list[WindowResult]) -> dict[str, Any]:
    resolved = [r for r in rows if r.reason != "unresolved"]
    paired = [r for r in resolved if r.paired]
    singles = [r for r in resolved if r.reason.startswith("single_")]
    pnl = sum(r.pnl for r in resolved)
    notional = sum(r.notional for r in resolved)
    wv = sum(r.wv for r in resolved)
    fee_equiv = sum(r.fee_equiv for r in resolved)
    net_bps = [r.net_bps for r in resolved if r.notional > 0]
    single_loss = [r.pnl for r in singles]
    return {
        "windows": len(rows),
        "resolved": len(resolved),
        "unresolved": len(rows) - len(resolved),
        "paired": len(paired),
        "single_up": sum(1 for r in resolved if r.reason.startswith("single_up")),
        "single_down": sum(1 for r in resolved if r.reason.startswith("single_down")),
        "no_fill": sum(1 for r in resolved if r.reason == "no_fill"),
        "pair_ratio": len(paired) / len(resolved) if resolved else 0.0,
        "single_leg_loss_mean": (sum(r.pnl for r in singles) / len(singles)) if singles else 0.0,
        "pnl": pnl,
        "notional": notional,
        "net_bps": pnl / notional * 10_000.0 if notional > 0 else 0.0,
        "net_bps_p10": quantile(net_bps, 0.10),
        "net_bps_p50": quantile(net_bps, 0.50),
        "net_bps_p90": quantile(net_bps, 0.90),
        "single_leg_loss_p10": quantile(single_loss, 0.10),
        "single_leg_loss_p50": quantile(single_loss, 0.50),
        "single_leg_loss_p90": quantile(single_loss, 0.90),
        "wv": wv,
        "fee_equiv": fee_equiv,
        "rebate_t1": fee_equiv * 0.03,
        "rebate_t2": fee_equiv * 0.07,
        "rebate_t3": fee_equiv * 0.10,
        "rebate_t4": fee_equiv * 0.15,
    }


def simulate(
    windows: dict[str, Window],
    books: dict[str, list[PolyBook]],
    trades: dict[str, list[Trade]],
    resolutions: dict[str, Resolution],
    deltas: Iterable[float],
    size: float,
    warmup: float,
    expiry_stop: float,
    min_mid: float,
    max_mid: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    out_rows: list[dict[str, Any]] = []
    for slug, window in sorted(windows.items()):
        quote_ts_ns = window.start_ns + int(warmup * 1e9)
        for delta in deltas:
            quote = build_quote(window, books.get(slug, []), quote_ts_ns, delta, min_mid, max_mid)
            if quote is None:
                continue
            for model in ("touch", "queue"):
                result = simulate_window(window, books.get(slug, []), trades.get(slug, []), resolutions.get(slug), quote, size, model, warmup, expiry_stop)
                out_rows.append(
                    {
                        "slug": result.slug,
                        "delta": result.delta,
                        "model": result.model,
                        "mid": result.mid,
                        "bid_up": result.bid_up,
                        "bid_down": result.bid_down,
                        "filled_up": result.filled_up,
                        "filled_down": result.filled_down,
                        "paired": result.paired,
                        "outcome": result.outcome,
                        "pnl": result.pnl,
                        "notional": result.notional,
                        "net_bps": result.net_bps,
                        "wv": result.wv,
                        "fee_equiv": result.fee_equiv,
                        "reason": result.reason,
                    }
                )
    by_key: dict[tuple[float, str], list[WindowResult]] = defaultdict(list)
    for row in out_rows:
        wr = WindowResult(
            row["slug"], row["delta"], row["model"], row["mid"], row["bid_up"], row["bid_down"],
            row["filled_up"], row["filled_down"], row["paired"], row["outcome"], row["pnl"],
            row["notional"], row["wv"], row["fee_equiv"], row["reason"],
        )
        by_key[(wr.delta, wr.model)].append(wr)
    aggregates = {
        f"delta_{delta:.4f}_{model}": aggregate(rows)
        for (delta, model), rows in sorted(by_key.items())
    }
    return out_rows, aggregates


def write_outputs(out_dir: Path, rows: list[dict[str, Any]], aggregates: dict[str, Any], meta: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sim.json").write_text(json.dumps({"meta": meta, "aggregates": aggregates, "windows": rows}, indent=2), encoding="utf-8")
    fields = list(rows[0].keys()) if rows else ["slug", "delta", "model", "pnl", "net_bps"]
    with (out_dir / "sim.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def print_table(aggregates: dict[str, Any]) -> None:
    print(f"{'bucket':28} {'resolved':>8} {'paired':>7} {'pair%':>7} {'pnl':>10} {'net_bps':>9} {'t1':>7} {'t4':>7}")
    for name, agg in aggregates.items():
        print(
            f"{name:28} {agg['resolved']:8d} {agg['paired']:7d} {agg['pair_ratio']*100:6.1f}% "
            f"{agg['pnl']:10.2f} {agg['net_bps']:9.1f} {agg['rebate_t1']:7.2f} {agg['rebate_t4']:7.2f}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay btc5m recordings")
    parser.add_argument("--events", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--delta", default="0.01,0.02,0.03")
    parser.add_argument("--size", type=float, default=20.0)
    parser.add_argument("--warmup", type=float, default=5.0)
    parser.add_argument("--expiry-stop", type=float, default=45.0)
    parser.add_argument("--min-mid", type=float, default=0.15)
    parser.add_argument("--max-mid", type=float, default=0.85)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    deltas = [float(x) for x in args.delta.split(",") if x.strip()]
    windows, books, trades, resolutions, bad = load_recording(Path(args.events))
    rows, aggregates = simulate(
        windows, books, trades, resolutions, deltas, args.size,
        args.warmup, args.expiry_stop, args.min_mid, args.max_mid,
    )
    meta = {
        "events": args.events,
        "windows": len(windows),
        "bad_lines": bad,
        "size": args.size,
        "deltas": deltas,
        "warmup": args.warmup,
        "expiry_stop": args.expiry_stop,
        "min_mid": args.min_mid,
        "max_mid": args.max_mid,
    }
    write_outputs(Path(args.out), rows, aggregates, meta)
    print_table(aggregates)


if __name__ == "__main__":
    main()
