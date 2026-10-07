import hashlib
import hmac
import json
import subprocess

from fastapi.testclient import TestClient

from api.main import app


client = TestClient(app)


def _signature(body: bytes, secret: str = "test-secret") -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "initial"],
    ):
        subprocess.run(command, cwd=repo, check=True)
    return repo


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_manual_review_delegates_to_existing_pipeline(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    expected = {
        "status": "completed",
        "base_commit": "base",
        "head_commit": "head",
        "tasks": [],
        "reports": [],
        "errors": [],
    }
    monkeypatch.setattr("api.main.run_review_pipeline", lambda **kwargs: expected)
    response = client.post("/review", json={"repo_path": str(repo)})
    assert response.status_code == 200
    assert response.json() == expected


def test_webhook_valid_push_hands_off_to_pipeline(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    calls = []

    def fake_pipeline(repo_path, base_ref, head_ref):
        calls.append((repo_path, base_ref, head_ref))
        return {"status": "completed", "tasks": [], "reports": [], "errors": []}

    monkeypatch.setattr("api.main.run_review_pipeline", fake_pipeline)
    payload = {"before": "base", "after": "head", "repo_path": str(repo)}
    body = json.dumps(payload).encode()
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _signature(body),
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-valid",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert calls == [(str(repo), "base", "head")]


def test_webhook_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    body = b'{"before":"base","after":"head"}'
    response = client.post(
        "/webhook/github",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=wrong", "X-GitHub-Event": "push"},
    )
    assert response.status_code == 401


def test_webhook_rejects_malformed_json(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    body = b"not-json"
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _signature(body),
            "X-GitHub-Event": "push",
        },
    )
    assert response.status_code == 400
    assert "Malformed JSON" in response.json()["detail"]


def test_webhook_rejects_unsupported_event(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    body = b"{}"
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _signature(body),
            "X-GitHub-Event": "issues",
        },
    )
    assert response.status_code == 400
    assert "Unsupported" in response.json()["detail"]


def test_webhook_deduplicates_delivery(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    calls = []
    monkeypatch.setattr(
        "api.main.run_review_pipeline",
        lambda *args: calls.append(args) or {"status": "completed", "tasks": [], "reports": [], "errors": []},
    )
    payload = {"before": "base", "after": "head", "repo_path": str(repo)}
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _signature(body),
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "delivery-duplicate-test",
    }
    assert client.post("/webhook/github", content=body, headers=headers).json()["status"] == "accepted"
    assert client.post("/webhook/github", content=body, headers=headers).json()["status"] == "duplicate"
    assert len(calls) == 1
