"""
Vector index abstraction: k-nearest-neighbour search over the rows of a source
table, backed by `sqlite-vec <https://github.com/asg017/sqlite-vec>`_ when the
extension is loadable and by a brute-force Python scan otherwise.

Why an abstraction instead of always using sqlite-vec?

* Zero-dependency install must keep working (brute force is fine up to a few
  thousand vectors).
* Some Python builds ship a ``sqlite3`` module without ``enable_load_extension``.
* The brute-force path doubles as the oracle in tests: both backends must
  return the same neighbours.

The source table (``chunks`` or ``memories``) remains the system of record;
the vec0 table is a derived index that can be dropped and rebuilt at any time
(``rebuild``), which is also how a change of embedding model is handled.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from .db import Database
from .embedding import cosine, pack, unpack


class VectorIndex:
    def __init__(self, db: Database, name: str, *, source: str, dim: int, model: str) -> None:
        self.db = db
        self.name = name                # e.g. "chunks_vec"
        self.source = source            # e.g. "chunks" (must have id, embedding, emb_model)
        self.dim = dim
        self.model = model
        self.accelerated = db.vec_available
        if self.accelerated:
            self._ensure_table()

    # -- schema ----------------------------------------------------------------
    def _meta_key(self) -> str:
        return f"vec:{self.name}"

    def _ensure_table(self) -> None:
        """Create the vec0 table; rebuild it if dim/model changed since last time."""
        row = self.db.conn.execute(
            "SELECT value FROM meta WHERE key=?", (self._meta_key(),)
        ).fetchone()
        want = f"{self.model}:{self.dim}"
        exists = self.db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (self.name,)
        ).fetchone() is not None
        if exists and row and row["value"] == want:
            return
        if exists:
            self.db.conn.execute(f"DROP TABLE {self.name}")
        self.db.conn.execute(
            f"CREATE VIRTUAL TABLE {self.name} USING vec0("
            f"embedding float[{self.dim}] distance_metric=cosine)"
        )
        self.db.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (self._meta_key(), want)
        )
        self.db.conn.commit()
        self.rebuild()

    # -- writes ----------------------------------------------------------------
    def upsert(self, rowid: int, vec: list[float]) -> None:
        if not self.accelerated or len(vec) != self.dim:
            return
        self.db.conn.execute(f"DELETE FROM {self.name} WHERE rowid=?", (rowid,))
        self.db.conn.execute(
            f"INSERT INTO {self.name}(rowid, embedding) VALUES (?, ?)", (rowid, pack(vec))
        )

    def delete(self, rowid: int) -> None:
        if self.accelerated:
            self.db.conn.execute(f"DELETE FROM {self.name} WHERE rowid=?", (rowid,))

    def delete_many(self, rowids: Iterable[int]) -> None:
        if self.accelerated:
            self.db.conn.executemany(
                f"DELETE FROM {self.name} WHERE rowid=?", [(r,) for r in rowids]
            )

    def rebuild(self) -> int:
        """Repopulate the index from every source row embedded with the current model."""
        if not self.accelerated:
            return 0
        self.db.conn.execute(f"DELETE FROM {self.name}")
        n = 0
        for row in self.db.conn.execute(
            f"SELECT id, embedding FROM {self.source} WHERE emb_model=?", (self.model,)
        ):
            vec = unpack(row["embedding"])
            if len(vec) == self.dim:
                self.db.conn.execute(
                    f"INSERT INTO {self.name}(rowid, embedding) VALUES (?, ?)",
                    (int(row["id"]), pack(vec)),
                )
                n += 1
        self.db.conn.commit()
        return n

    # -- reads -----------------------------------------------------------------
    def search(self, qvec: list[float], k: int, *, where: str = "", params: tuple = ()) -> list[tuple[int, float]]:
        """Return up to ``k`` ``(rowid, cosine_similarity)`` pairs, best first.

        ``where``/``params`` optionally restrict candidates to a subset of the
        source table (e.g. a memory scope). With sqlite-vec we over-fetch and
        filter, which is cheap; brute force applies the filter in SQL.
        """
        if k <= 0 or not qvec:
            return []
        if self.accelerated and len(qvec) == self.dim:
            try:
                return self._search_vec(qvec, k, where, params)
            except sqlite3.Error:
                pass  # fall through to brute force rather than fail the query
        return self._search_scan(qvec, k, where, params)

    def _search_vec(self, qvec, k, where, params) -> list[tuple[int, float]]:
        fetch = k if not where else max(k * 8, 64)
        rows = self.db.conn.execute(
            f"SELECT rowid, distance FROM {self.name} WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (pack(qvec), fetch),
        ).fetchall()
        if not rows:
            return []
        if where:
            ids = [int(r["rowid"]) for r in rows]
            marks = ",".join("?" * len(ids))
            allowed = {
                int(r["id"]) for r in self.db.conn.execute(
                    f"SELECT id FROM {self.source} WHERE id IN ({marks}) AND ({where})",
                    (*ids, *params),
                )
            }
            rows = [r for r in rows if int(r["rowid"]) in allowed]
        # vec0 cosine distance = 1 - cosine similarity
        return [(int(r["rowid"]), 1.0 - float(r["distance"])) for r in rows[:k]]

    def _search_scan(self, qvec, k, where, params) -> list[tuple[int, float]]:
        sql = f"SELECT id, embedding FROM {self.source} WHERE emb_model=?"
        args: tuple = (self.model,)
        if where:
            sql += f" AND ({where})"
            args += params
        scored = []
        for row in self.db.conn.execute(sql, args):
            vec = unpack(row["embedding"])
            if vec:
                scored.append((int(row["id"]), cosine(qvec, vec)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    def count(self) -> int:
        if not self.accelerated:
            return 0
        return int(self.db.conn.execute(f"SELECT COUNT(*) FROM {self.name}").fetchone()[0])
