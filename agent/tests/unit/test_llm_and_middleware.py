from types import SimpleNamespace

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai import ChatOpenAI

from agent.config import Settings
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.errors import BudgetExceeded, InvalidRequest
from agent.harness.middleware import RunMiddleware
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


def reply(total_tokens: int, reasoning: int = 0, tools=()) -> AIMessage:
    return AIMessage("hi", tool_calls=[{"name": name, "args": {"secret": "value"}, "id": name} for name in tools],
                     usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": total_tokens,
                                     "output_token_details": {"reasoning": reasoning}})


def model_call(middleware, message):
    return middleware.wrap_model_call(object(), lambda request: SimpleNamespace(result=[message]))


def tool_call(middleware, name, result):
    request = SimpleNamespace(tool_call={"name": name, "args": {"sql": "SELECT secret"}})
    return middleware.wrap_tool_call(request, lambda r: result)


def test_each_model_call_is_a_step_and_its_tokens_are_counted():
    budget = RunBudget(BudgetLimits())
    model_call(RunMiddleware(budget), reply(150))
    assert budget.steps == 1
    assert budget.tokens == 150


def test_a_spent_budget_stops_the_next_model_call():
    budget = RunBudget(BudgetLimits(max_tokens=100))
    middleware = RunMiddleware(budget)
    model_call(middleware, reply(150))
    with pytest.raises(BudgetExceeded, match="tokens"):
        model_call(middleware, reply(1))


def test_a_message_without_usage_is_harmless():
    budget = RunBudget(BudgetLimits())
    model_call(RunMiddleware(budget), AIMessage("hi"))
    assert budget.tokens == 0


def test_model_steps_are_traced_with_who_tokens_and_tools():
    events = []
    middleware = RunMiddleware(RunBudget(BudgetLimits()), agent="sql_author", run_id="r1", sink=events.append)
    model_call(middleware, reply(150, reasoning=40, tools=["run_readonly_sql"]))
    event = events[0]
    assert (event["event"], event["agent"], event["run_id"]) == ("model", "sql_author", "r1")
    assert event["tokens"] == 150
    assert event["reasoning_tokens"] == 40
    assert event["tools"] == ["run_readonly_sql"]


def test_tool_steps_are_traced_with_their_outcome():
    events = []
    middleware = RunMiddleware(RunBudget(BudgetLimits()), sink=events.append)
    tool_call(middleware, "good", ToolMessage("ok", tool_call_id="1"))
    tool_call(middleware, "bad", ToolMessage("FAILED [sql]: nope", tool_call_id="2"))
    assert [(e["event"], e.get("ok")) for e in events] == [("tool_start", None), ("tool_end", True), ("tool_start", None),
                                                           ("tool_end", False)]


def test_traces_hold_no_arguments_or_results():
    events = []
    middleware = RunMiddleware(RunBudget(BudgetLimits()), sink=events.append)
    model_call(middleware, reply(1, tools=["x"]))
    tool_call(middleware, "x", ToolMessage("private data", tool_call_id="1"))
    assert "secret" not in str(events) and "private data" not in str(events)
