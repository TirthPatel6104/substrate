# Retrieval: design and measurements

Both `memory.recall` and `files.search` are hybrid: a lexical ranker (SQLite
FTS5) and a vector ranker (KNN over embeddings) are fused with reciprocal-rank
fusion, then adjusted by cheap, explainable boosts. This document records the
design decisions and the numbers behind them. Every number here is reproducible
with `python scripts/bench_retrieval.py` (golden set) and
`python scripts/bench_retrieval.py --synthetic N` (scale).

## Pipeline

```
query ──▶ query_terms()  ──▶ FTS5 MATCH (exact identifiers first, then expanded) ──▶ lexical ranks
      └─▶ embedder.embed ──▶ VectorIndex.search(top 50)                            ──▶ vector ranks
                                                                                         │
                      score = Σ 1/(60 + rank)  over the lists the row appears in  ◀──────┘
                      × boosts:  files → filename match (×1.25)
                                 memory → pin (+0.5), importance (+0.2·imp), recency (30-day decay)
                      best chunk per file → top k
```

Why RRF rather than a weighted sum of scores: the two rankers produce scores on
incomparable scales (BM25 vs cosine), and RRF needs no tuning to combine them.
`k=60` is the value from the original paper; we saw no gain from tuning it on
our golden set and did not want to overfit 40 queries.

## Evaluation set

`evals/retrieval_golden.jsonl`: 40 queries over this repository, each with the
set of acceptable files. Three kinds:

- **keyword** (19) — terms that appear in the target ("fork bomb detection");
- **symbol** (2) — an identifier ("safety_level", "TOOL_SCHEMAS");
- **paraphrase** (19) — intent without the vocabulary ("how do I stop a
  command like rm -rf from running").

A query counts as a hit at k if any acceptable file is in the top k. The
golden file itself is excluded from the index (it contains the queries
verbatim). Note the set is small and written by the author of the code: it is
a regression harness and a comparison tool, not a leaderboard.

## Results: golden set

| Embedder | Vector backend | recall@1 | recall@5 | MRR | keyword | paraphrase | symbol | index | search p50 / p95 |
|---|---|---|---|---|---|---|---|---|---|
| hashing n-grams (256-d) | Python scan | 0.55 | 0.90 | 0.69 | 1.00 | 0.79 | 1.00 | 0.6 s | 14 / 24 ms |
| hashing n-grams | sqlite-vec | 0.55 | 0.90 | 0.69 | 1.00 | 0.79 | 1.00 | 0.6 s | 2.8 / 4.5 ms |
| FastEmbed bge-small-en-v1.5 (384-d) | Python scan | 0.65 | 0.975 | 0.79 | 1.00 | 0.95 | 1.00 | 101 s | 31 / 34 ms |
| FastEmbed bge-small-en-v1.5 | sqlite-vec | 0.65 | 0.975 | 0.79 | 1.00 | 0.95 | 1.00 | 115 s | 15 / 23 ms |

Corpus: 45–50 files, ~320–345 chunks (the repo grew between runs). CPU only,
Windows 11 laptop. FastEmbed index time is dominated by ONNX inference and is
a one-off; incremental re-index touches only changed files.

Reading the table:

- **The lexical side carries keyword and symbol queries** to 100% with either
  embedder. The embedder matters for paraphrase: 0.79 → 0.95.
- **sqlite-vec changes latency, not quality.** Both backends return the same
  neighbours (a test asserts parity); the accelerated path is 5× faster on the
  hashing embedder. With FastEmbed the remaining ~14 ms is embedding the query.
- **The one persistent miss** is "human in the loop review of commands before
  execution", which the index answers with the design docs that *describe*
  human-in-the-loop review rather than the modules that implement it. That is
  arguably a correct answer for a different question; it is kept as a miss to
  keep the harness honest.

### What moved the numbers

| Change | hashing recall@5 | symbol recall | Notes |
|---|---|---|---|
| MVP (JSON vectors, default tokenizer, full scan) | 0.875 | 0.50 | `safety_level` tokenised as `safety`, `level`; tests outranked the definition |
| identifier-aware FTS tokenizer (`tokenchars '_'`, schema v3) + exact-identifier-first pass | 0.925 | 1.00 | shipped as a migration that rebuilds the FTS tables |
| filename boost (×1.25 when a query term names the file) | 0.925 | 1.00 | fixed "doctor check installation" → `cli.py` |
| exclude the golden file from the index (leak fix) | 0.90 | 1.00 | the honest number |

## Results: scale

Synthetic corpus of N one-chunk Markdown files (60 random words each), hashing
embedder, 50 random 4-word queries, k=8:

| Chunks | Backend | Index time | Search p50 | Search p95 | DB size |
|---|---|---|---|---|---|
| 2,000 | Python scan | 14.8 s | 78 ms | 89 ms | 6.4 MB |
| 2,000 | sqlite-vec | 12.3 s | 7.4 ms | 10 ms | 11.1 MB |
| 10,000 | Python scan | 67 s | 306 ms | 360 ms | 35 MB |
| 10,000 | sqlite-vec | 66 s | 23 ms | 25 ms | 57 MB |

Observations:

- **Brute-force search is linear** (78 → 306 ms for 5× the rows) and becomes
  noticeable past a few thousand chunks. sqlite-vec is the recommended extra
  for repositories of that size; it costs ~60% more disk for the derived index.
- **Indexing is I/O bound on Windows.** Profiling showed 80% of index time in
  `open()`; the fix was to read each file once (hash the bytes, then decode)
  instead of opening it twice. Together with moving similarity edges off the
  index path (below) this took indexing 2,000 files from 177 s to ~13 s.
- **Similar-file lookup is on demand.** The MVP materialised every
  `similar_to` pair at index time, an O(n²) pure-Python loop that made
  indexing unusable past ~1,000 files. Each file now stores a centroid (the
  normalised mean of its chunk vectors) and `files.similar` is a KNN query over
  centroids, cached as edges and invalidated when the file changes. Cost moved
  from O(n²) at index time to O(n) (scan) or O(log n) (sqlite-vec) per lookup.

## Storage choices that affect retrieval

- **float32 blobs, not JSON.** 4× smaller and zero-copy into sqlite-vec.
- **`emb_model` on every row.** Changing embedder is detected and rows are
  re-embedded on the next index or maintenance pass; vectors from different
  models are never compared.
- **External-content FTS5 tables with triggers** keep lexical and vector views
  of the same row in one transaction.
- **Chunking** is paragraph-packed to ~1.2 kB, capped at 40 chunks per file.
  Function-level chunks via tree-sitter are the next planned improvement and
  the main lever left for paraphrase recall on code.

## Reproducing

```bash
pip install -e ".[dev]"                            # hashing embedder, sqlite-vec available
python scripts/bench_retrieval.py                  # golden set
SUBSTRATE_VEC=off python scripts/bench_retrieval.py
pip install "substrate-agent[embeddings]"
SUBSTRATE_EMBEDDER=fastembed python scripts/bench_retrieval.py
python scripts/bench_retrieval.py --synthetic 10000
```
