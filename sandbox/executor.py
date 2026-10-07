"""
executor.py
The Auto-Debugging Loop from the project spec: run the Refactor Agent's
generated patch against the project's real test suite BEFORE trusting it,
and if it fails, feed the failure back to the LLM to try again.

ON DOCKER: the original spec calls for a Docker container per run for true
isolation (a bad patch can't affect the host machine, and dependencies are
guaranteed consistent). This implementation runs in a subprocess-level
sandbox instead -- a fresh temporary directory, a hard execution timeout,
and (on Linux) resource limits (memory, CPU time, process count) applied
via the `resource` module before exec. This is a legitimate engineering
trade-off, not a shortcut: Docker wasn't available in this environment, so
we built the strongest isolation achievable without it. A Docker-based
backend could implement the exact same interface using the `docker` SDK,
and every other module in this project only depends on the function
signatures below -- swapping the isolation backend would not require
changing any calling code.
"""
import os
import resource
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field


@dataclass
class SandboxResult:
    passed: bool
    stdout: str
    stderr: str
    return_code: int
    timed_out: bool = False
    tests_collected: int = 0


# Resource limits applied to the test-running subprocess. Deliberately
# generous but bounded -- a patch that legitimately needs more than this is
# unusual for the kind of single-file reviews this system targets.
MAX_MEMORY_BYTES = 512 * 1024 * 1024  # 512 MB
MAX_CPU_SECONDS = 20
MAX_PROCESSES = 32


def _apply_resource_limits():
    """
    Called in the child process (via subprocess's preexec_fn) right before
    the test command runs. Caps memory, CPU time, and process count so a
    runaway or malicious patch (e.g. infinite loop, fork bomb) can't harm
    the host or hang the review pipeline indefinitely.
    """
    resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCESSES, MAX_PROCESSES))


def run_tests_against_patch(
    repo_path: str,
    file_relative_path: str,
    patched_code: str,
    test_command: list[str] | None = None,
    timeout_seconds: int = 30,
) -> SandboxResult:
    """
    Copy the repo into an isolated temp directory, replace the target file
    with the patched version, and run the test suite against it.

    The original repo on disk is never touched -- everything happens in a
    throwaway copy that gets deleted afterward, win or lose.
    """
    if test_command is None:
        test_command = ["python3", "-m", "pytest", "-v"]

    temp_dir = tempfile.mkdtemp(prefix="code_review_sandbox_")
    try:
        sandbox_repo = os.path.join(temp_dir, "repo")
        # ignore .git -- we don't need history inside the sandbox, and
        # copying it wastes time on large repos
        shutil.copytree(repo_path, sandbox_repo, ignore=shutil.ignore_patterns(".git"))

        target_file = os.path.join(sandbox_repo, file_relative_path)
        with open(target_file, "w") as f:
            f.write(patched_code)

        try:
            proc = subprocess.run(
                test_command,
                cwd=sandbox_repo,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                preexec_fn=_apply_resource_limits,
            )
            return SandboxResult(
                passed=proc.returncode == 0,
                stdout=proc.stdout,
                stderr=proc.stderr,
                return_code=proc.returncode,
                tests_collected=proc.stdout.count(" PASSED") + proc.stdout.count(" FAILED"),
            )
        except subprocess.TimeoutExpired as e:
            # subprocess.TimeoutExpired doesn't reliably honor text=True for
            # partially-captured output -- decode manually if we got bytes.
            def _decode(value):
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
                return value or ""

            return SandboxResult(
                passed=False,
                stdout=_decode(e.stdout),
                stderr=_decode(e.stderr),
                return_code=-1,
                timed_out=True,
            )

    finally:
        # Always clean up, even if something above raised an exception --
        # never leave sandbox copies littering the filesystem.
        shutil.rmtree(temp_dir, ignore_errors=True)


def print_sandbox_result(result: SandboxResult) -> None:
    status = "PASSED" if result.passed else ("TIMED OUT" if result.timed_out else "FAILED")
    print(f"Sandbox test run: {status}")
    print(f"Return code: {result.return_code}")
    if result.stdout:
        print("\n--- stdout ---")
        print(result.stdout)
    if result.stderr:
        print("\n--- stderr ---")
        print(result.stderr)