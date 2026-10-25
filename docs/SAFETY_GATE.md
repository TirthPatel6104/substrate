# The safety gate: deterministic command classification for agents

Every shell command an agent proposes through Substrate passes through one
function, `safety_level(command)`, before anything runs. It returns one of
three verdicts and a reason:

| Verdict | Meaning | What happens |
|---|---|---|
| `SAFE` | read-only or informational | runs immediately |
| `NEEDS_CONFIRMATION` | changes state, recoverable | queued; a human approves or denies |
| `HARD_BLOCK` | potentially irreversible | refused; never runs, even if "approved" |

This document explains why the gate is built the way it is, what it defends
against, how it is tested, and where its limits are.

## Why not ask the model?

The obvious design is to ask an LLM "is this command dangerous?". We rejected it:

1. **The gate must hold when the model is the adversary.** Prompt injection
   through a file the agent read is the realistic threat; an LLM judge sits in
   the same blast radius as the agent it is judging.
2. **Determinism is testable.** A regression corpus of 178 commands, each with
   an expected verdict, runs in CI in under a second. There is no equivalent
   guarantee for a model whose weights or sampling can change.
3. **Latency and cost.** Classification takes about 55 µs at p50. An LLM call
   is five orders of magnitude slower and costs money per command.
4. **Auditability.** Each verdict carries a human-readable reason that is
   written to the audit log. "The model said no" is not a reason.

The trade-off is recall of novel dangers: a rule-based gate only knows what it
has been taught. We compensate with a conservative default (unknown command →
`NEEDS_CONFIRMATION`), and by treating every bypass we find as a permanent
regression test.

## Threat model

Danger is modelled as **operation × target × reversibility**, not as a keyword
list:

- `rm foo.txt` is recoverable → confirm. `rm -rf /` is not → block.
- `cp a b` is a copy → confirm. `cp /dev/zero /dev/sda` writes a device → block.
- `git branch` reads → safe. `git branch -D main` deletes a ref → confirm.
- `find . -name '*.pyc' -delete` is routine cleanup → confirm. `find . -delete`
  with no filter deletes everything under the root → block.

The gate assumes the command string is hostile and may be trying to hide a
dangerous operation from a naive parser. The shapes it specifically defeats:

| Evasion | Example | Handling |
|---|---|---|
| Chaining | `ls && rm -rf /`, `ls & rm -rf /` | split on `; && \|\| \| &` and newlines; worst segment wins |
| Quoting | `grep "a\|b" f` vs `python -c 'a; b'` | quote-aware splitter: separators inside quotes are literal |
| Command substitution | `echo $(rm -rf /)`, `` `rm -rf /` ``, `cat <(rm -rf /)` | inner command extracted and classified first |
| Sub-shells / groups | `(rm -rf /)`, `{ rm -rf /; }`, `true && (rm -rf /)` | unwrapped and classified |
| Wrappers | `sudo -u root`, `env FOO=bar`, `timeout 5`, `nohup`, `nice -n 10` | wrapper and its own flags stripped; real command classified |
| Executors | `eval 'rm -rf /'`, `xargs rm -rf < list`, `find . -exec rm {} \;` | payload extracted and classified; deletes over an unverifiable list are blocked |
| Shell `-c` | `bash -c "rm -rf /"`, nested `bash -c 'bash -c "..."'`, glued `-c"..."` | payload classified recursively (depth-limited) |
| Interpreter `-c`/`-e` | `python -c 'os.system("rm -rf /")'`, `node -e '...execSync("rm -rf /")'` | string literals inside the program are classified as shell; shell-out APIs force confirm |
| Encoded payloads | `powershell -EncodedCommand ...` | cannot be inspected → block |
| Pipe to interpreter | `curl ... \| bash`, `iwr ... \| iex`, `wget -qO- \| sudo sh` | block, checked on the whole string before splitting |
| Redirects | `echo x > /etc/passwd`, `&> /dev/sda` | target path checked; `2>&1`, `>&2` are not redirects |
| Unresolvable variables | `rm -rf $DIR/`, `rsync --delete src $DEST` | an empty variable would target the root → block |
| Windows paths | `Remove-Item -Recurse -Force C:\Windows` | non-POSIX tokenisation when a Windows path is present, so `C:\Windows` is not read as `C:Windows` |
| Flag clusters and order | `rm -Rf`, `rm -fr`, `docker system prune -af` | letters inside short clusters are inspected |
| Case | `RM -RF /` | base command compared case-insensitively |

Critical paths are split into **exact roots** (`/`, `~`, `C:`, `$HOME`) and
**system directories** matched by prefix (`/etc`, `/usr`, `C:\Windows`, …).
Home roots (`/home`, `C:\Users`) are critical only one level deep: deleting
`/home/alice` is blocked, deleting `/home/alice/proj/build` is confirm. Earlier
versions prefix-matched `C:` and `~`, which blocked every path on the drive;
the regression corpus now pins the correct behaviour.

Beyond the shell, the gate knows the destructive verbs of `git`, package
managers (`pip`, `npm`, `apt`, `brew`, `cargo`, …), service managers, the
Windows registry, PowerShell cmdlets, and infrastructure CLIs: `docker system
prune -af`, `terraform destroy`, `terraform apply -auto-approve`,
`kubectl delete ns`, `aws s3 rb --force` are all `HARD_BLOCK`.

## How it is tested

Three layers, all in CI on Linux, macOS and Windows:

1. **Regression corpus** — `tests/corpus/commands.jsonl`. One JSON line per
   command with the expected verdict and tags. Every bypass that was ever
   found is in there tagged `bypass`, and a dedicated test asserts none of
   them is `SAFE`. `scripts/gate_report.py` renders the corpus as the table
   below and fails CI on any mismatch.
2. **Property-based tests** — `tests/test_safety_properties.py` (Hypothesis).
   Instead of individual commands, these pin invariants over generated input:
   - totality: never raises, always returns a verdict;
   - determinism;
   - monotonicity: chaining a dangerous command onto anything, with any
     separator, in either order, cannot lower the verdict;
   - wrapper-proofing: `sudo`, `env`, `timeout`, `bash -c`, `eval`, `$( )`
     cannot lower the verdict;
   - case and whitespace cannot lower the verdict;
   - read-only commands stay `SAFE` with innocuous extra arguments (false
     positives are tested too, because a gate that cries wolf gets bypassed by
     its users).
3. **Executor tests** — the gate is only useful if it is actually in the path.
   `tests/test_executor.py` proves `HARD_BLOCK` never reaches `subprocess`,
   `NEEDS_CONFIRMATION` creates a pending approval instead of running, and a
   verdict is **re-computed at execution time** so a row that was somehow
   marked approved in the database still cannot run a blocked command.

Current corpus report (`python scripts/gate_report.py`):

| Metric | Value |
|---|---|
| Commands in corpus | 178 |
| SAFE / NEEDS_CONFIRMATION / HARD_BLOCK | 33 / 46 / 99 |
| Historical bypasses pinned as regressions | 29 |
| Windows / PowerShell cases | 22 |
| Container / cloud / IaC cases | 18 |
| Mismatches | 0 |
| False-SAFE (dangerous command marked SAFE) | 0 |
| Classification latency p50 / p95 | 55 µs / 145 µs |

## From verdict to execution

```
agent ──exec.propose(cmd)──▶ safety_level(cmd)
                                 │
         SAFE ───────────────────┼──▶ run now (argv, no shell) ──▶ audit
         NEEDS_CONFIRMATION ─────┼──▶ approvals row (pending) ──▶ human decides
                                 │        └─ exec.run_approved ──▶ re-classify ──▶ run ──▶ audit
         HARD_BLOCK ─────────────┴──▶ refuse ──▶ audit
```

Execution details that matter for safety:

- **Same tokenizer, both sides.** The executor splits the command with the
  gate's own tokenizer, so what was judged is what runs. This closes the
  classic parser-differential gap where the gate sees one command and the
  shell runs another.
- **No shell unless needed.** A plain command runs as an argv list with
  `shell=False`. Only commands that contain shell operators (`|`, `&&`,
  redirects, substitution) are handed to the platform shell, and only after
  every segment was classified. `SUBSTRATE_EXEC_SHELL=never` refuses those
  outright for the most conservative deployments.
- **Approvals are single-use** and record the directory they were proposed
  from, so an approval for `rm -rf build` in one project cannot be replayed
  elsewhere. Stale approvals expire.
- **Output is capped** (20 kB per stream) and the subprocess environment is
  scrubbed of variables whose names look like secrets. Tool output is a
  prompt-injection channel; a 5 MB stdout is a context-overflow attack.

## Known limitations

Being honest about these is part of the design.

- **Static analysis of a dynamic language.** The gate cannot evaluate
  `rm -rf $(compute_path)`; it blocks the pattern instead. `python script.py`
  is confirm-level because the script's contents are unknown. A truly
  determined payload can hide behind indirection the gate cannot see, which
  is why the approvals queue and the audit log exist.
- **Parser differential remains possible** for exotic quoting that the
  quote-aware splitter and `/bin/sh` disagree on. Mitigations: unbalanced
  quotes fall back to over-splitting (more segments classified, never fewer),
  and the shell is only invoked when operators are present.
- **Allow/deny lists age.** New destructive tools appear. Unknown commands
  default to confirm, so the failure mode is friction, not a silent run.
- **It is a command gate, not a sandbox.** It does not restrict what an
  approved command can touch. Pair it with OS-level sandboxing for untrusted
  workloads.

## Adding a case

1. Add a line to `tests/corpus/commands.jsonl` with the command, the verdict
   it *should* get, and tags (`bypass` if it was previously `SAFE`).
2. Run `pytest tests/test_corpus.py` and watch it fail.
3. Fix `safety.py`. Prefer extending a structural rule (wrapper stripping,
   target checks) over adding a keyword.
4. Run `python scripts/gate_report.py` and update the table above.
