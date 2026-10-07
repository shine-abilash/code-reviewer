"""
security_agent.py
The Security Agent from the project spec.

Design decision: we do NOT ask the LLM to find vulnerabilities from scratch.
Small local models (like tinyllama) are inconsistent at that -- real bugs
get missed, phantom ones get invented. Instead:
  1. Deterministic tools (bandit, our secret_scanner) find the *facts* --
     which lines, what CWE, what pattern matched. This is reliable and cheap.
  2. The LLM's job is narrower and more achievable: turn those facts into a
     plain-English explanation and a concrete suggested fix.

REAL-WORLD FINDING (tested against tinyllama:1.1b): even with format="json"
forcing syntactically valid JSON, the model sometimes echoes the schema's
placeholder description back as the literal value. We detect this specific
failure mode and fall back to pre-written, rule-based explanations.

As of Step 7, the actual local-vs-cloud decision is delegated to
core.model_router.route_llm_call() rather than hardcoded here. This agent
passes along the file's `priority` (set by the Manager Agent in Step 2) so
a HIGH priority file -- one that already matched a suspicious keyword --
gets escalated to the stronger cloud model even for this "just explain it"
task, per the routing policy documented in model_router.py.
"""
from dataclasses import dataclass, field

from core.git_diff_parser import ChangedFile, get_file_content_at_commit
from core.llm_client import LLMResponseError
from core.model_router import route_llm_call, TaskType
from core.secret_scanner import scan_lines
from core.static_analysis import run_bandit


@dataclass
class SecurityFinding:
    line_number: int
    severity: str
    source: str          # "bandit" | "secret_scanner"
    raw_issue: str
    rule_id: str = ""
    explanation: str = ""
    suggested_fix: str = ""


@dataclass
class SecurityReport:
    file_path: str
    findings: list[SecurityFinding] = field(default_factory=list)
    llm_summary: str = ""
    llm_explanation_failed: bool = False
    model_used: str = ""  # "local" | "cloud" | "" (no LLM call made)

    @property
    def has_issues(self) -> bool:
        return len(self.findings) > 0


SYSTEM_PROMPT = """You are a security code reviewer assistant. You will be given
a code file and a list of security issues already detected by automated tools.
Your job is ONLY to explain each issue in plain English and suggest a concrete
fix. Do NOT invent new issues that are not in the provided list.

Respond ONLY with JSON in exactly this shape, no other text:
{
  "overall_summary": "<write 1-2 sentences here describing THIS specific file's issues>",
  "explanations": [
    {"line_number": <int>, "explanation": "<plain English explanation>", "suggested_fix": "<concrete fix>"}
  ]
}
"""

_PLACEHOLDER_ECHO_MARKERS = [
    "one or two sentence summary",
    "write 1-2 sentences here",
    "<plain english explanation>",
    "<concrete fix>",
]

_FALLBACK_EXPLANATIONS = {
    "B608": {
        "explanation": (
            "This line builds a SQL query by directly concatenating a variable into "
            "the query string. If that variable comes from user input, an attacker "
            "can inject arbitrary SQL (e.g. to bypass login or read other users' data)."
        ),
        "suggested_fix": (
            "Use parameterized queries instead: "
            "cursor.execute('SELECT ... WHERE id = ?', (user_id,)) "
            "-- never build SQL with string concatenation or f-strings."
        ),
    },
    "B105": {
        "explanation": "A hardcoded password-like string was found in source code.",
        "suggested_fix": "Move this value to an environment variable or a secrets manager.",
    },
}

_FALLBACK_SECRET_EXPLANATION = {
    "explanation": (
        "A real-looking secret/API key is committed directly in source code. Anyone "
        "with read access to this repository (including its full git history) can "
        "see and use this credential."
    ),
    "suggested_fix": (
        "Revoke this key immediately (assume it's compromised once committed), then "
        "load it from an environment variable via os.environ or a .env file that is "
        "in .gitignore -- never commit real credentials."
    ),
}


def _is_degenerate_output(summary: str, explanations: list[dict], findings_count: int) -> bool:
    summary_lower = summary.lower().strip()
    if any(marker in summary_lower for marker in _PLACEHOLDER_ECHO_MARKERS):
        return True
    if findings_count > 0 and len(explanations) == 0:
        return True
    return False


def _apply_fallback_explanations(findings: list[SecurityFinding]) -> None:
    for finding in findings:
        if finding.explanation:
            continue
        if finding.source == "bandit" and finding.rule_id in _FALLBACK_EXPLANATIONS:
            fallback = _FALLBACK_EXPLANATIONS[finding.rule_id]
        elif finding.source == "secret_scanner":
            fallback = _FALLBACK_SECRET_EXPLANATION
        else:
            continue
        finding.explanation = fallback["explanation"]
        finding.suggested_fix = fallback["suggested_fix"]


def _build_user_prompt(file_path: str, file_content: str, findings: list[SecurityFinding]) -> str:
    findings_summary = "\n".join(
        f"- Line {f.line_number} ({f.source}): {f.raw_issue}" for f in findings
    )
    code_block = "-----\n" + file_content + "\n-----"
    return (
        f"File: {file_path}\n\n"
        f"Code:\n{code_block}\n\n"
        f"Detected issues:\n{findings_summary}\n\n"
        "Explain each issue and suggest a fix, following the required JSON format."
    )


def run_security_review(
    repo_path: str, file: ChangedFile, commit_ref: str = "HEAD", priority: str = "normal"
) -> SecurityReport:
    report = SecurityReport(file_path=file.path)

    full_content = get_file_content_at_commit(repo_path, commit_ref, file.path)

    tmp_path = f"/tmp/_security_review_{hash(file.path) & 0xffffffff}.py"
    with open(tmp_path, "w") as f:
        f.write(full_content)

    bandit_findings = run_bandit(tmp_path)
    for bf in bandit_findings:
        report.findings.append(
            SecurityFinding(
                line_number=bf.line_number,
                severity=bf.severity,
                source="bandit",
                raw_issue=f"[{bf.test_id}] {bf.issue_text}",
                rule_id=bf.test_id,
            )
        )

    added_lines_tuples = [(l.line_number, l.content) for l in file.added_lines]
    secret_findings = scan_lines(added_lines_tuples)
    for sf in secret_findings:
        report.findings.append(
            SecurityFinding(
                line_number=sf.line_number,
                severity="HIGH",
                source="secret_scanner",
                raw_issue=f"Hardcoded secret detected: {sf.secret_type}",
                rule_id=sf.secret_type,
            )
        )

    if not report.findings:
        report.llm_summary = "No security issues detected by static analysis tools."
        return report

    # LLM explanation, routed via the hybrid model router (Step 7) instead
    # of a hardcoded local-only call. `priority` (from the Manager Agent)
    # decides whether this specific file gets escalated to the cloud model.
    try:
        result, backend_used = route_llm_call(
            task_type=TaskType.EXPLAIN,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=_build_user_prompt(file.path, full_content, report.findings),
            priority=priority,
        )
        report.model_used = backend_used
        summary = result.get("overall_summary", "")
        explanations = result.get("explanations", [])

        if _is_degenerate_output(summary, explanations, len(report.findings)):
            report.llm_explanation_failed = True
            report.llm_summary = (
                f"{len(report.findings)} issue(s) found by static analysis. "
                f"{backend_used.capitalize()} model returned a low-quality/placeholder "
                "response -- using built-in explanations instead."
            )
        else:
            report.llm_summary = summary
            explanations_by_line = {
                e["line_number"]: e for e in explanations if "line_number" in e
            }
            for finding in report.findings:
                match = explanations_by_line.get(finding.line_number)
                if match:
                    finding.explanation = match.get("explanation", "")
                    finding.suggested_fix = match.get("suggested_fix", "")

    except LLMResponseError as e:
        report.llm_explanation_failed = True
        report.llm_summary = (
            f"{len(report.findings)} issue(s) found by static analysis. "
            f"Both local and cloud models unavailable ({e}). Using built-in explanations."
        )

    _apply_fallback_explanations(report.findings)
    return report


def print_security_report(report: SecurityReport) -> None:
    print(f"Security Report: {report.file_path}")
    if report.model_used:
        print(f"(Explanations generated by: {report.model_used})")
    print(f"Summary: {report.llm_summary}\n")
    if not report.has_issues:
        print("  No issues found.")
        return
    for f in report.findings:
        print(f"  [{f.severity}] Line {f.line_number} ({f.source}): {f.raw_issue}")
        if f.explanation:
            print(f"    Explanation: {f.explanation}")
        if f.suggested_fix:
            print(f"    Suggested fix: {f.suggested_fix}")
        print()