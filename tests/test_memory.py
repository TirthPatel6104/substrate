import pytest

from substrate.memory import SecretRefused


def test_remember_and_recall(sub):
    sub.memory.remember("We deploy with make release", mem_type="project", scope="test")
    sub.memory.remember("Tests live in the tests/ directory", scope="test")
    results = sub.memory.recall("how do we deploy", scope="test", k=3)
    assert results
    assert any("make release" in r["content"] for r in results)


def test_recall_respects_scope(sub):
    sub.memory.remember("secret to project A", scope="proj-a")
    sub.memory.remember("global fact everyone sees", scope="global")
    results = sub.memory.recall("fact", scope="proj-b", k=5)
    contents = [r["content"] for r in results]
    assert "secret to project A" not in contents
    assert any("global fact" in c for c in contents)


def test_supersede_keeps_history_but_hides_from_recall(sub):
    old = sub.memory.remember("We use npm", mem_type="project", scope="test")
    sub.memory.supersede(old, "We use pnpm now", mem_type="project", scope="test")
    active = sub.memory.recall("package manager", scope="test", k=5)
    assert any("pnpm" in r["content"] for r in active)
    assert not any(r["content"] == "We use npm" for r in active)
    # But it is still reachable when explicitly including superseded memories.
    withhist = sub.memory.recall("npm", scope="test", k=5, include_superseded=True)
    assert any("We use npm" == r["content"] for r in withhist)


def test_secret_is_refused(sub):
    with pytest.raises(SecretRefused):
        sub.memory.remember("my aws key is AKIAIOSFODNN7EXAMPLE")
    with pytest.raises(SecretRefused):
        sub.memory.remember("password: hunter2secret")


def test_working_memory_expires(sub):
    sub.memory.remember("ephemeral note", mem_type="working", scope="test", ttl_seconds=-1)
    removed = sub.memory.expire_working()
    assert removed >= 1


def test_brief_structure_and_markdown(sub):
    sub.memory.remember("Uses Postgres 16", mem_type="project", scope="test")
    sub.memory.remember("Never force-push main", mem_type="semantic", scope="test", pinned=True)
    brief = sub.memory.brief("test")
    assert brief["scope"] == "test"
    assert any("Postgres" in m["content"] for m in brief["project"])
    md = sub.memory.render_brief_markdown("test")
    assert "# Project brief: test" in md
    assert "Postgres" in md


def test_usage_reinforcement(sub):
    mid = sub.memory.remember("reinforced fact about caching", scope="test")
    sub.memory.recall("caching", scope="test", k=1)
    row = sub.db.conn.execute("SELECT uses FROM memories WHERE id=?", (mid,)).fetchone()
    assert row["uses"] >= 1
