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
import math
import re
import time
from pathlib import Path

from .db import Database
from .embedding import get_embedder, pack, unpack
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


def _mean_unit(vectors: list[list[float]]) -> list[float]:
    """L2-normalised mean of unit vectors (a file's centroid); [] if none."""
    vectors = [v for v in vectors if v]
    if not vectors:
        return []
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            acc[i] += v[i]
    norm = math.sqrt(sum(x * x for x in acc)) or 1.0
    return [x / norm for x in acc]


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


def query_terms(query: str) -> list[str]:
    """Tokens for FTS: identifiers are kept whole *and* split on '_' so that
    ``safety_level`` matches the symbol while ``safety level`` matches prose."""
    terms: list[str] = []
    for tok in re.findall(r"[A-Za-z0-9_]+", query):
        terms.append(tok)
        if "_" in tok:
            terms.extend(p for p in tok.split("_") if len(p) > 1)
    return list(dict.fromkeys(terms))


def fts_query(terms: list[str]) -> str:
    return " OR ".join(f'"{t}"' for t in terms)


def fts_queries(terms: list[str]) -> list[str]:
    """MATCH expressions in priority order.

    When the query names identifiers, an exact-identifier query runs first so a
    symbol definition outranks prose that happens to contain both halves of
    the name; the expanded OR query then fills in the remaining ranks.
    """
    idents = [t for t in terms if "_" in t]
    out = [fts_query(idents)] if idents else []
    out.append(fts_query(terms))
    return out


def lexical_ranks(rows_for: callable, terms: list[str], limit: int = 50) -> dict[int, int]:
    """Run the prioritised FTS queries; return {rowid: rank} with earlier queries ranked first."""
    ranks: dict[int, int] = {}
    for match in fts_queries(terms):
        for rid in rows_for(match):
            if rid not in ranks:
                ranks[rid] = len(ranks)
            if len(ranks) >= limit:
                return ranks
    return ranks


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
        self.file_vec = VectorIndex(db, "files_vec", source="files", column="centroid",
                                    dim=self.embedder.dim, model=self.embedder.name)

    # -- indexing -------------------------------------------------------------
    def index_path(self, root: str | Path, *, exclude: tuple[str, ...] = ()) -> dict:
        """Scan a file or directory tree; (re)index changed files. Returns stats.

        ``exclude`` lists extra top-level directory names to skip (on top of the
        built-in ignore list and the root ``.gitignore``).
        """
        root = Path(root).expanduser().resolve()
        t0 = time.perf_counter()
        stats = {"scanned": 0, "indexed": 0, "skipped": 0, "unchanged": 0, "removed": 0,
                 "chunks": 0}
        if root.is_file():
            targets = [root]
            pats: list[str] = []
        else:
            pats = _load_gitignore(root) + [f"/{e}" for e in exclude]
            targets = list(self._walk(root, pats))
        changed: list[int] = []
        seen: set[str] = set()
        for n, path in enumerate(targets, start=1):
            stats["scanned"] += 1
            outcome, file_id, nchunks = self._index_file(path)
            stats[outcome] += 1
            stats["chunks"] += nchunks
            if file_id is not None:
                seen.add(str(path))
                if outcome == "indexed":
                    changed.append(file_id)
            if n % 100 == 0:
                self.db.conn.commit()  # bounded loss on crash, far fewer fsyncs than per-file
        self.db.conn.commit()
        if root.is_dir():
            stats["removed"] = self._prune_missing(root, seen)
        # Re-embed rows written by an older embedding model; fill centroids for
        # rows from databases that predate them.
        changed += self._reembed_stale()
        self._backfill_centroids()
        self._invalidate_similarity_cache(changed)
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
            st = path.stat()
            size = st.st_size
            if size > _MAX_BYTES or size == 0:
                return "skipped", None, 0
            # One open per file: on Windows each open() costs several ms (AV
            # scanning), and it dominated indexing when we hashed and then read.
            raw = path.read_bytes()
        except OSError:
            return "skipped", None, 0
        file_hash = hashlib.blake2b(raw, digest_size=16).hexdigest()

        spath = str(path)
        existing = self.db.conn.execute(
            "SELECT id, hash FROM files WHERE path=?", (spath,)
        ).fetchone()
        if existing and existing["hash"] == file_hash:
            return "unchanged", int(existing["id"]), 0

        text = raw.decode("utf-8", errors="replace")
        language = _LANG_BY_EXT.get(path.suffix.lower())
        now = time.time()
        if existing:
            file_id = int(existing["id"])
            self.db.conn.execute(
                "UPDATE files SET hash=?, size=?, language=?, mtime=?, indexed_at=? WHERE id=?",
                (file_hash, size, language, st.st_mtime, now, file_id),
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
                (spath, file_hash, size, language, st.st_mtime, now),
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
        self._store_centroid(file_id, vectors)

        for mod in _extract_imports(text, language):
            self.db.conn.execute(
                "INSERT OR REPLACE INTO edges(src, dst, type, weight) VALUES (?,?,?,1.0)",
                (f"file:{file_id}", f"module:{mod}", "imports"),
            )
        return "indexed", file_id, len(chunks)

    def _store_centroid(self, file_id: int, vectors: list[list[float]]) -> None:
        centroid = _mean_unit(vectors)
        self.db.conn.execute(
            "UPDATE files SET centroid=?, emb_model=? WHERE id=?",
            (pack(centroid) if centroid else None, self.embedder.name, file_id),
        )
        if centroid:
            self.file_vec.upsert(file_id, centroid)
        else:
            self.file_vec.delete(file_id)

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
        self.file_vec.delete(file_id)
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
        for fid in touched:
            self._recompute_centroid(fid)
        self.db.conn.commit()
        return sorted(touched)

    def _recompute_centroid(self, file_id: int) -> None:
        rows = self.db.conn.execute(
            "SELECT embedding FROM chunks WHERE file_id=? AND emb_model=?",
            (file_id, self.embedder.name)).fetchall()
        self._store_centroid(file_id, [unpack(r["embedding"]) for r in rows if r["embedding"]])

    def _backfill_centroids(self) -> int:
        """Databases from before schema v4 have no centroids; compute them once."""
        ids = [int(r["id"]) for r in self.db.conn.execute(
            "SELECT id FROM files WHERE centroid IS NULL OR emb_model IS NOT ?",
            (self.embedder.name,))]
        for fid in ids:
            self._recompute_centroid(fid)
        if ids:
            self.db.conn.commit()
        return len(ids)

    def _invalidate_similarity_cache(self, changed: list[int]) -> None:
        if not changed:
            return
        self.db.conn.executemany(
            "DELETE FROM edges WHERE type='similar_to' AND (src=? OR dst=?)",
            [(f"file:{fid}", f"file:{fid}") for fid in changed],
        )
        self.db.conn.commit()

    # -- search ---------------------------------------------------------------
    def search(self, query: str, *, k: int = 8) -> list[dict]:
        """Hybrid chunk search (RRF of FTS5 + vector KNN); best chunk per file."""
        import sqlite3

        lexical_rank: dict[int, int] = {}
        terms = query_terms(query)
        if terms:
            def rows_for(match: str) -> list[int]:
                return [int(r["rowid"]) for r in self.db.conn.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 50",
                    (match,))]
            try:
                lexical_rank = lexical_ranks(rows_for, terms)
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
        # Filename boost: a query term that names the file (cli, safety, release)
        # is strong evidence, and chunk text rarely repeats its own filename.
        lower_terms = {t.lower() for t in terms}
        best_per_file: dict[int, dict] = {}
        for cid in ids:
            r = rows.get(cid)
            if r is None:
                continue
            fid = int(r["file_id"])
            stem = Path(r["path"]).stem.lower()
            stem_parts = set(re.split(r"[_\-.]", stem)) | {stem}
            boost = 1.25 if lower_terms & stem_parts else 1.0
            score = fused[cid] * boost
            if fid not in best_per_file or score > best_per_file[fid]["score"]:
                best_per_file[fid] = {
                    "path": r["path"],
                    "score": round(score, 4),
                    "snippet": r["content"][:280],
                }
        ranked = sorted(best_per_file.values(), key=lambda x: x["score"], reverse=True)
        return ranked[:k]

    def similar_files(self, path: str | Path, *, k: int = 5) -> list[dict]:
        """Files whose centroid is nearest to this file's.

        A KNN query over per-file centroids (sqlite-vec or Python scan), computed
        on demand and cached as ``similar_to`` edges. The MVP materialised every
        pair at index time, which made indexing O(n^2); this is O(n) per lookup
        in the fallback and O(log n) with sqlite-vec.
        """
        path = str(Path(path).expanduser().resolve())
        row = self.db.conn.execute(
            "SELECT id, centroid FROM files WHERE path=?", (path,)).fetchone()
        if not row:
            return []
        file_id = int(row["id"])
        cached = self.db.conn.execute(
            "SELECT dst, weight FROM edges WHERE src=? AND type='similar_to' "
            "ORDER BY weight DESC LIMIT ?", (f"file:{file_id}", k)).fetchall()
        if cached:
            pairs = [(int(n["dst"].split(":")[1]), float(n["weight"])) for n in cached]
        else:
            centroid = unpack(row["centroid"])
            if not centroid:
                return []
            hits = self.file_vec.search(centroid, max(k, _SIMILAR_K) + 1)
            pairs = [(fid, sim) for fid, sim in hits
                     if fid != file_id and sim >= _SIMILAR_THRESHOLD][:_SIMILAR_K]
            for fid, sim in pairs:
                self.db.conn.execute(
                    "INSERT OR REPLACE INTO edges(src, dst, type, weight) VALUES (?,?,?,?)",
                    (f"file:{file_id}", f"file:{fid}", "similar_to", sim))
            self.db.conn.commit()
            pairs = pairs[:k]
        out = []
        for fid, sim in pairs:
            f = self.db.conn.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
            if f:
                out.append({"path": f["path"], "similarity": round(sim, 4)})
        return out

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
