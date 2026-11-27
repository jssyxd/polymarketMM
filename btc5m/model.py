"""Typed wire model for the btc5m recorder and simulator.

One event per line. Every event carries ``type`` and ``ts_ns``; the payload is
under ``data``. The simulator reads the same files, so the schema is the
contract between the live recorder and offline replay.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Window:
    slug: str
    condition_id: str
    question: str
    start_ts: float
    end_ts: float
    up_token: str
    down_token: str
    tick_size: float = 0.01
    min_order_size: float = 5.0
    fee_rate: float = 0.05
    neg_risk: bool = False

    @property
    def start_ns(self) -> int:
        return int(self.start_ts * 1_000_000_000)

    @property
    def end_ns(self) -> int:
        return int(self.end_ts * 1_000_000_000)


@dataclass(frozen=True)
class PolyBook:
    ts_ns: int
    slug: str
    token_id: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    last_trade_price: float | None = None


@dataclass(frozen=True)
class OkxBook:
    ts_ns: int
    inst_id: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    seq_id: int = 0
    prev_seq_id: int = 0


@dataclass(frozen=True)
class Trade:
    ts_ns: int
    received_ts_ns: int
    venue: str
    symbol: str
    price: float
    size: float
    side: str
    trade_id: str
    slug: str | None = None
    token_id: str | None = None


@dataclass(frozen=True)
class Resolution:
    ts_ns: int
    slug: str
    condition_id: str
    closed: bool
    up_price: float | None
    down_price: float | None
    outcome: str | None


@dataclass
class RunStats:
    started_ns: int = field(default_factory=time.time_ns)
    events: int = 0
    dropped: int = 0
    windows: int = 0
    poly_books: int = 0
    okx_books: int = 0
    trades: int = 0
    resolutions: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["ended_ns"] = time.time_ns()
        out["duration_sec"] = (out["ended_ns"] - self.started_ns) / 1e9
        return out


def event(kind: str, data: dict[str, Any], ts_ns: int | None = None) -> dict[str, Any]:
    return {"type": kind, "ts_ns": ts_ns if ts_ns is not None else time.time_ns(), "data": data}


def window_event(w: Window, ts_ns: int | None = None) -> dict[str, Any]:
    return event("window", asdict(w), ts_ns)


def poly_book_event(b: PolyBook) -> dict[str, Any]:
    return event(
        "poly_book",
        {
            "slug": b.slug,
            "token_id": b.token_id,
            "bids": [[x.price, x.size] for x in b.bids],
            "asks": [[x.price, x.size] for x in b.asks],
            "last_trade_price": b.last_trade_price,
        },
        b.ts_ns,
    )


def okx_book_event(b: OkxBook) -> dict[str, Any]:
    return event(
        "okx_book",
        {
            "inst_id": b.inst_id,
            "bids": [[x.price, x.size] for x in b.bids],
            "asks": [[x.price, x.size] for x in b.asks],
            "seq_id": b.seq_id,
            "prev_seq_id": b.prev_seq_id,
        },
        b.ts_ns,
    )


def trade_event(t: Trade) -> dict[str, Any]:
    return event("trade", asdict(t), t.ts_ns)


def resolution_event(r: Resolution) -> dict[str, Any]:
    return event("resolution", asdict(r), r.ts_ns)


def dump_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def read_events(path: str | Path) -> Iterable[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def parse_levels(raw: Iterable[Any]) -> tuple[Level, ...]:
    levels: list[Level] = []
    for row in raw:
        if isinstance(row, dict):
            price, size = row.get("price"), row.get("size")
        else:
            price, size = row[0], row[1]
        try:
            p, s = float(price), float(size)
        except (TypeError, ValueError):
            continue
        if s > 0:
            levels.append(Level(p, s))
    return tuple(levels)


def top(levels: Iterable[Level], n: int, reverse: bool) -> tuple[Level, ...]:
    return tuple(sorted(levels, key=lambda x: x.price, reverse=reverse)[:n])
