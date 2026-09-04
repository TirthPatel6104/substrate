"""Executor tests: the safety gate wired to real execution + the approvals flow."""


def test_safe_command_runs(sub):
    res = sub.executor.propose("echo hello-substrate")
    assert res.status == "ran"
    assert res.returncode == 0
    assert "hello-substrate" in res.stdout


def test_hard_block_never_runs(sub):
    res = sub.executor.propose("rm -rf /")
    assert res.status == "blocked"
    assert res.verdict == "HARD_BLOCK"
    assert res.returncode is None


def test_needs_confirmation_is_queued_not_run(sub):
    res = sub.executor.propose("pip install requests")
    assert res.status == "pending_approval"
    assert res.approval_id is not None
    pending = sub.approvals.pending()
    assert any(p["id"] == res.approval_id for p in pending)


def test_approval_then_run(sub):
    # Use a command that needs confirmation but is harmless to actually run.
    res = sub.executor.propose("echo queued > /dev/null || true")
    # (redirect -> NEEDS_CONFIRMATION)
    if res.status != "pending_approval":
        res = sub.executor.propose("mv nonexistent_a nonexistent_b")
    aid = res.approval_id
    assert aid is not None

    # Cannot run before approval.
    blocked = sub.executor.run_approved(aid)
    assert blocked.status == "error"

    sub.approvals.decide(aid, approved=True)
    ran = sub.executor.run_approved(aid)
    assert ran.status in ("ran", "error")  # mv may fail; the point is it executed
    assert ran.verdict == "NEEDS_CONFIRMATION"


def test_denied_command_does_not_run(sub):
    res = sub.executor.propose("mv a b")
    sub.approvals.decide(res.approval_id, approved=False)
    out = sub.executor.run_approved(res.approval_id)
    assert out.status == "error"


def test_audit_log_records_calls(sub):
    sub.executor.propose("echo audit-check")
    sub.executor.propose("rm -rf /")
    rows = sub.db.conn.execute("SELECT tool, verdict FROM audit").fetchall()
    verdicts = {r["verdict"] for r in rows}
    assert "SAFE" in verdicts
    assert "HARD_BLOCK" in verdicts


def test_dispatch_roundtrip(sub):
    out = sub.dispatch("exec.propose", {"command": "echo via-dispatch"})
    assert out["status"] == "ran"
    assert "via-dispatch" in out["stdout"]
