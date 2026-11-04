"""Settings, read from the environment. Nothing here may reach the model."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    superset_url: str
    superset_admin_user: str
    superset_admin_password: str
    trino_url: str = "http://trino:8080"
    database_url: str = ""

    nova_skills_dir: str = "skills"

    nova_max_steps: int = 150
    nova_max_tokens: int = 2_000_000
    nova_max_wall_seconds: float = 900
    nova_max_sql_queries: int = 120
    nova_query_timeout_seconds: int = 30
    nova_max_estimated_rows: float = 50_000_000
    nova_max_estimated_bytes: float = 5_000_000_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
