"""
Tests for sandbox.executor -- the auto-debugging loop's core mechanism.
"""
import subprocess
from sandbox.executor import run_tests_against_patch


def _make_repo(tmp_path, calc_content, test_content):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "calc.py").write_text(calc_content)
    (repo_dir / "test_calc.py").write_text(test_content)
    return str(repo_dir)


ORIGINAL_CALC = "def add(a, b):\n    return a + b\n"
ORIGINAL_TEST = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def test_correct_patch_passes(tmp_path):
    repo = _make_repo(tmp_path, ORIGINAL_CALC, ORIGINAL_TEST)
    good_patch = "def add(a, b):\n    return a + b\n"
    result = run_tests_against_patch(repo, "calc.py", good_patch)
    assert result.passed
    assert result.return_code == 0


def test_broken_patch_fails(tmp_path):
    repo = _make_repo(tmp_path, ORIGINAL_CALC, ORIGINAL_TEST)
    broken_patch = "def add(a, b):\n    return a - b\n"
    result = run_tests_against_patch(repo, "calc.py", broken_patch)
    assert not result.passed
    assert "assert" in result.stdout.lower()


def test_infinite_loop_times_out_instead_of_hanging(tmp_path):
    repo = _make_repo(tmp_path, ORIGINAL_CALC, ORIGINAL_TEST)
    bad_patch = "def add(a, b):\n    while True:\n        pass\n"
    result = run_tests_against_patch(repo, "calc.py", bad_patch, timeout_seconds=3)
    assert result.timed_out
    assert not result.passed


def test_original_repo_is_never_modified(tmp_path):
    repo = _make_repo(tmp_path, ORIGINAL_CALC, ORIGINAL_TEST)
    run_tests_against_patch(repo, "calc.py", "def add(a, b):\n    return 999\n")
    with open(f"{repo}/calc.py") as f:
        assert f.read() == ORIGINAL_CALC


def test_temp_directory_is_cleaned_up_after_run(tmp_path):
    import os
    import tempfile

    before = set(os.listdir(tempfile.gettempdir()))
    repo = _make_repo(tmp_path, ORIGINAL_CALC, ORIGINAL_TEST)
    run_tests_against_patch(repo, "calc.py", ORIGINAL_CALC)
    after = set(os.listdir(tempfile.gettempdir()))

    leftover = [d for d in (after - before) if d.startswith("code_review_sandbox_")]
    assert leftover == []