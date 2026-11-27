"""Build a Markdown report from one recorder run and its analyses."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def coverage(events: Path) -> dict[str, Any]:
    windows: dict[str, dict[str, Any]] = {}
    books: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    counts: Counter[str] = Counter()
    resolutions = 0
    for line in events.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = row.get("type")
        counts[kind] += 1
        data = row.get("data") or {}
        if kind == "window":
            windows[data["slug"]] = data
        elif kind == "poly_book":
            books[data["slug"]][data["token_id"]].append(int(row["ts_ns"]))
        elif kind == "resolution":
            resolutions += 1
    rows = []
    for slug, window in sorted(windows.items()):
        stamps = [ts for token in books[slug].values() for ts in token]
        rows.append({
            "slug": slug,
            "books": len(stamps),
            "first_offset": (min(stamps) - int(float(window["start_ts"]) * 1e9)) / 1e9 if stamps else None,
            "last_offset": (max(stamps) - int(float(window["start_ts"]) * 1e9)) / 1e9 if stamps else None,
        })
    return {"counts": dict(counts), "windows": rows, "resolutions": resolutions}


def sim_table(sim: dict[str, Any] | None) -> str:
    if not sim:
        return "No simulator output."
    lines = ["| bucket | resolved | paired | pair % | pnl | net bps | p10 | p90 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, row in sim.get("aggregates", {}).items():
        lines.append(
            f"| {name} | {row.get('resolved')} | {row.get('paired')} | {row.get('pair_ratio', 0)*100:.1f}% | "
            f"{row.get('pnl', 0):.2f} | {row.get('net_bps', 0):.1f} | {row.get('net_bps_p10') or 0:.1f} | {row.get('net_bps_p90') or 0:.1f} |"
        )
    return "\n".join(lines)


def predictive_table(pred: dict[str, Any] | None) -> str:
    if not pred:
        return "No predictive output."
    lines = ["| cutoff | n | verdict | accuracy | majority | AUC |", "|---|---:|---|---:|---:|---:|"]
    for name, row in pred.get("results", {}).items():
        lines.append(
            f"| {name} | {row.get('n')} | {row.get('verdict')} | {row.get('accuracy')} | "
            f"{row.get('majority_accuracy')} | {row.get('auc')} |"
        )
    return "\n".join(lines)


def gap_block(gap: dict[str, Any] | None) -> str:
    if not gap:
        return "No gap output."
    return (
        f"quotes={gap.get('quotes')}, fills={gap.get('fills')}, wins={gap.get('wins')}, "
        f"win_rate={gap.get('win_rate')}, pnl={gap.get('pnl')}, net_bps={gap.get('net_bps')}"
    )


def cancel_block(cancel: dict[str, Any] | None) -> str:
    if not cancel:
        return "No cancel output."
    s = cancel.get("summary", {})
    return (
        f"mode={s.get('mode')}, observations={s.get('observations')}, races={s.get('races')}, wins={s.get('wins')}, "
        f"win_rate={s.get('win_rate')}, cancel p50={s.get('cancel_ack_ms_p50')} ms, "
        f"cancel p99={s.get('cancel_ack_ms_p99')} ms"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a btc5m capture report")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    run = args.run_dir
    events = run / "events.resolved.jsonl" if (run / "events.resolved.jsonl").exists() else run / "events.jsonl"
    cov = coverage(events)
    sim = load_json(run / "sim" / "sim.json")
    pred = load_json(run / "predictive" / "predictive.json")
    cancel = load_json(run / "cancel" / "cancel.json")
    gap = load_json(run / "gap" / "gap.json")
    lines = [
        f"# btc5m capture report: {run.name}",
        "",
        "## Coverage",
        "",
        f"Events: `{cov['counts']}`. Resolutions: `{cov['resolutions']}`.",
        "",
        "| window | books | first offset s | last offset s |",
        "|---|---:|---:|---:|",
    ]
    for row in cov["windows"]:
        lines.append(f"| {row['slug']} | {row['books']} | {row['first_offset']} | {row['last_offset']} |")
    lines += [
        "", "## Simulator", "",
        "`touch` is an upper bound that assumes the new order is first in queue. `queue` subtracts the visible size ahead and is the realistic lower bound.",
        "", sim_table(sim), "", "## Gap refill", "", gap_block(gap), "", "## Predictive", "", predictive_table(pred), "", "## Cancel harness", "", cancel_block(cancel), "",
        "Replay mode uses an assumed cancel delay, not a live acknowledgement. A live race needs the credited environment and a second funded account.",
        "",
    ]
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
