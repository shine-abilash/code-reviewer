"""
llm_client.py
Thin wrapper around the local Ollama model and the cloud model (Groq).

Key design decision, based on real testing in Step 1: tinyllama ignores
plain-text formatting instructions (asked for "one sentence", got a full
paragraph). Rather than fight that with prompt engineering, we use Ollama's
`format="json"` mode, which forces the model to emit syntactically valid
JSON no matter how well it follows the rest of the prompt. We still validate
the result and fail gracefully if the *content* doesn't match what we asked
for -- small local models can produce valid-but-wrong JSON.

call_cloud_llm_json() talks to Groq the same way, for tasks (like the
Refactor Agent) that need stronger reasoning than the local model reliably
provides.
"""
import json

import ollama

from core.config import settings


class LLMResponseError(Exception):
    """Raised when a model's output can't be parsed as expected."""


def call_local_llm_json(system_prompt: str, user_prompt: str) -> dict:
    """
    Call the local Ollama model and force JSON-formatted output.
    Returns the parsed dict. Raises LLMResponseError if the model's output
    isn't valid JSON at all (rare with format="json", but not impossible).
    """
    client = ollama.Client(
        host=settings.ollama_base_url,
        timeout=settings.llm_timeout_seconds,
    )

    response = client.chat(
        model=settings.ollama_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        format="json",
        options={"temperature": 0.1},
    )

    raw_content = response["message"]["content"]

    try:
        return json.loads(raw_content)
    except json.JSONDecodeError as e:
        raise LLMResponseError(
            f"Local model did not return valid JSON. Raw output: {raw_content!r}"
        ) from e


def call_cloud_llm_json(system_prompt: str, user_prompt: str) -> dict:
    """
    Call the cloud model (Groq) and force JSON-formatted output.
    Used for tasks that need stronger reasoning than the local model can
    reliably provide (e.g. generating an actual code patch, not just
    explaining an already-known issue).
    """
    from groq import Groq  # imported lazily so local-only usage never needs the package configured

    if not settings.groq_api_key:
        raise LLMResponseError("GROQ_API_KEY is not set in .env -- cannot call cloud model.")

    client = Groq(
        api_key=settings.groq_api_key,
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
    )

    response = client.chat.completions.create(
        model=settings.groq_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )

    raw_content = response.choices[0].message.content

    try:
        return json.loads(raw_content)
    except json.JSONDecodeError as e:
        raise LLMResponseError(
            f"Cloud model did not return valid JSON. Raw output: {raw_content!r}"
        ) from e
