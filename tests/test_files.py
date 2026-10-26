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


def test_removed_files_are_pruned(sub, tmp_path):
    _write(tmp_path, "gone.py", "x = 1\n")
    _write(tmp_path, "stay.py", "y = 2\n")
    sub.files.index_path(tmp_path)
    (tmp_path / "gone.py").unlink()
    stats = sub.files.index_path(tmp_path)
    assert stats["removed"] == 1
    assert sub.files.stats()["files"] == 1
    assert sub.db.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1


def test_gitignore_is_honoured(sub, tmp_path):
    (tmp_path / ".gitignore").write_text("generated/\n*.log\n", encoding="utf-8")
    (tmp_path / "generated").mkdir()
    _write(tmp_path, "generated/big.py", "x = 1\n")
    _write(tmp_path, "notes.log", "ignored\n")
    _write(tmp_path, "keep.py", "y = 2\n")
    stats = sub.files.index_path(tmp_path)
    assert stats["indexed"] == 1
    paths = [r[0] for r in sub.db.conn.execute("SELECT path FROM files")]
    assert len(paths) == 1 and paths[0].endswith("keep.py")


def test_similarity_edges_update_incrementally(sub, tmp_path):
    _write(tmp_path, "one.py", "def add(a, b):\n    return a + b\n")
    _write(tmp_path, "two.py", "def add_numbers(a, b):\n    return a + b\n")
    sub.files.index_path(tmp_path)
    assert sub.files.similar_files(tmp_path / "one.py")[0]["path"].endswith("two.py")
    # The lookup is cached as an edge ...
    cached = sub.db.conn.execute("SELECT COUNT(*) FROM edges WHERE type='similar_to'").fetchone()[0]
    assert cached == 1
    # ... and changing two.py to something unrelated invalidates it.
    _write(tmp_path, "two.py", "# Shopping list\n- milk\n- eggs\n- bread\n")
    sub.files.index_path(tmp_path)
    assert sub.files.similar_files(tmp_path / "one.py") == []


def test_v3_database_gets_centroids_backfilled(sub, tmp_path):
    _write(tmp_path, "a.py", "def add(a, b):\n    return a + b\n")
    _write(tmp_path, "b.py", "def add_numbers(a, b):\n    return a + b\n")
    sub.files.index_path(tmp_path)
    sub.db.conn.execute("UPDATE files SET centroid=NULL, emb_model=NULL")
    sub.db.conn.execute("DELETE FROM edges WHERE type='similar_to'")
    sub.db.conn.commit()
    stats = sub.files.index_path(tmp_path)
    assert stats["unchanged"] == 2
    assert sub.db.conn.execute("SELECT COUNT(*) FROM files WHERE centroid IS NULL").fetchone()[0] == 0
    assert sub.files.similar_files(tmp_path / "a.py")[0]["path"].endswith("b.py")


def test_stats_include_backend_and_languages(sub, tmp_path):
    _write(tmp_path, "a.py", "x = 1\n")
    sub.files.index_path(tmp_path)
    st = sub.files.stats()
    assert st["languages"] == {"python": 1}
    assert st["vector_backend"] in ("sqlite-vec", "python-scan")
