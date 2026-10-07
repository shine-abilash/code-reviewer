# Code Review Crew

Code Review Crew is a multi-agent Python code-review service. It turns a Git push (or a manual review request) into a structured review by reusing the existing Manager Agent, RAG context layer, Security Agent, Performance & Style Agent, Refactor Agent, Hybrid Model Router, and sandboxed auto-debug loop.

## What it solves

Traditional review automation often produces generic advice or an unverified patch. Code Review Crew separates deterministic analysis from model reasoning, retrieves project-specific context, and tests generated fixes in a disposable sandbox before reporting them.

## Architecture and data flow

```text
GitHub Push
    │ X-Hub-Signature-256
    ▼
FastAPI /webhook/github
    ▼
Manager Agent ───────────────┐
    │ review plan             │
    ▼                         │
RAG context retrieval         │
    ├── Security Agent        │
    └── Performance Agent     │
            │ findings        │
            ▼                 │
      Refactor Agent ◄────────┘
            │ generated patch
            ▼
      Auto-Debug Loop
            │
            ▼
  Disposable sandbox tests ── pass → final review
            └─────────────── fail → retry patch generation
```

- **Manager Agent** prioritizes changed files and delegates every reviewable file to the specialist agents.
- **RAG** is a context/knowledge layer, not a reviewer. Qdrant stores repository code chunks so agents can retrieve conventions, similar fixes, and project style.
- **Security Agent** combines Bandit and secret scanning, then asks the routed model only for explanations and fixes.
- **Performance Agent** combines Ruff and Radon complexity findings, then explains the deterministic findings.
- **Refactor Agent** generates a complete-file patch for actual findings and can include retrieved repository context.
- **Auto-Debug Loop** runs the patch in a temporary copy with time/resource limits and asks for corrections when tests fail.
- **Hybrid Model Router** chooses local or cloud models based on task type, priority, and `ROUTING_MODE`.
- **FastAPI** is only the transport/orchestration boundary; it delegates to the same pipeline used by manual reviews.

## Installation

Python 3.11+ is recommended.

```bash
git clone https://github.com/shine-abilash/code-reviewer.git
cd code-reviewer
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

The project uses local Qdrant-on-disk mode by default. An external Qdrant server is optional. Ollama and Groq are also optional for deterministic-only reviews; the agents retain built-in explanations when models are unavailable.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `OLLAMA_BASE_URL` | Ollama endpoint | `http://localhost:11434` |
| `OLLAMA_MODEL` | Local model | `llama3:8b` |
| `GROQ_API_KEY` | Optional cloud fallback credential | empty |
| `GROQ_MODEL` | Groq model | `llama-3.3-70b-versatile` |
| `ROUTING_MODE` | `auto`, `local`, or `cloud` | `auto` |
| `LLM_TIMEOUT_SECONDS` | Maximum duration of one model request | `90` |
| `QDRANT_MODE` | `local` or `server` | `local` |
| `QDRANT_PATH` | Local Qdrant data directory | `./qdrant_data` |
| `QDRANT_URL` | Qdrant server URL | `http://localhost:6333` |
| `GITHUB_WEBHOOK_SECRET` | HMAC secret configured in GitHub | empty |
| `GITHUB_REPOSITORY_ROOT` | Optional root containing checked-out webhook repositories | empty |
| `SANDBOX_IMAGE` | Reserved container backend setting | `python:3.12-slim` |
| `SANDBOX_TIMEOUT_SECONDS` | Sandbox timeout setting | `60` |

Never commit `.env`, GitHub tokens, model keys, webhook secrets, or other credentials.

## Run the backend

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Endpoints:

- `GET /health` — service and webhook configuration status.
- `POST /review` — review a checked-out local Git repository.
- `POST /webhook/github` — authenticated GitHub `push` webhook.
- OpenAPI documentation is available at `/docs`.

Manual review example:

```bash
curl -X POST http://localhost:8000/review \
  -H 'Content-Type: application/json' \
  -d '{"repo_path":"/absolute/path/to/repository","base_ref":"HEAD~1","head_ref":"HEAD","run_refactor":false}'
```

## GitHub webhook setup

1. Check out the repository on the machine running the service and ensure the checkout contains both `before` and `after` commits from the webhook payload.
2. Set `GITHUB_WEBHOOK_SECRET` in the service environment.
3. If using multiple local checkouts, set `GITHUB_REPOSITORY_ROOT`. The webhook payload may also include a trusted `repo_path`/`repository.local_path` when the integration controls payload creation.
4. In GitHub repository settings, add a webhook pointing to `https://your-host/webhook/github`.
5. Select **application/json**, enter the same secret, and subscribe to **Push** events.
6. The service verifies `X-Hub-Signature-256` using constant-time HMAC comparison, rejects malformed/unsupported requests, and de-duplicates delivery IDs when GitHub sends a retry.

For production, put FastAPI behind TLS and a reverse proxy, use a process manager, and update the checkout from the pushed commit before invoking the review pipeline.

## Qdrant/RAG

Local mode creates the configured Qdrant data directory and indexes Python functions/classes with `rag.code_index.index_repository`. Retrieval is additive: an unavailable or empty index never prevents a refactor. The first FastEmbed retrieval may download its embedding model.

## Tests

```bash
pytest -q
pytest -q tests/test_api.py
```

The tests cover the existing agent behavior, RAG chunking, model routing, patch generation, sandbox pass/fail/self-correction, plus `/health`, manual review delegation, valid and invalid webhooks, malformed payloads, unsupported events, and duplicate deliveries.

## Example workflow

```text
Developer pushes code
→ GitHub signs a push webhook
→ FastAPI verifies the signature and delivery
→ Manager builds a prioritized plan
→ RAG supplies repository-specific context
→ Security + Performance agents produce deterministic findings
→ Refactor Agent generates a complete patch
→ Auto-Debug Loop runs sandbox tests
→ failed patch is corrected and re-tested
→ final review result is returned
```

## Limitations and future improvements

- The current webhook expects a locally available, updated checkout; a production deployment should add a controlled GitHub checkout/fetch worker.
- The default sandbox is subprocess/resource-limit based when Docker is unavailable. A Docker executor can implement the same `run_tests_against_patch` interface.
- Review execution is synchronous in this first API integration. A queue/worker can be added for large repositories.
- Future work can add pull-request comments, persistent review history, stronger repository identity checks, and richer metrics.
