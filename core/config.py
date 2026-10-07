"""
Centralized configuration for the whole project.
Every other module imports `settings` from here instead of reading
os.environ directly — keeps env handling in one auditable place.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Local LLM (Ollama)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3:8b"

    # Cloud fallback LLM
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # Routing behavior: auto | local | cloud
    routing_mode: str = "auto"

    # Bound model calls so a slow/unavailable backend cannot block an API
    # request indefinitely.
    llm_timeout_seconds: float = 90.0

    # Vector DB
    qdrant_mode: str = "local"
    qdrant_path: str = "./qdrant_data"
    qdrant_url: str = "http://localhost:6333"

    # GitHub webhook
    github_webhook_secret: str = ""
    github_repository_root: str = ""

    # Sandbox / auto-debug loop
    sandbox_image: str = "python:3.12-slim"
    sandbox_timeout_seconds: int = 60


settings = Settings()
