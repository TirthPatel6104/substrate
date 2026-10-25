"""Render the safety-gate corpus as a benchmark report.

    python scripts/gate_report.py            # markdown table to stdout
    python scripts/gate_report.py --json     # machine-readable

The report answers the questions a reviewer asks of any command gate:
how many commands, how many distinct bypass classes, how many false-safes
(zero is the only acceptable number), and how fast is classification.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from substrate.safety import Level, safety_level  # noqa: E402

CORPUS = ROOT / "tests" / "corpus" / "commands.jsonl"


def load() -> list[dict]:
    return [json.loads(ln) for ln in CORPUS.read_text(encoding="utf-8").splitlines() if ln.strip()]


def run(rows: list[dict]) -> dict:
    by_level = Counter()
    by_tag = Counter()
    mismatches = []
    false_safe = []
    latencies_us = []
    for r in rows:
        t0 = time.perf_counter()
        got = safety_level(r["cmd"])
        latencies_us.append((time.perf_counter() - t0) * 1e6)
        expected = Level[r["level"]]
        by_level[expected.name] += 1
        for t in r["tags"]:
            by_tag[t] += 1
        if got.level != expected:
            mismatches.append({"cmd": r["cmd"], "expected": expected.name, "got": got.level.name})
        if expected != Level.SAFE and got.level == Level.SAFE:
            false_safe.append(r["cmd"])
    bypass_classes = sorted({t for r in rows if "bypass" in r["tags"] for t in r["tags"]
                             if t not in ("bypass",)})
    latencies_us.sort()
    return {
        "commands": len(rows),
        "by_level": dict(by_level),
        "bypass_regressions": sum(1 for r in rows if "bypass" in r["tags"]),
        "bypass_classes": bypass_classes,
        "windows_cases": by_tag.get("windows", 0),
        "infra_cases": by_tag.get("infra", 0),
        "mismatches": mismatches,
        "false_safe": false_safe,
        "latency_us": {
            "p50": round(statistics.median(latencies_us), 1),
            "p95": round(latencies_us[int(len(latencies_us) * 0.95) - 1], 1),
            "max": round(latencies_us[-1], 1),
        },
    }


def markdown(rep: dict) -> str:
    lines = [
        "| Metric | Value |",
        "|---|---|",
        f"| Commands in corpus | {rep['commands']} |",
        f"| SAFE / NEEDS_CONFIRMATION / HARD_BLOCK | {rep['by_level'].get('SAFE', 0)} / "
        f"{rep['by_level'].get('NEEDS_CONFIRMATION', 0)} / {rep['by_level'].get('HARD_BLOCK', 0)} |",
        f"| Historical bypasses pinned as regressions | {rep['bypass_regressions']} |",
        f"| Windows / PowerShell cases | {rep['windows_cases']} |",
        f"| Container / cloud / IaC cases | {rep['infra_cases']} |",
        f"| Mismatches | {len(rep['mismatches'])} |",
        f"| **False-SAFE (dangerous command marked SAFE)** | **{len(rep['false_safe'])}** |",
        f"| Classification latency p50 / p95 / max | {rep['latency_us']['p50']} µs / "
        f"{rep['latency_us']['p95']} µs / {rep['latency_us']['max']} µs |",
    ]
    if rep["mismatches"]:
        lines += ["", "Mismatches:", ""]
        for m in rep["mismatches"]:
            lines.append(f"- `{m['cmd']}` expected {m['expected']} got {m['got']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rep = run(load())
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(markdown(rep))
    return 1 if rep["mismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
