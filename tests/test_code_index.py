"""
Tests for rag.code_index. Covers the pure-Python chunking and file-walking
logic with mocked Qdrant calls -- actual embedding/retrieval quality is
verified manually/on-device, since fastembed needs to download its model
from the internet on first use (not available in all CI/sandbox environments).
"""
from unittest.mock import MagicMock, patch

from rag.code_index import (
    _extract_chunks_from_file,
    _stable_id,
    index_repository,
    retrieve_similar_code,
)


def test_extracts_one_chunk_per_function_and_class():
    code = (
        "def add(a, b):\n    return a + b\n\n"
        "def subtract(a, b):\n    return a - b\n\n"
        "class Calculator:\n    def multiply(self, a, b):\n        return a * b\n"
    )
    chunks = _extract_chunks_from_file("calc.py", code)
    names = {c.chunk_name for c in chunks}
    assert names == {"add", "subtract", "Calculator"}


def test_falls_back_to_whole_file_on_syntax_error():
    broken = "def add(a, b\n    return a + b"
    chunks = _extract_chunks_from_file("broken.py", broken)
    assert len(chunks) == 1
    assert chunks[0].chunk_name == "<module>"


def test_falls_back_to_whole_file_when_no_functions_present():
    script = "x = 1\ny = 2\nprint(x + y)\n"
    chunks = _extract_chunks_from_file("script.py", script)
    assert len(chunks) == 1
    assert chunks[0].chunk_name == "<module>"


def test_stable_id_is_deterministic_and_unique():
    id1 = _stable_id("calc.py", "add", 3)
    id2 = _stable_id("calc.py", "add", 3)
    id3 = _stable_id("calc.py", "subtract", 6)
    assert id1 == id2
    assert id1 != id3


def test_index_repository_walks_subdirectories_and_skips_pycache(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "utils.py").write_text("def helper():\n    pass\n")
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "ignored.py").write_text("junk = 1\n")
    (tmp_path / "readme.txt").write_text("not python")

    fake_client = MagicMock()
    with patch("rag.code_index.get_client", return_value=fake_client):
        count = index_repository(str(tmp_path))

    assert count == 2
    file_paths = {m["file_path"] for m in fake_client.add.call_args.kwargs["metadata"]}
    assert file_paths == {"calc.py", "subdir/utils.py"}


def test_retrieve_returns_empty_list_when_collection_missing():
    fake_client = MagicMock()
    fake_client.collection_exists.return_value = False
    with patch("rag.code_index.get_client", return_value=fake_client):
        results = retrieve_similar_code("some query")
    assert results == []
    assert not fake_client.query.called