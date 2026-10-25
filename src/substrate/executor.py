"""
Safe command executor: the safety gate wired to subprocess execution.

Flow:

    propose(command) -> safety_level(command)
        SAFE               -> run immediately
        NEEDS_CONFIRMATION -> create a pending approval; do NOT run.
                              Once approved, run_approved(id) executes it.
        HARD_BLOCK         -> refuse; never run.

Execution model
---------------
* A plain command (``ls -la``) is tokenised with the *same* tokenizer the gate
  used and executed with ``shell=False``: no shell, no injection surface.
* A command that needs shell operators (``ps aux | grep x``, ``a && b``) is
  handed to the platform shell *only after* the gate classified every segment.
  The gate is the chokepoint; the shell is just the runtime. Set
  ``SUBSTRATE_EXEC_SHELL=never`` to refuse such commands outright.
* On Windows, ``cmd`` builtins (``dir``, ``type``, ``copy``...) have no
  executable and are routed through ``cmd /d /c``.

All executions are audited. Output is captured, truncated, and returned as
structured data so it can't silently blow up an agent's context or smuggle in
a huge prompt-injection payload.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

from .approvals import ApprovalQueue
from .db import Database
from .safety import Level, has_shell_operators, safety_level, tokenize_command

_MAX_OUTPUT = 20_000  # chars per stream returned to the agent

_CMD_BUILTINS = {
    'dir', 'type', 'copy', 'move', 'del', 'erase', 'md', 'mkdir', 'rd', 'rmdir',
    'ren', 'rename', 'echo', 'set', 'ver', 'vol', 'cd', 'cls', 'date', 'time',
    'path', 'where', 'tasklist', 'more',
}


@dataclass
class ExecResult:
    status: str                       # ran | pending_approval | blocked | error
    verdict: str                      # SAFE | NEEDS_CONFIRMATION | HARD_BLOCK
    reason: str
    approval_id: int | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "verdict": self.verdict,
            "reason": self.reason,
            "approval_id": self.approval_id,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            **self.extra,
        }


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + f"\n...[truncated {len(text) - _MAX_OUTPUT} chars]"


def _scrubbed_env() -> dict:
    """Environment with obvious secrets stripped before handing to a subprocess."""
    unsafe = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL")
    return {k: v for k, v in os.environ.items() if not any(u in k.upper() for u in unsafe)}


def _shell_policy() -> str:
    """'auto' (default) runs operator commands via the shell; 'never' refuses them."""
    return os.environ.get("SUBSTRATE_EXEC_SHELL", "auto").lower()


def _shell_argv(command: str) -> list[str]:
    if sys.platform == "win32":
        return ["cmd", "/d", "/c", command]
    return ["/bin/sh", "-c", command]


class SafeExecutor:
    def __init__(self, db: Database, *, cwd: str | None = None, timeout: int = 60) -> None:
        self.db = db
        self.approvals = ApprovalQueue(db)
        self.cwd = cwd
        self.timeout = timeout

    # -- public API -----------------------------------------------------------
    def propose(self, command: str, *, actor: str = "agent", cwd: str | None = None,
                timeout: int | None = None) -> ExecResult:
        """Classify a command and either run it, queue it, or refuse it."""
        cls = safety_level(command)
        verdict = str(cls.level)

        if cls.level == Level.HARD_BLOCK:
            self.db.audit(actor, "exec.propose", command, verdict, "blocked")
            return ExecResult("blocked", verdict, cls.reason)

        if cls.level == Level.NEEDS_CONFIRMATION:
            aid = self.approvals.request(command, cls.reason, requested_by=actor, cwd=cwd)
            self.db.audit(actor, "exec.propose", command, verdict, f"pending:{aid}")
            return ExecResult("pending_approval", verdict, cls.reason, approval_id=aid)

        result = self._run(command, verdict, cwd=cwd, timeout=timeout)
        self.db.audit(actor, "exec.propose", command, verdict, f"{result.status}:{result.returncode}")
        return result

    def run_approved(self, approval_id: int, *, actor: str = "user") -> ExecResult:
        """Execute a command that a human has approved in the queue."""
        appr = self.approvals.get(approval_id)
        if appr is None:
            return ExecResult("error", "-", f"No approval with id {approval_id}")
        if appr["status"] != "approved":
            return ExecResult(
                "error", "-",
                f"Approval {approval_id} is '{appr['status']}', not 'approved'",
            )
        # Re-classify at execution time: a HARD_BLOCK must never run even if a
        # record was somehow marked approved (defense in depth against DB edits).
        cls = safety_level(appr["command"])
        if cls.level == Level.HARD_BLOCK:
            self.db.audit(actor, "exec.run_approved", appr["command"], str(cls.level), "blocked")
            return ExecResult("blocked", str(cls.level), cls.reason)
        result = self._run(appr["command"], str(cls.level), cwd=appr.get("cwd"))
        self.approvals.mark_executed(approval_id)
        self.db.audit(actor, "exec.run_approved", appr["command"], str(cls.level),
                      f"{result.status}:{result.returncode}")
        return result

    # -- execution ------------------------------------------------------------
    def _run(self, command: str, verdict: str, *, cwd: str | None = None,
             timeout: int | None = None) -> ExecResult:
        needs_shell = has_shell_operators(command)
        if needs_shell:
            if _shell_policy() == "never":
                return ExecResult("error", verdict,
                                  "Command uses shell operators and SUBSTRATE_EXEC_SHELL=never")
            argv = _shell_argv(command)
            mode = "shell"
        else:
            argv = tokenize_command(command)
            if not argv:
                return ExecResult("error", verdict, "Empty command")
            if sys.platform == "win32" and argv[0].lower() in _CMD_BUILTINS:
                argv = _shell_argv(command)
                mode = "cmd-builtin"
            else:
                mode = "argv"

        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout or self.timeout,
                cwd=cwd or self.cwd,
                shell=False,           # we pass the shell binary explicitly when needed
                env=_scrubbed_env(),
            )
            return ExecResult(
                status="ran",
                verdict=verdict,
                reason="Executed",
                returncode=proc.returncode,
                stdout=_truncate(proc.stdout),
                stderr=_truncate(proc.stderr),
                duration_ms=int((time.perf_counter() - t0) * 1000),
                extra={"mode": mode},
            )
        except subprocess.TimeoutExpired:
            return ExecResult("error", verdict, f"Command timed out after {timeout or self.timeout}s",
                              duration_ms=int((time.perf_counter() - t0) * 1000))
        except FileNotFoundError:
            return ExecResult("error", verdict, f"Command not found: {argv[0]}")
        except (OSError, ValueError) as e:
            return ExecResult("error", verdict, str(e))
