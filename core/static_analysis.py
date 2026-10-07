"""
static_analysis.py
Thin wrapper around bandit (Python security static analyzer), invoked via
subprocess with JSON output. Subprocess + JSON is used instead of bandit's
internal Python API because that internal API is not considered stable
across bandit versions — JSON output is bandit's actual public contract.
"""
import json
import subprocess
from dataclasses import dataclass


@dataclass
class StaticAnalysisFinding:
    line_number: int
    severity: str       # LOW | MEDIUM | HIGH
    confidence: str      # LOW | MEDIUM | HIGH
    issue_text: str
    test_id: str          # bandit's rule ID, e.g. "B608"
    cwe_id: int | None = None


def run_bandit(file_path: str) -> list[StaticAnalysisFinding]:
    """
    Run bandit against a single Python file and return structured findings.
    Returns an empty list for non-Python files or files with no issues.
    """
    if not file_path.endswith(".py"):
        return []

    result = subprocess.run(
        ["python3", "-m", "bandit", "-f", "json", file_path],
        capture_output=True,
        text=True,
    )

    # bandit exits non-zero when it finds issues — that's expected, not an error.
    # Only a genuinely empty/unparseable stdout indicates a real failure.
    if not result.stdout.strip():
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    findings = []
    for item in data.get("results", []):
        findings.append(
            StaticAnalysisFinding(
                line_number=item["line_number"],
                severity=item["issue_severity"],
                confidence=item["issue_confidence"],
                issue_text=item["issue_text"],
                test_id=item["test_id"],
                cwe_id=item.get("issue_cwe", {}).get("id"),
            )
        )
    return findings

@dataclass
class StyleFinding:
    line_number: int
    code: str            # ruff's rule code, e.g. "F841"
    message: str
    docs_url: str = ""


def run_ruff(file_path: str) -> list[StyleFinding]:
    """
    Run ruff (fast Python linter) against a single file and return
    structured style/lint findings.
    """
    if not file_path.endswith(".py"):
        return []

    result = subprocess.run(
        ["python3", "-m", "ruff", "check", file_path, "--output-format=json"],
        capture_output=True,
        text=True,
    )

    if not result.stdout.strip():
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    findings = []
    for item in data:
        findings.append(
            StyleFinding(
                line_number=item["location"]["row"],
                code=item["code"],
                message=item["message"],
                docs_url=item.get("url", ""),
            )
        )
    return findings


@dataclass
class ComplexityFinding:
    function_name: str
    line_number: int
    complexity: int
    rank: str  # A (simplest) through F (most complex), radon's letter grade


# Functions at or above this complexity score are flagged as worth a closer
# look. Radon's own convention: A=1-5 (simple), B=6-10, C=11-20, D=21-30,
# E=31-40, F=41+. We flag B and above — "simple" functions are left alone.
COMPLEXITY_FLAG_THRESHOLD = 6


def run_radon_complexity(file_path: str) -> list[ComplexityFinding]:
    """
    Run radon cyclomatic complexity analysis and return findings for
    functions at or above COMPLEXITY_FLAG_THRESHOLD.
    """
    if not file_path.endswith(".py"):
        return []

    result = subprocess.run(
        ["python3", "-m", "radon", "cc", file_path, "-j"],
        capture_output=True,
        text=True,
    )

    if not result.stdout.strip():
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    findings = []
    for file_results in data.values():
        for item in file_results:
            if item["type"] != "function":
                continue
            if item["complexity"] < COMPLEXITY_FLAG_THRESHOLD:
                continue
            findings.append(
                ComplexityFinding(
                    function_name=item["name"],
                    line_number=item["lineno"],
                    complexity=item["complexity"],
                    rank=item["rank"],
                )
            )
    return findings

    