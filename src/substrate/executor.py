"""
Safe command executor: the safety gate wired to subprocess execution.

Flow (this is the integration the original prototype was missing):

    propose(command) -> safety_level(command)
        SAFE               -> run immediately
        NEEDS_CONFIRMATION -> create a pending approval; do NOT run.
                              Once approved, run_approved(id) executes it.
        HARD_BLOCK         -> refuse; never run.

All executions are audited. Output is captured, truncated, and returned as
structured data so it can't silently blow up an agent's context or smuggle in a
huge prompt-injection payload.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field

from .approvals import ApprovalQueue
from .db import Database
from .safety import Level, safety_level

_MAX_OUTPUT = 20_000  # chars per stream returned to the agent


@dataclass
class ExecResult:
    status: str                       # ran | pending_approval | blocked | error
    verdict: str                      # SAFE | NEEDS_CONFIRMATION | HARD_BLOCK
    reason: str
    approval_id: int | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
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


class SafeExecutor:
    def __init__(self, db: Database, *, cwd: str | None = None, timeout: int = 60) -> None:
        self.db = db
        self.approvals = ApprovalQueue(db)
        self.cwd = cwd
        self.timeout = timeout

    def propose(self, command: str, *, actor: str = "agent") -> ExecResult:
        """Classify a command and either run it, queue it, or refuse it."""
        cls = safety_level(command)
        verdict = str(cls.level)

        if cls.level == Level.HARD_BLOCK:
            self.db.audit(actor, "exec.propose", command, verdict, "blocked")
            return ExecResult("blocked", verdict, cls.reason)

        if cls.level == Level.NEEDS_CONFIRMATION:
            aid = self.approvals.request(command, cls.reason, requested_by=actor)
            self.db.audit(actor, "exec.propose", command, verdict, f"pending:{aid}")
            return ExecResult("pending_approval", verdict, cls.reason, approval_id=aid)

        result = self._run(command)
        self.db.audit(actor, "exec.propose", command, verdict, f"ran:{result.returncode}")
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
        # record was somehow marked approved.
        cls = safety_level(appr["command"])
        if cls.level == Level.HARD_BLOCK:
            self.db.audit(actor, "exec.run_approved", appr["command"], str(cls.level), "blocked")
            return ExecResult("blocked", str(cls.level), cls.reason)
        result = self._run(appr["command"])
        self.db.audit(actor, "exec.run_approved", appr["command"], str(cls.level),
                      f"ran:{result.returncode}")
        return result

    def _run(self, command: str) -> ExecResult:
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return ExecResult("error", str(safety_level(command).level),
                              f"Could not parse command: {e}")
        if not argv:
            return ExecResult("error", "SAFE", "Empty command")
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.cwd,
                shell=False,           # never invoke a shell — no injection surface
                env=_scrubbed_env(),
            )
            return ExecResult(
                status="ran",
                verdict=str(safety_level(command).level),
                reason="Executed",
                returncode=proc.returncode,
                stdout=_truncate(proc.stdout),
                stderr=_truncate(proc.stderr),
            )
        except subprocess.TimeoutExpired:
            return ExecResult("error", "SAFE", f"Command timed out after {self.timeout}s")
        except FileNotFoundError:
            return ExecResult("error", "SAFE", f"Command not found: {argv[0]}")
        except Exception as e:  # pragma: no cover - defensive
            return ExecResult("error", "SAFE", str(e))
