# Architecture

Substrate is a local daemon that gives MCP-compatible agents four capabilities
they lack: persistent memory, file intelligence, a durable task ledger, and
gated command execution. This document describes how the pieces fit, the
storage model, and the decisions behind them.

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ Claude Code  │  │    Codex     │  │  your agent  │      MCP clients
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │ stdio           │ HTTP            │ HTTP
       ▼                 ▼                 ▼
┌──────────────┐  ┌──────────────────────────────────┐
│ server.py    │  │ httpd.py  (sessions, identity,   │   transports
│ (1 agent)    │  │  approvals UI, /healthz, CSRF)   │
└──────┬───────┘  └──────┬───────────────────────────┘
       └────────┬────────┘
                ▼
        ┌──────────────────────────────┐
        │ core.Substrate.dispatch()    │  one entry point, RLock-serialised,
        │   27 tools, actor identity   │  shared by MCP + CLI
        └──┬──────┬──────┬──────┬──────┘
           ▼      ▼      ▼      ▼
       memory  files  tasks  executor ──▶ safety.py (chokepoint) ──▶ approvals
           │      │      │      │                                        │
           ▼      ▼      ▼      ▼                                        ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ db.py  SQLite (WAL) · FTS5 · float32 vectors · edges · audit    │
        │ vec.py VectorIndex: sqlite-vec vec0 ──or── Python cosine scan   │
        └─────────────────────────────────────────────────────────────────┘
```

## Modules

| Module | Responsibility | Key decisions |
|---|---|---|
| `core.py` | Wires subsystems; `dispatch(tool, params, actor)`; composite views (`brief`, `session_end`, `maintenance`); tool catalog | Single router shared by every transport and the CLI so they cannot drift. Errors become structured `{error, code}` payloads, never exceptions across the wire. |
| `server.py` | MCP over stdio; `MCPProtocol` is transport-agnostic | Zero dependencies: a hand-rolled JSON-RPC loop covering `initialize`, `tools/list`, `tools/call`, `ping`. Identity from `clientInfo.name`. |
| `httpd.py` | MCP streamable HTTP; sessions; approvals page; health; JSON APIs | stdlib `ThreadingHTTPServer`. Loopback bind, optional bearer token, CSRF rejection by `Origin`/`Sec-Fetch-Site`, `Content-Type` gate on `/mcp`. |
| `safety.py` | Deterministic command classifier | See [SAFETY_GATE.md](SAFETY_GATE.md). |
| `executor.py` | Runs SAFE commands; queues confirm-level ones; re-classifies approved ones | Uses the gate's tokenizer; argv execution unless shell operators are present. |
| `approvals.py` | Durable pending → approved/denied → executed/expired queue | Replaces a blocking `input()` so human-in-the-loop works in headless agents. |
| `memory.py` | Typed, scoped, provenance-tracked memories; hybrid recall; compaction; decay | Supersede-not-delete; secrets refused at write time; RRF with pin/importance/recency boosts. |
| `files.py` | Incremental index: hash → chunk → embed → FTS + vectors + edges; hybrid search | Blake2b content hashes skip unchanged files; one read per file; `.gitignore`-aware walk; per-file centroids make `similar` a KNN query cached as edges, not an O(n²) pass at index time. |
| `tasks.py` | Tasks, steps, resume packs, leases, handoffs | Lease-based single writer with expiry; reads never block. |
| `embedding.py` | Embedder selection; float32 vector (de)serialisation | Hashing n-grams by default (offline, deterministic); FastEmbed when installed. |
| `vec.py` | `VectorIndex`: KNN over a source table | sqlite-vec when loadable, Python scan otherwise; both tested for parity. |
| `db.py` | Schema, forward-only migrations, audit helper | One SQLite file per workspace; `schema_version` stamp; migrations are append-only. |
| `cli.py` | Operator surface: `doctor`, `serve`, `hook`, approvals, memory, tasks | Every command calls `dispatch`, so the CLI is a thin client of the same core. |

## Storage model

One SQLite database per workspace (plus a `global` scope visible everywhere),
WAL mode, `synchronous=NORMAL`. Tables:

- `memories` — typed rows with `scope`, `source` (which agent), `confidence`,
  `importance`, `uses`, `pinned`, `superseded_by`, `expires_at`, and a
  float32 `embedding` + `emb_model`. `memories_fts` is an external-content
  FTS5 table kept in sync by triggers.
- `files` / `chunks` — file metadata (including a centroid vector per file)
  and ~1.2 kB paragraph chunks with their embeddings; `chunks_fts` likewise.
- `edges` — a generic graph: `imports`, `similar_to`, `supersedes`. A plain
  `(src, dst, type, weight)` table with indexes is enough; a graph database
  would be operational overkill for a desktop daemon.
- `tasks` / `task_steps` — with `lease_owner` / `lease_until`.
- `approvals` — the human review queue, with the proposing `cwd`.
- `audit` — append-only log of every execution decision and handoff.
- `*_vec` — derived sqlite-vec indexes, rebuilt on demand (e.g. model change).

### Vectors

Vectors are stored as little-endian float32 blobs. That is 4× smaller than
the JSON arrays the first version used, and it is exactly the byte layout
sqlite-vec consumes, so the same column feeds both backends. Every row records
`emb_model`; when the active embedder differs, rows are re-embedded on the next
index or maintenance pass instead of being compared across incompatible spaces.

### Hybrid retrieval

Both memory recall and file search fuse two rankers with reciprocal-rank
fusion (`1/(60+rank)` per list):

1. **Lexical** — FTS5 with an identifier-aware tokenizer (`tokenchars '_'`),
   so `safety_level` is one token. Queries containing identifiers run an
   exact-identifier pass first, then an expanded pass with the identifier's
   parts, so a symbol definition outranks prose that mentions both halves.
2. **Vector** — KNN over the chunk or memory vectors (top 50).

Memory recall then multiplies by pin, importance and a 30-day recency curve;
file search adds a filename boost when a query term names the file.
Numbers are in [RETRIEVAL.md](RETRIEVAL.md).

### Migrations

`SCHEMA_VERSION` is stamped in `meta`. On open, a fresh database gets the
current schema and stamp; an older one runs each missing step from
`_MIGRATIONS` in order and bumps the stamp after each. Steps are append-only:
never edit a shipped step, add a new version. v2 added `approvals.cwd`; v3
recreated the FTS tables with the identifier tokenizer and rebuilt them; v4
added `files.centroid` (backfilled lazily by the index on first use).

## Concurrency

The HTTP daemon serves several agents from worker threads. SQLite is a
single-writer store, so `Substrate.dispatch` takes a process-wide re-entrant
lock: tool calls are serialised. Calls take milliseconds, so this is both
correct and fast enough for a per-machine daemon, and it avoids `database is
locked` errors entirely. Agent-level coordination (two agents on one task) is
handled above the lock by task leases, not by SQLite.

## Identity and provenance

Every tool call carries an `actor`: the MCP `clientInfo.name`, the
`X-Substrate-Agent` header, `--agent` on the CLI, or `human@approvals-ui`.
It is written as memory `source`, task `lease_owner`, approval
`requested_by`/`decided_by`, and audit `actor`. "Which agent decided this?" is
always answerable.

## What is deliberately not here

- **No LLM calls.** Compaction, summaries and the gate are all deterministic.
  Agents are good at summarising; Substrate stores what they produce.
- **No network.** Nothing leaves the machine. The optional FastEmbed extra
  downloads a model once, on explicit install.
- **No background watcher yet.** Indexing is incremental and fast enough to
  run at `files.index` / session start; a filesystem watcher is on the
  roadmap and would sit in front of the same `index_path`.
- **No external services.** SQLite, FTS5 and (optionally) sqlite-vec are the
  whole stack. Backup is `cp`.
