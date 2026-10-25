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


def test_shell_operators_run_through_shell_after_gating(sub):
    res = sub.executor.propose("echo one && echo two")
    assert res.status == "ran"
    assert res.extra["mode"] == "shell"
    assert "one" in res.stdout and "two" in res.stdout


def test_shell_policy_never_refuses_operator_commands(sub, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_EXEC_SHELL", "never")
    res = sub.executor.propose("echo one | cat")
    assert res.status == "error"
    assert "SUBSTRATE_EXEC_SHELL" in res.reason


def test_plain_command_runs_without_shell(sub):
    res = sub.executor.propose("echo plain")
    assert res.extra["mode"] in ("argv", "cmd-builtin")
    assert "plain" in res.stdout


def test_approval_is_single_use(sub):
    res = sub.executor.propose("mv nonexistent_a nonexistent_b")
    aid = res.approval_id
    sub.approvals.decide(aid, approved=True)
    first = sub.executor.run_approved(aid)
    assert first.status in ("ran", "error")
    assert sub.approvals.get(aid)["status"] == "executed"
    second = sub.executor.run_approved(aid)
    assert second.status == "error"
    assert "executed" in second.reason


def test_approval_records_cwd_and_runs_there(sub, tmp_path):
    res = sub.executor.propose("mkdir made-here", cwd=str(tmp_path))
    assert res.status == "pending_approval"
    assert sub.approvals.get(res.approval_id)["cwd"] == str(tmp_path)
    sub.approvals.decide(res.approval_id, approved=True)
    out = sub.executor.run_approved(res.approval_id)
    assert out.status == "ran"
    assert (tmp_path / "made-here").is_dir()


def test_stale_approvals_expire(sub):
    res = sub.executor.propose("mv a b")
    assert sub.approvals.expire_stale(ttl_seconds=0) == 1
    assert sub.approvals.get(res.approval_id)["status"] == "expired"


def test_result_reports_duration(sub):
    res = sub.executor.propose("echo timed")
    assert res.duration_ms is not None and res.duration_ms >= 0
