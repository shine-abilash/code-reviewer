"""
performance_agent.py
The Performance & Style Agent from the project spec.

Same design pattern as security_agent.py: ruff (linting) and radon
(cyclomatic complexity) find the *facts*, the LLM explains them, and a
fallback covers degenerate/failed LLM output. As of Step 7, the LLM call
goes through core.model_router.route_llm_call() so the local-vs-cloud
decision is centralized and driven by the same evidence-based policy used
by every other agent.
"""
from dataclasses import dataclass, field

from core.git_diff_parser import ChangedFile, get_file_content_at_commit
from core.llm_client import LLMResponseError
from core.model_router import route_llm_call, TaskType
from core.static_analysis import run_ruff, run_radon_complexity


@dataclass
class PerformanceFinding:
    line_number: int
    source: str            # "ruff" | "radon"
    code: str               # ruff rule code, or radon rank (A-F)
    raw_issue: str
    explanation: str = ""
    suggested_fix: str = ""


@dataclass
class PerformanceReport:
    file_path: str
    findings: list[PerformanceFinding] = field(default_factory=list)
    llm_summary: str = ""
    llm_explanation_failed: bool = False
    model_used: str = ""

    @property
    def has_issues(self) -> bool:
        return len(self.findings) > 0


SYSTEM_PROMPT = """You are a code performance and style reviewer. You will be
given a code file and a list of complexity/style issues already detected by
automated tools. Your job is ONLY to explain each issue in plain English and
suggest a concrete improvement. Do NOT invent new issues not in the list.

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

_RUFF_FALLBACKS = {
    "F401": {
        "explanation": "An imported module is never used in this file.",
        "suggested_fix": "Remove the unused import to keep the file clean and imports honest.",
    },
    "F841": {
        "explanation": "A variable is assigned but never read afterward -- likely dead code or a leftover from debugging.",
        "suggested_fix": "Remove the unused variable, or use it if it was meant to be used.",
    },
    "E741": {
        "explanation": "A variable name like 'l', 'O', or 'I' is easily confused with the digits 1 or 0.",
        "suggested_fix": "Rename it to something descriptive (e.g. 'items' instead of 'l').",
    },
}

_RADON_FALLBACK = {
    "explanation": (
        "This function has high cyclomatic complexity, usually caused by deeply "
        "nested loops/conditionals. High complexity functions are harder to test, "
        "harder to reason about, and are frequently where performance bottlenecks "
        "hide (e.g. an accidental O(n^2) nested loop)."
    ),
    "suggested_fix": (
        "Break the function into smaller helper functions, replace nested loops "
        "with a set/dict lookup where possible, or use built-in optimized "
        "operations instead of manual iteration."
    ),
}


def _is_degenerate_output(summary: str, explanations: list[dict], findings_count: int) -> bool:
    summary_lower = summary.lower().strip()
    if any(marker in summary_lower for marker in _PLACEHOLDER_ECHO_MARKERS):
        return True
    if findings_count > 0 and len(explanations) == 0:
        return True
    return False


def _apply_fallback_explanations(findings: list[PerformanceFinding]) -> None:
    for finding in findings:
        if finding.explanation:
            continue
        if finding.source == "ruff" and finding.code in _RUFF_FALLBACKS:
            fallback = _RUFF_FALLBACKS[finding.code]
        elif finding.source == "radon":
            fallback = _RADON_FALLBACK
        else:
            continue
        finding.explanation = fallback["explanation"]
        finding.suggested_fix = fallback["suggested_fix"]


def _build_user_prompt(file_path: str, file_content: str, findings: list[PerformanceFinding]) -> str:
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


def run_performance_review(
    repo_path: str, file: ChangedFile, commit_ref: str = "HEAD", priority: str = "normal"
) -> PerformanceReport:
    report = PerformanceReport(file_path=file.path)

    full_content = get_file_content_at_commit(repo_path, commit_ref, file.path)

    tmp_path = f"/tmp/_perf_review_{hash(file.path) & 0xffffffff}.py"
    with open(tmp_path, "w") as f:
        f.write(full_content)

    for rf in run_ruff(tmp_path):
        report.findings.append(
            PerformanceFinding(
                line_number=rf.line_number,
                source="ruff",
                code=rf.code,
                raw_issue=f"[{rf.code}] {rf.message}",
            )
        )

    for cf in run_radon_complexity(tmp_path):
        report.findings.append(
            PerformanceFinding(
                line_number=cf.line_number,
                source="radon",
                code=cf.rank,
                raw_issue=(
                    f"Function '{cf.function_name}' has cyclomatic complexity "
                    f"{cf.complexity} (rank {cf.rank})"
                ),
            )
        )

    if not report.findings:
        report.llm_summary = "No performance or style issues detected by static analysis tools."
        return report

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


def print_performance_report(report: PerformanceReport) -> None:
    print(f"Performance & Style Report: {report.file_path}")
    if report.model_used:
        print(f"(Explanations generated by: {report.model_used})")
    print(f"Summary: {report.llm_summary}\n")
    if not report.has_issues:
        print("  No issues found.")
        return
    for f in report.findings:
        print(f"  [{f.source}] Line {f.line_number}: {f.raw_issue}")
        if f.explanation:
            print(f"    Explanation: {f.explanation}")
        if f.suggested_fix:
            print(f"    Suggested fix: {f.suggested_fix}")
        print()