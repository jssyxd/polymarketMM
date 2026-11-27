"""Test whether OKX and Polymarket book state predicts the btc5m outcome.

Pure standard library. The point is not to ship a model. The point is to put a
number and a sample size on the question "does the top-20 book tell us where
the next 5-minute binary settles?" before anyone wires it into quoting.

Run:

    PYTHONPATH=. python -m btc5m.predictive \
      --events data/btc5m/<run>/events.jsonl --out data/btc5m/<run>/predictive
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass
class WindowData:
    slug: str
    start_ts: float
    end_ts: float
    up_token: str
    down_token: str
    outcome: str | None = None
    poly_books: dict[str, list[tuple[int, list[tuple[float, float]], list[tuple[float, float]]]]] | None = None
    okx_books: dict[str, list[tuple[int, list[tuple[float, float]], list[tuple[float, float]]]]] | None = None
    trades: list[dict[str, Any]] | None = None


def _levels(raw: Any) -> list[tuple[float, float]]:
    out = []
    for row in raw or []:
        try:
            if isinstance(row, dict):
                out.append((float(row["price"]), float(row["size"])))
            else:
                out.append((float(row[0]), float(row[1])))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return out


def load(path: Path) -> tuple[dict[str, WindowData], int]:
    windows: dict[str, WindowData] = {}
    bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            kind, data = row["type"], row["data"]
            ts = int(row["ts_ns"])
            if kind == "window":
                w = WindowData(
                    slug=str(data["slug"]),
                    start_ts=float(data["start_ts"]),
                    end_ts=float(data["end_ts"]),
                    up_token=str(data["up_token"]),
                    down_token=str(data["down_token"]),
                    poly_books={},
                    okx_books={},
                    trades=[],
                )
                windows[w.slug] = w
            elif kind == "poly_book" and data.get("slug") in windows:
                windows[data["slug"]].poly_books.setdefault(str(data["token_id"]), []).append(
                    (ts, _levels(data.get("bids")), _levels(data.get("asks")))
                )
            elif kind == "okx_book":
                for w in windows.values():
                    w.okx_books.setdefault(str(data["inst_id"]), []).append(
                        (ts, _levels(data.get("bids")), _levels(data.get("asks")))
                    )
            elif kind == "trade":
                slug = data.get("slug")
                if slug in windows:
                    windows[slug].trades.append({"ts_ns": ts, **data})
                else:
                    for w in windows.values():
                        w.trades.append({"ts_ns": ts, **data})
            elif kind == "resolution" and data.get("slug") in windows:
                windows[data["slug"]].outcome = data.get("outcome")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            bad += 1
    for w in windows.values():
        for book in (w.poly_books or {}).values():
            book.sort(key=lambda x: x[0])
        for book in (w.okx_books or {}).values():
            book.sort(key=lambda x: x[0])
        (w.trades or []).sort(key=lambda x: x["ts_ns"])
    return windows, bad


def at(rows: list[Any], ts_ns: int) -> Any | None:
    if not rows:
        return None
    stamps = [r[0] for r in rows]
    idx = bisect.bisect_right(stamps, ts_ns) - 1
    return rows[idx] if idx >= 0 else None


def best(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    bb = max((p for p, s in bids), default=0.0)
    ba = min((p for p, s in asks), default=1.0)
    bs = next((s for p, s in bids if p == bb), 0.0)
    as_ = next((s for p, s in asks if p == ba), 0.0)
    return bb, ba, bs, as_


def depth(levels: list[tuple[float, float]], n: int = 20) -> float:
    return sum(s for _, s in levels[:n])


def book_features(row: tuple[int, list[tuple[float, float]], list[tuple[float, float]]] | None, prob: bool = False) -> dict[str, float]:
    if row is None:
        return {}
    _, bids, asks = row
    bb, ba, bs, as_ = best(bids, asks)
    if bb <= 0 or ba <= 0 or (prob and ba >= 1):
        return {}
    mid = (bb + ba) / 2.0
    bd, ad = depth(bids), depth(asks)
    micro = (bb * as_ + ba * bs) / (bs + as_) if bs + as_ > 0 else mid
    return {
        "mid": mid,
        "spread_bps": (ba - bb) / mid * 10_000.0,
        "depth_imbalance": (bd - ad) / (bd + ad) if bd + ad > 0 else 0.0,
        "micro_dev_bps": (micro - mid) / mid * 10_000.0,
    }


def ofi(rows: list[tuple[int, list[tuple[float, float]], list[tuple[float, float]]]], ts_ns: int, window_ns: int) -> float:
    start = ts_ns - window_ns
    sample = [r for r in rows if start <= r[0] <= ts_ns]
    if len(sample) < 2:
        return 0.0
    total = 0.0
    prev = sample[0]
    for cur in sample[1:]:
        pb, pa, pbs, pas = best(prev[1], prev[2])
        cb, ca, cbs, cas = best(cur[1], cur[2])
        if cb > pb:
            total += cbs
        elif cb == pb:
            total += cbs - pbs
        else:
            total -= pbs
        if ca < pa:
            total -= cas
        elif ca == pa:
            total -= cas - pas
        else:
            total += pas
        prev = cur
    return total


def realized_vol(rows: list[tuple[int, list[tuple[float, float]], list[tuple[float, float]]]], ts_ns: int, window_ns: int) -> float:
    start = ts_ns - window_ns
    mids = []
    for row in rows:
        if start <= row[0] <= ts_ns:
            bb, ba, _, _ = best(row[1], row[2])
            if bb > 0 and ba < 1:
                mids.append((row[0], (bb + ba) / 2.0))
    if len(mids) < 3:
        return 0.0
    returns = []
    for i in range(1, len(mids)):
        dt = max((mids[i][0] - mids[i - 1][0]) / 1e9, 1e-3)
        returns.append(math.log(mids[i][1] / mids[i - 1][1]) / math.sqrt(dt))
    return statistics.pstdev(returns) if len(returns) > 1 else 0.0


def flow(rows: list[dict[str, Any]], ts_ns: int, window_ns: int, venue: str, symbol: str | None = None) -> float:
    buy = sell = 0.0
    for row in rows:
        if row.get("venue") != venue or row["ts_ns"] < ts_ns - window_ns or row["ts_ns"] > ts_ns:
            continue
        if symbol is not None and row.get("symbol") != symbol:
            continue
        side = str(row.get("side") or "").upper()
        size = float(row.get("size") or 0.0)
        if side in ("BUY", "BID"):
            buy += size
        elif side in ("SELL", "ASK"):
            sell += size
    return (buy - sell) / (buy + sell) if buy + sell > 0 else 0.0


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def features(w: WindowData, cutoff_ts: float) -> dict[str, float] | None:
    if w.outcome not in ("Up", "Down"):
        return None
    ts_ns = int(cutoff_ts * 1e9)
    start_ns = int(w.start_ts * 1e9)
    poly_up = at((w.poly_books or {}).get(w.up_token, []), ts_ns)
    poly_down = at((w.poly_books or {}).get(w.down_token, []), ts_ns)
    spot = at((w.okx_books or {}).get("BTC-USDT", []), ts_ns)
    swap = at((w.okx_books or {}).get("BTC-USDT-SWAP", []), ts_ns)
    pf = book_features(poly_up, prob=True)
    if not pf:
        return None
    if not pf.get("mid"):
        return None
    sf = book_features(spot)
    swf = book_features(swap)
    if not sf:
        return None
    start_spot = at((w.okx_books or {}).get("BTC-USDT", []), start_ns)
    start_mid = 0.0
    if start_spot:
        bb, ba, _, _ = best(start_spot[1], start_spot[2])
        start_mid = (bb + ba) / 2.0 if bb > 0 and ba < 1 else 0.0
    ret = math.log(sf["mid"] / start_mid) if start_mid > 0 and sf["mid"] > 0 else 0.0
    tau = max(w.end_ts - cutoff_ts, 1.0)
    sigma = realized_vol((w.okx_books or {}).get("BTC-USDT", []), ts_ns, 60 * 10**9)
    denom = sigma * math.sqrt(max(tau, 1.0))
    p_okx = normal_cdf(ret / denom) if denom > 0 else 0.5
    return {
        "cutoff_ts": cutoff_ts,
        "poly_mid": pf["mid"],
        "poly_spread_bps": pf["spread_bps"],
        "poly_depth_imbalance": pf["depth_imbalance"],
        "poly_flow_30s": flow(w.trades or [], ts_ns, 30 * 10**9, "polymarket"),
        "okx_ret_since_start": ret,
        "okx_spread_bps": sf["spread_bps"],
        "okx_depth_imbalance": sf["depth_imbalance"],
        "okx_micro_dev_bps": sf["micro_dev_bps"],
        "okx_ofi_5s": ofi((w.okx_books or {}).get("BTC-USDT", []), ts_ns, 5 * 10**9),
        "okx_flow_30s": flow(w.trades or [], ts_ns, 30 * 10**9, "okx", "BTC-USDT"),
        "swap_depth_imbalance": swf.get("depth_imbalance", 0.0),
        "p_okx": p_okx,
        "divergence": pf["mid"] - p_okx,
        "label": 1.0 if w.outcome == "Up" else 0.0,
    }


def auc(scores: list[float], labels: list[int]) -> float | None:
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(pos) * len(neg))


def fit_logistic(xs: list[list[float]], ys: list[int], l2: float = 1.0, steps: int = 400, lr: float = 0.1) -> tuple[list[float], list[float]]:
    if not xs:
        return [], [], [0.0]
    n_features = len(xs[0])
    means = [sum(row[j] for row in xs) / len(xs) for j in range(n_features)]
    stds = []
    for j in range(n_features):
        var = sum((row[j] - means[j]) ** 2 for row in xs) / max(len(xs) - 1, 1)
        stds.append(math.sqrt(var) or 1.0)
    weights = [0.0] * (n_features + 1)
    for _ in range(steps):
        grads = [0.0] * (n_features + 1)
        for row, y in zip(xs, ys):
            z = weights[0] + sum(weights[j + 1] * ((row[j] - means[j]) / stds[j]) for j in range(n_features))
            pred = 1.0 / (1.0 + math.exp(-max(min(z, 30), -30)))
            err = pred - y
            grads[0] += err
            for j in range(n_features):
                grads[j + 1] += err * ((row[j] - means[j]) / stds[j])
        scale = 1.0 / max(len(xs), 1)
        weights[0] -= lr * grads[0] * scale
        for j in range(n_features):
            weights[j + 1] -= lr * (grads[j + 1] * scale + l2 * weights[j + 1] * scale)
    return means, stds, weights


def predict(row: list[float], means: list[float], stds: list[float], weights: list[float]) -> float:
    z = weights[0] + sum(weights[j + 1] * ((row[j] - means[j]) / stds[j]) for j in range(len(row)))
    return 1.0 / (1.0 + math.exp(-max(min(z, 30), -30)))


def loo(rows: list[dict[str, float]], feature_names: list[str]) -> dict[str, Any]:
    xs = [[r[name] for name in feature_names] for r in rows]
    ys = [int(r["label"]) for r in rows]
    if len(rows) < 2:
        majority = (max(sum(ys), len(ys) - sum(ys)) / len(ys)) if ys else 0.0
        return {"n": len(rows), "majority_accuracy": majority, "accuracy": None, "brier": None, "auc": None, "predictions": []}
    preds = []
    for i in range(len(rows)):
        train_x = xs[:i] + xs[i + 1:]
        train_y = ys[:i] + ys[i + 1:]
        if len(set(train_y)) < 2:
            preds.append(sum(train_y) / len(train_y))
            continue
        means, stds, weights = fit_logistic(train_x, train_y)
        preds.append(predict(xs[i], means, stds, weights))
    majority = max(sum(ys), len(ys) - sum(ys)) / len(ys)
    acc = sum((p >= 0.5) == bool(y) for p, y in zip(preds, ys)) / len(ys)
    brier = sum((p - y) ** 2 for p, y in zip(preds, ys)) / len(ys)
    return {
        "n": len(ys),
        "majority_accuracy": majority,
        "accuracy": acc,
        "brier": brier,
        "auc": auc(preds, ys),
        "predictions": preds,
    }


def univariate(rows: list[dict[str, float]], names: list[str]) -> dict[str, Any]:
    labels = [int(r["label"]) for r in rows]
    out = {}
    for name in names:
        scores = [r[name] for r in rows]
        out[name] = {"auc": auc(scores, labels)}
    return out


def run(windows: dict[str, WindowData], cutoffs: list[float]) -> dict[str, Any]:
    results = {}
    for cutoff in cutoffs:
        rows = []
        for w in windows.values():
            f = features(w, w.end_ts - cutoff)
            if f:
                rows.append(f)
        if not rows:
            results[f"cutoff_{cutoff:.0f}s"] = {"n": 0, "verdict": "inconclusive"}
            continue
        names = [
            "okx_ret_since_start", "poly_mid", "divergence", "okx_depth_imbalance",
            "okx_ofi_5s", "okx_flow_30s", "poly_flow_30s", "swap_depth_imbalance",
        ]
        model = loo(rows, names)
        model["univariate"] = univariate(rows, names)
        if model["n"] < 30:
            verdict = "inconclusive"
        elif model["accuracy"] - model["majority_accuracy"] < 0.05 or (model["auc"] is not None and model["auc"] < 0.6):
            verdict = "no_edge"
        else:
            verdict = "candidate"
        model["verdict"] = verdict
        model["cutoff_seconds_before_end"] = cutoff
        results[f"cutoff_{cutoff:.0f}s"] = model
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Predictive check for btc5m books")
    parser.add_argument("--events", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cutoffs", default="30,60,120,180,240")
    parser.add_argument("--min-windows", type=int, default=30)
    args = parser.parse_args()
    windows, bad = load(Path(args.events))
    cutoffs = [float(x) for x in args.cutoffs.split(",") if x.strip()]
    results = run(windows, cutoffs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    payload = {"events": args.events, "windows": len(windows), "bad_lines": bad, "min_windows": args.min_windows, "results": results}
    (out / "predictive.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"windows={len(windows)} bad_lines={bad}")
    for name, result in results.items():
        print(f"{name}: n={result.get('n')} verdict={result.get('verdict')} acc={result.get('accuracy')} base={result.get('majority_accuracy')} auc={result.get('auc')}")


if __name__ == "__main__":
    main()
