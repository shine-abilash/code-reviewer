"""
model_router.py
The Hybrid Model Routing layer from the project spec.

Before this module existed, each agent hardcoded its own model choice:
Security/Performance always tried local first, Refactor always tried cloud
first. That worked, but it wasn't actually a *router* -- just separate
hardcoded decisions duplicated across three files, with no single place to
change the policy or measure its effect.

This module centralizes that decision behind one function, route_llm_call(),
driven by real evidence gathered in Steps 3-5:
  - tinyllama (local) is fine for EXPLAIN tasks most of the time, but can
    produce degenerate/placeholder output -- acceptable because a fallback
    template exists and the task is low-stakes (explaining an already-known
    issue, not deciding anything).
  - tinyllama is NOT reliable for GENERATE_PATCH tasks -- generating a full,
    correct, syntactically valid file is a strictly harder task, and a wrong
    patch is actively harmful (could break working code), not just a weaker
    explanation.
  - The Manager Agent's file-level "priority" signal (Step 2) is real
    information about risk -- a HIGH priority file (e.g. contains a SQL
    query or a secret pattern) deserves the stronger model even for the
    "easier" EXPLAIN task, since a wrong/missed explanation there is more
    costly than on a routine file.

settings.routing_mode from .env controls the policy:
  "auto"  -> apply the rules above (this is the interesting/demoable mode)
  "local" -> force local only, regardless of task or priority (free, offline)
  "cloud" -> force cloud only, regardless of task or priority (max quality)
"""
from dataclasses import dataclass, field
from enum import Enum

from core.config import settings
from core.llm_client import call_local_llm_json, call_cloud_llm_json, LLMResponseError


class TaskType(Enum):
    EXPLAIN = "explain"
    GENERATE_PATCH = "generate_patch"


@dataclass
class RoutingStats:
    local_calls: int = 0
    cloud_calls: int = 0
    local_failures: int = 0
    cloud_failures: int = 0
    decisions: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Routing stats: {self.local_calls} local call(s), {self.cloud_calls} cloud call(s) "
            f"({self.local_failures} local failure(s), {self.cloud_failures} cloud failure(s))"
        )


stats = RoutingStats()


def _decide_backend(task_type: TaskType, priority: str) -> str:
    mode = settings.routing_mode

    if mode == "local":
        return "local"
    if mode == "cloud":
        return "cloud"

    if task_type == TaskType.GENERATE_PATCH:
        return "cloud"
    if task_type == TaskType.EXPLAIN and priority == "high":
        return "cloud"
    return "local"


def route_llm_call(
    task_type: TaskType,
    system_prompt: str,
    user_prompt: str,
    priority: str = "normal",
) -> tuple[dict, str]:
    primary = _decide_backend(task_type, priority)
    secondary = "cloud" if primary == "local" else "local"

    decision_note = f"{task_type.value} (priority={priority}) -> {primary}"
    stats.decisions.append(decision_note)

    backends = {
        "local": call_local_llm_json,
        "cloud": call_cloud_llm_json,
    }

    if settings.routing_mode in {"local", "cloud"}:
        backends_to_try = (primary,)
    else:
        backends_to_try = (primary, secondary)

    for backend_name in backends_to_try:
        call_fn = backends[backend_name]
        try:
            response = call_fn(system_prompt, user_prompt)
            if backend_name == "local":
                stats.local_calls += 1
            else:
                stats.cloud_calls += 1
            return response, backend_name
        except Exception:
            if backend_name == "local":
                stats.local_failures += 1
            else:
                stats.cloud_failures += 1
            continue

    raise LLMResponseError(
        f"Both local and cloud models failed for task {task_type.value}."
    )
