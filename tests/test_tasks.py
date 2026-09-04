def test_create_resume_and_progress(sub):
    tid = sub.tasks.create("Migrate 3 endpoints", ["endpoint A", "endpoint B", "endpoint C"])
    pack = sub.tasks.resume(tid)
    assert pack["progress"] == "0/3 steps done"
    assert len(pack["remaining_steps"]) == 3

    steps = sub.db.conn.execute(
        "SELECT id FROM task_steps WHERE task_id=? ORDER BY ordinal", (tid,)
    ).fetchall()
    sub.tasks.update_step(steps[0]["id"], status="done")
    sub.tasks.update_step(steps[1]["id"], status="done", notes="tricky auth edge case handled")

    pack = sub.tasks.resume(tid)
    assert pack["progress"] == "2/3 steps done"
    assert len(pack["remaining_steps"]) == 1
    assert "tricky auth edge case handled" in pack["notes"]


def test_autocomplete_when_all_steps_done(sub):
    tid = sub.tasks.create("Small task", ["only step"])
    step = sub.db.conn.execute(
        "SELECT id FROM task_steps WHERE task_id=?", (tid,)
    ).fetchone()
    sub.tasks.update_step(step["id"], status="done")
    pack = sub.tasks.resume(tid)
    assert pack["status"] == "done"


def test_lease_prevents_second_writer(sub):
    tid = sub.tasks.create("Shared task")
    assert sub.tasks.acquire_lease(tid, "agent-1") is True
    assert sub.tasks.acquire_lease(tid, "agent-2") is False
    # Same owner can re-acquire (extend) its own lease.
    assert sub.tasks.acquire_lease(tid, "agent-1") is True


def test_handoff_releases_lease(sub):
    tid = sub.tasks.create("Handme task", ["step"])
    sub.tasks.acquire_lease(tid, "agent-1")
    pack = sub.tasks.handoff(tid, to_hint="agent-2")
    assert pack["handoff_to"] == "agent-2"
    # Lease released -> another agent can now take it.
    assert sub.tasks.acquire_lease(tid, "agent-2") is True


def test_list_open_excludes_done(sub):
    a = sub.tasks.create("open one")
    b = sub.tasks.create("done one")
    sub.tasks.set_status(b, "done")
    open_ids = {t["id"] for t in sub.tasks.list_open()}
    assert a in open_ids
    assert b not in open_ids
