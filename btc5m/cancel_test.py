"""Cancel latency and cancel-vs-fill harness for btc5m.

Default mode is dry-run: it discovers the live window and prints the order it
would place, without building a signing client. Live mode needs the same
credentials as polymarket-mm and refuses to start without them.

The harness never sends a marketable order and never self-matches. A forced
250 ms race needs a second funded account; this tool measures natural races
and, offline, replays recorded trades against an assumed cancel latency.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
REQUIRED_ENV = ("POLY_OWNER_PK", "POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE")


@dataclass
class Observation:
    ts_ns: int
    slug: str
    token_id: str
    price: float
    size: float
    place_ack_ms: float | None = None
    cancel_request_ms: float | None = None
    cancel_ack_ms: float | None = None
    first_fill_ms: float | None = None
    order_id: str | None = None
    status: str = ""
    cancel_win: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts_ns": self.ts_ns,
            "slug": self.slug,
            "token_id": self.token_id,
            "price": self.price,
            "size": self.size,
            "place_ack_ms": self.place_ack_ms,
            "cancel_request_ms": self.cancel_request_ms,
            "cancel_ack_ms": self.cancel_ack_ms,
            "first_fill_ms": self.first_fill_ms,
            "order_id": self.order_id,
            "status": self.status,
            "cancel_win": self.cancel_win,
        }


def missing_env(env: dict[str, str]) -> list[str]:
    return [key for key in REQUIRED_ENV if not env.get(key)]


def percentile(values: Iterable[float], pct: float) -> float | None:
    rows = sorted(float(x) for x in values)
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    rank = (len(rows) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(rows) - 1)
    return rows[low] + (rows[high] - rows[low]) * (rank - low)


def cancel_win(cancel_ack_ms: float | None, first_fill_ms: float | None) -> bool | None:
    if first_fill_ms is None:
        return None
    if cancel_ack_ms is None:
        return False
    return cancel_ack_ms < first_fill_ms


def discover_window(client: httpx.Client, slug: str | None) -> dict[str, Any]:
    if slug is None:
        slot = int(time.time() // 300) * 300
        slug = f"btc-updown-5m-{slot}"
    payload = client.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=12).json()
    rows = payload if isinstance(payload, list) else []
    if not rows:
        raise RuntimeError(f"no market for slug {slug}")
    row = rows[0]
    tokens = json.loads(row["clobTokenIds"]) if isinstance(row["clobTokenIds"], str) else row["clobTokenIds"]
    outcomes = json.loads(row["outcomes"]) if isinstance(row["outcomes"], str) else row["outcomes"]
    up_idx = next((i for i, name in enumerate(outcomes) if str(name).lower() == "up"), 0)
    return {
        "slug": str(row.get("slug") or slug),
        "condition_id": str(row.get("conditionId") or ""),
        "token_id": str(tokens[up_idx]),
        "tick_size": float(row.get("orderPriceMinTickSize") or 0.01),
        "min_size": float(row.get("orderMinSize") or 5),
    }


def public_best_bid(client: httpx.Client, token_id: str) -> float:
    payload = client.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=12).json()
    bids = [float(x["price"]) for x in payload.get("bids") or []]
    return max(bids) if bids else 0.0


def build_live_client():
    from py_clob_client_v2 import ApiCreds, ClobClient

    key = os.environ["POLY_OWNER_PK"]
    funder = os.environ.get("POLY_FUNDER") or None
    signature_type = int(os.environ.get("POLY_SIGNATURE_TYPE") or 1)
    creds = ApiCreds(
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ["POLY_API_SECRET"],
        api_passphrase=os.environ["POLY_API_PASSPHRASE"],
    )
    return ClobClient(
        host=os.environ.get("POLY_HOST", CLOB),
        chain_id=int(os.environ.get("POLY_CHAIN_ID") or 137),
        key=key,
        creds=creds,
        signature_type=signature_type,
        funder=funder,
    )


def run_live(client: httpx.Client, args: argparse.Namespace) -> list[Observation]:
    from py_clob_client_v2 import OrderArgsV2, OrderPayload
    from py_clob_client_v2.clob_types import OrderType

    window = discover_window(client, args.slug)
    best_bid = public_best_bid(client, window["token_id"])
    price = max(window["tick_size"], round(best_bid - args.price_offset, 2))
    size = max(window["min_size"], args.size)
    print(f"[cancel] {window['slug']} best_bid={best_bid:.2f} quote={price:.2f} size={size:g}", flush=True)
    clob = build_live_client()
    order = clob.create_order(OrderArgsV2(token_id=window["token_id"], price=price, size=size, side="BUY"))
    t0 = time.time_ns()
    response = clob.post_order(order, OrderType.GTC, post_only=True)
    t1 = time.time_ns()
    order_id = str(response.get("orderID") or response.get("orderId") or response.get("id") or "")
    observation = Observation(
        ts_ns=t0,
        slug=window["slug"],
        token_id=window["token_id"],
        price=price,
        size=size,
        place_ack_ms=(t1 - t0) / 1e6,
        order_id=order_id,
        status=str(response.get("status") or ""),
    )
    if order_id:
        deadline = time.monotonic() + max(args.hold_ms, 0) / 1000.0
        while time.monotonic() < deadline:
            try:
                state = clob.get_order(order_id)
                matched = float(state.get("size_matched") or state.get("sizeMatched") or 0.0)
                if matched > 0:
                    observation.first_fill_ms = (time.time_ns() - t0) / 1e6
                    observation.status = str(state.get("status") or "matched")
                    break
            except Exception:
                pass
            time.sleep(0.05)
        t2 = time.time_ns()
        observation.cancel_request_ms = (t2 - t0) / 1e6
        try:
            clob.cancel_order(OrderPayload(orderID=order_id))
        except Exception as exc:
            observation.status = f"cancel_failed:{exc}"
        t3 = time.time_ns()
        observation.cancel_ack_ms = (t3 - t0) / 1e6
        observation.cancel_win = cancel_win(observation.cancel_ack_ms, observation.first_fill_ms)
    return [observation]


def replay(events_path: Path, args: argparse.Namespace) -> list[Observation]:
    windows: dict[str, dict[str, Any]] = {}
    books: dict[str, list[dict[str, Any]]] = {}
    trades: dict[str, list[dict[str, Any]]] = {}
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") == "window":
            windows[row["data"]["slug"]] = row["data"]
        elif row.get("type") == "poly_book":
            books.setdefault(row["data"]["slug"], []).append(row)
        elif row.get("type") == "trade" and row["data"].get("venue") == "polymarket":
            trades.setdefault(row["data"].get("slug"), []).append(row)
    out: list[Observation] = []
    for slug, window in windows.items():
        start_ns = int(window["start_ts"] * 1e9)
        quote_ts = start_ns + int(args.warmup * 1e9)
        up_token = str(window["up_token"])
        token_books = [b for b in books.get(slug, []) if b["data"].get("token_id") == up_token and b["ts_ns"] <= quote_ts]
        if not token_books:
            continue
        raw_bids = token_books[-1]["data"].get("bids") or []
        best_bid = max((float(x["price"]) if isinstance(x, dict) else float(x[0]) for x in raw_bids), default=0.0)
        price = max(float(window.get("tick_size") or 0.01), round(best_bid - args.price_offset, 2))
        cancel_ns = quote_ts + int(args.hold_ms * 1e6)
        ack_ns = cancel_ns + int(args.assumed_ack_ms * 1e6)
        first_fill = None
        for row in trades.get(slug, []):
            data = row["data"]
            if int(row["ts_ns"]) < quote_ts or data.get("token_id") != up_token:
                continue
            if str(data.get("side", "")).upper() != "SELL" or float(data.get("price", 1)) > price:
                continue
            first_fill = int(row["ts_ns"])
            break
        out.append(Observation(
            ts_ns=quote_ts,
            slug=slug,
            token_id=up_token,
            price=price,
            size=args.size,
            cancel_request_ms=args.hold_ms,
            cancel_ack_ms=args.hold_ms + args.assumed_ack_ms,
            first_fill_ms=(first_fill - quote_ts) / 1e6 if first_fill else None,
            status="replay",
            cancel_win=cancel_win(args.hold_ms + args.assumed_ack_ms, (first_fill - quote_ts) / 1e6 if first_fill else None),
        ))
    return out


def summarize(rows: list[Observation], mode: str = "unknown") -> dict[str, Any]:
    races = [r for r in rows if r.first_fill_ms is not None]
    wins = [r for r in races if r.cancel_win is True]
    return {
        "mode": mode,
        "observations": len(rows),
        "races": len(races),
        "wins": len(wins),
        "win_rate": len(wins) / len(races) if races else None,
        "place_ack_ms_p50": percentile([r.place_ack_ms for r in rows if r.place_ack_ms is not None], 50),
        "place_ack_ms_p99": percentile([r.place_ack_ms for r in rows if r.place_ack_ms is not None], 99),
        "cancel_ack_ms_p50": percentile([r.cancel_ack_ms for r in rows if r.cancel_ack_ms is not None], 50),
        "cancel_ack_ms_p99": percentile([r.cancel_ack_ms for r in rows if r.cancel_ack_ms is not None], 99),
    }


def write_outputs(out_dir: Path, rows: list[Observation], summary: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cancel.json").write_text(json.dumps({"summary": summary, "rows": [r.as_dict() for r in rows]}, indent=2), encoding="utf-8")
    fields = list(rows[0].as_dict().keys()) if rows else ["ts_ns", "slug", "cancel_win"]
    import csv

    with (out_dir / "cancel.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows([r.as_dict() for r in rows])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cancel latency and race harness")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--out", default="data/btc5m/cancel")
    parser.add_argument("--slug")
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--hold-ms", type=float, default=500.0)
    parser.add_argument("--assumed-ack-ms", type=float, default=120.0)
    parser.add_argument("--price-offset", type=float, default=0.03)
    parser.add_argument("--size", type=float, default=5.0)
    parser.add_argument("--warmup", type=float, default=5.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.live:
        missing = missing_env(dict(os.environ))
        if missing:
            print(f"refusing --live: missing {', '.join(missing)}", file=sys.stderr)
            raise SystemExit(2)
    if args.replay:
        rows = replay(args.replay, args)
        mode = "replay"
    elif args.live:
        with httpx.Client() as client:
            rows = run_live(client, args)
        mode = "live"
    else:
        with httpx.Client() as client:
            window = discover_window(client, args.slug)
            best_bid = public_best_bid(client, window["token_id"])
            price = max(window["tick_size"], round(best_bid - args.price_offset, 2))
            print(f"[dry-run] {window['slug']} best_bid={best_bid:.2f} would_quote={price:.2f} size={max(window['min_size'], args.size):g}", flush=True)
            rows = []
        mode = "dry-run"
    summary = summarize(rows, mode)
    write_outputs(Path(args.out), rows, summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
