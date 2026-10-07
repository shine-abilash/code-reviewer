"""
refactor_agent.py
The Refactor Agent from the project spec.

As of Step 7, local-vs-cloud routing is delegated to core.model_router.
As of Step 8, the prompt optionally includes RAG context: real chunks of
code already in this repository that are semantically similar to the file
being fixed, retrieved from Qdrant (rag.code_index). This is what makes the
Refactor Agent's suggestions follow THIS repo's actual conventions instead
of generic textbook style -- e.g. if every other DB function in the repo
uses a context manager, the retrieved examples nudge the generated patch
toward that same pattern.

RAG context is strictly additive: if the repo hasn't been indexed, or
Qdrant/the embedding model isn't available, _build_rag_context_block()
returns "" and the agent proceeds exactly as it did before Step 8. A
missing or failed RAG lookup is never a reason to fail a refactor.
"""
from dataclasses import dataclass, field

from core.git_diff_parser import ChangedFile, get_file_content_at_commit
from core.llm_client import LLMResponseError
from core.model_router import route_llm_call, TaskType
from agents.security_agent import SecurityFinding
from agents.performance_agent import PerformanceFinding


@dataclass
class RefactorResult:
    file_path: str
    original_code: str
    patched_code: str = ""
    explanation: str = ""
    model_used: str = ""
    succeeded: bool = False
    error: str = ""
    used_rag_context: bool = False


SYSTEM_PROMPT = """You are an expert software engineer fixing real bugs found
by automated code review tools. You will be given the full original source
file and a list of specific issues (security and/or performance) that were
already detected. You may also be given reference code from elsewhere in
the same repository showing its existing conventions -- follow those
conventions where they don't conflict with fixing the listed issues.

Your job is to rewrite the ENTIRE file with all listed issues fixed, while
preserving all working functionality and the file's public function/class
names exactly as they are (other code may depend on them).

Rules:
- Fix ONLY the listed issues. Do not make unrelated stylistic changes.
- Preserve function signatures (names and parameters) unless the fix
  specifically requires changing them.
- The output must be complete, valid, runnable code -- not a diff or snippet.

Respond ONLY with JSON in exactly this shape, no other text:
{
  "patched_code": "<the complete corrected file content, as a single string with \\n for newlines>",
  "explanation": "<2-3 sentences summarizing what was changed and why>"
}
"""


def _build_rag_context_block(original_code: str, findings_text: str) -> str:
    """
    Retrieve chunks of the repo's OWN existing code that are semantically
    similar to the file being fixed. Returns "" (not an error) if nothing
    relevant is indexed -- RAG is an enhancement, never a requirement.
    """
    from rag.code_index import retrieve_similar_code  # local import: refactor_agent works even without rag/ set up

    try:
        query = original_code[:500] + "\n" + findings_text[:500]
        chunks = retrieve_similar_code(query, top_k=3)
    except Exception:
        return ""

    relevant = [c for c in chunks if c.score > 0.3][:3]
    if not relevant:
        return ""

    lines = [
        "Reference: here is how similar code already exists elsewhere in this repository.",
        "Follow this repo's existing style/conventions where it doesn't conflict with the fix:",
    ]
    for c in relevant:
        lines.append(f"\n--- {c.file_path} :: {c.chunk_name} (similarity={c.score:.2f}) ---")
        lines.append(c.content)

    return "\n".join(lines)


def _build_user_prompt(
    file_path: str,
    original_code: str,
    security_findings: list[SecurityFinding],
    performance_findings: list[PerformanceFinding],
    use_rag: bool = False,
) -> tuple[str, bool]:
    """Returns (prompt_text, rag_context_was_included)."""
    issues_lines = []
    for f in security_findings:
        issues_lines.append(f"- [SECURITY] Line {f.line_number}: {f.raw_issue}")
        if f.suggested_fix:
            issues_lines.append(f"  Suggested approach: {f.suggested_fix}")
    for f in performance_findings:
        issues_lines.append(f"- [PERFORMANCE] Line {f.line_number}: {f.raw_issue}")
        if f.suggested_fix:
            issues_lines.append(f"  Suggested approach: {f.suggested_fix}")

    issues_text = "\n".join(issues_lines)
    code_block = "-----\n" + original_code + "\n-----"

    rag_block = ""
    rag_included = False
    if use_rag:
        rag_context = _build_rag_context_block(original_code, issues_text)
        if rag_context:
            rag_block = f"\n\n{rag_context}"
            rag_included = True

    prompt = (
        f"File: {file_path}\n\n"
        f"Original code:\n{code_block}\n\n"
        f"Issues to fix:\n{issues_text}"
        f"{rag_block}\n\n"
        "Rewrite the complete file with these issues fixed, following the required JSON format."
    )
    return prompt, rag_included


def _build_retry_prompt(
    file_path: str, previous_patch: str, sandbox_stdout: str, sandbox_stderr: str
) -> str:
    output_block = "-----\n" + (sandbox_stdout or "(no stdout)") + "\n-----"
    error_block = "-----\n" + (sandbox_stderr or "(no stderr)") + "\n-----"
    code_block = "-----\n" + previous_patch + "\n-----"

    return (
        f"File: {file_path}\n\n"
        f"Your previous patch:\n{code_block}\n\n"
        f"Running the test suite against this patch FAILED. Test output:\n{output_block}\n\n"
        f"Test errors:\n{error_block}\n\n"
        "Fix your patch so the tests pass. Respond with the corrected complete "
        "file, following the same required JSON format."
    )


def generate_patch(
    repo_path: str,
    file: ChangedFile,
    security_findings: list[SecurityFinding],
    performance_findings: list[PerformanceFinding],
    commit_ref: str = "HEAD",
    use_rag: bool = True,
) -> RefactorResult:
    original_code = get_file_content_at_commit(repo_path, commit_ref, file.path)
    result = RefactorResult(file_path=file.path, original_code=original_code)

    if not security_findings and not performance_findings:
        result.error = "No findings to fix -- refactor not needed."
        return result

    prompt, rag_included = _build_user_prompt(
        file.path, original_code, security_findings, performance_findings, use_rag=use_rag
    )
    result.used_rag_context = rag_included

    try:
        response, backend_used = route_llm_call(
            task_type=TaskType.GENERATE_PATCH,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
        )
        result.patched_code = response.get("patched_code", "")
        result.explanation = response.get("explanation", "")
        result.model_used = backend_used
        result.succeeded = bool(result.patched_code.strip())
        if backend_used == "local" and result.succeeded:
            result.explanation = (
                "[Generated by local fallback model -- lower confidence, verify carefully] "
                + result.explanation
            )
        if not result.succeeded:
            result.error = f"{backend_used} model returned empty patched_code."
    except LLMResponseError as e:
        result.model_used = ""
        result.succeeded = False
        result.error = f"Both local and cloud models failed: {e}"

    return result


def generate_patch_with_auto_debug(
    repo_path: str,
    file: ChangedFile,
    security_findings: list[SecurityFinding],
    performance_findings: list[PerformanceFinding],
    commit_ref: str = "HEAD",
    max_attempts: int = 3,
    test_command: list[str] | None = None,
    use_rag: bool = True,
) -> RefactorResult:
    from sandbox.executor import run_tests_against_patch

    result = generate_patch(repo_path, file, security_findings, performance_findings, commit_ref, use_rag=use_rag)

    if not result.succeeded:
        return result

    for attempt in range(1, max_attempts + 1):
        sandbox_result = run_tests_against_patch(
            repo_path, file.path, result.patched_code, test_command=test_command
        )

        if sandbox_result.passed:
            result.explanation += f" [Verified: tests passed in sandbox, attempt {attempt}/{max_attempts}]"
            return result

        if attempt == max_attempts:
            result.succeeded = False
            result.error = (
                f"Patch failed sandbox tests after {max_attempts} attempt(s). "
                f"Last failure:\n{sandbox_result.stdout[-1000:]}"
            )
            return result

        retry_prompt = _build_retry_prompt(
            file.path, result.patched_code, sandbox_result.stdout, sandbox_result.stderr
        )
        try:
            response, backend_used = route_llm_call(
                task_type=TaskType.GENERATE_PATCH,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=retry_prompt,
            )
            new_code = response.get("patched_code", "")
            if new_code.strip():
                result.patched_code = new_code
                result.model_used = backend_used
                result.explanation = response.get("explanation", "") + f" [retry {attempt}]"
        except LLMResponseError:
            pass

    return result


def print_refactor_result(result: RefactorResult) -> None:
    print(f"Refactor Result: {result.file_path}")
    print(f"Model used: {result.model_used}")
    print(f"RAG context used: {result.used_rag_context}")
    print(f"Succeeded: {result.succeeded}")
    if result.error:
        print(f"Error/notes: {result.error}")
    if result.explanation:
        print(f"Explanation: {result.explanation}")
    if result.patched_code:
        print("\n--- Patched code ---")
        print(result.patched_code)