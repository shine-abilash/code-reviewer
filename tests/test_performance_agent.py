"""
Tests for agents.performance_agent — covers the deterministic tool-finding
logic (ruff + radon). LLM explanation quality is verified manually/on-device.
"""
from core.git_diff_parser import ChangedFile
from agents.performance_agent import run_performance_review


def test_performance_review_flags_high_complexity_function():
    file = ChangedFile(path="user_service.py")
    report = run_performance_review("sample_repo", file, commit_ref="HEAD")

    assert report.has_issues
    radon_findings = [f for f in report.findings if f.source == "radon"]
    assert len(radon_findings) == 1
    assert "find_duplicate_emails" in radon_findings[0].raw_issue
    assert radon_findings[0].explanation != ""


def test_performance_review_clean_baseline_has_no_findings():
    file = ChangedFile(path="user_service.py")
    report = run_performance_review("sample_repo", file, commit_ref="HEAD~1")
    assert not report.has_issues
    assert report.findings == []


def test_performance_review_degrades_gracefully_without_llm():
    file = ChangedFile(path="user_service.py")
    report = run_performance_review("sample_repo", file, commit_ref="HEAD")
    assert all(f.explanation != "" for f in report.findings)