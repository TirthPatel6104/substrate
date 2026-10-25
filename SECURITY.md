# Security

## Scope

Substrate executes shell commands on behalf of AI agents after classifying
them. The security-relevant surface is:

1. the safety gate (`src/substrate/safety.py`) — a command that is destructive
   but classified `SAFE` is a vulnerability;
2. the executor (`src/substrate/executor.py`) — any way to run a command the
   gate did not judge, or to run a `HARD_BLOCK` verdict;
3. the HTTP daemon (`src/substrate/httpd.py`) — any way for a remote origin to
   approve, run or read without the token / same-origin checks;
4. the memory store — any way for a stored secret to be written despite the
   secret filter, or read across workspace scopes.

Out of scope: damage done by a command a human explicitly approved, and
OS-level sandboxing (the gate is a classifier, not a sandbox).

## Reporting

Email the maintainer (see `git log` for the address) with the command or
request that demonstrates the issue. Please do not open a public issue for a
gate bypass until a fix is released. You will get an acknowledgement within a
few days, and the fix will land as a corpus line plus a code change, credited
to you unless you prefer otherwise.

## Design notes relevant to reviewers

- The gate never calls an LLM and never crashes open: an internal error yields
  `NEEDS_CONFIRMATION`, never `SAFE`.
- Verdicts are recomputed at execution time; an edited `approvals` row cannot
  run a blocked command.
- The daemon binds `127.0.0.1` by default. Browser-driven state changes
  require same-origin; `/mcp` requires `Content-Type: application/json`.
- The subprocess environment is scrubbed of variables whose names contain
  `KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `PASSWD`, `CREDENTIAL`.
- Known limitations are listed in [docs/SAFETY_GATE.md](docs/SAFETY_GATE.md#known-limitations).
