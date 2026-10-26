# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-10-26

### Added
- **Multi-agent daemon**: MCP streamable-HTTP transport (`substrate serve --transport http`)
  with sessions, per-agent identity, batch requests, `/healthz`, JSON APIs and a
  human **approvals page** (approve / deny / approve-and-run).
- **Enforced task leases**: writes require the lease; competing writers get a
  structured `lease_held` error naming the owner; `tasks.claim`, `tasks.release`,
  `tasks.add_step`, `tasks.set_status`; handoffs record a note and appear in resume packs.
- **Sessions**: `session.start` / `session.end` (episodic summary + working-memory
  compaction); `maintenance.run` (TTL expiry, compaction, decay, re-embedding, stale
  approvals/leases).
- **Vector layer**: float32 blob storage; `sqlite-vec` KNN with brute-force fallback;
  automatic re-embedding when the embedding model changes; FastEmbed auto-detection.
- **Retrieval**: identifier-aware FTS5 tokenizer (schema v3), exact-identifier-first
  lexical ranking, filename boost, recency boost; `.gitignore`-aware indexing;
  pruning of deleted files; per-file centroids (schema v4) so `files.similar` is an
  on-demand KNN query instead of an O(n²) edge rebuild at index time (2,000 files:
  177 s → 13 s); files are read once per index pass.
- **Safety gate**: quote-aware splitting; `eval`/`xargs`/`find -exec`/`timeout`/`sudo -u`/
  `env VAR=` wrappers; interpreter string-literal scanning; `&` and `&>` handling;
  unresolvable `$VAR` in destructive positions; Windows path tokenisation; PowerShell,
  registry, container, Kubernetes, Terraform and cloud-CLI destructive verbs;
  encoded PowerShell refusal; 178-command regression corpus; Hypothesis property tests;
  `scripts/gate_report.py`.
- **Executor**: runs on Windows (cmd builtins, backslash paths); gated shell operators;
  `SUBSTRATE_EXEC_SHELL=never`; single-use approvals with recorded `cwd`; durations.
- **CLI**: `doctor`, `serve`, `mcp-config`, `hook session-start|stop` (Claude Code),
  `classify`, `claim`, `handoff`, `step`, `audit`, `memories`, `supersede`,
  `approve --run`, `session end`, `maintenance`.
- Schema migrations with a `schema_version` stamp (v2: `approvals.cwd`; v3: FTS tokenizer;
  v4: `files.centroid`).
- `evals/retrieval_golden.jsonl` + `scripts/bench_retrieval.py` (recall@k, MRR, latency).
- CI on Linux, macOS and Windows with coverage, wheel build + smoke test, PyPI release
  workflow via trusted publishing. Examples for Claude Code and a two-agent handoff demo.

### Fixed
- `find . -delete` and `find . -exec rm -rf {} \;` were classified SAFE.
- `ls & rm -rf /` was never split on the background `&`.
- `python -c '...; os.system("rm -rf /")'` was split at the quoted `;` before the
  payload could be inspected.
- `C:\Windows` was tokenised to `C:Windows` and missed by the critical-path check.
- `C:` and `~` were prefix-matched, blocking every path on the drive / under home.
- SAFE verdicts always reported "No sub-commands" as their reason.
- README claims (lease locking over MCP, tasks in the brief) that were not wired through.

### Changed
- Distribution name is `substrate-agent` (import name stays `substrate`).
- Default embedder is `auto`: FastEmbed when installed, hashing n-grams otherwise.

## [0.1.0] — 2026-10-25

Initial MVP: memory (remember/recall/brief/forget), file index, task ledger,
safety gate wired to an executor with a persistent approvals queue, MCP stdio
server, CLI, 58 tests.
