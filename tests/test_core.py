"""Core composite behaviour: brief with tasks, sessions, maintenance, error codes."""


def test_brief_includes_open_tasks_and_pending_approvals(sub):
    sub.dispatch("tasks.create", {"goal": "Migrate endpoints", "steps": ["a", "b"]}, actor="A")
    sub.dispatch("exec.propose", {"command": "pip install x"}, actor="A")
    b = sub.brief("test")
    assert b["open_tasks"] and b["open_tasks"][0]["goal"] == "Migrate endpoints"
    assert b["open_tasks"][0]["progress"] == "0/2 steps done"
    assert b["pending_approvals"] == 1
    md = sub.brief_markdown("test")
    assert "## Open tasks" in md and "Migrate endpoints" in md
    assert "Pending approvals: 1" in md


def test_session_end_records_episode_and_compacts_working(sub):
    sub.memory.remember("tried approach X, failed", mem_type="working", scope="test")
    sub.memory.remember("approach Y works", mem_type="working", scope="test")
    out = sub.dispatch("session.end", {"summary": "Fixed the auth bug via approach Y"},
                       actor="claude-code")
    assert out["episode_id"] and out["compacted_into"]
    episodes = sub.memory.list(scope="test", mem_type="episodic")
    contents = " ".join(e["content"] for e in episodes)
    assert "Fixed the auth bug" in contents and "approach Y works" in contents
    # Working notes are superseded by the compaction summary, not visible any more.
    assert sub.memory.list(scope="test", mem_type="working") == []


def test_session_end_rejects_empty_summary(sub):
    out = sub.dispatch("session.end", {"summary": "   "})
    assert out["code"] == "bad_request"


def test_maintenance_runs_all_jobs(sub):
    sub.memory.remember("ttl note", mem_type="working", scope="test", ttl_seconds=-1)
    out = sub.dispatch("maintenance.run", {})
    assert set(out) >= {"expired_working", "compacted_into", "decayed", "expired_approvals",
                        "released_leases", "reembedded_memories"}
    assert out["expired_working"] >= 1


def test_unknown_tool_and_missing_param_codes(sub):
    assert sub.dispatch("nope.tool", {})["code"] == "unknown_tool"
    assert sub.dispatch("memory.recall", {})["code"] == "bad_request"


def test_exec_classify_is_dry_run(sub):
    out = sub.dispatch("exec.classify", {"command": "rm -rf /"})
    assert out["verdict"] == "HARD_BLOCK"
    assert sub.db.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0


def test_memory_supersede_via_dispatch(sub):
    old = sub.dispatch("memory.remember", {"content": "We use npm", "type": "project"})["id"]
    new = sub.dispatch("memory.supersede", {"id": old, "content": "We use pnpm"})["id"]
    active = sub.memory.list(scope="test")
    ids = {m["id"] for m in active}
    assert new in ids and old not in ids
    assert any(m["content"] == "We use pnpm" for m in active)


def test_dispatch_is_thread_safe(sub):
    import threading

    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for i in range(20):
                sub.dispatch("memory.remember", {"content": f"fact {n}-{i}"}, actor=f"t{n}")
                sub.dispatch("memory.recall", {"query": "fact"}, actor=f"t{n}")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sub.db.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 80
