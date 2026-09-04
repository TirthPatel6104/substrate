"""
File intelligence: scan folders, hash and chunk files, embed chunks, and build
graph edges (imports, similar_to). Search is hybrid (FTS5 + vector cosine).

Kept dependency-free: language detection is by extension, code "imports" are
parsed with lightweight regexes, and chunking is by blank-line / size. The
public API is stable so tree-sitter and sqlite-vec can be dropped in later.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .db import Database
from .embedding import cosine, get_embedder

# Directories never worth indexing.
_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode", "target",
    ".next", ".cache", "site-packages",
}
_TEXT_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h",
    ".cpp", ".hpp", ".rb", ".php", ".sh", ".md", ".txt", ".rst", ".toml",
    ".yaml", ".yml", ".json", ".cfg", ".ini", ".html", ".css", ".sql",
}
_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".rb": "ruby",
    ".php": "php", ".sh": "shell", ".md": "markdown", ".sql": "sql",
}
_MAX_BYTES = 1_000_000       # skip files larger than ~1MB
_MAX_CHUNKS = 40             # cap chunks per file
_SIMILAR_THRESHOLD = 0.55


def _hash_file(path: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _chunk_text(text: str, target: int = 1200) -> list[str]:
    """Chunk by paragraphs, packing up to ~target chars, capped at _MAX_CHUNKS."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 > target and buf:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
        if len(chunks) >= _MAX_CHUNKS:
            break
    if buf and len(chunks) < _MAX_CHUNKS:
        chunks.append(buf)
    return chunks or ([text[: target * _MAX_CHUNKS]] if text.strip() else [])


def _extract_imports(text: str, language: str | None) -> list[str]:
    import re

    mods: list[str] = []
    if language == "python":
        for m in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", text, re.M):
            mods.append((m.group(1) or m.group(2)).split(".")[0])
    elif language in ("javascript", "typescript"):
        for m in re.finditer(r"""(?:import[^'"]*['"]([^'"]+)['"]|require\(['"]([^'"]+)['"]\))""", text):
            mods.append(m.group(1) or m.group(2))
    return sorted(set(mods))


class FileIndex:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.embedder = get_embedder()

    def index_path(self, root: str | Path) -> dict:
        """Scan a file or directory tree; (re)index changed files. Returns stats."""
        root = Path(root).expanduser().resolve()
        stats = {"scanned": 0, "indexed": 0, "skipped": 0, "unchanged": 0}
        targets = [root] if root.is_file() else self._walk(root)
        for path in targets:
            stats["scanned"] += 1
            outcome = self._index_file(path)
            stats[outcome] += 1
        self._rebuild_similarity_edges()
        return stats

    def _walk(self, root: Path):
        for p in root.rglob("*"):
            if p.is_dir():
                continue
            if any(part in _IGNORE_DIRS for part in p.parts):
                continue
            yield p

    def _index_file(self, path: Path) -> str:
        try:
            if path.suffix.lower() not in _TEXT_EXTS:
                return "skipped"
            size = path.stat().st_size
            if size > _MAX_BYTES or size == 0:
                return "skipped"
            file_hash = _hash_file(path)
        except OSError:
            return "skipped"

        spath = str(path)
        existing = self.db.conn.execute(
            "SELECT id, hash FROM files WHERE path=?", (spath,)
        ).fetchone()
        if existing and existing["hash"] == file_hash:
            return "unchanged"

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "skipped"

        language = _LANG_BY_EXT.get(path.suffix.lower())
        now = time.time()
        if existing:
            file_id = existing["id"]
            self.db.conn.execute(
                "UPDATE files SET hash=?, size=?, language=?, mtime=?, indexed_at=? WHERE id=?",
                (file_hash, size, language, path.stat().st_mtime, now, file_id),
            )
            self.db.conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
            self.db.conn.execute(
                "DELETE FROM edges WHERE src=? AND type='imports'", (f"file:{file_id}",)
            )
        else:
            cur = self.db.conn.execute(
                "INSERT INTO files(path, hash, size, language, mtime, indexed_at) VALUES (?,?,?,?,?,?)",
                (spath, file_hash, size, language, path.stat().st_mtime, now),
            )
            file_id = int(cur.lastrowid)

        for i, chunk in enumerate(_chunk_text(text)):
            emb = json.dumps(self.embedder.embed(chunk))
            self.db.conn.execute(
                "INSERT INTO chunks(file_id, ordinal, content, embedding, emb_model) VALUES (?,?,?,?,?)",
                (file_id, i, chunk, emb, self.embedder.name),
            )

        for mod in _extract_imports(text, language):
            self.db.conn.execute(
                "INSERT OR REPLACE INTO edges(src, dst, type, weight) VALUES (?,?,?,1.0)",
                (f"file:{file_id}", f"module:{mod}", "imports"),
            )

        self.db.conn.commit()
        return "indexed"

    def search(self, query: str, *, k: int = 8) -> list[dict]:
        """Hybrid chunk search; returns best chunk per file with its path."""
        import re
        import sqlite3

        lexical_rank: dict[int, int] = {}
        terms = re.findall(r"[A-Za-z0-9]+", query)
        if terms:
            try:
                fts = self.db.conn.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 50",
                    (" OR ".join(terms),),
                ).fetchall()
                for i, r in enumerate(fts):
                    lexical_rank[int(r["rowid"])] = i
            except sqlite3.Error:
                pass

        qvec = self.embedder.embed(query)
        rows = self.db.conn.execute(
            "SELECT c.id, c.file_id, c.content, c.embedding, f.path "
            "FROM chunks c JOIN files f ON f.id = c.file_id"
        ).fetchall()

        scored = []
        for r in rows:
            emb = json.loads(r["embedding"]) if r["embedding"] else []
            vscore = cosine(qvec, emb)
            fused = vscore
            if int(r["id"]) in lexical_rank:
                fused += 1.0 / (10 + lexical_rank[int(r["id"])])
            scored.append((fused, r))
        scored.sort(key=lambda x: x[0], reverse=True)

        best_per_file: dict[int, dict] = {}
        for fused, r in scored:
            fid = int(r["file_id"])
            if fid not in best_per_file:
                best_per_file[fid] = {
                    "path": r["path"],
                    "score": round(fused, 4),
                    "snippet": r["content"][:280],
                }
            if len(best_per_file) >= k:
                break
        return list(best_per_file.values())

    def similar_files(self, path: str | Path, *, k: int = 5) -> list[dict]:
        path = str(Path(path).expanduser().resolve())
        row = self.db.conn.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
        if not row:
            return []
        file_id = row["id"]
        neighbors = self.db.conn.execute(
            "SELECT dst, weight FROM edges WHERE src=? AND type='similar_to' "
            "ORDER BY weight DESC LIMIT ?",
            (f"file:{file_id}", k),
        ).fetchall()
        out = []
        for n in neighbors:
            fid = int(n["dst"].split(":")[1])
            f = self.db.conn.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
            if f:
                out.append({"path": f["path"], "similarity": round(n["weight"], 4)})
        return out

    def _file_vector(self, file_id: int) -> list[float]:
        """Mean of a file's chunk vectors (its centroid)."""
        rows = self.db.conn.execute(
            "SELECT embedding FROM chunks WHERE file_id=?", (file_id,)
        ).fetchall()
        vecs = [json.loads(r["embedding"]) for r in rows if r["embedding"]]
        if not vecs:
            return []
        dim = len(vecs[0])
        acc = [0.0] * dim
        for v in vecs:
            for i in range(dim):
                acc[i] += v[i]
        n = len(vecs)
        return [x / n for x in acc]

    def _rebuild_similarity_edges(self) -> None:
        """Recompute similar_to edges between all indexed files (MVP: full pass)."""
        files = self.db.conn.execute("SELECT id FROM files").fetchall()
        centroids = {int(f["id"]): self._file_vector(int(f["id"])) for f in files}
        centroids = {k: v for k, v in centroids.items() if v}
        ids = list(centroids)
        self.db.conn.execute("DELETE FROM edges WHERE type='similar_to'")
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                sim = cosine(centroids[ids[i]], centroids[ids[j]])
                if sim >= _SIMILAR_THRESHOLD:
                    self.db.conn.execute(
                        "INSERT OR REPLACE INTO edges(src, dst, type, weight) VALUES (?,?,?,?)",
                        (f"file:{ids[i]}", f"file:{ids[j]}", "similar_to", sim),
                    )
                    self.db.conn.execute(
                        "INSERT OR REPLACE INTO edges(src, dst, type, weight) VALUES (?,?,?,?)",
                        (f"file:{ids[j]}", f"file:{ids[i]}", "similar_to", sim),
                    )
        self.db.conn.commit()

    def stats(self) -> dict:
        f = self.db.conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
        c = self.db.conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
        return {"files": f, "chunks": c}
