"""
code_index.py
The Contextual RAG Integration from the project spec: index a repository's
own codebase into Qdrant so agents can retrieve real examples of "how this
codebase actually does things" rather than giving generic textbook advice.

Why chunk by function instead of by whole file: a whole file as one
document loses precision -- if a repo has one 500-line file with one
relevant function and 15 irrelevant ones, embedding the whole file dilutes
the vector with irrelevant content and a similarity search is less likely
to surface it for a specific question. Chunking by function (using Python's
own `ast` module, not regex) gives each retrievable unit a single, coherent
purpose, which matches how a developer would actually search ("show me
functions like X") far better than whole-file embedding.

Uses Qdrant's local, on-disk mode (QdrantClient(path=...)) -- no separate
Qdrant server process required, matching settings.qdrant_mode == "local"
from .env. The embedding model (fastembed's default, BAAI/bge-small-en) is
downloaded once on first use and cached locally by fastembed itself.
"""
import ast
import hashlib
import os
from dataclasses import dataclass

from qdrant_client import QdrantClient

from core.config import settings


@dataclass
class CodeChunk:
    file_path: str
    chunk_name: str      # function/class name, or "<module>" for module-level code
    content: str
    start_line: int


@dataclass
class RetrievedChunk:
    file_path: str
    chunk_name: str
    content: str
    score: float


_client: QdrantClient | None = None


def get_client() -> QdrantClient:
    """
    Lazily create and reuse a single Qdrant client for this process.
    Local on-disk mode -- data persists in settings.qdrant_path across runs,
    so a repo doesn't need to be re-indexed on every single review.
    """
    global _client
    if _client is None:
        _client = QdrantClient(path=settings.qdrant_path)
    return _client


def _extract_chunks_from_file(file_path: str, content: str) -> list[CodeChunk]:
    """
    Parse a Python file with `ast` and extract one chunk per top-level
    function and class. Falls back to treating the whole file as one chunk
    if the file has a syntax error (so indexing never hard-fails on a
    single bad file) or has no top-level functions/classes to extract.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return [CodeChunk(file_path=file_path, chunk_name="<module>", content=content, start_line=1)]

    lines = content.splitlines()
    chunks = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno - 1
            end = getattr(node, "end_lineno", start + 1)
            chunk_source = "\n".join(lines[start:end])
            chunks.append(
                CodeChunk(
                    file_path=file_path,
                    chunk_name=node.name,
                    content=chunk_source,
                    start_line=node.lineno,
                )
            )

    if not chunks:
        chunks.append(CodeChunk(file_path=file_path, chunk_name="<module>", content=content, start_line=1))

    return chunks


def _stable_id(file_path: str, chunk_name: str, start_line: int) -> int:
    """
    Qdrant point IDs must be int or UUID. Derive a stable integer from the
    chunk's identity so re-indexing the same unchanged chunk overwrites
    the same point instead of creating a duplicate.
    """
    raw = f"{file_path}:{chunk_name}:{start_line}"
    return int(hashlib.sha256(raw.encode()).hexdigest()[:16], 16)


def index_repository(
    repo_path: str,
    collection_name: str = "repo_code",
    file_extensions: tuple[str, ...] = (".py",),
) -> int:
    """
    Walk the repo, chunk every matching file by function/class, and index
    all chunks into the given Qdrant collection. Returns the number of
    chunks indexed.
    """
    client = get_client()
    all_chunks: list[CodeChunk] = []

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv", "node_modules")]
        for filename in files:
            if not filename.endswith(file_extensions):
                continue
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, repo_path)
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            all_chunks.extend(_extract_chunks_from_file(rel_path, content))

    if not all_chunks:
        return 0

    documents = [c.content for c in all_chunks]
    ids = [_stable_id(c.file_path, c.chunk_name, c.start_line) for c in all_chunks]
    metadata = [
        {"file_path": c.file_path, "chunk_name": c.chunk_name, "start_line": c.start_line}
        for c in all_chunks
    ]

    client.add(
        collection_name=collection_name,
        documents=documents,
        metadata=metadata,
        ids=ids,
    )

    return len(all_chunks)


def retrieve_similar_code(
    query_text: str,
    collection_name: str = "repo_code",
    top_k: int = 3,
) -> list[RetrievedChunk]:
    """
    Find the top_k chunks in the indexed repo most semantically similar to
    query_text. Returns an empty list (not an error) if the collection
    doesn't exist yet.
    """
    client = get_client()

    if not client.collection_exists(collection_name):
        return []

    results = client.query(
        collection_name=collection_name,
        query_text=query_text,
        limit=top_k,
    )

    return [
        RetrievedChunk(
            file_path=r.metadata.get("file_path", "?"),
            chunk_name=r.metadata.get("chunk_name", "?"),
            content=r.document,
            score=r.score,
        )
        for r in results
    ]
