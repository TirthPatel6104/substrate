# External evaluation sets

The in-repo golden set (`../retrieval_golden.jsonl`) was written by the author
of the code it searches, which biases the queries toward the vocabulary the
code uses. These sets evaluate retrieval over code the author did **not**
write, at a pinned commit so the numbers are reproducible.

| Set | Corpus | Pinned commit | Queries |
|---|---|---|---|
| `requests_golden.jsonl` | [psf/requests](https://github.com/psf/requests) | `611c6162cbc4ac2020a2f91c7cfa4f3abf9bbb60` | 36 (8 symbol, 13 keyword, 15 paraphrase) |

Paths in each golden file are relative to the corpus root. Queries were written
from the library's public documentation and API names, then checked against
the source so every expected file really contains the answer; several
paraphrase queries accept both the implementing module and the user-guide page
that documents the feature.

## Reproduce

```bash
git clone https://github.com/psf/requests.git /tmp/requests
git -C /tmp/requests checkout 611c6162cbc4ac2020a2f91c7cfa4f3abf9bbb60

python scripts/bench_retrieval.py --repo /tmp/requests --golden evals/external/requests_golden.jsonl
SUBSTRATE_EMBEDDER=fastembed python scripts/bench_retrieval.py --repo /tmp/requests --golden evals/external/requests_golden.jsonl
```

Results are recorded in [`docs/RETRIEVAL.md`](../../docs/RETRIEVAL.md).
