import textwrap


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def test_index_and_search(sub, tmp_path):
    _write(tmp_path, "auth.py", """
        import hashlib
        def hash_password(pw):
            return hashlib.sha256(pw.encode()).hexdigest()
    """)
    _write(tmp_path, "billing.py", """
        def charge_customer(amount):
            return {"charged": amount}
    """)
    stats = sub.files.index_path(tmp_path)
    assert stats["indexed"] == 2

    results = sub.files.search("password hashing", k=5)
    assert results
    assert results[0]["path"].endswith("auth.py")


def test_reindex_unchanged_is_skipped(sub, tmp_path):
    _write(tmp_path, "a.py", "x = 1\n")
    first = sub.files.index_path(tmp_path)
    assert first["indexed"] == 1
    second = sub.files.index_path(tmp_path)
    assert second["unchanged"] == 1
    assert second["indexed"] == 0


def test_import_edges_are_built(sub, tmp_path):
    _write(tmp_path, "mod.py", "import os\nfrom collections import defaultdict\n")
    sub.files.index_path(tmp_path)
    edges = sub.db.conn.execute(
        "SELECT dst FROM edges WHERE type='imports'"
    ).fetchall()
    dsts = {e["dst"] for e in edges}
    assert "module:os" in dsts
    assert "module:collections" in dsts


def test_similar_files(sub, tmp_path):
    _write(tmp_path, "one.py", "def add(a, b):\n    return a + b\n")
    _write(tmp_path, "two.py", "def add_numbers(a, b):\n    return a + b\n")
    _write(tmp_path, "unrelated.md", "# Shopping list\n- milk\n- eggs\n")
    sub.files.index_path(tmp_path)
    similar = sub.files.similar_files(tmp_path / "one.py", k=5)
    paths = [s["path"] for s in similar]
    assert any(p.endswith("two.py") for p in paths)
