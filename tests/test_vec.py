"""Vector layer: blob packing, backend parity, model-change handling."""

import os

import pytest

from substrate import embedding
from substrate.core import Substrate
from substrate.embedding import HashingEmbedder, cosine, pack, unpack


def test_pack_unpack_roundtrip():
    vec = HashingEmbedder(dim=16).embed("hello world")
    assert len(pack(vec)) == 16 * 4
    back = unpack(pack(vec))
    assert len(back) == 16
    assert cosine(vec, back) > 0.9999


def test_unpack_reads_legacy_json():
    assert unpack("[0.5, 0.5]") == [0.5, 0.5]
    assert unpack(None) == [] and unpack(b"") == []


def _needs_vec():
    from substrate.db import Database
    db = Database(":memory:")
    try:
        if not db.vec_available:
            pytest.skip("sqlite-vec not installed")
    finally:
        db.close()


def test_backends_return_identical_neighbours(monkeypatch):
    _needs_vec()
    facts = [
        "We deploy with make release and never push to main",
        "Tests live in the tests directory and run with pytest",
        "The database is Postgres 16 running in docker compose",
        "Use pnpm, not npm, for every javascript package",
        "Authentication uses JWT with a 15 minute expiry",
        "Logging goes to stdout as JSON lines",
    ]
    queries = ["how do we deploy", "which package manager", "database version", "jwt expiry"]
    results = {}
    for backend in ("off", "auto"):
        monkeypatch.setenv("SUBSTRATE_VEC", backend)
        s = Substrate(workspace="parity", db_path=":memory:")
        assert s.db.vec_available == (backend == "auto")
        for f in facts:
            s.memory.remember(f, scope="parity")
        results[backend] = {q: [r["content"] for r in s.memory.recall(q, scope="parity", k=3)]
                            for q in queries}
        s.close()
    assert results["off"] == results["auto"]


def test_vec_index_tracks_deletes_and_supersedes(sub):
    a = sub.memory.remember("alpha fact about caching", scope="test")
    b = sub.memory.remember("beta fact about caching", scope="test")
    sub.memory.forget(a)
    sub.memory.supersede(b, "gamma fact about caching")
    got = [r["content"] for r in sub.memory.recall("caching", scope="test", k=5)]
    assert got == ["gamma fact about caching"]
    if sub.db.vec_available:
        assert sub.memory.vec.count() == 1


def test_model_change_triggers_reembed(sub, tmp_path):
    (tmp_path / "a.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    sub.files.index_path(tmp_path)
    # Simulate rows written by an older model.
    sub.db.conn.execute("UPDATE chunks SET emb_model='old-model-v0'")
    sub.db.conn.commit()
    stats = sub.files.index_path(tmp_path)
    assert stats["unchanged"] == 1
    models = {r[0] for r in sub.db.conn.execute("SELECT DISTINCT emb_model FROM chunks")}
    assert models == {sub.files.embedder.name}
    assert sub.files.search("add numbers", k=1)[0]["path"].endswith("a.py")


def test_embedder_env_selection(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_EMBEDDER", "hashing")
    embedding.reset_embedder()
    assert embedding.get_embedder().name == "hashing-ngram-v1"
    monkeypatch.setenv("SUBSTRATE_EMBEDDER", "fastembed")
    embedding.reset_embedder()
    if embedding.fastembed_available() and os.environ.get("SUBSTRATE_TEST_FASTEMBED"):
        assert embedding.get_embedder().name.startswith("fastembed")  # pragma: no cover
    elif not embedding.fastembed_available():
        with pytest.raises(RuntimeError):
            embedding.get_embedder()
    embedding.reset_embedder()
    monkeypatch.setenv("SUBSTRATE_EMBEDDER", "hashing")
    embedding.reset_embedder()
