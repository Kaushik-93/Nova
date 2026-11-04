import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from agent.config import Settings
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.errors import BudgetExceeded, InvalidRequest
from agent.harness.middleware import BudgetMiddleware
from agent.llm import build_model


def settings(provider: str, name: str) -> Settings:
    return Settings(model_provider=provider, model_name=name, superset_url="http://x", superset_admin_user="a",
                    superset_admin_password="b")


def test_openai_uses_the_responses_api(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    model = build_model(settings("openai", "gpt-6-luna"))
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "gpt-6-luna"
    assert model.use_responses_api is True


def test_anthropic_is_supported(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    model = build_model(settings("anthropic", "claude-opus-5-5"))
    assert isinstance(model, ChatAnthropic)
    assert model.model == "claude-opus-5-5"


def test_an_unknown_provider_is_refused():
    with pytest.raises(InvalidRequest, match="MODEL_PROVIDER"):
        build_model(settings("nope", "x"))


def test_settings_hold_no_llm_key():
    assert not [name for name in Settings.model_fields if "api_key" in name]


def reply(total_tokens: int) -> AIMessage:
    return AIMessage("hi", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": total_tokens})


def test_each_model_call_is_a_step_and_its_tokens_are_counted():
    budget = RunBudget(BudgetLimits())
    middleware = BudgetMiddleware(budget)

    middleware.before_model({}, None)
    middleware.after_model({"messages": [HumanMessage("q"), reply(150)]}, None)

    assert budget.steps == 1
    assert budget.tokens == 150


def test_a_spent_budget_stops_the_next_model_call():
    budget = RunBudget(BudgetLimits(max_tokens=100))
    middleware = BudgetMiddleware(budget)
    middleware.after_model({"messages": [reply(150)]}, None)
    with pytest.raises(BudgetExceeded, match="tokens"):
        middleware.before_model({}, None)


def test_a_message_without_usage_is_harmless():
    budget = RunBudget(BudgetLimits())
    BudgetMiddleware(budget).after_model({"messages": [AIMessage("hi")]}, None)
    assert budget.tokens == 0
