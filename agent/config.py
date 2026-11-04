"""Settings, read from the environment. Nothing here may reach the model."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    superset_url: str
    superset_admin_user: str
    superset_admin_password: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
