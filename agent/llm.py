"""The chat model. The provider is configuration: nothing else in Nova depends on it.

API keys are read from the environment by the provider libraries. They are not in
Settings, so they can't end up in a prompt, a log line or a tool result.
"""

from __future__ import annotations

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from agent.config import Settings
from agent.harness.errors import InvalidRequest

REQUEST_TIMEOUT_SECONDS = 300
MAX_RETRIES = 4  # with backoff, inside the provider client


def build_model(settings: Settings) -> BaseChatModel:
    common = {"timeout": REQUEST_TIMEOUT_SECONDS, "max_retries": MAX_RETRIES}
    match settings.model_provider:
        case "openai":
            # Luna rejects tools plus reasoning on chat completions, so this has to
            # be the Responses API.
            return init_chat_model(f"openai:{settings.model_name}", use_responses_api=True, **common)
        case "anthropic":
            return init_chat_model(f"anthropic:{settings.model_name}", max_tokens=16_000, **common)
    raise InvalidRequest(f"unknown MODEL_PROVIDER {settings.model_provider!r}; use 'openai' or 'anthropic'")
