# Substrate

**A local-first intelligence layer for AI coding agents.** Substrate is a small
daemon that gives any MCP-compatible agent — Claude Code, Codex, OpenCode, or
your own — four things they lack out of the box:

| | What it gives the agent | Why it matters |
|---|---|---|
| **Memory** | Facts, decisions and session summaries that persist across sessions *and across tools*, with provenance and scope | Session two starts knowing what session one learned. No more "we use pnpm, not npm" every morning. |
| **File intelligence** | An incremental index of your codebase with hybrid lexical + semantic search and an import / similarity graph | Orientation in a repo is one tool call instead of an afternoon of grepping. |
| **Task ledger** | Durable multi-step tasks with resume packs, write leases and handoffs | A 40-step migration survives context overflow; a second agent can pick it up where the first stopped. |
| **Safe execution** | A deterministic SAFE / NEEDS_CONFIRMATION / HARD_BLOCK gate in front of every shell command, a human approvals queue, and an audit log | `rm -rf /` never runs. `pip install x` waits for you. `ls` just works. |

Everything lives in one SQLite file per workspace. Zero runtime dependencies.
Zero network calls unless you opt into the semantic-embedding extra.

[![CI](https://github.com/TirthPatel6104/substrate/actions/workflows/ci.yml/badge.svg)](https://github.com/TirthPatel6104/substrate/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)
![Platforms](https://img.shields.io/badge/platforms-linux%20%7C%20macos%20%7C%20windows-informational)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Why

Modern coding agents are strong reasoners bolted to amnesiac environments.
Every session re-learns the project. Work done in one tool is invisible to the
next. Long tasks die on context overflow. Each agent re-implements (or skips)
command safety, and nobody can audit what agents did on the machine.

MCP standardised how agents *call tools*. Substrate is the stateful layer
underneath: memory, file model, task state and execution policy shared by every
agent on your machine, stored locally, readable with `sqlite3`.

## Quick start

```bash
pip install substrate-agent          # import name: substrate
cd your-project
substrate doctor                     # checks Python, SQLite/FTS5, gate, executor; prints MCP config
substrate mcp-config > .mcp.json     # Claude Code picks this up automatically
```

Then, in Claude Code: *"Remember: we deploy with `make release`, never push to
main directly."* The agent calls `memory.remember`. Next session, the brief
already contains the rule. To make that automatic, copy
[`examples/claude-code/settings.json`](examples/claude-code/settings.json) into
`.claude/settings.json`: its `SessionStart` hook injects `substrate brief` into
context and its `Stop` hook stores what the session did as an episodic memory.

Several agents on one machine? Run one daemon and point them all at it:

```bash
substrate serve --transport http --port 8731
# MCP endpoint:  http://127.0.0.1:8731/mcp
# approvals UI:  http://127.0.0.1:8731/approvals
```

## What it looks like

```bash
$ substrate exec "rm -rf /"
{"status": "blocked", "verdict": "HARD_BLOCK",
 "reason": "Targeting critical system path '/' for deletion"}

$ substrate exec "pip install requests"
{"status": "pending_approval", "verdict": "NEEDS_CONFIRMATION", "approval_id": 1,
 "reason": "'pip install' modifies installed packages"}

$ substrate approve 1 --run           # or click Approve in the web UI

$ substrate exec "ps aux | grep python"
{"status": "ran", "verdict": "SAFE", "mode": "shell", "returncode": 0, ...}
```

Two agents, one task ([`examples/multi_agent_handoff.py`](examples/multi_agent_handoff.py)):

```
[claude-code] ok  tasks.create       {"task_id": 1, "lease_owner": "claude-code"}
[claude-code] ok  tasks.update_step  {"updated": true}
[codex      ] ERR tasks.update_step  {"error": "task 1 is leased to 'claude-code' for another 1799s; ask for tasks.handoff ...", "code": "lease_held"}
[claude-code] ok  tasks.handoff      {"handoff_to": "codex", "handoff_note": "context budget exhausted; /logout and /refresh remain", ...}
[codex      ] ok  tasks.claim        {"next_step": {"step_id": 2, "description": "/logout"}, "progress": "1/3 steps done", ...}
[codex      ] ok  session.end        {"episode_id": 2}
```

## The safety gate

Every command passes through one deterministic function before anything runs.
No LLM in the loop: the gate must hold when the model is the thing being
attacked. Danger is modelled as *operation × target × reversibility*, and the
gate assumes the command string is trying to hide something from a naive parser.

| Evasion | Example | Verdict |
|---|---|---|
| Chaining, background | `ls && rm -rf /`, `ls & rm -rf /` | HARD_BLOCK |
| Substitution | `echo $(rm -rf /)`, `` cat `rm -rf /` ``, `cat <(rm -rf /)` | HARD_BLOCK |
| Wrappers | `sudo -u root rm -rf /`, `env FOO=1 rm -rf /`, `timeout 5 rm -rf /` | HARD_BLOCK |
| Executors | `eval 'rm -rf /'`, `xargs rm -rf < list`, `find . -exec rm -rf {} \;` | HARD_BLOCK |
| Interpreter payloads | `bash -c "rm -rf /"`, `python -c 'os.system("rm -rf /")'` | HARD_BLOCK |
| Quoted separators | `grep "a\|b" file`, `echo "a; rm -rf /"` | SAFE (not split) |
| Unresolvable variables | `rm -rf $DIR/`, `rsync --delete src $DEST` | HARD_BLOCK |
| Windows | `Remove-Item -Recurse -Force C:\Windows`, `powershell -EncodedCommand …` | HARD_BLOCK |
| Infra | `docker system prune -af`, `terraform destroy`, `kubectl delete ns prod` | HARD_BLOCK |
| Routine work | `rm -rf ./build`, `git push`, `npm run build`, `find . -name '*.pyc' -delete` | NEEDS_CONFIRMATION |

How it is tested: a **178-command regression corpus** (every bypass ever found
is pinned; 29 so far), **Hypothesis property tests** that chaining, wrapping,
case or whitespace can never *lower* a verdict, and executor tests proving a
`HARD_BLOCK` never reaches `subprocess` even if the approvals table is edited.
Classification takes ~55 µs. Full write-up, threat model and known limitations:
[`docs/SAFETY_GATE.md`](docs/SAFETY_GATE.md).

## Retrieval, measured

Hybrid search fuses FTS5 (identifier-aware tokenizer) with vector KNN by
reciprocal-rank fusion. Evaluated on a 40-query golden set over this repository
(`python scripts/bench_retrieval.py`, details in
[`docs/RETRIEVAL.md`](docs/RETRIEVAL.md)):

| Embedder | Vector backend | recall@1 | recall@5 | MRR | search p50 |
|---|---|---|---|---|---|
| hashing n-grams (default, offline) | Python scan | 0.55 | 0.90 | 0.69 | 14 ms |
| hashing n-grams | sqlite-vec | 0.55 | 0.90 | 0.69 | 2.8 ms |
| FastEmbed bge-small (`[embeddings]`) | Python scan | 0.65 | **0.975** | **0.79** | 31 ms |
| FastEmbed bge-small | sqlite-vec | 0.65 | **0.975** | **0.79** | 15 ms |

The identifier-aware tokenizer took symbol queries (`safety_level`) from 50% to
100% recall and is shipped as a schema migration. At scale the backends
diverge: with 10,000 chunks a search costs 306 ms with the Python scan and
23 ms with sqlite-vec, so install `substrate-agent[vec]` when a repo gets large.
Indexing 2,000 files takes ~13 s after profiling moved similar-file edges off
the index path (the MVP's O(n²) pass took 177 s).

## Tools (27)

| Area | Tools |
|---|---|
| memory | `remember` · `recall` · `brief` · `supersede` · `list` · `forget` |
| files | `index` · `search` · `similar` · `stats` |
| tasks | `create` · `claim` · `release` · `add_step` · `update_step` · `set_status` · `resume` · `handoff` · `list` |
| exec | `classify` (dry run) · `propose` · `run_approved` |
| approvals | `pending` · `decide` |
| session | `start` · `end` · `maintenance.run` |

Every tool is also a CLI subcommand; both go through the same `dispatch`
function, so the CLI and MCP surface cannot drift.

## Architecture

```
Claude Code ─stdio─┐                   ┌─ memory   (typed, scoped, provenance, supersede-not-delete)
Codex ──────HTTP───┼─▶ dispatch(tool, ─┼─ files    (hash → chunk → embed → FTS5 + vectors + edges)
your agent ─HTTP───┘    params, actor) ├─ tasks    (steps, resume packs, leases, handoffs)
                             │          └─ executor ─▶ safety gate ─▶ approvals ─▶ audit
                             ▼
              SQLite (WAL) · FTS5 · float32 vectors · sqlite-vec or Python KNN
```

- **One entry point.** `Substrate.dispatch(tool, params, actor)` serves stdio
  MCP, HTTP MCP and the CLI. Calls are serialised with a lock: SQLite is a
  single-writer store and tool calls take milliseconds.
- **Identity everywhere.** The MCP client's name becomes the `actor` on memory
  provenance, task leases, approvals and the audit log.
- **Storage you can read.** One `.substrate.db` per workspace. Forward-only
  migrations with a `schema_version` stamp (currently v4).
- **Optional acceleration, same API.** sqlite-vec and FastEmbed are extras.
  The test-suite runs under both vector backends and asserts identical results.
- **Security by default.** Loopback bind, optional bearer token, CSRF rejection
  on the approvals UI, re-classification at execution time, scrubbed subprocess
  environment, capped output. See [`SECURITY.md`](SECURITY.md).

More in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Install options

```bash
pip install substrate-agent                 # stdlib only
pip install "substrate-agent[vec]"          # + sqlite-vec: ~6-8x faster KNN
pip install "substrate-agent[embeddings]"   # + FastEmbed: real semantic search (downloads a 130 MB ONNX model once)
pip install "substrate-agent[all]"
```

Environment knobs: `SUBSTRATE_HOME` (data dir), `SUBSTRATE_EMBEDDER`
(`auto|hashing|fastembed`), `SUBSTRATE_VEC` (`auto|off`),
`SUBSTRATE_EXEC_SHELL` (`auto|never`), `SUBSTRATE_TOKEN` (HTTP bearer token).

## Development

```bash
pip install -e ".[dev]"
pytest -q                            # 359 tests; runs twice if sqlite-vec is installed
ruff check src tests scripts examples
python scripts/gate_report.py        # corpus table; non-zero exit on any mismatch
python scripts/bench_retrieval.py    # recall@k / MRR / latency on the golden set
```

CI runs on Linux, macOS and Windows across Python 3.10–3.13, builds the wheel
and smoke-tests `substrate doctor` from a clean virtualenv. Releases publish to
PyPI through trusted publishing on a `v*` tag.

## Status and roadmap

v0.2 is a working daemon, not a demo: the gate, executor, storage, both
transports and the CLI are tested end to end. Honest gaps, in the order I plan
to close them:

1. **Filesystem watcher** so the index stays warm without calling `files.index`.
2. **Graph expansion in recall** (one hop over `imports` / `similar_to` edges).
3. **Tree-sitter symbols** for function-level chunks and an accurate call graph.
4. **Policy profiles** (per-workspace allow/deny rules layered on the gate).
5. **Memory quality loop**: contradiction detection between memories.

The project began as an AI support-triage prototype; the design reviews that
turned its safety-gate code into this layer are in
[`docs/CODE_REVIEW.md`](docs/CODE_REVIEW.md) and
[`docs/AGENT_LAYER_BLUEPRINT.md`](docs/AGENT_LAYER_BLUEPRINT.md).
[`CHANGELOG.md`](CHANGELOG.md) has the full list of changes.

## License

MIT
