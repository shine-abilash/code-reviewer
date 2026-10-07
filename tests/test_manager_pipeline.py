"""
Tests for core.git_diff_parser and agents.manager_agent.
Run with: pytest tests/test_manager_pipeline.py -v
"""
import subprocess
from pathlib import Path

import pytest

from core.git_diff_parser import ChangedFile, AddedLine, ParsedDiff, parse_commit_range
from agents.manager_agent import build_review_plan


@pytest.fixture
def temp_git_repo(tmp_path):
    """Creates a throwaway git repo with a clean commit then a risky commit."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    def run(*args):
        subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@t.com")
    run("config", "user.name", "T")

    (repo_dir / "app.py").write_text("def add(a, b):\n    return a + b\n")
    run("add", "-A")
    run("commit", "-q", "-m", "clean commit")

    (repo_dir / "app.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "def run_query(user_id):\n"
        "    query = \"SELECT * FROM t WHERE id=\" + str(user_id)\n"
        "    return query\n"
    )
    run("add", "-A")
    run("commit", "-q", "-m", "risky commit")

    return str(repo_dir)


def test_parse_commit_range_detects_added_lines(temp_git_repo):
    diff = parse_commit_range(temp_git_repo)
    assert len(diff.changed_files) == 1
    assert diff.changed_files[0].path == "app.py"
    assert len(diff.changed_files[0].added_lines) == 4  # includes the blank separator line


def test_reviewable_files_excludes_deleted(tmp_path):
    file = ChangedFile(path="gone.py", is_deleted=True)
    parsed = ParsedDiff(base_commit="a", head_commit="b", changed_files=[file])
    assert parsed.reviewable_files == []


def test_reviewable_files_excludes_non_code_extensions():
    file = ChangedFile(path="notes.txt")
    parsed = ParsedDiff(base_commit="a", head_commit="b", changed_files=[file])
    assert parsed.reviewable_files == []


def test_manager_flags_sql_injection_pattern_as_high_priority(temp_git_repo):
    diff = parse_commit_range(temp_git_repo)
    plan = build_review_plan(diff)
    assert len(plan) == 1
    assert plan[0].priority == "high"
    assert "security" in plan[0].assigned_agents


def test_manager_marks_safe_code_as_normal_priority():
    safe_file = ChangedFile(
        path="formatters.py",
        added_lines=[AddedLine(1, "def double(x):"), AddedLine(2, "    return x * 2")],
    )
    parsed = ParsedDiff(base_commit="a", head_commit="b", changed_files=[safe_file])
    plan = build_review_plan(parsed)
    assert plan[0].priority == "normal"
    # still gets a routine security pass, just not urgent
    assert "security" in plan[0].assigned_agents


def test_high_priority_tasks_sorted_first():
    risky = ChangedFile(
        path="risky.py",
        added_lines=[AddedLine(1, "cursor.execute(query)")],
    )
    safe = ChangedFile(
        path="safe.py",
        added_lines=[AddedLine(1, "return 1")],
    )
    # deliberately put safe file first in the diff to test sorting works
    parsed = ParsedDiff(base_commit="a", head_commit="b", changed_files=[safe, risky])
    plan = build_review_plan(parsed)
    assert plan[0].file.path == "risky.py"
    assert plan[0].priority == "high"
