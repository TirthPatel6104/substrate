# Code Review — `master` @ `dfc9fc5`

Companion to [BLUEPRINT.md](./BLUEPRINT.md) (the target architecture). This document reviews
what actually exists on `master` today.

> **Update:** the findings below have been addressed. The prototype was rebuilt into the
> `substrate` package (`src/substrate/`). Every bypass is now a passing regression test in
> `tests/test_safety.py`, the gate is wired to the executor (`src/substrate/executor.py`),
> the blocking `input()` approval became a persistent queue (`src/substrate/approvals.py`),
> and the unrelated cricket-data files were removed. Status per finding:
>
> - Command-substitution bypass — **fixed** (`_extract_substitutions`, recursive classify).
> - `-v` false-safe — **fixed** (rule 14 restricted to long flags with no other args).
> - Newlines not separators — **fixed** (newline added to `_SEPARATOR_RE`).
> - `git config` / `git bisect` misfiled — **fixed** (moved to confirm-level).
> - `bash -c "payload"` — **fixed** (rule 13 classifies the payload recursively).
> - `mkfs.*` variants — **fixed** (prefix match; caught by a test, not shipped broken).
> - Gate not wired to executor — **fixed** (`SafeExecutor.propose`).
> - `input()` approval — **fixed** (`ApprovalQueue`, decided out-of-band).
> - Output hygiene — **fixed** (truncated + scrubbed env in `executor._run`).
> - Repo hygiene / deps / tests — **fixed** (pyproject, `.gitignore`, pytest + CI).

## Inventory

| File | What it is | Verdict |
|---|---|---|
| `safety_classification.py` | 529-line deterministic shell-command safety gate (SAFE / NEEDS_CONFIRMATION / HARD_BLOCK) with a 20-case inline test harness | **The keeper.** Well-designed, LLM-independent guardrail for agentic tool execution |
| `execute.py` | `subprocess.run` tool wrapper for an AI agent (`shell=False`, timeout, structured result) | Sound skeleton, but **not wired to the safety gate** |
| `package_installer.py` | Package-install tool with console `input()` human approval | Right instinct (HITL), wrong mechanism (`input()` blocks; unusable in a service) |
| `main.py` | Pandas EDA over IPL cricket data, hardcoded `E:\` Windows path | Unrelated to the project; delete |
| `data/deliveries.csv` | 150k rows of IPL ball-by-ball cricket data | Unrelated; delete |
| `requirements.txt` | numpy, pandas, sklearn, tensorflow, torch, opencv, … | None of these are imported by real project code; replace |
| `__pycache__/*.pyc` | Committed bytecode | Remove; extend `.gitignore` |

**Headline finding:** there is no support-triage code. No LLM call, no ticket model, no
classification/routing/drafting anywhere. What exists instead is the beginning of a *safe
agentic tool-execution layer* — genuinely useful, but it is a component of the described
product, not the product.

## `safety_classification.py` — detailed review

Strengths: deterministic and always-on (no LLM in the safety path — correct design);
worst-sub-command-wins on chained commands; conservative default (unknown →
NEEDS_CONFIRMATION); reasons attached to every verdict; cross-platform awareness.

Issues found:

1. **Command-substitution bypass.** `$(...)` and backticks are not inspected:
   `echo $(rm -rf /)` tokenizes to base `echo` → SAFE. Same for `bash -c "rm -rf /"`
   (rule 17 returns NEEDS_CONFIRMATION for executing a script, but the embedded payload is
   never classified). Sub-shell content must be recursively extracted and classified.
2. **`-v` flag false-safe.** Rule 13 marks any *unknown* command carrying `-v`/`-h` as SAFE,
   but `-v` is not universally "version" (`someTool -v --purge-all` → SAFE). Restrict rule
   13 to `--version`/`--help` long flags, or require the flag be the *only* argument.
3. **Newlines are not separators.** `_split_subcommands` splits on `; && || |` but not
   `\n`, so a multi-line payload is classified only by its first effective segment.
4. **`git config` is misfiled as read-only.** It's in `_GIT_SAFE_SUBS`, but
   `git config core.sshCommand ...` or `alias.x '!cmd'` writes config and can plant command
   execution. `git bisect` also mutates state. Move both to confirm-level.
5. **No environment-variable expansion.** `rm -rf $TARGET` can't be resolved statically —
   fine — but the gate should flag unresolvable variables in destructive positions rather
   than treating `$TARGET` as an ordinary path.
6. **Inline print-based test harness.** Move the 20 cases to `pytest` parametrized tests so
   CI can gate on them; keep growing the case table with every bypass found (each of the
   items above should become a red test first).

## Integration gaps (the layer doesn't cohere yet)

- `execute.py` never calls `safety_level()` — the gate exists but nothing passes through
  it. The intended flow `agent proposes → classify → SAFE auto-run / CONFIRM ask human /
  BLOCK refuse` is not implemented.
- The gate classifies command *strings*, but `execute_agent_command` takes a *list* — the
  two interfaces don't compose without re-joining args into a string (lossy) or
  classifying pre-parse (better: classify the string the agent proposed, then execute the
  parsed list).
- `package_installer.py`'s `input()` approval cannot work in an async agent loop or a web
  service; approval must become a state ("pending approval") persisted somewhere a human
  can act on — which is exactly the human-in-the-loop review queue the triage product
  needs anyway.
- No output-size cap or sanitation on `stdout` returned to the agent loop — tool output is
  a prompt-injection channel and should be truncated and fenced.

## Immediate cleanup list

1. Delete `main.py`, `data/deliveries.csv`, `__pycache__/`; add `__pycache__/`, `*.pyc`,
   `.env` to `.gitignore`.
2. Replace `requirements.txt` with actual dependencies (today: nothing beyond stdlib;
   next: `anthropic`, `pydantic`, `fastapi`, `pytest`).
3. Wire `safety_level()` into `execute_agent_command()`; add pytest suite including the
   bypass cases above.
4. Restructure into a package (`src/agent_tools/`) with `pyproject.toml`, ruff, and CI.

## How this fits the triage product

The safety gate + executor is the right foundation for the *agentic actions* stage of the
triage platform (BLUEPRINT §4 long-term: auto-resolution with tool calling). The
three-level model maps directly onto ticket automation policy:

- SAFE → auto-execute (lookup order status, fetch subscription state)
- NEEDS_CONFIRMATION → queue for human approval (issue refund, change plan)
- HARD_BLOCK → never automated (account deletion, legal/security actions)

Build the triage core first (BLUEPRINT §8), then reuse this gate as the guardrail when
tickets start triggering real actions.
