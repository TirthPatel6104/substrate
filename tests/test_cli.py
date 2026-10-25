"""CLI smoke tests: every command goes through the same dispatch as MCP."""

import json

from substrate.cli import main


def _run(capsys, *argv, db):
    code = main(["--db", str(db), "-w", "cli-test", *argv])
    out = capsys.readouterr().out
    return code, out


def test_remember_brief_recall(tmp_path, capsys):
    db = tmp_path / "cli.db"
    code, out = _run(capsys, "remember", "We deploy with make release", "--type", "project", "--pin", db=db)
    assert code == 0 and json.loads(out)["id"] == 1
    code, out = _run(capsys, "brief", db=db)
    assert "make release" in out
    code, out = _run(capsys, "recall", "deploy", db=db)
    assert "make release" in out


def test_exec_classify_approvals_flow(tmp_path, capsys):
    db = tmp_path / "cli.db"
    _, out = _run(capsys, "classify", "rm -rf /", db=db)
    assert json.loads(out)["verdict"] == "HARD_BLOCK"
    _, out = _run(capsys, "exec", "mkdir cli-made", "--cwd", str(tmp_path), db=db)
    aid = json.loads(out)["approval_id"]
    _, out = _run(capsys, "approvals", db=db)
    assert json.loads(out)["pending"][0]["id"] == aid
    _, out = _run(capsys, "approve", str(aid), "--run", db=db)
    res = json.loads(out)
    assert res["decided"] is True and res["run"]["status"] == "ran"
    assert (tmp_path / "cli-made").is_dir()
    _, out = _run(capsys, "audit", db=db)
    assert any(r["tool"] == "exec.run_approved" for r in json.loads(out)["audit"])


def test_task_lifecycle(tmp_path, capsys):
    db = tmp_path / "cli.db"
    _, out = _run(capsys, "task", "ship it", "--step", "a", "--step", "b", db=db)
    tid = json.loads(out)["task_id"]
    _, out = _run(capsys, "resume", str(tid), db=db)
    pack = json.loads(out)
    step_id = pack["next_step"]["step_id"]
    _, out = _run(capsys, "step", str(step_id), "--status", "done", db=db)
    assert json.loads(out)["updated"] is True
    _, out = _run(capsys, "handoff", str(tid), "--to", "codex", "--note", "b is tricky", db=db)
    pack = json.loads(out)
    assert pack["handoff_to"] == "codex" and pack["progress"] == "1/2 steps done"


def test_index_search_stats(tmp_path, capsys):
    db = tmp_path / "cli.db"
    src = tmp_path / "src"
    src.mkdir()
    (src / "auth.py").write_text("def hash_password(pw):\n    return pw[::-1]\n", encoding="utf-8")
    _, out = _run(capsys, "index", str(src), db=db)
    assert json.loads(out)["indexed"] == 1
    _, out = _run(capsys, "search", "password hashing", db=db)
    assert json.loads(out)["results"][0]["path"].endswith("auth.py")
    _, out = _run(capsys, "stats", db=db)
    assert json.loads(out)["files"] == 1


def test_session_end_and_maintenance(tmp_path, capsys):
    db = tmp_path / "cli.db"
    _, out = _run(capsys, "session", "end", "--summary", "Refactored the gate", db=db)
    assert json.loads(out)["episode_id"]
    _, out = _run(capsys, "maintenance", db=db)
    assert "decayed" in json.loads(out)


def test_hook_stop_reads_transcript(tmp_path, capsys, monkeypatch):
    import io

    db = tmp_path / "cli.db"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "fix the bug"}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on it"}]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash"},
            {"type": "text", "text": "Fixed the null check in auth.py and added a test."}]}}),
    ]), encoding="utf-8")
    payload = json.dumps({"hook_event_name": "Stop", "transcript_path": str(transcript)})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    code, _ = _run(capsys, "hook", "stop", db=db)
    assert code == 0
    _, out = _run(capsys, "memories", "--type", "episodic", db=db)
    mems = json.loads(out)["memories"]
    assert any("Fixed the null check" in m["content"] for m in mems)


def test_hook_session_start_prints_brief(tmp_path, capsys):
    db = tmp_path / "cli.db"
    _run(capsys, "remember", "pinned rule", "--pin", db=db)
    code, out = _run(capsys, "hook", "session-start", db=db)
    assert code == 0 and "pinned rule" in out


def test_doctor_passes(tmp_path, capsys):
    code = main(["--db", str(tmp_path / "d.db"), "-w", "doc", "doctor"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "[PASS] safety gate: rm -rf / blocked" in out
    assert "mcpServers" in out
