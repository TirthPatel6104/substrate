"""Retrieval evaluation + latency benchmark for the file index.

    python scripts/bench_retrieval.py                 # golden set over this repo
    python scripts/bench_retrieval.py --synthetic 5000 # latency at N synthetic chunks
    python scripts/bench_retrieval.py --json

Quality is measured on ``evals/retrieval_golden.jsonl`` (query -> acceptable
files). Each query counts as a hit at k if *any* acceptable file is in the top
k. Reported: recall@1, recall@5, MRR, plus index time and search latency p50/p95.

Run it under different ``SUBSTRATE_EMBEDDER`` / ``SUBSTRATE_VEC`` settings to
compare backends; the README table was produced this way.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from substrate.core import Substrate  # noqa: E402

GOLDEN = ROOT / "evals" / "retrieval_golden.jsonl"


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def golden_eval(k: int = 5) -> dict:
    rows = [json.loads(ln) for ln in GOLDEN.read_text(encoding="utf-8").splitlines() if ln.strip()]
    with tempfile.TemporaryDirectory() as td:
        s = Substrate(workspace="bench", db_path=Path(td) / "bench.db")
        t0 = time.perf_counter()
        # The golden file holds the queries verbatim: indexing it would be a leak.
        stats = s.files.index_path(ROOT, exclude=("evals",))
        index_s = time.perf_counter() - t0
        # warm-up (model load, page cache)
        s.files.search("warm up", k=k)

        hits1 = hits5 = 0
        rr = []
        lat = []
        misses = []
        by_kind: dict[str, list[int]] = {}
        for r in rows:
            t1 = time.perf_counter()
            res = s.files.search(r["query"], k=k)
            lat.append((time.perf_counter() - t1) * 1000)
            paths = [Path(x["path"]).resolve().relative_to(ROOT).as_posix() for x in res]
            rank = next((i for i, p in enumerate(paths) if p in r["expect"]), None)
            hits1 += rank == 0
            hits5 += rank is not None
            rr.append(0.0 if rank is None else 1.0 / (rank + 1))
            by_kind.setdefault(r.get("kind", "other"), []).append(0 if rank is None else 1)
            if rank is None:
                misses.append({"query": r["query"], "expected": r["expect"], "got": paths[:3]})
        s.close()
    n = len(rows)
    return {
        "embedder": stats["embedder"],
        "vector_backend": stats["vector_backend"],
        "files": stats["scanned"] - stats["skipped"],
        "chunks": stats["chunks"],
        "index_seconds": round(index_s, 2),
        "queries": n,
        "recall@1": round(hits1 / n, 3),
        f"recall@{k}": round(hits5 / n, 3),
        "mrr": round(sum(rr) / n, 3),
        "by_kind": {kind: round(sum(v) / len(v), 3) for kind, v in sorted(by_kind.items())},
        "search_ms_p50": round(statistics.median(lat), 2),
        "search_ms_p95": round(_pct(lat, 0.95), 2),
        "misses": misses,
    }


_WORDS = ("auth token cache queue index schema lease approval memory vector shell gate "
          "migration session handoff embed chunk fusion recall brief task daemon http "
          "sqlite thread lock audit policy secret scope episode compaction decay").split()


def synthetic_latency(n_chunks: int, k: int = 8, queries: int = 50) -> dict:
    """Index N synthetic files (one chunk each) and time searches."""
    rnd = random.Random(42)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        src.mkdir()
        for i in range(n_chunks):
            body = " ".join(rnd.choice(_WORDS) for _ in range(60))
            (src / f"f{i:05d}.md").write_text(f"# doc {i}\n\n{body}\n", encoding="utf-8")
        s = Substrate(workspace="bench-syn", db_path=Path(td) / "b.db")
        t0 = time.perf_counter()
        stats = s.files.index_path(src)
        index_s = time.perf_counter() - t0
        lat = []
        for _ in range(queries):
            q = " ".join(rnd.choice(_WORDS) for _ in range(4))
            t1 = time.perf_counter()
            s.files.search(q, k=k)
            lat.append((time.perf_counter() - t1) * 1000)
        db_bytes = os.path.getsize(Path(td) / "b.db")
        s.close()
    return {
        "embedder": stats["embedder"],
        "vector_backend": stats["vector_backend"],
        "chunks": stats["chunks"],
        "index_seconds": round(index_s, 2),
        "search_ms_p50": round(statistics.median(lat), 2),
        "search_ms_p95": round(_pct(lat, 0.95), 2),
        "db_mb": round(db_bytes / 1e6, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", type=int, default=0, help="N chunks for the latency run")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rep = synthetic_latency(args.synthetic) if args.synthetic else golden_eval()
    if args.json:
        print(json.dumps(rep, indent=2))
        return 0
    for key, val in rep.items():
        if key == "misses":
            continue
        print(f"{key:>18}: {val}")
    if rep.get("misses"):
        print("\nmisses:")
        for m in rep["misses"]:
            print(f"  - {m['query']!r}: expected {m['expected']}, got {m['got']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
