
"""
manager_agent.py
The "Manager Agent" role from the project spec.

Deliberately implemented as deterministic Python logic, not an LLM call.
Reasoning: delegation decisions (which specialist looks at which file, and
how urgently) need to be reliable and repeatable every single run — an LLM
call here would add latency and non-determinism for zero benefit, since the
actual judgment calls (is this code vulnerable? is this slow?) belong to the
specialist agents, not the router.

This mirrors a real engineering-manager's job: they don't personally review
every line, they decide who looks at what and how urgently.
"""
from dataclasses import dataclass, field

from core.git_diff_parser import ChangedFile, ParsedDiff

# Keywords that suggest a file deserves a closer security look.
# This doesn't replace the Security Agent's own analysis — it just decides
# review PRIORITY, so urgent files get reviewed by the (potentially slower,
# more expensive) cloud model first under hybrid routing.
SECURITY_SIGNAL_KEYWORDS = [
    "select ", "insert ", "update ", "delete ", "execute(", "cursor.execute",
    "password", "secret", "api_key", "apikey", "token", "auth",
    "eval(", "exec(", "pickle.loads", "subprocess", "os.system",
    "sk_live_", "sk_test_",
]

# Keywords suggesting the change touches something performance-sensitive.
PERFORMANCE_SIGNAL_KEYWORDS = [
    "for ", "while ", "range(", "sort(", "sorted(", ".append(",
    "nested", "recursion", "recursive",
]


@dataclass
class ReviewTask:
    file: ChangedFile
    assigned_agents: list[str] = field(default_factory=list)
    priority: str = "normal"  # "high" | "normal"
    reasons: list[str] = field(default_factory=list)

    def __repr__(self):
        return (
            f"ReviewTask(file={self.file.path!r}, "
            f"agents={self.assigned_agents}, priority={self.priority})"
        )


def _added_code_text(file: ChangedFile) -> str:
    """All newly added lines joined into one lowercase blob for keyword scanning."""
    return "\n".join(line.content for line in file.added_lines).lower()


def _decide_agents_for_file(file: ChangedFile) -> ReviewTask:
    task = ReviewTask(file=file)
    code_text = _added_code_text(file)

    # Every reviewable file gets Style & Performance review — always cheap,
    # always useful, no reason to skip it.
    task.assigned_agents.append("performance_style")

    security_hit = any(kw in code_text for kw in SECURITY_SIGNAL_KEYWORDS)
    if security_hit or file.extension == ".sql":
        task.assigned_agents.append("security")
        task.priority = "high"
        matched = [kw for kw in SECURITY_SIGNAL_KEYWORDS if kw in code_text]
        task.reasons.append(f"Security-sensitive patterns found: {matched}")
    else:
        # Still worth a lightweight security pass on any code file — just
        # not urgent enough to jump the queue.
        task.assigned_agents.append("security")
        task.reasons.append("Routine security pass (no high-risk keywords matched)")

    perf_hit = any(kw in code_text for kw in PERFORMANCE_SIGNAL_KEYWORDS)
    if perf_hit:
        task.reasons.append("Contains loops/sorting — worth a complexity check")

    return task


def build_review_plan(parsed_diff: ParsedDiff) -> list[ReviewTask]:
    """
    Given a parsed diff, decide which specialist agents should review each
    file and in what priority order. High-priority tasks are returned first.
    """
    tasks = [_decide_agents_for_file(f) for f in parsed_diff.reviewable_files]
    tasks.sort(key=lambda t: 0 if t.priority == "high" else 1)
    return tasks


def print_review_plan(tasks: list[ReviewTask]) -> None:
    """Human-readable summary — useful for debugging and for demo day."""
    print(f"Manager Agent review plan ({len(tasks)} file(s)):\n")
    for task in tasks:
        print(f"  [{task.priority.upper()}] {task.file.path}")
        print(f"    -> agents: {', '.join(task.assigned_agents)}")
        for reason in task.reasons:
            print(f"    -> reason: {reason}")
        print()
