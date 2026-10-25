"""
Memory store: remember / recall / brief / forget with provenance, scoping,
supersede-not-delete semantics, usage reinforcement, and hybrid retrieval
(FTS5 lexical + vector KNN, fused with reciprocal-rank fusion).

Memory types
------------
working   – scratch state for the current task; has a TTL and is compacted
episodic  – "what happened" summaries of sessions / tasks
semantic  – durable facts and decisions
task      – notes attached to a task
project   – profile of the workspace (stack, conventions, commands)
"""

from __future__ import annotations

import math
import re
import time

from .db import Database
from .embedding import get_embedder, pack
from .vec import VectorIndex

VALID_TYPES = {"working", "episodic", "semantic", "task", "project"}

# Refuse to store obvious secrets at write time (data minimization).
_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"),      # API-key-ish (OpenAI/Stripe style)
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),            # Anthropic
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),           # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                     # AWS access key
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),         # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY"),
    re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|token|bearer)\s*[:=]\s*['\"]?\S{6,}"),
    re.compile(r"(?i)\b(?:postgres|mysql|mongodb|redis|amqp)://[^:\s]+:[^@\s]+@"),  # DSN with password
]


class SecretRefused(ValueError):
    """Raised when a memory write looks like it contains a credential."""


def _looks_like_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS)


class MemoryStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.embedder = get_embedder()
        self.vec = VectorIndex(db, "memories_vec", source="memories",
                               dim=self.embedder.dim, model=self.embedder.name)

    # -- write ----------------------------------------------------------------
    def remember(
        self,
        content: str,
        *,
        mem_type: str = "semantic",
        scope: str = "global",
        source: str | None = None,
        confidence: float = 1.0,
        importance: float = 1.0,
        pinned: bool = False,
        ttl_seconds: float | None = None,
        allow_secrets: bool = False,
    ) -> int:
        content = content.strip()
        if not content:
            raise ValueError("Cannot remember empty content")
        if mem_type not in VALID_TYPES:
            raise ValueError(f"Invalid memory type '{mem_type}'; must be one of {sorted(VALID_TYPES)}")
        if not allow_secrets and _looks_like_secret(content):
            raise SecretRefused(
                "Content appears to contain a credential/secret; refusing to store it. "
                "Pass allow_secrets=True only if you are certain this is safe."
            )
        now = time.time()
        expires_at = now + ttl_seconds if ttl_seconds else None
        vec = self.embedder.embed(content)
        cur = self.db.conn.execute(
            """INSERT INTO memories
               (type, scope, content, source, confidence, importance, pinned,
                embedding, emb_model, created_at, updated_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mem_type, scope, content, source, confidence, importance, int(pinned),
             pack(vec), self.embedder.name, now, now, expires_at),
        )
        mid = int(cur.lastrowid)
        self.vec.upsert(mid, vec)
        self.db.conn.commit()
        return mid

    def supersede(self, old_id: int, new_content: str, **kwargs) -> int:
        """Create a new memory that replaces ``old_id`` (kept as a tombstone)."""
        old = self.db.conn.execute("SELECT * FROM memories WHERE id=?", (old_id,)).fetchone()
        if old is None:
            raise ValueError(f"No memory with id {old_id}")
        kwargs.setdefault("mem_type", old["type"])
        kwargs.setdefault("scope", old["scope"])
        kwargs.setdefault("pinned", bool(old["pinned"]))
        new_id = self.remember(new_content, **kwargs)
        self.db.conn.execute(
            "UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?",
            (new_id, time.time(), old_id),
        )
        self.db.conn.execute(
            "INSERT OR REPLACE INTO edges(src, dst, type, weight) VALUES (?,?,?,1.0)",
            (f"memory:{new_id}", f"memory:{old_id}", "supersedes"),
        )
        self.vec.delete(old_id)  # superseded rows must not come back via KNN
        self.db.conn.commit()
        return new_id

    def forget(self, memory_id: int) -> bool:
        cur = self.db.conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        self.vec.delete(memory_id)
        self.db.conn.commit()
        return cur.rowcount > 0

    def expire_working(self) -> int:
        """Delete expired working memory. Returns count removed."""
        ids = [int(r["id"]) for r in self.db.conn.execute(
            "SELECT id FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?",
            (time.time(),))]
        if not ids:
            return 0
        self.vec.delete_many(ids)
        self.db.conn.executemany("DELETE FROM memories WHERE id=?", [(i,) for i in ids])
        self.db.conn.commit()
        return len(ids)

    # -- read -----------------------------------------------------------------
    def recall(
        self,
        query: str,
        *,
        scope: str | None = None,
        k: int = 5,
        include_superseded: bool = False,
    ) -> list[dict]:
        """Hybrid retrieval: reciprocal-rank fusion of FTS5 and vector KNN."""
        self.expire_working()
        where = ["(superseded_by IS NULL OR ? = 1)"]
        params: list = [1 if include_superseded else 0]
        if scope:
            where.append("(scope = ? OR scope = 'global')")
            params.append(scope)
        where_sql = " AND ".join(where)

        # Lexical ranking via FTS5 (ids in relevance order, filtered to scope).
        lexical_rank: dict[int, int] = {}
        terms = re.findall(r"[A-Za-z0-9_]+", query)
        if terms:
            try:
                fts = self.db.conn.execute(
                    f"SELECT m.id FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                    f"WHERE memories_fts MATCH ? AND {where_sql} ORDER BY f.rank LIMIT 50",
                    (" OR ".join(f'"{t}"' for t in terms), *params),
                ).fetchall()
                for i, r in enumerate(fts):
                    lexical_rank[int(r["id"])] = i
            except sqlite3_err():
                pass

        # Vector ranking via KNN (filtered to scope).
        qvec = self.embedder.embed(query)
        knn = self.vec.search(qvec, 50, where=where_sql, params=tuple(params))
        vector_rank = {mid: i for i, (mid, _) in enumerate(knn)}

        candidates = set(lexical_rank) | set(vector_rank)
        if not candidates:
            return []
        marks = ",".join("?" * len(candidates))
        by_id = {
            int(r["id"]): r for r in self.db.conn.execute(
                f"SELECT * FROM memories WHERE id IN ({marks})", tuple(candidates))
        }

        # Reciprocal-rank fusion + importance/recency/pin boosts.
        now = time.time()
        fused: dict[int, float] = {}
        for mid, r in by_id.items():
            score = 0.0
            if mid in lexical_rank:
                score += 1.0 / (60 + lexical_rank[mid])
            if mid in vector_rank:
                score += 1.0 / (60 + vector_rank[mid])
            age_days = max(0.0, (now - float(r["updated_at"])) / 86400.0)
            recency = 1.0 + 0.3 * math.exp(-age_days / 30.0)
            score *= recency * (1.0 + 0.2 * float(r["importance"]) + (0.5 if r["pinned"] else 0.0))
            fused[mid] = score

        top = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:k]
        results = []
        for mid, score in top:
            self.db.conn.execute("UPDATE memories SET uses = uses + 1 WHERE id=?", (mid,))
            results.append(_row_to_dict(by_id[mid]) | {"score": round(score, 4)})
        self.db.conn.commit()
        return results

    def brief(self, scope: str, *, max_items: int = 20) -> dict:
        """Session-start pack: project profile + pinned facts + semantic facts + recent episodes.

        Open tasks are added by ``Substrate.brief`` so the memory store stays
        independent of the task ledger.
        """
        self.expire_working()

        def fetch(mem_type: str, limit: int) -> list[dict]:
            rows = self.db.conn.execute(
                """SELECT * FROM memories
                   WHERE superseded_by IS NULL AND type = ?
                     AND (scope = ? OR scope = 'global')
                   ORDER BY pinned DESC, importance DESC, updated_at DESC LIMIT ?""",
                (mem_type, scope, limit),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

        recent = self.db.conn.execute(
            """SELECT * FROM memories
               WHERE superseded_by IS NULL AND type='episodic'
                 AND (scope = ? OR scope = 'global')
               ORDER BY created_at DESC LIMIT ?""",
            (scope, max_items // 2),
        ).fetchall()

        return {
            "scope": scope,
            "project": fetch("project", max_items),
            "pinned_facts": [
                _row_to_dict(r)
                for r in self.db.conn.execute(
                    """SELECT * FROM memories
                       WHERE superseded_by IS NULL AND pinned=1
                         AND (scope = ? OR scope = 'global')
                       ORDER BY importance DESC LIMIT ?""",
                    (scope, max_items),
                ).fetchall()
            ],
            "semantic": fetch("semantic", max_items),
            "recent_episodes": [_row_to_dict(r) for r in recent],
        }

    def list(self, *, scope: str | None = None, mem_type: str | None = None,
             limit: int = 50) -> list[dict]:
        where, params = ["superseded_by IS NULL"], []
        if scope:
            where.append("(scope = ? OR scope = 'global')")
            params.append(scope)
        if mem_type:
            where.append("type = ?")
            params.append(mem_type)
        rows = self.db.conn.execute(
            f"SELECT * FROM memories WHERE {' AND '.join(where)} "
            f"ORDER BY updated_at DESC LIMIT ?", (*params, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # -- maintenance ------------------------------------------------------------
    def compact_working(self, scope: str, *, older_than_seconds: float = 3600.0,
                        source: str = "compaction") -> int | None:
        """Fold stale working memories of a scope into one episodic summary.

        No LLM is involved: the summary is a deduplicated bullet list of the
        working notes, newest last. Returns the new episodic memory id, or None
        when nothing needed compaction.
        """
        cutoff = time.time() - older_than_seconds
        rows = self.db.conn.execute(
            """SELECT id, content FROM memories
               WHERE type='working' AND scope=? AND superseded_by IS NULL AND updated_at < ?
               ORDER BY created_at""",
            (scope, cutoff),
        ).fetchall()
        if not rows:
            return None
        seen: set[str] = set()
        bullets = []
        for r in rows:
            key = r["content"].strip().lower()
            if key not in seen:
                seen.add(key)
                bullets.append(f"- {r['content'].strip()}")
        summary = f"Working notes compacted ({len(rows)} items):\n" + "\n".join(bullets[:40])
        new_id = self.remember(summary, mem_type="episodic", scope=scope, source=source,
                               importance=0.8, allow_secrets=True)
        ids = [int(r["id"]) for r in rows]
        self.vec.delete_many(ids)
        self.db.conn.executemany(
            "UPDATE memories SET superseded_by=? WHERE id=?", [(new_id, i) for i in ids]
        )
        self.db.conn.commit()
        return new_id

    def decay(self, half_life_days: float = 30.0) -> int:
        """Apply recency decay to importance, reinforced by usage. Returns rows touched."""
        now = time.time()
        n = 0
        for row in self.db.conn.execute(
                "SELECT id, importance, updated_at, uses, pinned FROM memories "
                "WHERE superseded_by IS NULL").fetchall():
            if row["pinned"]:
                continue
            age_days = (now - row["updated_at"]) / 86400.0
            decay = math.pow(0.5, age_days / half_life_days)
            reinforced = decay * (1.0 + 0.1 * row["uses"])
            self.db.conn.execute(
                "UPDATE memories SET importance = ? WHERE id = ?",
                (max(0.01, min(2.0, row["importance"] * reinforced)), row["id"]),
            )
            n += 1
        self.db.conn.commit()
        return n

    def reembed_stale(self) -> int:
        """Re-embed memories written by a different embedding model."""
        rows = self.db.conn.execute(
            "SELECT id, content FROM memories WHERE emb_model IS NOT ? OR emb_model IS NULL",
            (self.embedder.name,),
        ).fetchall()
        if not rows:
            return 0
        vectors = self.embedder.embed_many([r["content"] for r in rows])
        for r, vec in zip(rows, vectors, strict=True):
            self.db.conn.execute(
                "UPDATE memories SET embedding=?, emb_model=? WHERE id=?",
                (pack(vec), self.embedder.name, int(r["id"])),
            )
            self.vec.upsert(int(r["id"]), vec)
        self.db.conn.commit()
        return len(rows)


def _row_to_dict(row) -> dict:
    d = dict(row)
    d.pop("embedding", None)
    return d


def sqlite3_err():
    import sqlite3

    return sqlite3.Error
