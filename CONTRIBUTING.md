# Contributing

Thanks for taking a look. Substrate is small on purpose: standard library
only at runtime, one SQLite file per workspace, one dispatch function. Please
keep it that way.

## Setup

```bash
git clone https://github.com/TirthPatel6104/substrate
cd substrate
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q                                        # ~350 tests, both vector backends
ruff check src tests scripts examples
substrate doctor
```

The test-suite forces the offline hashing embedder (`SUBSTRATE_EMBEDDER=hashing`)
so it never downloads a model. If `sqlite-vec` is installed, every test runs
twice: once with the accelerated backend and once with the Python scan.

## Ground rules

- **Every bypass becomes a corpus line first.** If you find a command the gate
  gets wrong, add it to `tests/corpus/commands.jsonl` (tag it `bypass` if it
  was wrongly `SAFE`), watch the test fail, then fix `safety.py`. See
  [docs/SAFETY_GATE.md](docs/SAFETY_GATE.md#adding-a-case).
- **No runtime dependencies.** Optional accelerators go behind an extra and
  must degrade gracefully when absent (`vec.py` is the pattern).
- **Schema changes are migrations.** Bump `SCHEMA_VERSION`, append a step to
  `_MIGRATIONS`, and add a test in `tests/test_db.py` that opens an old-shaped
  database. Never edit a shipped step.
- **Tools go through `dispatch`.** Add the handler in `core.py`, the schema in
  `TOOL_SCHEMAS`, and a CLI subcommand if a human would use it. Return
  `{"error": ..., "code": ...}` for failures; never raise across the wire.
- **Measure retrieval changes.** Run `python scripts/bench_retrieval.py`
  before and after and put both numbers in the PR.

## Commit messages

Imperative subject under 72 characters, scoped by area (`safety:`, `storage:`,
`daemon:`, `cli:`, `docs:`), body explaining *why*. Reference corpus cases or
benchmark numbers where relevant.

## Reporting a security issue

Please do not open a public issue for a gate bypass that could cause damage.
See [SECURITY.md](SECURITY.md).
