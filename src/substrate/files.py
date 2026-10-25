"""
File intelligence: scan folders, hash and chunk files, embed chunks, and build
graph edges (imports, similar_to). Search is hybrid (FTS5 + vector KNN fused
with reciprocal-rank fusion).

Kept dependency-free: language detection is by extension, code "imports" are
parsed with lightweight regexes, and chunking is by blank-line / size. Vector
search goes through ``VectorIndex`` (sqlite-vec if available, Python scan
otherwise). ``.gitignore``-style excludes are honoured for the common cases.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import time
from pathlib import Path

from .db import Database
from .embedding import cosine, get_embedder, pack, unpack
from .vec import VectorIndex

# Directories never worth indexing.
_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode", "target",
    ".next", ".cache", "site-packages", ".tox", ".eggs", "coverage", ".hypothesis",
}
_TEXT_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h",
    ".cpp", ".hpp", ".rb", ".php", ".sh", ".md", ".txt", ".rst", ".toml",
    ".yaml", ".yml", ".json", ".jsonl", ".cfg", ".ini", ".html", ".css", ".sql",
    ".kt", ".swift", ".scala", ".cs", ".ps1", ".bat", ".dockerfile", ".tf", ".proto",
}
_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".rb": "ruby",
    ".php": "php", ".sh": "shell", ".md": "markdown", ".sql": "sql", ".kt": "kotlin",
    ".swift": "swift", ".scala": "scala", ".cs": "csharp", ".ps1": "powershell",
    ".tf": "terraform", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".json": "json",
}
_MAX_BYTES = 1_000_000       # skip files larger than ~1MB
_MAX_CHUNKS = 40             # cap chunks per file
_SIMILAR_THRESHOLD = 0.55
_SIMILAR_K = 8               # similar_to edges kept per file


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


_PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)
_JS_IMPORT_RE = re.compile(r"""(?:import[^'"]*['"]([^'"]+)['"]|require\(['"]([^'"]+)['"]\))""")
_GO_IMPORT_RE = re.compile(r'^\s*(?:import\s+)?"([^"]+)"\s*$', re.M)
_RS_USE_RE = re.compile(r"^\s*use\s+([\w:]+)", re.M)


def _extract_imports(text: str, language: str | None) -> list[str]:
    mods: list[str] = []
    if language == "python":
        for m in _PY_IMPORT_RE.finditer(text):
            mods.append((m.group(1) or m.group(2)).split(".")[0])
    elif language in ("javascript", "typescript"):
        for m in _JS_IMPORT_RE.finditer(text):
            mods.append(m.group(1) or m.group(2))
    elif language == "go":
        mods.extend(m.group(1) for m in _GO_IMPORT_RE.finditer(text))
    elif language == "rust":
        mods.extend(m.group(1).split("::")[0] for m in _RS_USE_RE.finditer(text))
    return sorted(set(mods))


def _load_gitignore(root: Path) -> list[str]:
    """Return simple glob patterns from a root .gitignore (no negation support)."""
    gi = root / ".gitignore"
    if not gi.is_file():
        return []
    pats = []
    for line in gi.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        pats.append(line.rstrip("/"))
    return pats


def _gitignored(rel: Path, pats: list[str]) -> bool:
    parts = rel.parts
    for pat in pats:
        if "/" in pat:
            if fnmatch.fnmatch(rel.as_posix(), pat.lstrip("/")) or \
                    fnmatch.fnmatch(rel.as_posix(), pat.lstrip("/") + "/*"):
                return True
        elif any(fnmatch.fnmatch(part, pat) for part in parts):
            return True
    return False


class FileIndex:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.embedder = get_embedder()
        self.vec = VectorIndex(db, "chunks_vec", source="chunks",
                               dim=self.embedder.dim, model=self.embedder.name)

    # -- indexing -------------------------------------------------------------
    def index_path(self, root: str | Path) -> dict:
        """Scan a file or directory tree; (re)index changed files. Returns stats."""
        root = Path(root).expanduser().resolve()
        t0 = time.perf_counter()
        stats = {"scanned": 0, "indexed": 0, "skipped": 0, "unchanged": 0, "removed": 0,
                 "chunks": 0}
        if root.is_file():
            targets = [root]
            pats: list[str] = []
        else:
            pats = _load_gitignore(root)
            targets = list(self._walk(root, pats))
        changed: list[int] = []
        seen: set[str] = set()
        for path in targets:
            stats["scanned"] += 1
            outcome, file_id, nchunks = self._index_file(path)
            stats[outcome] += 1
            stats["chunks"] += nchunks
            if file_id is not None:
                seen.add(str(path))
                if outcome == "indexed":
                    changed.append(file_id)
        if root.is_dir():
            stats["removed"] = self._prune_missing(root, seen)
        # Re-embed rows written by an older embedding model, then refresh edges.
        changed += self._reembed_stale()
        self._update_similarity_edges(changed)
        stats["embedder"] = self.embedder.name
        stats["vector_backend"] = "sqlite-vec" if self.vec.accelerated else "python-scan"
        stats["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        return stats

    def _walk(self, root: Path, pats: list[str]):
        for p in root.rglob("*"):
            if p.is_dir():
                continue
            if any(part in _IGNORE_DIRS for part in p.parts):
                continue
            if pats and _gitignored(p.relative_to(root), pats):
                continue
            yield p

    def _index_file(self, path: Path) -> tuple[str, int | None, int]:
        try:
            if path.suffix.lower() not in _TEXT_EXTS and path.name.lower() not in (
                    "dockerfile", "makefile", "readme", "license"):
                return "skipped", None, 0
            size = path.stat().st_size
            if size > _MAX_BYTES or size == 0:
                return "skipped", None, 0
            file_hash = _hash_file(path)
        except OSError:
            return "skipped", None, 0

        spath = str(path)
        existing = self.db.conn.execute(
            "SELECT id, hash FROM files WHERE path=?", (spath,)
        ).fetchone()
        if existing and existing["hash"] == file_hash:
            return "unchanged", int(existing["id"]), 0

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "skipped", None, 0

        language = _LANG_BY_EXT.get(path.suffix.lower())
        now = time.time()
        if existing:
            file_id = int(existing["id"])
            self.db.conn.execute(
                "UPDATE files SET hash=?, size=?, language=?, mtime=?, indexed_at=? WHERE id=?",
                (file_hash, size, language, path.stat().st_mtime, now, file_id),
            )
            old_chunks = [int(r["id"]) for r in self.db.conn.execute(
                "SELECT id FROM chunks WHERE file_id=?", (file_id,))]
            self.vec.delete_many(old_chunks)
            self.db.conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
            self.db.conn.execute(
                "DELETE FROM edges WHERE src=? AND type='imports'", (f"file:{file_id}",)
            )
        else:
            cur = self.db.conn.execute(
                "INSERT INTO files(path, hash, size, language, mtime, indexed_at) "
                "VALUES (?,?,?,?,?,?)",
                (spath, file_hash, size, language, path.stat().st_mtime, now),
            )
            file_id = int(cur.lastrowid)

        chunks = _chunk_text(text)
        vectors = self.embedder.embed_many(chunks)
        for i, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
            cur = self.db.conn.execute(
                "INSERT INTO chunks(file_id, ordinal, content, embedding, emb_model) "
                "VALUES (?,?,?,?,?)",
                (file_id, i, chunk, pack(vec), self.embedder.name),
            )
            self.vec.upsert(int(cur.lastrowid), vec)

        for mod in _extract_imports(text, language):
            self.db.conn.execute(
                "INSERT OR REPLACE INTO edges(src, dst, type, weight) VALUES (?,?,?,1.0)",
                (f"file:{file_id}", f"module:{mod}", "imports"),
            )

        self.db.conn.commit()
        return "indexed", file_id, len(chunks)

    def _prune_missing(self, root: Path, seen: set[str]) -> int:
        """Drop index rows for files under ``root`` that no longer exist."""
        prefix = str(root)
        gone = [
            (int(r["id"]), r["path"]) for r in self.db.conn.execute(
                "SELECT id, path FROM files WHERE path LIKE ?", (prefix + "%",))
            if r["path"] not in seen and not Path(r["path"]).exists()
        ]
        for fid, _ in gone:
            self.remove_file(fid)
        return len(gone)

    def remove_file(self, file_id: int) -> None:
        chunk_ids = [int(r["id"]) for r in self.db.conn.execute(
            "SELECT id FROM chunks WHERE file_id=?", (file_id,))]
        self.vec.delete_many(chunk_ids)
        self.db.conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        self.db.conn.execute("DELETE FROM edges WHERE src=? OR dst=?",
                             (f"file:{file_id}", f"file:{file_id}"))
        self.db.conn.execute("DELETE FROM files WHERE id=?", (file_id,))
        self.db.conn.commit()

    def _reembed_stale(self) -> list[int]:
        """Re-embed chunks written by a different embedding model. Returns file ids touched."""
        rows = self.db.conn.execute(
            "SELECT id, file_id, content FROM chunks WHERE emb_model IS NOT ? OR emb_model IS NULL",
            (self.embedder.name,),
        ).fetchall()
        if not rows:
            return []
        vectors = self.embedder.embed_many([r["content"] for r in rows])
        touched: set[int] = set()
        for r, vec in zip(rows, vectors, strict=True):
            self.db.conn.execute(
                "UPDATE chunks SET embedding=?, emb_model=? WHERE id=?",
                (pack(vec), self.embedder.name, int(r["id"])),
            )
            self.vec.upsert(int(r["id"]), vec)
            touched.add(int(r["file_id"]))
        self.db.conn.commit()
        return sorted(touched)

    # -- search ---------------------------------------------------------------
    def search(self, query: str, *, k: int = 8) -> list[dict]:
        """Hybrid chunk search (RRF of FTS5 + vector KNN); best chunk per file."""
        import sqlite3

        lexical_rank: dict[int, int] = {}
        terms = re.findall(r"[A-Za-z0-9_]+", query)
        if terms:
            try:
                fts = self.db.conn.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 50",
                    (" OR ".join(f'"{t}"' for t in terms),),
                ).fetchall()
                for i, r in enumerate(fts):
                    lexical_rank[int(r["rowid"])] = i
            except sqlite3.Error:
                pass

        qvec = self.embedder.embed(query)
        vector_rank = {cid: i for i, (cid, _) in enumerate(self.vec.search(qvec, 50))}

        fused: dict[int, float] = {}
        for cid in set(lexical_rank) | set(vector_rank):
            score = 0.0
            if cid in lexical_rank:
                score += 1.0 / (60 + lexical_rank[cid])
            if cid in vector_rank:
                score += 1.0 / (60 + vector_rank[cid])
            fused[cid] = score
        if not fused:
            return []

        ids = sorted(fused, key=fused.get, reverse=True)[: max(k * 4, 32)]
        marks = ",".join("?" * len(ids))
        rows = {
            int(r["id"]): r for r in self.db.conn.execute(
                f"SELECT c.id, c.file_id, c.content, f.path FROM chunks c "
                f"JOIN files f ON f.id = c.file_id WHERE c.id IN ({marks})", ids)
        }
        best_per_file: dict[int, dict] = {}
        for cid in ids:
            r = rows.get(cid)
            if r is None:
                continue
            fid = int(r["file_id"])
            if fid not in best_per_file:
                best_per_file[fid] = {
                    "path": r["path"],
                    "score": round(fused[cid], 4),
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

    # -- graph ----------------------------------------------------------------
    def _file_vector(self, file_id: int) -> list[float]:
        """Mean of a file's chunk vectors (its centroid)."""
        rows = self.db.conn.execute(
            "SELECT embedding FROM chunks WHERE file_id=? AND emb_model=?",
            (file_id, self.embedder.name),
        ).fetchall()
        vecs = [unpack(r["embedding"]) for r in rows if r["embedding"]]
        if not vecs:
            return []
        dim = len(vecs[0])
        acc = [0.0] * dim
        for v in vecs:
            for i in range(dim):
                acc[i] += v[i]
        n = len(vecs)
        return [x / n for x in acc]

    def _update_similarity_edges(self, changed: list[int]) -> None:
        """Recompute similar_to edges only for files whose content changed.

        Cost is O(changed x total) instead of the MVP's O(total^2) full pass.
        Edges are symmetric, so touching a changed file refreshes both directions.
        """
        if not changed:
            return
        all_ids = [int(r["id"]) for r in self.db.conn.execute("SELECT id FROM files")]
        centroids = {fid: self._file_vector(fid) for fid in all_ids}
        centroids = {k: v for k, v in centroids.items() if v}
        changed_set = {c for c in changed if c in centroids}
        for cid in changed_set:
            self.db.conn.execute(
                "DELETE FROM edges WHERE type='similar_to' AND (src=? OR dst=?)",
                (f"file:{cid}", f"file:{cid}"),
            )
        for cid in changed_set:
            sims = []
            for other, vec in centroids.items():
                if other == cid:
                    continue
                sim = cosine(centroids[cid], vec)
                if sim >= _SIMILAR_THRESHOLD:
                    sims.append((sim, other))
            sims.sort(reverse=True)
            for sim, other in sims[:_SIMILAR_K]:
                for a, b in ((cid, other), (other, cid)):
                    self.db.conn.execute(
                        "INSERT OR REPLACE INTO edges(src, dst, type, weight) VALUES (?,?,?,?)",
                        (f"file:{a}", f"file:{b}", "similar_to", sim),
                    )
        self.db.conn.commit()

    def stats(self) -> dict:
        f = self.db.conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
        c = self.db.conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
        e = self.db.conn.execute("SELECT type, COUNT(*) n FROM edges GROUP BY type").fetchall()
        langs = self.db.conn.execute(
            "SELECT language, COUNT(*) n FROM files GROUP BY language ORDER BY n DESC"
        ).fetchall()
        return {
            "files": f,
            "chunks": c,
            "edges": {r["type"]: r["n"] for r in e},
            "languages": {(r["language"] or "other"): r["n"] for r in langs},
            "embedder": self.embedder.name,
            "vector_backend": "sqlite-vec" if self.vec.accelerated else "python-scan",
        }
