"""
Tests for the auto-debugging self-correction loop
(agents.refactor_agent.generate_patch_with_auto_debug), updated for Step 7
to mock route_llm_call instead of the old direct model-call functions.
"""
import subprocess
from unittest.mock import patch

from core.git_diff_parser import ChangedFile
from agents.security_agent import SecurityFinding
from agents.refactor_agent import generate_patch_with_auto_debug


def _make_repo(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo_dir / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t.com"],
                ["git", "config", "user.name", "T"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "init"]):
        subprocess.run(cmd, cwd=repo_dir, check=True)
    return str(repo_dir)


def test_succeeds_immediately_when_first_patch_is_correct(tmp_path):
    repo = _make_repo(tmp_path)
    calls = {"n": 0}

    def correct_first_try(task_type, system_prompt, user_prompt):
        calls["n"] += 1
        return {"patched_code": "def add(a, b):\n    return a + b\n", "explanation": "ok"}, "cloud"

    with patch("agents.refactor_agent.route_llm_call", side_effect=correct_first_try):
        file = ChangedFile(path="calc.py")
        finding = SecurityFinding(line_number=1, severity="LOW", source="bandit", raw_issue="dummy")
        result = generate_patch_with_auto_debug(repo, file, [finding], [], max_attempts=3)

    assert result.succeeded
    assert calls["n"] == 1


def test_self_corrects_after_one_failed_attempt(tmp_path):
    repo = _make_repo(tmp_path)
    calls = {"n": 0}

    def fails_then_fixes(task_type, system_prompt, user_prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"patched_code": "def add(a, b):\n    return a - b\n", "explanation": "broken"}, "cloud"
        return {"patched_code": "def add(a, b):\n    return a + b\n", "explanation": "fixed"}, "cloud"

    with patch("agents.refactor_agent.route_llm_call", side_effect=fails_then_fixes):
        file = ChangedFile(path="calc.py")
        finding = SecurityFinding(line_number=1, severity="LOW", source="bandit", raw_issue="dummy")
        result = generate_patch_with_auto_debug(repo, file, [finding], [], max_attempts=3)

    assert result.succeeded
    assert calls["n"] == 2


def test_gives_up_cleanly_after_max_attempts(tmp_path):
    repo = _make_repo(tmp_path)
    calls = {"n": 0}

    def always_broken(task_type, system_prompt, user_prompt):
        calls["n"] += 1
        return {"patched_code": "def add(a, b):\n    return a - b\n", "explanation": "still broken"}, "cloud"

    with patch("agents.refactor_agent.route_llm_call", side_effect=always_broken):
        file = ChangedFile(path="calc.py")
        finding = SecurityFinding(line_number=1, severity="LOW", source="bandit", raw_issue="dummy")
        result = generate_patch_with_auto_debug(repo, file, [finding], [], max_attempts=3)

    assert not result.succeeded
    assert calls["n"] == 3
    assert "3 attempt" in result.error