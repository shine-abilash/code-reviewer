"""
Tests for the RAG integration in agents.refactor_agent -- verifies RAG
context is additive (enhances when available, never blocks when absent),
and that low-relevance matches are filtered out.
"""
from unittest.mock import patch

from core.git_diff_parser import ChangedFile
from agents.security_agent import SecurityFinding
from agents.refactor_agent import generate_patch
from rag.code_index import RetrievedChunk


def _make_repo(tmp_path):
    import subprocess
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t.com"],
                ["git", "config", "user.name", "T"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "init"]):
        subprocess.run(cmd, cwd=repo_dir, check=True)
    return str(repo_dir)


def _make_fake_route(captured):
    def fake_route(task_type, system_prompt, user_prompt):
        captured.append(user_prompt)
        return {"patched_code": "def add(a, b):\n    return a + b\n", "explanation": "ok"}, "cloud"
    return fake_route


def test_relevant_rag_context_gets_included_in_prompt(tmp_path):
    repo = _make_repo(tmp_path)
    chunk = RetrievedChunk(file_path="other.py", chunk_name="similar_func", content="def similar(): pass", score=0.85)
    captured = []

    with patch("agents.refactor_agent.route_llm_call", side_effect=_make_fake_route(captured)), \
         patch("rag.code_index.retrieve_similar_code", return_value=[chunk]):
        file = ChangedFile(path="calc.py")
        finding = SecurityFinding(line_number=1, severity="HIGH", source="bandit", raw_issue="dummy")
        result = generate_patch(repo, file, [finding], [], use_rag=True)

    assert result.used_rag_context
    assert "similar_func" in captured[0]


def test_missing_rag_index_does_not_block_refactor(tmp_path):
    repo = _make_repo(tmp_path)
    captured = []

    with patch("agents.refactor_agent.route_llm_call", side_effect=_make_fake_route(captured)), \
         patch("rag.code_index.retrieve_similar_code", return_value=[]):
        file = ChangedFile(path="calc.py")
        finding = SecurityFinding(line_number=1, severity="HIGH", source="bandit", raw_issue="dummy")
        result = generate_patch(repo, file, [finding], [], use_rag=True)

    assert not result.used_rag_context
    assert result.succeeded


def test_low_relevance_chunks_are_filtered_out(tmp_path):
    repo = _make_repo(tmp_path)
    noise = RetrievedChunk(file_path="unrelated.py", chunk_name="noise", content="x = 1", score=0.12)
    captured = []

    with patch("agents.refactor_agent.route_llm_call", side_effect=_make_fake_route(captured)), \
         patch("rag.code_index.retrieve_similar_code", return_value=[noise]):
        file = ChangedFile(path="calc.py")
        finding = SecurityFinding(line_number=1, severity="HIGH", source="bandit", raw_issue="dummy")
        result = generate_patch(repo, file, [finding], [], use_rag=True)

    assert not result.used_rag_context
    assert "noise" not in captured[0]


def test_use_rag_false_skips_retrieval_entirely(tmp_path):
    repo = _make_repo(tmp_path)
    captured = []

    with patch("agents.refactor_agent.route_llm_call", side_effect=_make_fake_route(captured)), \
         patch("rag.code_index.retrieve_similar_code") as mock_retrieve:
        file = ChangedFile(path="calc.py")
        finding = SecurityFinding(line_number=1, severity="HIGH", source="bandit", raw_issue="dummy")
        generate_patch(repo, file, [finding], [], use_rag=False)

    assert not mock_retrieve.called