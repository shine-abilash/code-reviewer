"""FastAPI entrypoints for Code Review Crew.

The API deliberately delegates review work to ``run_review_pipeline``.  It does
not duplicate the Manager/RAG/specialist/refactor/auto-debug responsibilities;
the endpoint layer only validates transport input, authenticates GitHub, and
serializes pipeline results.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents.manager_agent import ReviewTask, build_review_plan
from agents.performance_agent import PerformanceReport, run_performance_review
from agents.refactor_agent import RefactorResult, generate_patch_with_auto_debug
from agents.security_agent import SecurityReport, run_security_review
from core.config import settings
from core.git_diff_parser import ParsedDiff, parse_commit_range

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Code Review Crew",
    description="Webhook-driven multi-agent code review pipeline.",
    version="1.0.0",
)


class ReviewRequest(BaseModel):
    """Input for a manual review of an already checked-out Git repository."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(..., min_length=1)
    base_ref: str = "HEAD~1"
    head_ref: str = "HEAD"
    run_refactor: bool = False
    use_rag: bool = True
    max_attempts: int = Field(default=3, ge=1, le=10)
    test_command: list[str] | None = None

    @field_validator("repo_path")
    @classmethod
    def repo_must_exist(cls, value: str) -> str:
        if not Path(value).exists():
            raise ValueError("repo_path does not exist")
        return value


class ReviewResponse(BaseModel):
    status: str
    base_commit: str
    head_commit: str
    tasks: list[dict[str, Any]]
    reports: list[dict[str, Any]]
    errors: list[str] = Field(default_factory=list)


_seen_deliveries: set[str] = set()
_delivery_lock = threading.Lock()


def _jsonable(value: Any) -> Any:
    """Convert dataclasses/enums and nested values to JSON-safe structures."""
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _task_to_dict(task: ReviewTask) -> dict[str, Any]:
    data = _jsonable(task)
    # The ChangedFile dataclass is useful internally but is noisy in API output.
    data["file"] = {
        "path": task.file.path,
        "extension": task.file.extension,
        "is_new_file": task.file.is_new_file,
        "added_lines": len(task.file.added_lines),
        "removed_lines": len(task.file.removed_lines),
    }
    return data


def run_review_pipeline(
    repo_path: str,
    base_ref: str = "HEAD~1",
    head_ref: str = "HEAD",
    *,
    run_refactor: bool = False,
    use_rag: bool = True,
    max_attempts: int = 3,
    test_command: list[str] | None = None,
) -> dict[str, Any]:
    """Run the existing Manager → specialists → Refactor → sandbox flow."""
    parsed_diff: ParsedDiff = parse_commit_range(repo_path, base_ref, head_ref)
    plan = build_review_plan(parsed_diff)
    reports: list[dict[str, Any]] = []
    errors: list[str] = []

    for task in plan:
        security_report: SecurityReport | None = None
        performance_report: PerformanceReport | None = None
        try:
            security_report = run_security_review(
                repo_path, task.file, head_ref, priority=task.priority
            )
            performance_report = run_performance_review(
                repo_path, task.file, head_ref, priority=task.priority
            )
            refactor_result: RefactorResult | None = None
            if run_refactor and (
                security_report.findings or performance_report.findings
            ):
                refactor_result = generate_patch_with_auto_debug(
                    repo_path,
                    task.file,
                    security_report.findings,
                    performance_report.findings,
                    commit_ref=head_ref,
                    max_attempts=max_attempts,
                    test_command=test_command,
                    use_rag=use_rag,
                )
            reports.append(
                {
                    "file_path": task.file.path,
                    "security": _jsonable(security_report),
                    "performance": _jsonable(performance_report),
                    "refactor": _jsonable(refactor_result),
                }
            )
        except Exception as exc:  # isolate one file without hiding pipeline failure
            logger.exception("Review failed for %s", task.file.path)
            errors.append(f"{task.file.path}: {exc}")

    return {
        "status": "completed" if not errors else "completed_with_errors",
        "base_commit": parsed_diff.base_commit,
        "head_commit": parsed_diff.head_commit,
        "tasks": [_task_to_dict(task) for task in plan],
        "reports": reports,
        "errors": errors,
    }


def _configured_webhook_secret() -> str:
    # Read the central setting, while allowing tests/deployments to set the
    # environment after importing the app.
    return os.getenv("GITHUB_WEBHOOK_SECRET", settings.github_webhook_secret)


def verify_github_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """Verify GitHub's sha256 HMAC using a constant-time comparison."""
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _repository_path(payload: dict[str, Any]) -> str:
    repository = payload.get("repository") or {}
    candidate = payload.get("repo_path") or repository.get("local_path") or repository.get("path")
    if candidate:
        return str(candidate)

    root = os.getenv("GITHUB_REPOSITORY_ROOT", "")
    full_name = str(repository.get("full_name") or "")
    if root and full_name:
        # Only use the final repository component; never allow payload path
        # traversal to escape the configured checkout root.
        candidate_path = (Path(root) / Path(full_name).name).resolve()
        if Path(root).resolve() in candidate_path.parents:
            return str(candidate_path)
    raise ValueError("payload must identify a checked-out repository via repo_path or repository.local_path")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "code-review-crew",
        "webhook_configured": bool(_configured_webhook_secret()),
    }


@app.post("/review", response_model=ReviewResponse)
def manual_review(request: ReviewRequest) -> dict[str, Any]:
    try:
        return run_review_pipeline(**request.model_dump())
    except Exception as exc:
        logger.exception("Manual review failed")
        raise HTTPException(status_code=500, detail=f"Review pipeline failed: {exc}") from exc


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await request.body()
    secret = _configured_webhook_secret()
    if not verify_github_signature(body, x_hub_signature_256, secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid GitHub webhook signature")

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook payload must be a JSON object")

    event = (x_github_event or payload.get("hook", {}).get("event") or "").lower()
    if event == "ping":
        return {
            "status": "ok",
            "event": "ping",
            "delivery_id": x_github_delivery or payload.get("zen"),
        }
    if event != "push":
        raise HTTPException(status_code=400, detail=f"Unsupported GitHub event: {event or 'missing'}")
    after = payload.get("after")
    before = payload.get("before")
    if not after or not before:
        raise HTTPException(status_code=400, detail="Push payload must include before and after commit SHAs")

    delivery_id = x_github_delivery or str(payload.get("delivery") or "")
    if delivery_id:
        with _delivery_lock:
            if delivery_id in _seen_deliveries:
                return {"status": "duplicate", "delivery_id": delivery_id}
            _seen_deliveries.add(delivery_id)

    try:
        repo_path = _repository_path(payload)
        result = run_review_pipeline(repo_path, before, after)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("GitHub webhook review failed")
        raise HTTPException(status_code=500, detail=f"Webhook review failed: {exc}") from exc

    return {"status": "accepted", "delivery_id": delivery_id or None, "review": result}


# Convenient import target for ``uvicorn api.main:app``.
__all__ = ["app", "run_review_pipeline", "verify_github_signature"]
