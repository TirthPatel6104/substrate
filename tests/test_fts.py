"""Lexical side of hybrid retrieval: identifier-aware tokenizer, query expansion, filename boost."""

from substrate.core import Substrate
from substrate.db import SCHEMA_VERSION
from substrate.files import fts_query, query_terms


def test_query_terms_keep_and_split_identifiers():
    assert query_terms("safety_level") == ["safety_level", "safety", "level"]
    assert query_terms("plain words") == ["plain", "words"]
    assert fts_query(["a", "b_c"]) == '"a" OR "b_c"'


def test_identifier_queries_match_symbols_and_prose(sub, tmp_path):
    (tmp_path / "gate.py").write_text("def safety_level(cmd):\n    return classify(cmd)\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("The safety level of a command is decided by the gate.\n", encoding="utf-8")
    sub.files.index_path(tmp_path)
    assert sub.files.search("safety_level", k=1)[0]["path"].endswith("gate.py")
    paths = [r["path"] for r in sub.files.search("safety level", k=2)]
    assert any(p.endswith("notes.md") for p in paths)


def test_filename_boost(sub, tmp_path):
    (tmp_path / "cli.py").write_text("import argparse\n\ndef main():\n    pass\n", encoding="utf-8")
    (tmp_path / "other.py").write_text(
        "# the cli is documented elsewhere; argparse argparse argparse\n", encoding="utf-8")
    sub.files.index_path(tmp_path)
    assert sub.files.search("cli argparse", k=1)[0]["path"].endswith("cli.py")


def test_v2_database_migrates_fts_tokenizer(tmp_path, vector_backend):
    """A v2 DB has FTS tables without tokenchars; v3 recreates and rebuilds them."""
    path = tmp_path / "v2.db"
    s = Substrate(workspace="m", db_path=path)
    s.memory.remember("the function safety_level classifies commands", scope="m")
    s.db.conn.executescript(
        """
        DROP TABLE memories_fts;
        CREATE VIRTUAL TABLE memories_fts USING fts5(content, content='memories', content_rowid='id');
        INSERT INTO memories_fts(memories_fts) VALUES ('rebuild');
        UPDATE meta SET value='2' WHERE key='schema_version';
        """
    )
    s.db.conn.commit()
    s.close()

    s = Substrate(workspace="m", db_path=path)
    assert s.db.schema_version == SCHEMA_VERSION
    ddl = s.db.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='memories_fts'").fetchone()[0]
    assert "tokenchars" in ddl
    hit = s.db.conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?", ('"safety_level"',)).fetchall()
    assert len(hit) == 1
    assert s.memory.recall("safety_level", scope="m", k=1)
    s.close()
