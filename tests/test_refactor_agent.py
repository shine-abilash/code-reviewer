"""
Tests for agents.refactor_agent -- covers the deterministic control-flow
logic. As of Step 7, mocks target core.model_router.route_llm_call (the
single entry point) rather than the old separate call_local/call_cloud
functions, since refactor_agent.py now delegates routing entirely to it.
"""
import subprocess
from unittest.mock import patch

from core.git_diff_parser import ChangedFile
from agents.security_agent import SecurityFinding
from agents.refactor_agent import generate_patch
from core.llm_client import LLMResponseError


def _make_repo(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "f.py").write_text("def f(x):\n    return x\n")
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t.com"],
                ["git", "config", "user.name", "T"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "c1"]):
        subprocess.run(cmd, cwd=repo_dir, check=True)
    return str(repo_dir)


def test_no_findings_skips_llm_entirely(tmp_path):
    repo = _make_repo(tmp_path)
    with patch("agents.refactor_agent.route_llm_call") as mock_route:
        file = ChangedFile(path="f.py")
        result = generate_patch(repo, file, [], [], commit_ref="HEAD")

    assert not result.succeeded
    assert "not needed" in result.error.lower()
    assert not mock_route.called


def test_uses_router_and_records_backend_used(tmp_path):
    repo = _make_repo(tmp_path)
    with patch(
        "agents.refactor_agent.route_llm_call",
        return_value=({"patched_code": "def f(x):\n    return x\n", "explanation": "ok"}, "cloud"),
    ) as mock_route:
        file = ChangedFile(path="f.py")
        finding = SecurityFinding(line_number=1, severity="HIGH", source="bandit", raw_issue="dummy")
        result = generate_patch(repo, file, [finding], [], commit_ref="HEAD")

    assert result.succeeded
    assert result.model_used == "cloud"
    assert mock_route.called


def test_handles_total_routing_failure_gracefully(tmp_path):
    repo = _make_repo(tmp_path)
    with patch("agents.refactor_agent.route_llm_call", side_effect=LLMResponseError("both down")):
        file = ChangedFile(path="f.py")
        finding = SecurityFinding(line_number=1, severity="HIGH", source="bandit", raw_issue="dummy")
        result = generate_patch(repo, file, [finding], [], commit_ref="HEAD")

    assert not result.succeeded
    assert "both down" in result.error.lower() or "failed" in result.error.lower()