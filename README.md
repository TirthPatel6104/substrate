# Substrate

**A local-first intelligence layer for AI agents.** Substrate gives any
MCP-compatible agent — Claude Code, Codex, OpenCode, or your own — persistent
memory, file intelligence, a durable task ledger, and permission-gated command
execution. It is **not** an agent and has no chat loop; it makes the agents you
already use remember your project, understand your files, resume long tasks, and
act on your machine safely.

Everything is stored locally in SQLite. With the default configuration the
daemon makes **zero network calls**.

> This repository began as an AI support-triage prototype. The design reviews
> that led here — and how the original safety-gate code became the execution
> layer below — are in [`docs/`](docs/): `AGENT_LAYER_BLUEPRINT.md` (the product
> vision), `CODE_REVIEW.md` (what the prototype got right and wrong), and
> `BLUEPRINT.md` (the earlier triage design).

## Why

Modern agents are strong reasoners bolted to amnesiac environments. Every
session re-learns the project; work done in one tool is invisible to the next;
long tasks die on context overflow; and each agent re-implements (or skips)
command safety. MCP standardized how agents *call tools* — Substrate is the
stateful local substrate underneath them.

## What's in the box (v0.1 MVP)

| Pillar | Tools | Notes |
|---|---|---|
| **Persistent memory** | `memory.remember/recall/brief/forget` | Typed (working/episodic/semantic/task/project), scoped, provenance-tracked, supersede-not-delete, secret-refusing, hybrid FTS5 + vector recall |
| **File intelligence** | `files.index/search/similar/stats` | Incremental hashing, chunking, embeddings, import + similarity graph edges |
| **Task ledger** | `tasks.create/update_step/resume/handoff/list` | Resume packs survive session death; lease-based single-writer locking for multi-agent handoff |
| **Safe execution** | `exec.propose/run_approved`, `approvals.pending/decide` | Deterministic SAFE / NEEDS_CONFIRMATION / HARD_BLOCK gate wired to a real executor with a persistent approvals queue and audit log |

## Install

```bash
pip install -e ".[dev]"      # editable install with test deps
```

No required third-party dependencies at runtime — Substrate uses only the
standard library. Install the `embeddings` extra and set
`SUBSTRATE_EMBEDDER=fastembed` to upgrade from the built-in offline embedder to
real semantic embeddings.

## Use it from the CLI

```bash
substrate -w myproject remember "We deploy with 'make release'; never push to main" --type project --pin
substrate -w myproject brief                      # session-start context pack (markdown)
substrate -w myproject recall "how do we deploy"

substrate -w myproject index ./src                # index a codebase
substrate -w myproject search "password hashing"

substrate -w myproject exec "echo hi"             # SAFE -> runs
substrate -w myproject exec "rm -rf /"            # HARD_BLOCK -> refused
substrate -w myproject exec "pip install requests" # NEEDS_CONFIRMATION -> queued
substrate -w myproject approvals                  # see the queue
substrate -w myproject approve 1 && substrate -w myproject run 1
```

## Connect it to an MCP agent

Substrate speaks the Model Context Protocol over stdio. Point any MCP client at:

```json
{
  "mcpServers": {
    "substrate": {
      "command": "python",
      "args": ["-m", "substrate.server", "--workspace", "myproject"]
    }
  }
}
```

The server exposes all 17 tools via `tools/list` and `tools/call`.

## Architecture

```
Agents (MCP clients)  ──stdio JSON-RPC──▶  Substrate
                                            ├─ integration:  MCP server (server.py)
                                            ├─ dispatch:     core.Substrate.dispatch
                                            ├─ memory / files / tasks / approvals
                                            ├─ safety gate:   safety.py  (chokepoint)
                                            └─ storage:       SQLite (FTS5 + vectors + edges)
```

The safety gate is a **chokepoint, not a helper**: every `exec.*` call is
classified by pure, LLM-free logic before anything runs. See
`src/substrate/safety.py` and the regression tests in `tests/test_safety.py`
(which pin every historical bypass shut: command substitution, `-c` payloads,
newline splitting, `git config`, the `-v` false-safe, `mkfs.*`).

## Development

```bash
pytest -q          # 58 tests
ruff check src tests
```

CI runs lint + tests on Python 3.10–3.12 (`.github/workflows/ci.yml`).

## Roadmap

See [`docs/AGENT_LAYER_BLUEPRINT.md`](docs/AGENT_LAYER_BLUEPRINT.md) §12. Next
up after this MVP: episodic auto-summaries + compaction, graph-expansion recall,
a read-only environment doctor, and a Tauri tray app for the approvals queue.

## License

MIT
