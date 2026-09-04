# Local-First Intelligence Layer for AI Agents — Product & Engineering Blueprint

> Working name used throughout: **Substrate** (rename freely).
> Related: the safety gate already on `master` (`safety_classification.py`, `execute.py`)
> is the seed of §11 (safety layer) — see [CODE_REVIEW.md](./CODE_REVIEW.md).

---

## 1. Project description (README-ready)

**Substrate is a local-first intelligence layer that makes AI agents smarter without
replacing them.** It runs as a small daemon on your machine and exposes memory, file
intelligence, task orchestration, and safe tool execution to any MCP-compatible agent —
Claude Code, Codex CLI, OpenCode, or your own. Agents connected to Substrate remember
decisions across sessions, understand your projects' file structure and history, pick up
long tasks where they left off, and execute local actions through a permission-aware
safety gate. Everything is stored on your disk in open formats (SQLite); nothing leaves
your machine unless you explicitly allow it.

## 2. Problem statement

Modern coding agents are strong reasoners attached to amnesiac, shallow environments:

- **Session amnesia.** Every session re-learns the project: conventions, past decisions,
  what was tried and failed. Context files (CLAUDE.md, AGENTS.md) are static, hand-written,
  and per-tool.
- **Shallow file awareness.** Agents grep on demand but hold no persistent model of the
  workspace — no notion of which files change together, which are stale, which relate to
  which task.
- **No cross-agent continuity.** Work started in Claude Code is invisible to Codex or
  OpenCode. Each tool keeps its own silo (or nothing).
- **Fragile long tasks.** Multi-step work dies on context overflow or session end; there
  is no durable task ledger an agent can resume from.
- **Ad-hoc execution safety.** Each agent re-implements (or skips) command safety;
  approvals don't persist, and there's no shared audit trail.

MCP standardized how agents *call tools*. Nobody has standardized the **stateful local
substrate underneath them** — memory, file model, task state, and execution policy shared
across every agent on the machine. That's the gap.

## 3. Why this matters

- Developers repeat the same explanations to their agent daily ("we use pnpm, tests live
  in `tests/`, don't touch the generated folder"). Real cost: minutes per session, every
  session, plus wrong actions when the agent guesses.
- Long refactors/migrations fail not from model weakness but from state loss — the agent
  forgets which of 40 files were already migrated.
- Troubleshooting knowledge evaporates: the same PATH/dependency issue is re-debugged from
  scratch a month later.
- Multi-tool users (Claude Code for deep work, a lighter agent for quick edits) get zero
  continuity between them.
- Teams can't audit what agents did on a machine, or enforce one execution policy across
  tools.

## 4. Core features (v1 scope)

1. **Persistent memory** — facts, decisions, preferences, and episode summaries with
   provenance, scoped global/project, retrievable by any connected agent.
2. **Local file intelligence** — a continuously updated index of watched folders:
   content hashes, symbols, embeddings, co-change history, staleness.
3. **Task orchestration** — a durable task ledger (goals → steps → status → artifacts)
   that survives sessions and transfers between agents.
4. **Desktop integration (gated)** — downloads triage, app/file launching, environment
   doctor (PATH/dependency diagnostics) as opt-in MCP tools.
5. **Knowledge graph / structured context** — typed edges between files, tasks, decisions,
   and memories (imports, co-edited-with, produced-by, supersedes).
6. **Multi-agent support** — one MCP server, many clients; per-agent identity in the audit
   log; shared state with optimistic locking.
7. **Safe tool execution** — deterministic SAFE / NEEDS_CONFIRMATION / HARD_BLOCK gate
   (already prototyped on `master`), persistent approvals, full audit trail.

## 5. Differentiation

| vs. | They are | Substrate is |
|---|---|---|
| **Claude Code / Codex / OpenCode** | The *reasoning agents* — the brains | The *stateful environment* they plug into; explicitly not an agent, has no chat loop, does no autonomous reasoning |
| **Memory-only tools (mem0, Letta/MemGPT, Zep, OpenMemory MCP, claude-mem)** | Store/retrieve conversational facts, mostly cloud-backed | Memory is one of four pillars; adds file intelligence, a task ledger, and an execution policy layer, all local-first in SQLite |
| **Desktop automation (AutoHotkey, Hammerspoon, Raycast, Shortcuts)** | Human-triggered macros | Agent-facing, permission-gated tools with audit; automation is initiated by an LLM and policed by a deterministic gate |
| **RAG frameworks (LlamaIndex etc.)** | Libraries you embed in *your* app | A running product agents connect to; indexing is continuous and shared, not per-app |
| **CLAUDE.md / AGENTS.md files** | Static, hand-written, per-tool context | Generated, continuously updated, queryable, and shared across tools (and can *emit* those files as an output format) |

The one-line positioning: **agents are the brains; Substrate is the hippocampus, filing
cabinet, and safety interlock they all share.**

## 6. Architecture

```
┌────────────────────────── Agents (MCP clients) ─────────────────────────┐
│   Claude Code      Codex CLI      OpenCode      Custom agents           │
└───────────────┬──────────────────────────────────────────┬──────────────┘
                │ MCP (stdio per-client → local daemon)     │
┌───────────────▼──────────────────────────────────────────▼──────────────┐
│                      Substrate daemon (single process)                   │
│                                                                          │
│  Integration layer   MCP server: memory.* files.* tasks.* exec.* desk.*  │
│  ──────────────────────────────────────────────────────────────────────  │
│  Retrieval layer     hybrid search (FTS5 + vectors + graph expansion),   │
│                      context assembly, token budgeting, reranking        │
│  ──────────────────────────────────────────────────────────────────────  │
│  Memory layer        working / episodic / semantic / task / project      │
│  File-intel layer    watcher → hasher → parser (tree-sitter) → embedder  │
│                      → graph builder (incremental, debounced)            │
│  Orchestration layer task ledger, routing table, handoff docs            │
│  ──────────────────────────────────────────────────────────────────────  │
│  Safety layer        deterministic gate + policy profiles + approvals    │
│                      queue + append-only audit log (wraps ALL writes/    │
│                      exec — nothing bypasses it)                         │
│  ──────────────────────────────────────────────────────────────────────  │
│  Storage             SQLite per workspace + one global DB                │
│                      (FTS5, sqlite-vec, edges table, WAL mode)           │
└──────────────────────────────────────────────────────────────────────────┘
        Optional: system tray / Tauri UI (memory browser, approvals, audit)
```

Key decisions:
- **One daemon, many MCP clients.** Each agent spawns a thin stdio shim that proxies to
  the daemon over a local socket — shared state, per-client identity.
- **SQLite everywhere.** FTS5 gives BM25; `sqlite-vec` gives vectors; a plain `edges`
  table gives the graph. No servers, no Docker, single-file backup, transparent to users.
- **The safety layer is not a module agents call — it's a chokepoint** every mutating or
  executing tool passes through.
- Local embedding model by default (ONNX, ~30 MB); cloud embeddings are an explicit opt-in
  per workspace.

## 7. Memory design

**Types** (one `memories` table, `type` column; different retention policies):

| Type | Contents | Lifetime |
|---|---|---|
| **Working** | Current-session scratch: open task, recent tool results, active files | TTL hours; promoted or dropped at session end |
| **Episodic** | Auto-generated session summaries: what was done, what failed, what was decided | Months; compacted into semantic facts over time |
| **Semantic** | Durable facts & decisions: "deploys use `make release`", "we chose Postgres over Mongo because X" | Indefinite until superseded |
| **Task** | Task ledger state: goal, plan, per-step status, artifacts, blockers | Until task closed + grace period |
| **Project** | Per-workspace profile: build/test/lint commands, conventions, layout map, gotchas | Continuously refreshed |

Every memory carries **provenance** (which agent, which session, which evidence),
**scope** (global / workspace / task), **confidence**, and timestamps.

**Retention & forgetting:**
- Score = recency-decayed importance × usage reinforcement (retrieved-and-used memories
  strengthen; never-retrieved ones decay).
- **Supersede, don't delete:** contradictions create a new memory with a `supersedes` edge;
  the old one is tombstoned (retrievable only on explicit history queries). This handles
  "we switched from npm to pnpm" correctly.
- Below-threshold episodic memories are compacted: an LLM pass distills N old episodes
  into a few semantic facts, then archives the episodes.
- Hard rules: secrets/credentials are refused at write time (regex + entropy detection);
  user can pin (never forget) or ban (never store) topics; everything is visible and
  deletable in the memory browser.

**Retrieval:**
- `memory.recall(query, scope, k)` — hybrid FTS5 + vector search, filtered by scope,
  boosted by score, then **one hop of graph expansion** (a retrieved decision pulls in the
  files/tasks it links to).
- `memory.brief(workspace)` — the killer API: a token-budgeted session-start pack
  (project profile + active tasks + recent episodes + top pinned facts), assembled in
  <100 ms, designed to be injected as the agent's first context. Also exportable as
  CLAUDE.md / AGENTS.md for tools that only read files.
- Time-travel: `as_of` parameter reconstructs what was known at a past date (tombstones
  make this cheap).

## 8. File intelligence design

**Pipeline (incremental, debounced):**
1. **Watch** registered roots (watchdog/FSEvents/inotify), respecting `.gitignore` +
   a Substrate ignore file; debounce bursts (builds touch thousands of files).
2. **Hash** changed files (BLAKE3); only re-process on content change. First scan of a
   big repo runs at low priority; the daemon is usable immediately with partial index.
3. **Parse** by type: tree-sitter for code (symbols, imports, docstrings); text/markdown
   chunked by headings; PDFs/office docs optional extractors; binaries → metadata only.
4. **Embed** chunks with the local model; store in `sqlite-vec` with model+version tagged
   (re-embedding is a migration, not a mystery).
5. **Graph** edges, typed:
   - `imports` / `imported_by` (from parsing),
   - `co_changed_with` (from git history + watcher deltas — files that change together),
   - `similar_to` (embedding proximity above threshold, computed lazily),
   - `derived_from` (build outputs, copies, `duplicate_of` via hash),
   - `touched_by_task` / `cited_by_decision` (from the task ledger and memory writes).
6. **Enrich** with staleness (mtime vs. references), hotness (edit frequency), and
   cluster labels (community detection over the edge graph → "auth subsystem",
   "billing docs" groupings, LLM-labeled once per cluster).

**Efficiency rules:** never re-embed unchanged content; cap per-file chunk counts;
lazy-compute `similar_to` only for files that get queried; nightly compaction job.
Target: a 100k-file monorepo indexes overnight, stays current in milliseconds per save.

## 9. Orchestration design

Substrate **routes and records; it does not reason.** The connected agent is always the
planner — this keeps Substrate model-agnostic and honest about what it is.

- **Task ledger** (`tasks.*` tools): `tasks.create(goal, plan?)`,
  `tasks.update(step, status, artifacts, notes)`, `tasks.blockers`, `tasks.resume(id)` →
  returns a **resume pack**: goal, remaining steps, decisions made, files touched, last
  error. This is what makes 40-file migrations survivable across sessions.
- **Routing table** (deterministic, config-driven): incoming requests are dispatched to
  the right internal capability — memory recall vs. file lookup vs. env doctor vs.
  execution — based on tool called + declared intent. No LLM in the routing path.
- **Troubleshooting playbooks**: parameterized diagnostic sequences (e.g. "module not
  found" → check venv active → check installed version → check PATH shadowing) that run
  read-only diagnostics and hand the agent a findings report. Past resolutions are stored
  as episodic memory, so the second occurrence of an issue retrieves the first fix.
- **Agent handoff**: `tasks.handoff(id, to_hint)` freezes a task and emits a handoff doc
  (goal, state, conventions, gotchas). Any other connected agent runs `tasks.resume(id)`
  and continues with full context. Locking: one writer per task at a time (lease-based),
  many readers.

## 10. Desktop integration

All desktop tools are **opt-in per workspace, permission-profiled, and audited.** OS
adapters (macOS/Windows/Linux) sit behind one interface; ship read-only versions first.

- **Downloads triage:** watch `~/Downloads`; on request (or rule), classify new files,
  suggest destinations, link them to active tasks ("this PDF belongs to the
  visa-application task"). Suggest-then-move, never silent-move.
- **Environment doctor:** read-only diagnostics for the classic time-sinks — PATH
  shadowing, wrong python/node resolved, missing compilers, version-manager conflicts,
  broken venvs. Emits a structured report the agent turns into a fix plan (fixes
  themselves go through the exec gate).
- **App/file actions:** open file in default app, reveal in Finder/Explorer, list running
  processes (read-only tier); move/rename/launch (confirmation tier).
- **Repetitive automation:** user-defined recipes ("when I say 'new invoice', copy the
  template, name it by date, open it") stored as parameterized workflows — agents can
  invoke them but not silently create them.

## 11. Safety and privacy

- **Local-first, verifiable:** all state in user-readable SQLite on disk; the daemon
  makes zero network calls in default config (local embeddings). Cloud anything —
  embeddings, sync — is per-workspace opt-in with a visible indicator.
- **Deterministic execution gate:** every `exec.*` call is classified
  SAFE / NEEDS_CONFIRMATION / HARD_BLOCK by pure logic before running — no LLM in the
  safety path. The prototype exists on `master`; harden per CODE_REVIEW.md (command
  substitution, `-v` false-safe, newline splitting, `git config`) and extend with:
  path confinement (writes only inside registered roots), per-workspace policy profiles
  (strict / standard / trusted), and rate limits.
- **Persistent approvals queue:** NEEDS_CONFIRMATION actions become pending records the
  user approves in the tray UI or CLI — sync `input()` prompts don't exist. Approvals can
  be remembered ("always allow `pnpm install` in this workspace") as auditable policy
  entries.
- **Sandboxing:** `shell=False` subprocess with scrubbed env (secrets stripped), CPU/time
  /output caps, optional OS-level sandbox (sandbox-exec / firejail / job objects) for the
  trusted-agent-untrusted-command case. Tool output returned to agents is truncated and
  fenced (tool output is a prompt-injection channel).
- **Data minimization:** secret detection refuses credential-looking content at memory
  write; per-path deny list for indexing (`~/.ssh`, keychains, browser profiles by
  default); retention windows per memory type; one-command full export and full wipe.
- **Audit:** append-only log of every tool call — which agent, which arguments, gate
  verdict, who approved. This is also the team/compliance story.

## 12. MVP roadmap

**MVP (4–6 weeks) — "memory + brief that works in Claude Code":**
- Daemon + MCP stdio server; SQLite storage; local embeddings.
- `memory.remember / recall / brief / forget`; project profile auto-built from repo
  inspection (build files, README, git config).
- File index v1: watch + hash + FTS5 + embeddings for 1 workspace; `files.search`,
  `files.similar`.
- Hardened exec gate wired to executor (fix the 4 known bypasses, pytest suite);
  `exec.run` with audit log. CLI for setup, memory browsing, approvals.
- Success metric: session #2 in Claude Code starts with correct project context and zero
  re-explanation.

**v1 (2–3 months) — "the task ledger + multi-agent":**
- Tasks API with resume packs and handoff; multi-client daemon with per-agent identity;
  graph edges (imports, co-change) + one-hop retrieval expansion; episodic auto-summaries
  + compaction; environment doctor (read-only); CLAUDE.md/AGENTS.md export; tray app with
  approvals queue and memory browser (Tauri).

**v2 (3–6 months):**
- Clustering + LLM-labeled file groups; troubleshooting playbooks with resolution memory;
  downloads triage; desktop action tier 2 (confirmation-gated moves/launches); policy
  profiles + OS sandboxing; multi-workspace global search; plugin API for custom tools.

**Long-term:**
- Optional E2E-encrypted sync between the user's own machines; team mode (shared project
  memory with roles + audit export); memory quality loop (contradiction detection,
  confidence calibration); adapters for non-MCP agents; "memory marketplace" of
  importable playbooks (e.g. framework-specific troubleshooting packs).

## 13. Recommended tech stack

| Layer | Pick | Why |
|---|---|---|
| Daemon language | **Python 3.12** for MVP (FastMCP + rich ML ecosystem); port hot paths to **Rust** later if needed | Speed of iteration beats raw perf until the index is big |
| MCP | **FastMCP / official `mcp` SDK** (stdio + streamable HTTP) | The integration surface *is* the product |
| Storage | **SQLite** (WAL) + **FTS5** + **sqlite-vec** | Zero-ops, single-file, local-first; handles millions of chunks |
| Graph | Plain `edges(src, dst, type, weight)` table + recursive CTEs; **NetworkX** in-process for clustering | Neo4j is operational overkill for a desktop daemon |
| Embeddings | **fastembed** (ONNX bge-small / nomic-embed-text, ~30–130 MB, CPU-fast); optional Ollama or cloud per workspace | Local by default is the promise |
| Parsing | **tree-sitter** (+ language packs), **watchdog** for FS events, **blake3** for hashing, **pathspec** for gitignore | Battle-tested, cross-platform |
| Orchestration | Plain asyncio + SQLite-backed queues; **APScheduler** for maintenance jobs | No Celery/Redis on a laptop |
| Validation | **Pydantic v2** everywhere (tool schemas, config, memory records) | Schema-first tools |
| CLI | **Typer + Rich** | Setup, doctor, memory browser, approvals |
| Desktop UI | **Tauri 2** (tray + approvals + browser); ship CLI-only MVP first | 10× lighter than Electron |
| Testing | **pytest** (+ property-based tests via Hypothesis for the safety gate), golden-set retrieval evals | The gate and retrieval both need regression harnesses |
| Packaging | **uv** + PyInstaller/briefcase single binary; signed installers later | Install friction kills desktop tools |

## 14. Example workflows

1. **Remember project context.** Monday: you tell Claude Code "we deploy with
   `make release`, never push to main directly." It calls `memory.remember` (semantic,
   workspace scope). Thursday, in OpenCode: session starts, the shim injects
   `memory.brief` — the rule is in context before you type anything. No re-explaining,
   and the wrong-push never happens.
2. **Understand a codebase.** You open a 3-year-old monorepo. The index already holds
   symbols, import graph, co-change clusters. Agent asks `files.map(depth=2)` +
   `files.clusters` and gets "6 subsystems, auth is the hottest, `legacy/` untouched for
   14 months, these 3 files change together 80% of the time." Orientation that took an
   afternoon of grepping takes one tool call.
3. **Group related files.** "Clean up my thesis folder." `files.similar` + `duplicate_of`
   edges surface 4 near-identical drafts, 12 figures unreferenced by any draft, and 3
   PDFs cited in your notes. The agent proposes a grouping; moves execute only after you
   approve the batch in the tray queue.
4. **Solve a troubleshooting issue.** `ModuleNotFoundError: numpy` — agent invokes the
   env-doctor playbook: detects the shell resolves system Python 3.9 while the venv is
   3.12, and PATH shadowing from an old pyenv shim. Report → agent proposes the fix →
   gate marks the PATH edit NEEDS_CONFIRMATION → you approve once. The resolution is
   stored; next month the same error retrieves the fix instantly.
5. **Complete a multi-step task.** "Migrate 40 endpoints to the new auth middleware."
   Agent creates a task with a 40-step checklist. Context dies at step 23. New session
   (any agent): `tasks.resume` returns the resume pack — 23 done, 17 remaining, the
   edge-case decision made at step 11, the file that needs special handling. The
   migration finishes instead of restarting.

---

## Tagline, pitch, risks

**Tagline:** *The memory and hands your AI agents are missing.*

**Elevator pitch:** Substrate is a local-first daemon that gives any MCP-compatible AI
agent persistent memory, deep file awareness, durable task state, and permission-gated
execution — so Claude Code, Codex, and OpenCode stop forgetting your project every
session and start finishing multi-step work safely.

**Biggest risks / challenges:**
1. **Platform absorption** — Anthropic/OpenAI ship native cross-session memory and file
   indexes, shrinking the gap. Mitigation: multi-agent neutrality, local-first privacy,
   and the task ledger + execution policy combo, which vendors are least likely to share
   across competitors' tools.
2. **Memory quality is the product** — stale or wrong memories are worse than none; the
   supersede/decay/provenance machinery must actually work, and needs its own eval
   harness from day one.
3. **Indexing at real-world scale** — monorepos, node_modules storms, cloud-synced
   folders; incremental correctness is hard and users will judge the whole product by
   whether the daemon stays under ~1% CPU.
4. **Security surface** — an execution layer that many agents share is a prompt-injection
   amplifier if the gate has holes (the current prototype demonstrably does — fix
   test-first). The gate must be boringly, provably conservative.
5. **Integration drift** — MCP is evolving and each agent injects context differently;
   the shim layer needs per-agent conformance tests.
6. **Trust/adoption friction** — a daemon that reads your filesystem must earn trust:
   open source, readable storage, visible audit, easy wipe are not optional features but
   the adoption strategy.
