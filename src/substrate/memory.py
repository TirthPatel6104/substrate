"""
Memory store: remember / recall / brief / forget with provenance, scoping,
supersede-not-delete semantics, usage reinforcement, and hybrid retrieval
(FTS5 lexical + vector cosine).
"""

from __future__ import annotations

import json
import math
import re
import time

from .db import Database
from .embedding import cosine, get_embedder

VALID_TYPES = {"working", "episodic", "semantic", "task", "project"}

# Refuse to store obvious secrets at write time (data minimization).
_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|pk)-[A-Za-z0-9]{16,}\b"),          # API-key-ish
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),                # GitHub token
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                    # AWS access key
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key)\s*[:=]\s*\S{6,}"),
]


class SecretRefused(ValueError):
    """Raised when a memory write looks like it contains a credential."""


def _looks_like_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS)


class MemoryStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.embedder = get_embedder()

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
            raise ValueError(f"Invalid memory type '{mem_type}'; must be one of {VALID_TYPES}")
        if not allow_secrets and _looks_like_secret(content):
            raise SecretRefused(
                "Content appears to contain a credential/secret; refusing to store it. "
                "Pass allow_secrets=True only if you are certain this is safe."
            )
        now = time.time()
        expires_at = now + ttl_seconds if ttl_seconds else None
        emb = json.dumps(self.embedder.embed(content))
        cur = self.db.conn.execute(
            """INSERT INTO memories
               (type, scope, content, source, confidence, importance, pinned,
                embedding, emb_model, created_at, updated_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mem_type, scope, content, source, confidence, importance, int(pinned),
             emb, self.embedder.name, now, now, expires_at),
        )
        self.db.conn.commit()
        return int(cur.lastrowid)

    def supersede(self, old_id: int, new_content: str, **kwargs) -> int:
        """Create a new memory that replaces ``old_id`` (kept as a tombstone)."""
        new_id = self.remember(new_content, **kwargs)
        self.db.conn.execute(
            "UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?",
            (new_id, time.time(), old_id),
        )
        self.db.conn.execute(
            "INSERT OR REPLACE INTO edges(src, dst, type, weight) VALUES (?,?,?,1.0)",
            (f"memory:{new_id}", f"memory:{old_id}", "supersedes"),
        )
        self.db.conn.commit()
        return new_id

    def forget(self, memory_id: int) -> bool:
        cur = self.db.conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        self.db.conn.commit()
        return cur.rowcount > 0

    def expire_working(self) -> int:
        """Delete expired working memory. Returns count removed."""
        cur = self.db.conn.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?",
            (time.time(),),
        )
        self.db.conn.commit()
        return cur.rowcount

    # -- read -----------------------------------------------------------------
    def recall(
        self,
        query: str,
        *,
        scope: str | None = None,
        k: int = 5,
        include_superseded: bool = False,
    ) -> list[dict]:
        """Hybrid retrieval: reciprocal-rank fusion of FTS5 and vector cosine."""
        self.expire_working()
        where = ["(superseded_by IS NULL OR :inc = 1)"]
        params: dict = {"inc": 1 if include_superseded else 0}
        if scope:
            where.append("(scope = :scope OR scope = 'global')")
            params["scope"] = scope
        where_sql = " AND ".join(where)

        rows = self.db.conn.execute(
            f"SELECT * FROM memories WHERE {where_sql}", params
        ).fetchall()
        if not rows:
            return []

        # Lexical ranking via FTS5 (ids in relevance order).
        lexical_rank: dict[int, int] = {}
        try:
            fts = self.db.conn.execute(
                "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? "
                "ORDER BY rank LIMIT 50",
                (_fts_query(query),),
            ).fetchall()
            for i, r in enumerate(fts):
                lexical_rank[int(r["rowid"])] = i
        except sqlite3_err():
            pass

        # Vector ranking via cosine.
        qvec = self.embedder.embed(query)
        scored = []
        for row in rows:
            emb = json.loads(row["embedding"]) if row["embedding"] else []
            scored.append((int(row["id"]), cosine(qvec, emb)))
        scored.sort(key=lambda x: x[1], reverse=True)
        vector_rank = {mid: i for i, (mid, _) in enumerate(scored)}

        # Reciprocal-rank fusion + importance/recency/pin boosts.
        by_id = {int(r["id"]): r for r in rows}
        fused: dict[int, float] = {}
        for mid in by_id:
            score = 0.0
            if mid in lexical_rank:
                score += 1.0 / (60 + lexical_rank[mid])
            if mid in vector_rank:
                score += 1.0 / (60 + vector_rank[mid])
            r = by_id[mid]
            score *= 1.0 + 0.2 * float(r["importance"]) + (0.5 if r["pinned"] else 0.0)
            fused[mid] = score

        top = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:k]
        results = []
        for mid, score in top:
            r = by_id[mid]
            self.db.conn.execute("UPDATE memories SET uses = uses + 1 WHERE id=?", (mid,))
            results.append(_row_to_dict(r) | {"score": round(score, 4)})
        self.db.conn.commit()
        return results

    def brief(self, scope: str, *, max_items: int = 20) -> dict:
        """Session-start pack: project profile + pinned facts + active tasks + recent episodes."""
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

    def render_brief_markdown(self, scope: str) -> str:
        """Render the brief as CLAUDE.md-style markdown for file-only agents."""
        b = self.brief(scope)
        lines = [f"# Project brief: {scope}", ""]
        for title, key in [
            ("Project profile", "project"),
            ("Pinned facts", "pinned_facts"),
            ("Known facts & decisions", "semantic"),
            ("Recent activity", "recent_episodes"),
        ]:
            items = b[key]
            if items:
                lines.append(f"## {title}")
                for it in items:
                    lines.append(f"- {it['content']}")
                lines.append("")
        return "\n".join(lines).strip() + "\n"


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 OR query of the alphanumeric terms."""
    terms = re.findall(r"[A-Za-z0-9]+", query)
    return " OR ".join(terms) if terms else '""'


def _row_to_dict(row) -> dict:
    d = dict(row)
    d.pop("embedding", None)
    return d


def sqlite3_err():
    import sqlite3

    return sqlite3.Error


def decay_scores(db: Database, half_life_days: float = 30.0) -> None:  # pragma: no cover
    """Apply recency decay to importance (maintenance job)."""
    now = time.time()
    for row in db.conn.execute("SELECT id, importance, updated_at, uses FROM memories").fetchall():
        age_days = (now - row["updated_at"]) / 86400.0
        decay = math.pow(0.5, age_days / half_life_days)
        reinforced = decay * (1.0 + 0.1 * row["uses"])
        db.conn.execute(
            "UPDATE memories SET importance = ? WHERE id = ?",
            (max(0.01, row["importance"] * reinforced), row["id"]),
        )
    db.conn.commit()
