"""Record Polymarket BTC 5m books/trades plus OKX top-of-book for one window.

Run with the project venv:

    PYTHONPATH=. /path/to/venv/bin/python -m btc5m.recorder --duration 3600

The recorder writes one JSONL event stream under ``data/btc5m/<run_id>/``.
It never authenticates and never places an order.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import websockets

from .model import (
    Level,
    OkxBook,
    PolyBook,
    Resolution,
    RunStats,
    Trade,
    Window,
    dump_line,
    event,
    okx_book_event,
    parse_levels,
    poly_book_event,
    resolution_event,
    top,
    trade_event,
    window_event,
)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
OKX_WS = "wss://ws.okx.com:443/ws/v5/public"
OKX_INSTRUMENTS = ("BTC-USDT", "BTC-USDT-SWAP")
SLOT_SECONDS = 300


def iso_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_window(payload: dict[str, Any], slot: int) -> Window | None:
    if not payload.get("conditionId") or not payload.get("clobTokenIds"):
        return None
    try:
        tokens = json.loads(payload["clobTokenIds"]) if isinstance(payload["clobTokenIds"], str) else payload["clobTokenIds"]
        outcomes = json.loads(payload["outcomes"]) if isinstance(payload.get("outcomes"), str) else payload.get("outcomes", [])
    except (TypeError, json.JSONDecodeError):
        return None
    if len(tokens) < 2:
        return None
    up_idx = next((i for i, name in enumerate(outcomes) if str(name).lower() == "up"), 0)
    down_idx = next((i for i, name in enumerate(outcomes) if str(name).lower() == "down"), 1)
    start = iso_ts(payload.get("eventStartTime")) or float(slot)
    end = iso_ts(payload.get("endDate")) or float(slot + SLOT_SECONDS)
    fee = (payload.get("feeSchedule") or {}).get("rate", 0.05)
    return Window(
        slug=str(payload.get("slug") or f"btc-updown-5m-{slot}"),
        condition_id=str(payload["conditionId"]),
        question=str(payload.get("question") or ""),
        start_ts=start,
        end_ts=end,
        up_token=str(tokens[up_idx]),
        down_token=str(tokens[down_idx]),
        tick_size=float(payload.get("orderPriceMinTickSize") or 0.01),
        min_order_size=float(payload.get("orderMinSize") or 5),
        fee_rate=float(fee),
        neg_risk=bool(payload.get("negRisk") or False),
    )


class JsonlWriter:
    def __init__(self, path: Path, stats: RunStats) -> None:
        self.path = path
        self.stats = stats
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=200_000)
        self._task: asyncio.Task[None] | None = None
        self._fh: Any = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8", buffering=1)
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        assert self._fh is not None
        last_flush = time.monotonic()
        while True:
            item = await self.queue.get()
            if item is None:
                break
            self._fh.write(dump_line(item) + "\n")
            self.stats.events += 1
            if time.monotonic() - last_flush > 1.0:
                self._fh.flush()
                last_flush = time.monotonic()
        self._fh.flush()

    def emit(self, payload: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.stats.dropped += 1

    async def close(self) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)
        if self._task is not None:
            await self._task
        if self._fh is not None:
            self._fh.close()


class Recorder:
    def __init__(self, out_dir: Path, args: argparse.Namespace) -> None:
        self.out_dir = out_dir
        self.args = args
        self.stats = RunStats()
        self.writer = JsonlWriter(out_dir / "events.jsonl", self.stats)
        self.windows: dict[str, Window] = {}
        self.seen_trades: set[str] = set()
        self.resolved: set[str] = set()
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0),
            headers={"User-Agent": "btc5m-recorder/0.1"},
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
        self._stop = asyncio.Event()
        self._poly_ws_ok = False
        self._poly_gen = 0
        self._poly_caches: dict[str, PolyCache] = {}
        self._last_poly_ws_ns: dict[str, int] = {}
        self._token_to_window: dict[str, Window] = {}

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        for attempt in range(3):
            try:
                response = await self.client.get(url, **kwargs)
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001 - recorder must keep running
                self.stats.errors += 1
                if attempt == 2:
                    print(f"[recorder] GET {url} failed: {exc}", file=sys.stderr, flush=True)
                    return None
                await asyncio.sleep(0.3 * (attempt + 1))
        return None

    async def post_json(self, url: str, body: Any) -> Any:
        for attempt in range(3):
            try:
                response = await self.client.post(url, json=body)
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                if attempt == 2:
                    print(f"[recorder] POST {url} failed: {exc}", file=sys.stderr, flush=True)
                    return None
                await asyncio.sleep(0.3 * (attempt + 1))
        return None

    async def discover_once(self) -> None:
        now = time.time()
        slots = {int(now // SLOT_SECONDS) * SLOT_SECONDS}
        slots.add(max(slots) + SLOT_SECONDS)
        slots.add(max(slots) + SLOT_SECONDS)
        for slot in sorted(slots):
            slug = f"{self.args.asset}-updown-5m-{slot}"
            payload = await self.get_json(f"{GAMMA}/markets", params={"slug": slug})
            rows = payload if isinstance(payload, list) else []
            if not rows:
                continue
            window = parse_window(rows[0], slot)
            if window is None or window.slug in self.windows:
                continue
            self.windows[window.slug] = window
            self._token_to_window[window.up_token] = window
            self._token_to_window[window.down_token] = window
            self._poly_gen += 1
            self.stats.windows += 1
            self.writer.emit(window_event(window))
            print(
                f"[recorder] window {window.slug} {window.start_ts:.0f}..{window.end_ts:.0f}",
                file=sys.stderr,
                flush=True,
            )

    async def discover_loop(self) -> None:
        while not self._stop.is_set():
            await self.discover_once()
            await asyncio.sleep(5.0)

    def active_windows(self, *, include_recent: float = 0.0) -> list[Window]:
        now = time.time()
        return [
            w
            for w in self.windows.values()
            if w.start_ts - 5 <= now <= w.end_ts + include_recent
        ]

    async def poly_ws_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            tokens = sorted(self._token_to_window)
            if not tokens:
                await asyncio.sleep(0.5)
                continue
            generation = self._poly_gen
            caches: dict[str, PolyCache] = {token: PolyCache(token) for token in tokens}
            last_emit = {token: 0 for token in tokens}
            try:
                async with websockets.connect(
                    POLY_WS, ping_interval=20, ping_timeout=10, max_queue=4096
                ) as ws:
                    await ws.send(json.dumps({"type": "market", "assets_ids": tokens}))
                    self._poly_ws_ok = True
                    self._poly_caches = caches
                    print("[recorder] polymarket websocket connected", file=sys.stderr, flush=True)
                    backoff = 1.0
                    async for raw in ws:
                        if generation != self._poly_gen:
                            break
                        msg = json.loads(raw)
                        if isinstance(msg, list):
                            for row in msg:
                                self._apply_poly_message(row, caches, last_emit)
                        elif isinstance(msg, dict):
                            self._apply_poly_message(msg, caches, last_emit)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._poly_ws_ok = False
                self.stats.errors += 1
                print(f"[recorder] polymarket websocket error: {exc}; reconnecting", file=sys.stderr, flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
            finally:
                self._poly_ws_ok = False

    def _apply_poly_message(
        self,
        msg: dict[str, Any],
        caches: dict[str, "PolyCache"],
        last_emit: dict[str, int],
    ) -> None:
        event_type = msg.get("event_type")
        changed: set[str] = set()
        if event_type == "book":
            token = str(msg.get("asset_id") or "")
            cache = caches.get(token)
            if cache is not None:
                cache.set_book(msg)
                changed.add(token)
        elif event_type == "price_change":
            for change in msg.get("price_changes") or []:
                token = str(change.get("asset_id") or "")
                cache = caches.get(token)
                if cache is not None:
                    cache.apply_change(change)
                    changed.add(token)
        now_ns = time.time_ns()
        for token in changed:
            if now_ns - last_emit.get(token, 0) < self.args.okx_emit_interval_ns:
                continue
            last_emit[token] = now_ns
            window = self._token_to_window.get(token)
            if window is None:
                continue
            book = caches[token].snapshot(now_ns, window, self.args.book_levels)
            if book.bids or book.asks:
                self._last_poly_ws_ns[window.slug] = now_ns
                self.stats.poly_books += 1
                self.writer.emit(poly_book_event(book))

    async def poly_books_loop(self) -> None:
        while not self._stop.is_set():
            windows = self.active_windows(include_recent=5.0)
            if not windows:
                await asyncio.sleep(0.2)
                continue
            for window in windows:
                if time.time_ns() - self._last_poly_ws_ns.get(window.slug, 0) < 1_000_000_000:
                    continue
                body = [{"token_id": window.up_token}, {"token_id": window.down_token}]
                rows = await self.post_json(f"{CLOB}/books", body)
                if not isinstance(rows, list):
                    continue
                now_ns = time.time_ns()
                for row in rows:
                    token_id = str(row.get("asset_id") or "")
                    if not token_id:
                        continue
                    bids = top(parse_levels(row.get("bids") or []), self.args.book_levels, True)
                    asks = top(parse_levels(row.get("asks") or []), self.args.book_levels, False)
                    if not bids and not asks:
                        continue
                    book = PolyBook(
                        ts_ns=now_ns,
                        slug=window.slug,
                        token_id=token_id,
                        bids=bids,
                        asks=asks,
                        last_trade_price=_float_or_none(row.get("last_trade_price")),
                    )
                    self.stats.poly_books += 1
                    self.writer.emit(poly_book_event(book))
            await asyncio.sleep(self.args.poly_interval)

    async def trades_loop(self) -> None:
        while not self._stop.is_set():
            windows = self.active_windows(include_recent=180.0)
            for window in windows:
                payload = await self.get_json(
                    f"{DATA_API}/v2/trades",
                    params={"condition": window.condition_id, "limit": 100},
                )
                if not isinstance(payload, dict):
                    continue
                for row in payload.get("data") or []:
                    trade = _parse_poly_trade(row, window)
                    if trade is None or trade.trade_id in self.seen_trades:
                        continue
                    self.seen_trades.add(trade.trade_id)
                    self.stats.trades += 1
                    self.writer.emit(trade_event(trade))
            await asyncio.sleep(self.args.trades_interval)

    async def okx_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    OKX_WS, ping_interval=20, ping_timeout=10, max_queue=2048
                ) as ws:
                    args = []
                    for inst in OKX_INSTRUMENTS:
                        args.append({"channel": "books", "instId": inst})
                        args.append({"channel": "trades", "instId": inst})
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    print("[recorder] okx websocket connected", file=sys.stderr, flush=True)
                    backoff = 1.0
                    caches = {inst: OkxCache(inst) for inst in OKX_INSTRUMENTS}
                    last_emit = {inst: 0 for inst in OKX_INSTRUMENTS}
                    async for raw in ws:
                        msg = json.loads(raw)
                        arg = msg.get("arg") or {}
                        channel, inst = arg.get("channel"), arg.get("instId")
                        if channel == "books" and inst in caches:
                            cache = caches[inst]
                            for row in msg.get("data") or []:
                                cache.apply(row)
                                if cache.gapped:
                                    raise RuntimeError(f"okx sequence gap on {inst}")
                                now_ns = time.time_ns()
                                if now_ns - last_emit[inst] >= self.args.okx_emit_interval_ns:
                                    last_emit[inst] = now_ns
                                    book = cache.snapshot(now_ns, self.args.book_levels)
                                    if book.bids or book.asks:
                                        self.stats.okx_books += 1
                                        self.writer.emit(okx_book_event(book))
                        elif channel == "trades" and inst in OKX_INSTRUMENTS:
                            for row in msg.get("data") or []:
                                trade = _parse_okx_trade(row, inst)
                                if trade is not None:
                                    self.stats.trades += 1
                                    self.writer.emit(trade_event(trade))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                print(f"[recorder] okx websocket error: {exc}; reconnecting", file=sys.stderr, flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    async def resolution_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            for window in list(self.windows.values()):
                if window.slug in self.resolved or now < window.end_ts + 15:
                    continue
                resolution = None
                payload = await self.get_json(f"{CLOB}/markets/{window.condition_id}")
                if isinstance(payload, dict):
                    resolution = _parse_clob_resolution(payload, window)
                if resolution is None:
                    payload = await self.get_json(f"{GAMMA}/markets", params={"slug": window.slug})
                    rows = payload if isinstance(payload, list) else []
                    if rows:
                        resolution = _parse_resolution(rows[0], window)
                if resolution is None:
                    continue
                self.resolved.add(window.slug)
                self.stats.resolutions += 1
                self.writer.emit(resolution_event(resolution))
            await asyncio.sleep(10.0)

    async def run(self, duration: float) -> None:
        await self.writer.start()
        tasks = [
            asyncio.create_task(self.discover_loop()),
            asyncio.create_task(self.poly_ws_loop()),
            asyncio.create_task(self.poly_books_loop()),
            asyncio.create_task(self.trades_loop()),
            asyncio.create_task(self.okx_loop()),
            asyncio.create_task(self.resolution_loop()),
        ]
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)
        started = time.time()
        try:
            while not self._stop.is_set() and time.time() - started < duration:
                await asyncio.sleep(0.5)
        finally:
            self._stop.set()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self.writer.close()
            await self.client.aclose()
            self._write_run_json()

    def _write_run_json(self) -> None:
        payload = {
            "args": vars(self.args),
            "stats": self.stats.as_dict(),
            "windows": [w.slug for w in self.windows.values()],
        }
        (self.out_dir / "run.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


class PolyCache:
    def __init__(self, token_id: str) -> None:
        self.token_id = token_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_trade_price: float | None = None

    def set_book(self, row: dict[str, Any]) -> None:
        self.bids = _levels_to_map(row.get("bids") or [])
        self.asks = _levels_to_map(row.get("asks") or [])
        self.last_trade_price = _float_or_none(row.get("last_trade_price"))

    def apply_change(self, change: dict[str, Any]) -> None:
        try:
            price, size = float(change["price"]), float(change["size"])
        except (KeyError, TypeError, ValueError):
            return
        book = self.bids if str(change.get("side") or "").upper() == "BUY" else self.asks
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size

    def snapshot(self, ts_ns: int, window: Window, n: int) -> PolyBook:
        bids = tuple(Level(p, s) for p, s in sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:n] if s > 0)
        asks = tuple(Level(p, s) for p, s in sorted(self.asks.items(), key=lambda x: x[0])[:n] if s > 0)
        return PolyBook(ts_ns, window.slug, self.token_id, bids, asks, self.last_trade_price)


class OkxCache:
    def __init__(self, inst_id: str) -> None:
        self.inst_id = inst_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.seq_id = 0
        self.prev_seq_id = 0
        self.gapped = False

    def apply(self, row: dict[str, Any]) -> None:
        seq = int(row.get("seqId") or 0)
        prev = int(row.get("prevSeqId") or 0)
        if prev == -1 or not self.bids:
            self.bids.clear()
            self.asks.clear()
        elif self.seq_id and prev != self.seq_id:
            self.gapped = True
            self.bids.clear()
            self.asks.clear()
            self.seq_id = seq
            self.prev_seq_id = prev
            return
        self.seq_id = seq
        self.prev_seq_id = prev
        for side, book in (("bids", self.bids), ("asks", self.asks)):
            for level in row.get(side) or []:
                try:
                    price, size = float(level[0]), float(level[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if size == 0:
                    book.pop(price, None)
                else:
                    book[price] = size

    def snapshot(self, ts_ns: int, n: int) -> OkxBook:
        bids = tuple(
            Level(p, s) for p, s in sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:n] if s > 0
        )
        asks = tuple(
            Level(p, s) for p, s in sorted(self.asks.items(), key=lambda x: x[0])[:n] if s > 0
        )
        return OkxBook(ts_ns, self.inst_id, bids, asks, self.seq_id, self.prev_seq_id)


def _levels_to_map(raw: Any) -> dict[float, float]:
    out: dict[float, float] = {}
    for row in raw or []:
        try:
            if isinstance(row, dict):
                price, size = float(row["price"]), float(row["size"])
            else:
                price, size = float(row[0]), float(row[1])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if size > 0:
            out[price] = size
    return out


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_poly_trade(row: dict[str, Any], window: Window) -> Trade | None:
    try:
        ts = float(row["timestamp"])
        price = float(row["price"])
        size = float(row["size"])
    except (KeyError, TypeError, ValueError):
        return None
    tx = str(row.get("transaction_hash") or "")
    token = str(row.get("token_id") or "")
    side = str(row.get("side") or "")
    trade_id = "|".join((tx, token, f"{ts:.0f}", f"{price:.6f}", f"{size:.6f}", side))
    return Trade(
        ts_ns=int(ts * 1_000_000_000),
        received_ts_ns=time.time_ns(),
        venue="polymarket",
        symbol=window.slug,
        price=price,
        size=size,
        side=side,
        trade_id=trade_id,
        slug=window.slug,
        token_id=token,
    )


def _parse_okx_trade(row: dict[str, Any], inst: str) -> Trade | None:
    try:
        ts_ms = int(row["ts"])
        price = float(row["px"])
        size = float(row["sz"])
    except (KeyError, TypeError, ValueError):
        return None
    trade_id = str(row.get("tradeId") or f"{inst}|{ts_ms}|{price}|{size}")
    return Trade(
        ts_ns=ts_ms * 1_000_000,
        received_ts_ns=time.time_ns(),
        venue="okx",
        symbol=inst,
        price=price,
        size=size,
        side=str(row.get("side") or ""),
        trade_id=trade_id,
    )


def _parse_clob_resolution(row: dict[str, Any], window: Window) -> Resolution | None:
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
    return Resolution(
        ts_ns=time.time_ns(),
        slug=window.slug,
        condition_id=window.condition_id,
        closed=True,
        up_price=up,
        down_price=down,
        outcome=outcome,
    )


def _parse_resolution(row: dict[str, Any], window: Window) -> Resolution | None:
    closed = bool(row.get("closed"))
    raw = row.get("outcomePrices")
    up = down = None
    if raw:
        try:
            prices = json.loads(raw) if isinstance(raw, str) else raw
            up = float(prices[0])
            down = float(prices[1])
        except (TypeError, ValueError, json.JSONDecodeError, IndexError):
            pass
    if not closed:
        return None
    outcome = None
    if up is not None and down is not None:
        outcome = "Up" if up > down else "Down"
    return Resolution(
        ts_ns=time.time_ns(),
        slug=window.slug,
        condition_id=window.condition_id,
        closed=closed,
        up_price=up,
        down_price=down,
        outcome=outcome,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record btc5m books and trades")
    parser.add_argument("--out", default="data/btc5m")
    parser.add_argument("--duration", type=float, default=3600.0)
    parser.add_argument("--asset", default="btc")
    parser.add_argument("--poly-interval", type=float, default=0.5)
    parser.add_argument("--trades-interval", type=float, default=2.0)
    parser.add_argument("--book-levels", type=int, default=50)
    parser.add_argument("--okx-emit-interval-ms", type=float, default=100.0)
    parser.add_argument("--run-id", default=None)
    return parser


async def async_main(args: argparse.Namespace) -> None:
    run_id = args.run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = Path(args.out) / run_id
    args.okx_emit_interval_ns = int(args.okx_emit_interval_ms * 1_000_000)
    recorder = Recorder(out_dir, args)
    print(f"[recorder] recording run_id={run_id} out={out_dir}", flush=True)
    await recorder.run(args.duration)


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
