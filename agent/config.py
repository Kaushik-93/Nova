"""Settings, read from the environment. Nothing here may reach the model."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(protected_namespaces=())

    model_provider: str = "anthropic"
    model_name: str = "claude-opus-5-5"

    trino_url: str = "http://trino:8080"
    cube_url: str = "http://cube:4000"
    cubejs_api_secret: str = ""
    database_url: str = ""

    nova_skills_dir: str = "skills"
    nova_ui_origin: str = "http://localhost:13000"
    nova_public_api_url: str = "http://localhost:18000"
    # Where the dashboard renderer's browser really reaches the UI and the API, inside the network.
    nova_render_ui_url: str = "http://ui:3000"
    nova_render_api_url: str = "http://agent:8000"
    nova_trino_secrets_dir: str = "/trino-data/secrets"

    # Sign-in. The first admin is created from these on startup.
    nova_jwt_secret: str = ""
    nova_admin_email: str = ""
    nova_admin_password: str = ""
    fernet_key: str = ""
    demo_ro_password: str = ""

    # SSO over OpenID Connect. Stays off until a discovery URL is set.
    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_provider_name: str = "SSO"
    oidc_scopes: str = "openid email profile"
    oidc_allowed_domains: str = ""
    oidc_admin_emails: str = ""
    nova_chart_repair_rounds: int = 3

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
