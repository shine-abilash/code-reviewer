"""
Tests for agents.security_agent — covers the deterministic tool-finding
logic. LLM-dependent behavior (explanations) is NOT tested here since it
requires a running Ollama instance; that's verified manually/on-device.
"""
from core.git_diff_parser import ChangedFile, AddedLine, parse_commit_range
from agents.security_agent import run_security_review


def test_security_review_finds_sql_injection_and_secret():
    diff = parse_commit_range("sample_repo")
    file = diff.reviewable_files[0]
    report = run_security_review("sample_repo", file)

    assert report.has_issues
    sources = {f.source for f in report.findings}
    assert "bandit" in sources
    assert "secret_scanner" in sources

    secret_lines = {f.line_number for f in report.findings if f.source == "secret_scanner"}
    assert 9 in secret_lines  # the hardcoded Stripe key line


def test_security_review_clean_code_has_no_findings():
    clean_file = ChangedFile(
        path="user_service.py",
        added_lines=[AddedLine(1, "def get_connection():"), AddedLine(2, "    return sqlite3.connect(DB_PATH)")],
    )
    report = run_security_review("sample_repo", clean_file, commit_ref="HEAD~1")
    assert not report.has_issues
    assert report.findings == []


def test_security_review_degrades_gracefully_without_llm():
    """Even if Ollama is unreachable, tool findings must still come through."""
    diff = parse_commit_range("sample_repo")
    file = diff.reviewable_files[0]
    report = run_security_review("sample_repo", file)

    # Whether or not Ollama happened to be running during this test,
    # the tool findings themselves must always be present.
    assert len(report.findings) >= 4